"""answerleak command line: scan task images, then report."""
import argparse
import sys
from pathlib import Path


def _build_parser():
    p = argparse.ArgumentParser(prog="answerleak", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan", help="scan task images for readable fixes")
    s.add_argument("--ids", nargs="+")
    s.add_argument("--repo")
    s.add_argument("--sample", type=int)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--out", default="results/results.jsonl")
    s.add_argument("--keep-images", action="store_true")
    s.add_argument("--no-resume", action="store_true")
    s.add_argument("--dataset")
    pl = sub.add_parser("plan", help="split tasks into shards (GitHub Actions matrix JSON)")
    pl.add_argument("--repos", nargs="*", default=[], help="owner/name ...; empty = every repo")
    pl.add_argument("--exclude", nargs="*", default=[], help="owner/name ... to skip")
    pl.add_argument("--limit", type=int, default=0, help="max tasks (0 = all); spread across repos")
    pl.add_argument("--shards", type=int, default=40)
    pl.add_argument("--dataset")
    pl.add_argument("--github-output", help="append matrix=<json> to this file (GITHUB_OUTPUT)")
    j = sub.add_parser("rejudge", help="apply the strict same-module rule to saved results (no Docker)")
    j.add_argument("--results", required=True)
    j.add_argument("--out", required=True)
    j.add_argument("--overrides", help="JSON file of reviewer overrides (can only remove a role)")
    r = sub.add_parser("report", help="summarize results.jsonl")
    r.add_argument("--results", default="results/results.jsonl")
    r.add_argument("--notes", help="markdown file inserted after the scope section (credits, context)")
    r.add_argument("--md", default="results/report.md")
    r.add_argument("--csv", default="results/results.csv")
    return p


def main(argv=None):
    a = _build_parser().parse_args(argv)
    if a.cmd == "scan":
        from . import dataset, runner
        kw = dict(ids=a.ids, repo=a.repo, sample=a.sample, seed=a.seed)
        if a.dataset:
            kw["dataset"] = a.dataset
        tasks = dataset.load_tasks(**kw)
        print("%d task(s) selected" % len(tasks), flush=True)
        runner.scan_many(tasks, a.out, resume=not a.no_resume, keep_image=a.keep_images)
        return 0
    if a.cmd == "plan":
        from . import dataset, plan
        kw = {"dataset": a.dataset} if a.dataset else {}
        m = plan.matrix(dataset.load_tasks(**kw), a.repos, a.exclude, a.limit, a.shards)
        if not m["include"]:
            print("no tasks selected: check --repos (owner/name, e.g. pydata/xarray) and --exclude",
                  file=sys.stderr)
            return 1
        if a.github_output:
            with open(a.github_output, "a", encoding="utf-8") as f:
                f.write("matrix=%s\n" % plan.to_json(m))
        print(plan.summary(m), file=sys.stderr)
        print(plan.to_json(m))
        return 0
    if a.cmd == "rejudge":
        import json
        from . import runner
        src = Path(a.results)
        if not src.exists():
            print("no results file: %s" % a.results, file=sys.stderr)
            return 1
        ov = json.loads(Path(a.overrides).read_text(encoding="utf-8")) if a.overrides else None
        recs = [runner.rejudge(json.loads(l), ov) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        for r in recs:
            print("%s %s%s" % (r["instance_id"], r["verdict"],
                               " (+renamed-module lines)" if r.get("leak_renamed_module") else ""))
        return 0
    from . import report
    if not Path(a.results).exists():
        print("no results file: %s" % a.results, file=sys.stderr)
        return 1
    notes = Path(a.notes).read_text(encoding="utf-8") if a.notes else None
    md = report.to_markdown(report.summarize(a.results), notes=notes)
    Path(a.md).parent.mkdir(parents=True, exist_ok=True)
    Path(a.md).write_text(md, encoding="utf-8")
    report.to_csv(a.results, a.csv)
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
