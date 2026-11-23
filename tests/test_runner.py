import json

import pytest

from answerleak import runner

TASK = {"instance_id": "pydata__xarray-1", "repo": "pydata/xarray", "base_commit": "abc",
        "image": "example/img:latest", "patch": "", "test_patch": ""}


class FakeDocker:
    """Scripted replacement for runner._run."""

    def __init__(self, probe_results, pull_rc=0):
        self.probe_results = list(probe_results)  # list of (rc, stdout, stderr)
        self.pull_rc = pull_rc
        self.calls = []
        self.envs = []

    def __call__(self, cmd, input_text=None, timeout=900, env=None):
        self.calls.append(cmd)
        sub = cmd[1]
        if sub == "pull":
            return self.pull_rc, "", "denied" if self.pull_rc else ""
        if sub == "image":
            return 0, "example/img@sha256:" + "a" * 64 + "\n", ""
        if sub == "run":
            self.envs.append(env)
            return self.probe_results.pop(0)
        if sub in ("rmi", "rm"):
            return 0, "", ""
        raise AssertionError(cmd)


@pytest.fixture(autouse=True)
def stub(monkeypatch):
    monkeypatch.setattr(runner, "probe_source", lambda: "print('probe')")
    monkeypatch.setattr(runner, "build_spec", lambda t: {
        "base_commit": "abc", "testbed": "/testbed", "package_hint": "xarray",
        "max_unreachable_blobs": 2000,
        "targets": [{"path": "a.py", "role": "fix", "lines": ["x" * 50] * 10}]})


def probe(**kw):
    base = {"git": {"future_commits": 0, "future_refs": [], "unreachable_objects": 0,
                    "unreachable_hits": []},
            "fs_hits": [], "archives": [],
            "targets_summary": [{"path": "a.py", "role": "fix", "total": 3}], "errors": []}
    base.update(kw)
    return (0, "noise\n" + json.dumps(base) + "\n", "")


def scan(monkeypatch, results, **kw):
    fake = FakeDocker(results)
    monkeypatch.setattr(runner, "_run", fake)
    return runner.scan_task(TASK, **kw), fake


def test_leak_via_fs_hit(monkeypatch):
    hit = {"path": "/opt/x/a.py", "target": "a.py", "role": "fix", "found": 3, "total": 3, "leak": True}
    rec, fake = scan(monkeypatch, [probe(fs_hits=[hit])])
    assert rec["verdict"] == "LEAK" and rec["leak_fix"] and not rec["leak_tests"] and not rec["leak_git"]
    assert rec["digest"].startswith("example/img@sha256:")
    assert fake.calls[-1][:2] == ["docker", "rmi"]
    assert rec["error"] is None


def _old_rec(hits, copies=()):
    return {"instance_id": "pydata__xarray-9", "repo": "pydata/xarray", "verdict": "LEAK",
            "leak_fix": True, "leak_tests": False, "leak_git": False,
            "probe": {"fs_hits": hits, "other_copies": list(copies), "errors": [],
                      "git": {"history_hits": [], "unreachable_hits": []},
                      "targets_summary": [{"path": "xarray/core/combine.py", "role": "fix", "total": 1}]}}


def test_rejudge_drops_moved_module_leak():
    hit = {"path": "/opt/miniconda3/pkgs/xarray-2025/lib/xarray/structure/combine.py",
           "target": "xarray/core/combine.py", "role": "fix", "found": 1, "total": 1, "leak": True,
           "in_testbed": False}
    copy = {"path": "/opt/miniconda3/pkgs/xarray-2025/lib/xarray/tests/test_combine.py",
            "target": "xarray/tests/test_combine.py", "role": "tests"}
    r = runner.rejudge(_old_rec([hit], [copy]))
    h = r["probe"]["fs_hits"][0]
    assert h["same_module"] is False and h["lines_ok"] is True and h["leak"] is False
    assert r["verdict"] == "COPY" and r["leak_fix"] is False and r["leak_renamed_module"] is True
    assert r["rule"] == "strict-same-module"


