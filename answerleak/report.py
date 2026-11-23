"""Aggregate results.jsonl into counts, markdown and CSV (plain counts: a scan of every task of a
repo is a census, not a sample; wilson() is kept for sampled runs).

Verdict levels:
  LEAK        exact distinctive fix/test lines readable in a copy of the SAME module path outside
              /testbed (or in git objects); same-named files at a different path do not count
  COPY        no exact lines, but a different copy of a changed file's module path is readable
              outside /testbed (SWE-bench issue #640's definition of exposure)
  CLEAN       neither
  UNDECIDABLE the patch has no distinctive lines and no other copy was found
  ERROR       scan failed or was partial
"""
import csv
import json
import math
from collections import OrderedDict
from pathlib import Path

VERDICTS = ("LEAK", "COPY", "CLEAN", "UNDECIDABLE", "ERROR")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def load_results(results_path):
    """Last record per instance_id wins."""
    recs = OrderedDict()
    for line in Path(results_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        recs[r["instance_id"]] = r
    return list(recs.values())


def _rate_of(k, n):
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n if n else None), "ci_low": lo, "ci_high": hi}


def _rate(counts):
    """Exact-lines rate: LEAK / (LEAK + COPY + CLEAN)."""
    n = counts["LEAK"] + counts.get("COPY", 0) + counts["CLEAN"]
    return _rate_of(counts["LEAK"], n)


def _exposure_rate(counts):
    """Exact lines, or at least another version of a changed file: (LEAK + COPY) / (LEAK + COPY + CLEAN)."""
    n = counts["LEAK"] + counts.get("COPY", 0) + counts["CLEAN"]
    return _rate_of(counts["LEAK"] + counts.get("COPY", 0), n)


def _example(r):
    probe = r.get("probe") or {}
    for h in probe.get("fs_hits") or []:
        if h.get("leak"):
            return h.get("path"), h.get("found"), h.get("total"), h.get("matched") or []
    git = probe.get("git") or {}
    for h in git.get("history_hits") or []:
        if h.get("leak"):
            return ("git commit %s:%s" % ((h.get("commit") or "")[:10], h.get("target")),
                    h.get("found"), h.get("total"), [])
    for h in git.get("unreachable_hits") or []:
        if h.get("leak"):
            return ("git:%s %s" % ((h.get("sha") or "")[:10], h.get("target")),
                    h.get("found"), h.get("total"), [])
    if git.get("future_commits"):
        return "git: %d future commit(s)" % git["future_commits"], None, None, []
    return None, None, None, []


def _short_digest(r):
    digest = r.get("digest") or ""
    return digest.split("@")[-1][:19] if digest else ""


def summarize(results_path):
    recs = load_results(results_path)
    overall = {v: 0 for v in VERDICTS}
    repos = OrderedDict()
    for r in recs:
        v = r.get("verdict") if r.get("verdict") in VERDICTS else "ERROR"
        overall[v] += 1
        repos.setdefault(r.get("repo", "?"), {x: 0 for x in VERDICTS})[v] += 1
    leaks, copies, renamed, cleans, overrides = [], [], [], [], []
    for r in recs:
        if r.get("manual_override"):
            overrides.append({"instance_id": r["instance_id"], "verdict": r.get("verdict"),
                              "reason": r["manual_override"].get("reason", "")})
        if r.get("verdict") == "CLEAN":
            cleans.append({"instance_id": r["instance_id"], "digest": _short_digest(r)})
        # every role with enough lines found only in a moved module (one row per task+role)
        seen = set()
        for h in (r.get("probe") or {}).get("fs_hits") or []:
            if (h.get("lines_ok") and not h.get("same_module") and not h.get("in_testbed")
                    and (h.get("role"), h.get("target")) not in seen):
                seen.add((h.get("role"), h.get("target")))
                renamed.append({"instance_id": r["instance_id"], "verdict": r.get("verdict"),
                                "role": h.get("role"), "target": h.get("target"),
                                "path": h.get("path"), "found": h.get("found"),
                                "total": h.get("total")})
        if r.get("verdict") == "LEAK":
            channels = [c for c, f in (("fix", "leak_fix"), ("tests", "leak_tests"), ("git", "leak_git"))
                        if r.get(f)]
            if r.get("manual_override"):
                channels.append("(reviewer override, see below)")
            path, found, total, matched = _example(r)
            leaks.append({
                "instance_id": r["instance_id"], "repo": r.get("repo"), "digest": _short_digest(r),
                "channels": channels, "example_path": path, "found": found, "total": total,
                "matched": matched[:2],
            })
        elif r.get("verdict") == "COPY":
            cs = (r.get("probe") or {}).get("other_copies") or []
            copies.append({
                "instance_id": r["instance_id"], "repo": r.get("repo"), "digest": _short_digest(r),
                "roles": sorted(set(c.get("role") for c in cs)),
                "example_path": cs[0].get("path") if cs else None, "n_copies": len(cs),
            })
    leak_recs = [r for r in recs if r.get("verdict") == "LEAK"]

    def fix_max_total(r):
        hs = [h for h in (r.get("probe") or {}).get("fs_hits") or [] if h.get("leak") and h.get("role") == "fix"]
        return max([h.get("total") or 0 for h in hs] or [0])
    single_line_fix = [r["instance_id"] for r in leak_recs if r.get("leak_fix") and fix_max_total(r) == 1]
    dates = sorted((r.get("scanned_at") or "")[:10] for r in recs if r.get("scanned_at"))
    return {
        "total": len(recs),
        "scan_dates": (dates[0], dates[-1]) if dates else None,
        "renamed": renamed,
        "cleans": cleans,
        "overrides": overrides,
        "single_line_fix": single_line_fix,
        "leak_split": {
            "fix_only": sum(1 for r in leak_recs if r.get("leak_fix") and not r.get("leak_tests")),
            "tests_only": sum(1 for r in leak_recs if r.get("leak_tests") and not r.get("leak_fix")),
            "both": sum(1 for r in leak_recs if r.get("leak_fix") and r.get("leak_tests")),
            "git_only": sum(1 for r in leak_recs if not r.get("leak_fix") and not r.get("leak_tests")),
        },
        "counts": overall,
        "rate": _rate(overall),
        "exposure_rate": _exposure_rate(overall),
        "channels": {
            "leak_fix": sum(1 for r in recs if r.get("leak_fix")),
            "leak_tests": sum(1 for r in recs if r.get("leak_tests")),
            "leak_git": sum(1 for r in recs if r.get("leak_git")),
            "other_copy_fix": sum(1 for r in recs if r.get("other_copy_fix")),
            "other_copy_tests": sum(1 for r in recs if r.get("other_copy_tests")),
        },
        "per_repo": {repo: dict(c, rate=_rate(c), exposure_rate=_exposure_rate(c))
                     for repo, c in repos.items()},
        "leaks": leaks,
        "copies": copies,
    }


