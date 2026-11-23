# answerleak

Checks whether the answer to a coding-benchmark task is readable inside the task's own Docker image.

For each [SWE-bench Verified](https://huggingface.co/datasets/SWE-bench/SWE-bench_Verified) task, answerleak takes
the lines added by the gold patch (the fix) and by the hidden test patch, keeps only lines that are new relative to
the file at the task's `base_commit`, and searches the image's filesystem outside `/testbed` for copies of the same
module that contain those lines. It also looks for future git history inside `/testbed`.

It shows whether the answer is **present and readable** in the image. It does **not** show that any agent read or
used it.

This builds on [SWE-bench issue #640](https://github.com/SWE-bench/SWE-bench/issues/640), which reported newer
package source in the Conda package cache of several official images.

## Verdicts

| Verdict | Meaning |
|---|---|
| `LEAK` | Enough of the fix or hidden-test lines are present in a copy of the **same module path** outside `/testbed` (or in git objects). |
| `COPY` | A different version of a changed file is present at the same module path, but not enough of the lines to count. |
| `CLEAN` | Neither. |
| `UNDECIDABLE` | The patch has no lines distinctive enough to search for. |
| `ERROR` | The scan failed or was partial. |

**Distinctive line:** at least 20 characters, contains a letter, not a comment, not already in the original file,
and not just a re-wrap of existing code. Lines are compared ignoring whitespace and quote style.

**When a file counts:** at least half of that file's distinctive lines are found in one copy, and at least 2 of
them (or the single line when the change has only one).

**Same module path:** e.g. `xarray/core/combine.py` matches `.../site-packages/xarray/core/combine.py` or
`.../pip/_vendor/requests/models.py` for `requests/models.py`. A same-named file in a different folder (e.g. a
module moved upstream) does not count; it is listed separately in the report. Files at the repo root or under a
generic folder such as `tests/` also need the package name in the directories above.

## Run it locally

Requirements: Python 3.10+, Docker. Each image is downloaded, scanned offline (`--network none`) and deleted
again. Images are large (often 7+ GB unpacked), so expect several minutes per task on a home connection.

```bash
pip install -e .
answerleak scan --ids pydata__xarray-6992 --out results/results.jsonl
answerleak scan --repo psf/requests --out results/results.jsonl     # all tasks of one repo
answerleak report --results results/results.jsonl --md results/report.md --csv results/results.csv
```

Other commands:

- `answerleak plan`: split tasks into shards for parallel runs (prints a GitHub Actions matrix).
- `answerleak rejudge`: re-apply the current rules to saved results without Docker.

## Run it on GitHub Actions

`.github/workflows/scan.yml` scans many tasks in parallel on GitHub-hosted runners (free for public repositories).
Open the **Actions** tab, choose **scan**, then **Run workflow**:

- `limit`: start with `10` for a test run; `0` scans every selected task.
- `exclude`: repos to skip (default: `pydata/xarray psf/requests`).
- `shards`: number of parallel jobs (the free plan runs 20 at a time).

Each job frees disk space, scans its share of tasks, and uploads its results. A final job merges them into
`report.md`, `results.csv` and `results.jsonl` (artifact `report`). The job summary shows only the counts.

If Docker Hub rate-limits anonymous pulls, add a repository variable `DOCKERHUB_USER` and a secret
`DOCKERHUB_TOKEN` (a free Docker Hub access token); the workflow logs in when both are set.

## Limits

- Results describe the scanned images at the time of the scan; each result records the image digest.
- Exact-line matching is conservative: a fix that upstream later reformatted is not counted.
- One-line fixes can be a LEAK on that one line; the report says how many leaks rest on a single line.
- The tool reports presence, not use.

## Tests

```bash
pip install pytest
python -m pytest -q
```

## License

MIT