def test_rejudge_keeps_same_module_leak_and_own_file():
    same = {"path": "/opt/pkgs/xarray-2025/xarray/core/combine.py", "target": "xarray/core/combine.py",
            "role": "fix", "found": 2, "total": 3, "leak": True, "in_testbed": False}
    r = runner.rejudge(_old_rec([same]))
    assert r["verdict"] == "LEAK" and r["leak_fix"] and not r["leak_renamed_module"]
    own = {"path": "/testbed/test_requests.py", "target": "test_requests.py", "role": "tests",
           "found": 2, "total": 2, "leak": True, "in_testbed": True}
    rec = _old_rec([own])
    rec["repo"] = "psf/requests"
    assert runner.rejudge(rec)["verdict"] == "LEAK"


def test_rejudge_filters_other_package_copies():
    rec = _old_rec([], [
        {"path": "/opt/pkgs/packaging-24/info/test/tests/test_utils.py", "target": "tests/test_utils.py", "role": "tests"},
        {"path": "/opt/pkgs/requests-2.31/info/test/tests/test_utils.py", "target": "tests/test_utils.py", "role": "tests"}])
    rec["repo"] = "psf/requests"
    r = runner.rejudge(rec)
    assert [c["path"] for c in r["probe"]["other_copies"]] == ["/opt/pkgs/requests-2.31/info/test/tests/test_utils.py"]
    assert r["other_copy_tests"] is True and r["other_copy_fix"] is False


def test_rejudge_override_removes_a_role_only():
    fix = {"path": "/opt/pkgs/xarray-2025/xarray/core/combine.py", "target": "xarray/core/combine.py",
           "role": "fix", "found": 2, "total": 2, "leak": True, "in_testbed": False}
    tst = {"path": "/opt/pkgs/xarray-2025/xarray/tests/test_combine.py", "target": "xarray/tests/test_combine.py",
           "role": "tests", "found": 2, "total": 2, "leak": True, "in_testbed": False}
    ov = {"pydata__xarray-9": {"leak_fix": False, "leak_tests": True, "reason": "cosmetic"}}
    r = runner.rejudge(_old_rec([fix, tst]), ov)
    assert r["verdict"] == "LEAK" and r["leak_fix"] is False and r["leak_tests"] is True
    assert r["manual_override"]["reason"] == "cosmetic"
    r = runner.rejudge(_old_rec([fix]), {"pydata__xarray-9": {"leak_fix": False}})
    assert r["verdict"] == "CLEAN" and not r["leak_fix"]          # no copies left -> CLEAN
    r = runner.rejudge(_old_rec([fix]), {"someone-else": {"leak_fix": False}})
    assert r["verdict"] == "LEAK" and "manual_override" not in r


def test_rejudge_leaves_error_records():
    rec = {"instance_id": "x", "repo": "a/b", "verdict": "ERROR", "probe": None}
    assert runner.rejudge(rec) == rec


def test_leak_via_tests_role(monkeypatch):
    hit = {"path": "/p/t.py", "target": "t.py", "role": "tests", "found": 2, "total": 2, "leak": True}
    rec, _ = scan(monkeypatch, [probe(fs_hits=[hit])])
    assert rec["verdict"] == "LEAK" and rec["leak_tests"] and not rec["leak_fix"]


def test_non_leak_hit_is_clean(monkeypatch):
    hit = {"path": "/p/a.py", "target": "a.py", "role": "fix", "found": 0, "total": 3, "leak": False}
    rec, _ = scan(monkeypatch, [probe(fs_hits=[hit])])
    assert rec["verdict"] == "CLEAN"


def test_other_copy_without_lines_is_copy(monkeypatch):
    c = {"path": "/opt/pkgs/xarray/core/variable.py", "target": "xarray/core/variable.py",
         "role": "fix", "suffix_depth": 3}
    rec, _ = scan(monkeypatch, [probe(other_copies=[c])])
    assert rec["verdict"] == "COPY" and not rec["leak_fix"]
    assert rec["other_copy_fix"] is True and rec["other_copy_tests"] is False


def test_leak_outranks_copy_and_errors_outrank_copy(monkeypatch):
    c = {"path": "/opt/p/a.py", "target": "a.py", "role": "fix"}
    hit = {"path": "/opt/p/a.py", "target": "a.py", "role": "fix", "found": 2, "total": 2, "leak": True}
    rec, _ = scan(monkeypatch, [probe(other_copies=[c], fs_hits=[hit])])
    assert rec["verdict"] == "LEAK"
    rec, _ = scan(monkeypatch, [probe(other_copies=[c], errors=["git fsck rc=1: boom"])])
    assert rec["verdict"] == "ERROR"