def _fmt_rate(r):
    # Plain counts: the scanned tasks are every task of the listed repos (a census, not a
    # sample), so a confidence interval would be misleading. wilson() stays for sampled runs.
    if not r["n"]:
        return "n/a (N=0)"
    return "%d/%d = %.1f%%" % (r["k"], r["n"], 100 * r["rate"])


def _cell(s):
    return str(s).replace("|", "\\|")


def to_markdown(summary, notes=None):
    c = summary["counts"]
    ch = summary["channels"]
    sp = summary.get("leak_split") or {}
    L = ["# answerleak report", ""]
    L.append("Tasks scanned: %d  (LEAK %d, COPY %d, CLEAN %d, UNDECIDABLE %d, ERROR %d)" % (
        summary["total"], c["LEAK"], c["COPY"], c["CLEAN"], c["UNDECIDABLE"], c["ERROR"]))
    if summary.get("scan_dates"):
        L.append("Scan dates (UTC): %s to %s. Every result is pinned to the image digest recorded by "
                 "`docker pull`. Digests are shortened below; the full ones are in the CSV." % summary["scan_dates"])
    L += ["", "## Scope and limits (read first)", "",
          "- **Presence only.** A LEAK means the lines are present and readable inside the image. "
          "It does not show that any agent read or used them.",
          "- **Only the tasks listed here.** The per-repo table shows which repos were scanned. The "
          "numbers describe these tasks at these image digests only, not the whole benchmark.",
          "- **Strict rule.** Lines count only in a copy of the same module path (e.g. "
          "`xarray/core/combine.py`, or `requests/models.py` including the copies of requests "
          "bundled inside pip at `pip/_vendor/requests/`). Lines found in a same-named file that "
          "upstream later moved to another folder are listed separately below and are not counted.",
          "- **COPY is weak.** COPY only means another version of a changed file is present; the "
          "fix/test lines were not found in it (for requests, pip's own bundled copy of requests is "
          "in every image).",
          "- Builds on SWE-bench GitHub issue #640 (newer package source in the Conda package cache).",
          ""]
    if notes:
        L += [notes.strip(), ""]
    L += ["## Rates", "",
          "- Exact fix/test lines readable (LEAK): %s" % _fmt_rate(summary["rate"]),
          "- Exact lines, or at least another version of a changed file present (LEAK + COPY): %s"
          % _fmt_rate(summary["exposure_rate"]),
          "",
          "N = LEAK + COPY + CLEAN; ERROR and UNDECIDABLE tasks are excluded. These are counts over "
          "every scanned task, not a sample, so no confidence intervals are given.",
          "",
          "## What leaks in the LEAK tasks", "",
          "- Fix lines only: %d" % sp.get("fix_only", 0),
          "- Hidden-test lines only: %d" % sp.get("tests_only", 0),
          "- Both fix and hidden-test lines: %d" % sp.get("both", 0),
          ]
    if sp.get("git_only"):
        L.append("- Git objects only: %d" % sp["git_only"])
    L += ["",
          "## Channels", "",
          "- leak_fix (exact fix lines readable): %d%s" % (ch["leak_fix"], (
              " (for %d of these the fix has only ONE distinctive line, and that exact line is present: %s)"
              % (len(summary["single_line_fix"]), ", ".join(summary["single_line_fix"])))
              if summary.get("single_line_fix") else ""),
          "- leak_tests (exact hidden-test lines readable): %d" % ch["leak_tests"],
          "- leak_git (fix/test lines found in future git commits or unreachable objects): %d" % ch["leak_git"],
          "- other_copy_fix (another copy of a fixed file's module path): %d" % ch["other_copy_fix"],
          "- other_copy_tests (another copy of a test file's module path): %d" % ch["other_copy_tests"],
          "",
          "## Per repo", "",
          "| repo | LEAK | COPY | CLEAN | UNDECIDABLE | ERROR | exact lines (LEAK) k/N | another version of a changed file present (LEAK + COPY) k/N |",
          "|---|---|---|---|---|---|---|---|"]
    for repo, d in summary["per_repo"].items():
        L.append("| %s | %d | %d | %d | %d | %d | %s | %s |" % (
            repo, d["LEAK"], d["COPY"], d["CLEAN"], d["UNDECIDABLE"], d["ERROR"],
            _fmt_rate(d["rate"]), _fmt_rate(d["exposure_rate"])))
    L += ["", "## LEAK tasks (manual review required before any claim)", "",
          "Matched lines are shown as the tool compares them: all whitespace removed, ' shown as \".", ""]
    if summary["leaks"]:
        L += ["| instance_id | digest | channels | example path | found/total | sample matched lines |",
              "|---|---|---|---|---|---|"]
        for x in summary["leaks"]:
            ft = "%s/%s" % (x["found"], x["total"]) if x["found"] is not None else ""
            sample = "<br>".join("`%s`" % _cell(m) for m in x.get("matched") or [])
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                x["instance_id"], x["digest"], ", ".join(x["channels"]), _cell(x["example_path"] or ""),
                ft, sample))
    else:
        L.append("None.")
    L += ["", "## COPY tasks (another copy of a changed file, exact lines not found)", ""]
    if summary["copies"]:
        L += ["| instance_id | digest | roles | copies | example path |", "|---|---|---|---|---|"]
        for x in summary["copies"]:
            L.append("| %s | %s | %s | %d | %s |" % (
                x["instance_id"], x["digest"], ", ".join(x["roles"]), x["n_copies"],
                _cell(x["example_path"] or "")))
    else:
        L.append("None.")
    L += ["", "## CLEAN tasks", ""]
    if summary.get("cleans"):
        L += ["| instance_id | digest |", "|---|---|"]
        L += ["| %s | %s |" % (x["instance_id"], x["digest"]) for x in summary["cleans"]]
    else:
        L.append("None.")
    L += ["", "## Reviewer overrides (a role removed after manual review)", ""]
    if summary.get("overrides"):
        L += ["| instance_id | verdict | reason |", "|---|---|---|"]
        L += ["| %s | %s | %s |" % (x["instance_id"], x["verdict"], _cell(x["reason"]))
              for x in summary["overrides"]]
    else:
        L.append("None.")
    L += ["", "## Not counted: lines found only in a moved module (strict rule)", ""]
    if summary.get("renamed"):
        L += ["Enough distinctive lines were found, but in a same-named file at a different module "
              "path (upstream moved the module). Likely the same code, but excluded from LEAK. "
              "The verdict column is the task's verdict from its other (counted) evidence.", "",
              "| instance_id | verdict | role | changed file | found in | found/total |",
              "|---|---|---|---|---|---|"]
        for x in summary["renamed"]:
            L.append("| %s | %s | %s | %s | %s | %s/%s |" % (
                x["instance_id"], x["verdict"], x["role"], _cell(x["target"] or ""),
                _cell(x["path"] or ""), x["found"], x["total"]))
    else:
        L.append("None.")
    return "\n".join(L) + "\n"


def to_csv(results_path, csv_path):
    recs = load_results(results_path)
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    cols = ["instance_id", "repo", "image", "digest", "scanned_at", "verdict",
            "leak_fix", "leak_tests", "leak_git", "leak_renamed_module", "other_copy_fix", "other_copy_tests",
            "duration_s", "error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols + ["override_reason"])
        for r in recs:
            w.writerow([r.get(k, "") for k in cols] + [(r.get("manual_override") or {}).get("reason", "")])
