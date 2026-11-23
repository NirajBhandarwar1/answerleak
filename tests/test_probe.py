import json
import os
import subprocess
import sys

import pytest

PROBE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "answerleak", "probe_inside.py")

NEW_LINE = "    return compute_distinctive_value(x, strict=True)"
OLD_BODY = "def f(x):\n    return x\n"
NEW_BODY = "def f(x):\n" + NEW_LINE + "\n"


def run_probe(tmp_path, targets, base="HEAD", **extra):
    spec = {"base_commit": base, "testbed": str(tmp_path / "testbed"), "targets": targets,
            "package_hint": "pkg", "roots": [str(tmp_path)], "exclude": []}
    spec.update(extra)
    env = dict(os.environ, ANSWERLEAK_SPEC=json.dumps(spec))
    p = subprocess.run([sys.executable, PROBE], env=env, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=300)
    assert p.returncode == 0, p.stderr
    lines = p.stdout.strip().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def write(path, text):
    os.makedirs(os.path.dirname(str(path)), exist_ok=True)
    with open(str(path), "w", newline="\n") as f:
        f.write(text)


def fix_target(lines=(NEW_LINE,)):
    return [{"path": "pkg/core.py", "role": "fix", "lines": list(lines)}]


def test_leak_case(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    other = tmp_path / "opt" / "cache" / "pkg-2025" / "pkg" / "core.py"
    write(other, NEW_BODY)
    out = run_probe(tmp_path, fix_target())
    assert len(out["fs_hits"]) == 1
    h = out["fs_hits"][0]
    assert os.path.normcase(h["path"]) == os.path.normcase(str(other))
    assert h["leak"] is True and h["found"] == 1 and h["total"] == 1
    assert h["role"] == "fix" and h["target"] == "pkg/core.py"
    assert isinstance(h["readable_by_nonroot"], bool) and h["size"] > 0
    assert h["suffix_depth"] == 2 and h["in_testbed"] is False
    assert out["targets_summary"] == [{"path": "pkg/core.py", "role": "fix", "total": 1,
                                       "original": "worktree"}]
    assert "python_version" in out


def test_clean_case(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    out = run_probe(tmp_path, fix_target())
    assert out["fs_hits"] == []
    assert out["other_copies"] == []


def test_matched_lines_reported_for_review(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "cache" / "pkg-2025" / "pkg" / "core.py", NEW_BODY)
    h = run_probe(tmp_path, fix_target())["fs_hits"][0]
    assert len(h["matched"]) == 1 and "compute_distinctive_value" in h["matched"][0]


def test_other_copy_without_fix_lines(tmp_path):
    # A newer, refactored copy of the module (issue #640's "post-base source") that no longer
    # contains the exact fix lines: reported as other_copies (level 1), not as an fs_hit/LEAK.
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    other = tmp_path / "opt" / "cache" / "pkg-2025" / "pkg" / "core.py"
    write(other, "def f(x, y=None):\n    return refactored_everything(x, y)\n")
    out = run_probe(tmp_path, fix_target())
    assert out["fs_hits"] == []
    assert len(out["other_copies"]) == 1
    c = out["other_copies"][0]
    assert os.path.normcase(c["path"]) == os.path.normcase(str(other))
    assert c["role"] == "fix" and c["suffix_depth"] == 2


def test_one_of_two_lines_is_not_a_leak(tmp_path):
    second = "    other_specific_call_with_args(alpha, beta)"
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "cache" / "x" / "pkg" / "core.py", NEW_BODY)  # has only NEW_LINE
    h = run_probe(tmp_path, fix_target([NEW_LINE, second]))["fs_hits"][0]
    assert h["found"] == 1 and h["total"] == 2 and h["leak"] is False


def test_other_package_with_same_relative_test_path_is_not_a_copy(tmp_path):
    # xarray/tests/test_dataset.py must not match h5py/tests/test_dataset.py
    tgt = [{"path": "xarray/tests/test_dataset.py", "role": "tests", "lines": [NEW_LINE]}]
    write(tmp_path / "testbed" / "xarray" / "tests" / "test_dataset.py", OLD_BODY)
    write(tmp_path / "opt" / "pkgs" / "h5py" / "tests" / "test_dataset.py", "x = 2\n")
    good = tmp_path / "opt" / "pkgs" / "xarray-2025" / "xarray" / "tests" / "test_dataset.py"
    write(good, "y = 3\n")
    out = run_probe(tmp_path, tgt)
    assert [os.path.normcase(c["path"]) for c in out["other_copies"]] == [os.path.normcase(str(good))]


def test_src_layout_and_root_files(tmp_path):
    tgt = [{"path": "src/pkg/core.py", "role": "fix", "lines": [NEW_LINE]},
           {"path": "test_pkg.py", "role": "tests", "lines": [NEW_LINE]}]
    write(tmp_path / "testbed" / "src" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "testbed" / "test_pkg.py", OLD_BODY)
    write(tmp_path / "site" / "pkg" / "core.py", "z = 1\n")                 # installed copy
    write(tmp_path / "site" / "pkg-1.0.dist-info" / "test_pkg.py", "z = 1\n")  # hint in path
    write(tmp_path / "elsewhere" / "test_pkg.py", "z = 1\n")                 # no hint
    out = run_probe(tmp_path, tgt)
    got = sorted(os.path.normcase(c["path"]) for c in out["other_copies"])
    assert got == sorted(os.path.normcase(str(p)) for p in
                         [tmp_path / "site" / "pkg" / "core.py",
                          tmp_path / "site" / "pkg-1.0.dist-info" / "test_pkg.py"])


def test_lines_in_moved_module_are_not_a_leak(tmp_path):
    # strict rule: pkg/core.py's fix found only in pkg/moved/core.py (module moved upstream)
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "cache" / "pkg-2025" / "pkg" / "moved" / "core.py", NEW_BODY)
    out = run_probe(tmp_path, fix_target())
    assert len(out["fs_hits"]) == 1
    h = out["fs_hits"][0]
    assert h["found"] == 1 and h["lines_ok"] is True
    assert h["same_module"] is False and h["leak"] is False