def test_future_commits_alone_are_not_a_leak(monkeypatch):
    # e.g. release-branch tags/backports: history exists, but the task's lines were not found in it
    p = probe()
    d = json.loads(p[1].splitlines()[-1])
    d["git"]["future_commits"] = 4
    d["git"]["future_refs"] = ["refs/tags/2.0"]
    rec, _ = scan(monkeypatch, [(0, json.dumps(d), "")])
    assert rec["verdict"] == "CLEAN" and not rec["leak_git"]
    assert rec["future_history"] is True


def test_leak_via_git_history_hit(monkeypatch):
    h = {"commit": "c" * 40, "target": "a.py", "role": "fix", "found": 2, "total": 3, "leak": True}
    rec, _ = scan(monkeypatch, [probe(git={"future_commits": 1, "future_refs": [],
                                            "unreachable_objects": 0, "unreachable_hits": [],
                                            "history_hits": [h]})])
    assert rec["verdict"] == "LEAK" and rec["leak_git"] and rec["leak_fix"]


def test_probe_errors_block_clean(monkeypatch):
    rec, _ = scan(monkeypatch, [probe(errors=["git rev-list failed: bad object"])])
    assert rec["verdict"] == "ERROR" and "bad object" in rec["error"]


def test_probe_errors_do_not_hide_leak(monkeypatch):
    hit = {"path": "/opt/x/a.py", "target": "a.py", "role": "fix", "found": 3, "total": 3, "leak": True}
    rec, _ = scan(monkeypatch, [probe(fs_hits=[hit], errors=["git fsck rc=1"])])
    assert rec["verdict"] == "LEAK"


def test_timeout_removes_container(monkeypatch):
    rec, fake = scan(monkeypatch, [(124, "", "timeout after 900s")])
    rm = [c for c in fake.calls if c[1] == "rm"]
    run = next(c for c in fake.calls if c[1] == "run")
    assert rm and rm[0][-1] == run[run.index("--name") + 1]
    assert rec["verdict"] == "ERROR"
    assert fake.calls[-1][:2] == ["docker", "rmi"]


def test_leak_via_unreachable_hit(monkeypatch):
    u = {"sha": "d" * 40, "target": "a.py", "role": "fix", "found": 3, "total": 3, "leak": True}
    rec, _ = scan(monkeypatch, [probe(git={"future_commits": 0, "future_refs": [],
                                            "unreachable_objects": 5, "unreachable_hits": [u]})])
    assert rec["verdict"] == "LEAK" and rec["leak_git"] and rec["leak_fix"]


def test_undecidable(monkeypatch):
    rec, _ = scan(monkeypatch, [probe(targets_summary=[{"path": "a.py", "role": "fix", "total": 0}])])
    assert rec["verdict"] == "UNDECIDABLE"


def test_no_targets_is_undecidable_not_clean(monkeypatch):
    rec, _ = scan(monkeypatch, [probe(targets_summary=[])])
    assert rec["verdict"] == "UNDECIDABLE"


def test_fix_decidable_flag(monkeypatch):
    rec, _ = scan(monkeypatch, [probe(targets_summary=[
        {"path": "a.py", "role": "fix", "total": 0}, {"path": "t.py", "role": "tests", "total": 2}])])
    assert rec["verdict"] == "CLEAN" and rec["fix_decidable"] is False


def test_error_on_garbage(monkeypatch):
    rec, fake = scan(monkeypatch, [(1, "Traceback blah", "boom")])
    assert rec["verdict"] == "ERROR" and rec["error"]
    assert fake.calls[-1][:2] == ["docker", "rmi"]


def test_pull_failure_is_error_without_rmi(monkeypatch):
    fake = FakeDocker([], pull_rc=1)
    monkeypatch.setattr(runner, "_run", fake)
    rec = runner.scan_task(TASK)
    assert rec["verdict"] == "ERROR" and "pull" in rec["error"]
    assert all(c[1] != "rmi" for c in fake.calls)


