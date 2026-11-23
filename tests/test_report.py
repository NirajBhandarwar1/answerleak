import csv
import json

import pytest

from answerleak import cli, report


def test_wilson_known_values():
    lo, hi = report.wilson(5, 9)
    assert lo == pytest.approx(0.267, abs=0.002) and hi == pytest.approx(0.811, abs=0.002)
    lo, hi = report.wilson(0, 30)
    assert lo == 0.0 and hi == pytest.approx(0.114, abs=0.002)
    assert report.wilson(0, 0) == (0.0, 1.0)


def rec(i, repo, verdict, **kw):
    r = {"instance_id": i, "repo": repo, "digest": "img@sha256:" + "ab" * 32, "verdict": verdict,
         "leak_fix": False, "leak_tests": False, "leak_git": False, "probe": {}}
    r.update(kw)
    return r


@pytest.fixture
def results(tmp_path):
    p = tmp_path / "results.jsonl"
    rows = [
        rec("pydata__xarray-1", "pydata/xarray", "LEAK", leak_fix=True,
            probe={"fs_hits": [{"path": "/opt/x/dataset.py", "found": 3, "total": 4, "leak": True}]}),
        rec("pydata__xarray-2", "pydata/xarray", "CLEAN"),
        rec("pydata__xarray-3", "pydata/xarray", "COPY", other_copy_fix=True,
            probe={"other_copies": [{"path": "/opt/pkgs/xarray/core/variable.py", "role": "fix"}]}),
        rec("psf__requests-1", "psf/requests", "LEAK", leak_git=True, leak_tests=True,
            probe={"git": {"future_commits": 2, "unreachable_hits": []}}),
        rec("sympy__sympy-1", "sympy/sympy", "UNDECIDABLE"),
        rec("sympy__sympy-2", "sympy/sympy", "ERROR"),
        rec("sympy__sympy-2", "sympy/sympy", "CLEAN"),  # later record supersedes
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_summarize(results):
    s = report.summarize(results)
    assert s["total"] == 6
    assert s["counts"] == {"LEAK": 2, "COPY": 1, "CLEAN": 2, "UNDECIDABLE": 1, "ERROR": 0}
    assert s["rate"]["k"] == 2 and s["rate"]["n"] == 5            # exact lines
    assert s["exposure_rate"]["k"] == 3 and s["exposure_rate"]["n"] == 5
    assert s["channels"] == {"leak_fix": 1, "leak_tests": 1, "leak_git": 1,
                             "other_copy_fix": 1, "other_copy_tests": 0}
    assert s["per_repo"]["pydata/xarray"]["LEAK"] == 1
    assert s["per_repo"]["pydata/xarray"]["COPY"] == 1
    assert s["per_repo"]["sympy/sympy"]["rate"]["n"] == 1
    assert s["copies"][0]["instance_id"] == "pydata__xarray-3"


def test_markdown(results):
    md = report.to_markdown(report.summarize(results))
    assert "| repo |" in md and "| pydata/xarray | 1 | 1 | 1 | 0 | 0 |" in md
    assert "ERROR and UNDECIDABLE tasks are excluded" in md
    assert "pydata__xarray-1" in md and "/opt/x/dataset.py" in md and "3/4" in md
    assert "sha256:abab" in md
    assert "git: 2 future commit(s)" in md
    assert "## COPY tasks" in md and "/opt/pkgs/xarray/core/variable.py" in md


def test_split_scope_and_moved_module_section(tmp_path):
    p = tmp_path / "r.jsonl"
    rows = [
        rec("a-1", "o/a", "LEAK", leak_fix=True, scanned_at="2026-11-21T10:00:00+00:00"),
        rec("a-2", "o/a", "LEAK", leak_tests=True, scanned_at="2026-11-23T10:00:00+00:00"),
        rec("a-3", "o/a", "LEAK", leak_fix=True, leak_tests=True),
        rec("a-4", "o/a", "COPY", leak_renamed_module=True,
            probe={"fs_hits": [{"path": "/opt/a/moved/x.py", "target": "a/x.py", "role": "fix",
                                "found": 1, "total": 1, "lines_ok": True, "same_module": False,
                                "in_testbed": False, "leak": False}]}),
    ]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    s = report.summarize(p)
    assert s["leak_split"] == {"fix_only": 1, "tests_only": 1, "both": 1, "git_only": 0}
    assert s["scan_dates"] == ("2026-11-21", "2026-11-23")
    assert [x["instance_id"] for x in s["renamed"]] == ["a-4"]
    md = report.to_markdown(s)
    assert "Presence only" in md and "not the whole benchmark" in md and "#640" in md
    assert "Fix lines only: 1" in md and "Hidden-test lines only: 1" in md and "Both fix and hidden-test lines: 1" in md
    assert "## Not counted: lines found only in a moved module" in md and "/opt/a/moved/x.py" in md
    assert "2026-11-21 to 2026-11-23" in md


def test_no_confidence_intervals_and_extra_sections(results):
    s = report.summarize(results)
    md = report.to_markdown(s, notes="## Credit\n\nIssue #640 first reported this.")
    assert "95% CI" not in md and "no confidence intervals" in md
    assert "2/5 = 40.0%" in md
    assert "another version of a changed file present (LEAK + COPY)" in md and "any exposure" not in md
    assert "## Credit" in md and md.index("## Credit") < md.index("## Rates")
    assert "## CLEAN tasks" in md and "| pydata__xarray-2 | sha256:abab" in md
    assert "## Reviewer overrides" in md and "pip/_vendor/requests/" in md


def test_overrides_and_moved_rows_for_leak_tasks(tmp_path):
    p = tmp_path / "r.jsonl"
    moved = {"path": "/opt/a/moved/x.py", "target": "a/x.py", "role": "fix", "found": 1, "total": 1,
             "lines_ok": True, "same_module": False, "in_testbed": False, "leak": False}
    rows = [rec("a-1", "o/a", "LEAK", leak_tests=True, leak_renamed_module=True,
                manual_override={"leak_fix": False, "leak_tests": True, "reason": "only annotations"},
                probe={"fs_hits": [moved]})]
    p.write_text(json.dumps(rows[0]) + "\n")
    s = report.summarize(p)
    assert s["overrides"] == [{"instance_id": "a-1", "verdict": "LEAK", "reason": "only annotations"}]
    assert [x["instance_id"] for x in s["renamed"]] == ["a-1"]        # LEAK tasks are listed too
    md = report.to_markdown(s)
    assert "only annotations" in md and "reviewer override" in md


def test_single_line_fixes_digest_note_and_csv_override(tmp_path):
    p = tmp_path / "r.jsonl"
    one = {"path": "/opt/p/a/x.py", "target": "a/x.py", "role": "fix", "found": 1, "total": 1,
           "lines_ok": True, "same_module": True, "leak": True}
    many = dict(one, found=3, total=4)
    rows = [rec("a-1", "o/a", "LEAK", leak_fix=True, scanned_at="2026-11-21T00:00:00+00:00", probe={"fs_hits": [one]}),
            rec("a-2", "o/a", "LEAK", leak_fix=True, probe={"fs_hits": [one, many]}),
            rec("a-3", "o/a", "LEAK", leak_tests=True,
                manual_override={"leak_fix": False, "leak_tests": True, "reason": "cosmetic only"})]
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    s = report.summarize(p)
    assert s["single_line_fix"] == ["a-1"]
    md = report.to_markdown(s)
    assert "for 1 of these the fix has only ONE distinctive line" in md and "a-1" in md
    assert "image digest recorded by" in md and "full ones are in the CSV" in md
    assert "all whitespace removed" in md
    out = tmp_path / "r.csv"
    report.to_csv(p, out)
    got = list(csv.DictReader(open(out, encoding="utf-8")))
    assert got[2]["override_reason"] == "cosmetic only" and got[0]["override_reason"] == ""


def test_csv(results, tmp_path):
    out = tmp_path / "o" / "r.csv"
    report.to_csv(results, out)
    rows = list(csv.DictReader(open(out, encoding="utf-8")))
    assert len(rows) == 6 and rows[0]["instance_id"] == "pydata__xarray-1"


def test_cli_report(results, tmp_path, capsys):
    md, cs = tmp_path / "r.md", tmp_path / "r.csv"
    assert cli.main(["report", "--results", str(results), "--md", str(md), "--csv", str(cs)]) == 0
    assert md.exists() and cs.exists()
    assert cli.main(["report", "--results", str(tmp_path / "nope.jsonl")]) == 1