def test_generic_tests_folder_needs_package_hint(tmp_path):
    # requests' tests/test_utils.py must not match packaging's tests/test_utils.py
    tgt = [{"path": "tests/test_utils.py", "role": "tests", "lines": [NEW_LINE]}]
    write(tmp_path / "testbed" / "tests" / "test_utils.py", OLD_BODY)
    write(tmp_path / "opt" / "store" / "packaging-24" / "tests" / "test_utils.py", NEW_BODY)
    good = tmp_path / "opt" / "store" / "pkg-2.0" / "tests" / "test_utils.py"
    write(good, NEW_BODY)
    out = run_probe(tmp_path, tgt)
    leaks = [os.path.normcase(h["path"]) for h in out["fs_hits"] if h["leak"]]
    assert leaks == [os.path.normcase(str(good))]
    assert [os.path.normcase(c["path"]) for c in out["other_copies"]] == [os.path.normcase(str(good))]


def test_same_module_hit_is_flagged(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "cache" / "pkg-2025" / "pkg" / "core.py", NEW_BODY)
    h = run_probe(tmp_path, fix_target())["fs_hits"][0]
    assert h["same_module"] is True and h["lines_ok"] is True and h["leak"] is True


def test_identical_copy_and_unrelated_same_basename_not_reported(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "same" / "pkg" / "core.py", OLD_BODY)        # same as base
    write(tmp_path / "opt" / "other" / "lib" / "core.py", "x = 1\n")      # different module path
    out = run_probe(tmp_path, fix_target())
    assert out["other_copies"] == []


def test_nondistinctive_ignored(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY + "    already_there_in_original(x)\n")
    write(tmp_path / "opt" / "pkg" / "core.py", NEW_BODY)
    lines = ["x = 1", "# this is a long comment line here", "    already_there_in_original(x)",
             "}}}}}}}}}}}}}}}}}}}}}}}}", "12345678901234567890123", NEW_LINE, NEW_LINE]
    out = run_probe(tmp_path, fix_target(lines))
    assert out["targets_summary"][0]["total"] == 1
    assert out["fs_hits"][0]["total"] == 1


def test_partial_below_half_not_leak(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "pkg" / "core.py", NEW_BODY)
    lines = [NEW_LINE, "    another_missing_line_alpha(y)", "    another_missing_line_beta(y)"]
    out = run_probe(tmp_path, fix_target(lines))
    h = out["fs_hits"][0]
    assert (h["found"], h["total"], h["leak"]) == (1, 3, False)


def test_reformatted_line_still_matches(tmp_path):
    # upstream later ran a formatter: quotes and spacing changed, content identical
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    write(tmp_path / "opt" / "pkg" / "core.py", '    self.headers["Content-Length"]  =  "0"\n')
    out = run_probe(tmp_path, fix_target(["self.headers['Content-Length'] = '0'"]))
    assert out["fs_hits"] and out["fs_hits"][0]["leak"] is True