def test_keep_image(monkeypatch):
    _, fake = scan(monkeypatch, [probe()], keep_image=True)
    assert all(c[1] != "rmi" for c in fake.calls)


def test_python_fallback(monkeypatch):
    miss = (127, "", 'exec: "/opt/miniconda3/bin/python3": executable file not found in $PATH')
    rec, fake = scan(monkeypatch, [miss, probe()])
    runs = [c for c in fake.calls if c[1] == "run"]
    assert len(runs) == 2
    assert "/opt/miniconda3/bin/python3" in runs[0] and "python3" in runs[1]
    assert "/opt/miniconda3/bin/python3" not in runs[1]
    assert rec["verdict"] == "CLEAN"


def test_run_cmd_shape(monkeypatch):
    _, fake = scan(monkeypatch, [probe()])
    run = next(c for c in fake.calls if c[1] == "run")
    assert "--network" in run and "none" in run and run[-1] == "-"
    assert run[-2] == "-I" and run[-3] == "example/img:latest"
    spec = json.loads(fake.envs[0]["ANSWERLEAK_SPEC"])
    assert spec["package_hint"] == "xarray"


def test_spec_truncation(monkeypatch):
    big = {"base_commit": "a", "targets": [{"path": "a.py", "role": "fix",
                                            "lines": ["line %d " % i + "y" * 40 for i in range(1000)]}]}
    monkeypatch.setattr(runner, "build_spec", lambda t: big)
    rec, fake = scan(monkeypatch, [probe()], spec_limit=20_000)
    spec = json.loads(fake.envs[0]["ANSWERLEAK_SPEC"])
    assert len(spec["targets"][0]["lines"]) == 200
    assert rec["spec_truncated"] is True


def test_no_truncation_when_small(monkeypatch):
    rec, _ = scan(monkeypatch, [probe()])
    assert "spec_truncated" not in rec


def test_resume_skips_done_but_retries_errors(monkeypatch, tmp_path):
    out = tmp_path / "r" / "results.jsonl"
    out.parent.mkdir()
    out.write_text(json.dumps({"instance_id": "t1", "verdict": "CLEAN"}) + "\n"
                   + json.dumps({"instance_id": "t2", "verdict": "ERROR"}) + "\n")
    seen = []

    def fake_scan(task, **kw):
        seen.append(task["instance_id"])
        return {"instance_id": task["instance_id"], "verdict": "CLEAN", "duration_s": 0.1}

    monkeypatch.setattr(runner, "scan_task", fake_scan)
    tasks = [dict(TASK, instance_id=i) for i in ("t1", "t2", "t3")]
    runner.scan_many(tasks, out, resume=True)
    assert seen == ["t2", "t3"]
    assert len(out.read_text().splitlines()) == 4
    seen.clear()
    runner.scan_many(tasks, out, resume=False)
    assert seen == ["t1", "t2", "t3"]


def test_build_spec_real_patchparse(monkeypatch):
    pytest.importorskip("answerleak.patchparse")
    monkeypatch.undo()  # drop the autouse stub of build_spec
    diff = ("diff --git a/pkg/m.py b/pkg/m.py\n--- a/pkg/m.py\n+++ b/pkg/m.py\n"
            "@@ -1,1 +1,2 @@\n old\n+    return compute_value(x)\n")
    t = dict(TASK, patch=diff, test_patch=diff.replace("pkg/m.py", "tests/t.py"))
    spec = runner.build_spec(t)
    assert spec["testbed"] == "/testbed" and spec["package_hint"] == "xarray"
    assert spec["max_unreachable_blobs"] == 2000
    assert {(x["path"], x["role"]) for x in spec["targets"]} == {("pkg/m.py", "fix"), ("tests/t.py", "tests")}


def test_build_spec_prefilters_lines(monkeypatch):
    monkeypatch.undo()
    diff = ("--- a/pkg/m.py\n+++ b/pkg/m.py\n@@ -1,1 +1,4 @@\n old\n+x = 1\n"
            "+    return compute_value(x)\n+    return compute_value(x)\n+# a long comment line here!!\n")
    spec = runner.build_spec(dict(TASK, patch=diff))
    assert spec["targets"] == [{"path": "pkg/m.py", "role": "fix", "lines": ["return compute_value(x)"]}]