def test_quote_variant_of_base_line_not_distinctive(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY + '    call_existing_thing("abc")\n')
    out = run_probe(tmp_path, fix_target(["call_existing_thing('abc')", NEW_LINE]))
    assert out["targets_summary"][0]["total"] == 1


def git(cwd, *args, **kw):
    env = dict(os.environ, **kw.get("env", {}))
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"] + list(args),
                   cwd=str(cwd), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def rev(cwd, what="HEAD"):
    return subprocess.run(["git", "rev-parse", what], cwd=str(cwd), stdout=subprocess.PIPE,
                          universal_newlines=True).stdout.strip()


def dated(ts):
    d = "%d +0000" % ts
    return {"GIT_COMMITTER_DATE": d, "GIT_AUTHOR_DATE": d}


def make_repo(tmp_path):
    tb = tmp_path / "testbed"
    write(tb / "pkg" / "core.py", OLD_BODY)
    git(tb, "init", "-q")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "A")
    base = rev(tb)
    write(tb / "pkg" / "core.py", NEW_BODY)
    git(tb, "commit", "-q", "-am", "B")
    git(tb, "branch", "future")
    git(tb, "tag", "v-future")
    git(tb, "reset", "-q", "--hard", base)
    return tb, base


def test_git_future_refs(tmp_path):
    tb, base = make_repo(tmp_path)
    out = run_probe(tmp_path, fix_target(), base=base)
    g = out["git"]
    assert g["future_commits"] >= 1
    assert any("future" in r for r in g["future_refs"])
    assert any("v-future" in r for r in g["future_refs"])
    assert out["errors"] == []


def test_null_sha_ref_is_warning_not_error(tmp_path):
    # Real SWE-bench images ship refs/remotes/origin/HEAD pointing at 0000..., which makes
    # `git fsck` exit 2. That must not turn a scan into ERROR.
    tb, base = make_repo(tmp_path)
    ref = tb / ".git" / "refs" / "remotes" / "origin" / "HEAD"
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.write_text("0" * 40 + "\n")
    out = run_probe(tmp_path, fix_target(), base=base)
    assert out["errors"] == [], out["errors"]
    assert any("invalid sha1 pointer" in w for w in out["git"].get("fsck_warnings", []))
    assert out["git"]["future_commits"] >= 1


def test_git_unreachable(tmp_path):
    tb, base = make_repo(tmp_path)
    git(tb, "branch", "-D", "future")
    git(tb, "tag", "-d", "v-future")
    git(tb, "reflog", "expire", "--expire=now", "--all")
    out = run_probe(tmp_path, fix_target(), base=base)
    g = out["git"]
    # the dropped commit is no longer behind any ref, but its objects are still readable
    assert g["future_refs"] == [] and g["future_commits"] == 1
    assert g["unreachable_objects"] >= 1
    assert any(h["leak"] for h in g["history_hits"])
    assert any(h["leak"] and h["target"] == "pkg/core.py" for h in g["unreachable_hits"])


def test_git_future_refs_found_by_content(tmp_path):
    tb, base = make_repo(tmp_path)
    g = run_probe(tmp_path, fix_target(), base=base)["git"]
    assert any(h["leak"] and h["role"] == "fix" and h["target"] == "pkg/core.py"
               for h in g["history_hits"])


def test_old_side_branch_tag_is_not_future(tmp_path):
    # Release-branch tags older than base (kept even after SWE-bench #465) are
    # not ancestors of base but are not the future either.
    tb = tmp_path / "testbed"
    write(tb / "pkg" / "core.py", OLD_BODY)
    git(tb, "init", "-q")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "root", env=dated(1500000000))
    git(tb, "checkout", "-q", "-b", "stable")
    write(tb / "pkg" / "other.py", "backport = 1\n")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "backport", env=dated(1500001000))
    git(tb, "tag", "1.0.1")
    git(tb, "checkout", "-q", "-")
    git(tb, "branch", "-D", "stable")
    write(tb / "pkg" / "core.py", OLD_BODY + "# base\n")
    git(tb, "commit", "-q", "-am", "base", env=dated(1500002000))
    out = run_probe(tmp_path, fix_target(), base=rev(tb))
    g = out["git"]
    assert g["nonancestor_commits"] == 1
    assert g["future_commits"] == 0 and g["future_refs"] == []
    assert out["errors"] == []


def test_reflog_only_future_commit(tmp_path):
    # pre-#465 style: clone tip moved back with reset --hard; future only in the reflog
    tb = tmp_path / "testbed"
    write(tb / "pkg" / "core.py", OLD_BODY)
    git(tb, "init", "-q")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "A", env=dated(1500000000))
    base = rev(tb)
    write(tb / "pkg" / "core.py", NEW_BODY)
    git(tb, "commit", "-q", "-am", "fix", env=dated(1500001000))
    git(tb, "reset", "-q", "--hard", base)
    g = run_probe(tmp_path, fix_target(), base=base)["git"]
    assert g["future_commits"] == 1 and g["future_refs"] == []
    assert any(h["leak"] for h in g["history_hits"])


def test_future_commit_by_date_with_skewed_clock_descendant(tmp_path):
    # descendant of base with an earlier committer date still counts as future
    tb = tmp_path / "testbed"
    write(tb / "pkg" / "core.py", OLD_BODY)
    git(tb, "init", "-q")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "A", env=dated(1500005000))
    base = rev(tb)
    write(tb / "pkg" / "core.py", NEW_BODY)
    git(tb, "commit", "-q", "-am", "fix", env=dated(1500000000))
    git(tb, "branch", "later")
    git(tb, "reset", "-q", "--hard", base)
    g = run_probe(tmp_path, fix_target(), base=base)["git"]
    assert g["future_commits"] == 1 and any("later" in r for r in g["future_refs"])
    assert any(h["leak"] for h in g["history_hits"])


def test_tracked_testbed_copies_are_not_leaks(tmp_path):
    # Django-style: the same line exists in another tracked tests/*/tests.py at base
    tb = tmp_path / "testbed"
    write(tb / "pkg" / "core.py", OLD_BODY)
    write(tb / "other" / "core.py", NEW_BODY)
    git(tb, "init", "-q")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "A")
    base = rev(tb)
    out = run_probe(tmp_path, fix_target(), base=base)
    assert out["fs_hits"] == []
    assert out["targets_summary"][0]["original"] == "git"
    # an untracked newer copy inside /testbed is still reported
    write(tb / ".eggs" / "pkg" / "core.py", NEW_BODY)
    out = run_probe(tmp_path, fix_target(), base=base)
    assert len(out["fs_hits"]) == 1 and out["fs_hits"][0]["leak"] and out["fs_hits"][0]["in_testbed"]


def test_dirty_own_file_with_fix_is_leak(tmp_path):
    # the working copy already contains the fix: base version comes from git, so
    # the fix lines stay distinctive and the own file is reported
    tb = tmp_path / "testbed"
    write(tb / "pkg" / "core.py", OLD_BODY)
    git(tb, "init", "-q")
    git(tb, "add", "-A")
    git(tb, "commit", "-q", "-m", "A")
    base = rev(tb)
    write(tb / "pkg" / "core.py", NEW_BODY)
    out = run_probe(tmp_path, fix_target(), base=base)
    assert out["targets_summary"][0]["total"] == 1
    assert len(out["fs_hits"]) == 1 and out["fs_hits"][0]["leak"]


def test_new_file_target_absent_at_base(tmp_path):
    tb, base = make_repo(tmp_path)
    out = run_probe(tmp_path, [{"path": "pkg/brand_new.py", "role": "tests",
                                "lines": ["def test_brand_new_behaviour(self):"]}], base=base)
    assert out["targets_summary"][0]["original"] == "absent_at_base"
    assert out["targets_summary"][0]["total"] == 1


def test_missing_git_records_error(tmp_path):
    write(tmp_path / "testbed" / "pkg" / "core.py", OLD_BODY)
    out = run_probe(tmp_path, fix_target(), base="abc123")
    assert any("git" in e for e in out["errors"])
    assert out["git"]["future_commits"] == 0


def test_bad_spec_still_json(tmp_path):
    env = dict(os.environ)
    env.pop("ANSWERLEAK_SPEC", None)
    p = subprocess.run([sys.executable, PROBE], env=env, stdout=subprocess.PIPE,
                       universal_newlines=True)
    assert p.returncode == 0
    assert json.loads(p.stdout)["errors"]


def test_rewrapped_line_is_not_distinctive_in_probe(tmp_path):
    original = 'params = [\n    pytest.param(\n        operator.eq,\n        id="equal",\n    ),\n]\n'
    write(tmp_path / "testbed" / "pkg" / "core.py", original)
    write(tmp_path / "opt" / "cache" / "x" / "pkg" / "core.py", 'params = [pytest.param(operator.eq, id="equal"),]\n')
    out = run_probe(tmp_path, fix_target(['pytest.param(operator.eq, id="equal"),']))
    assert out["targets_summary"][0]["total"] == 0
    assert out["fs_hits"] == []
