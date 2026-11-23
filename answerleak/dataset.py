"""Fetch SWE-bench task rows from the Hugging Face datasets-server and cache them."""
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FIELDS = ("instance_id", "repo", "base_commit", "image", "patch", "test_patch")
PAGE = 100
RETRIES = 4


def _fetch_rows(dataset, offset, length):
    """Return (rows, total) for one page. Network isolated here for monkeypatching."""
    qs = urllib.parse.urlencode({
        "dataset": dataset, "config": "default", "split": "test",
        "offset": offset, "length": length,
    })
    url = "https://datasets-server.huggingface.co/rows?" + qs
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "answerleak"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            rows = [r["row"] for r in data["rows"]]
            return rows, data["num_rows_total"]
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:  # 429 = rate limited, retry
                raise
            last = e
        except urllib.error.URLError as e:
            last = e
        if attempt < RETRIES - 1:
            time.sleep(2 ** attempt)
    raise RuntimeError("datasets-server request failed after %d tries: %s" % (RETRIES, last))


def _fetch_all(dataset):
    tasks = []
    offset = 0
    total = None
    while total is None or offset < total:
        rows, total = _fetch_rows(dataset, offset, PAGE)
        if not rows:
            break
        tasks.extend({k: r.get(k) for k in FIELDS} for r in rows)
        offset += len(rows)
    for t in tasks:
        if not t.get("image") and t.get("instance_id"):
            # Official SWE-bench naming; used only if the dataset has no image column.
            t["image"] = "swebench/sweb.eval.x86_64.%s:latest" % t["instance_id"].replace("__", "_1776_")
    return tasks


def load_tasks(ids=None, repo=None, sample=None, seed=0,
               dataset="SWE-bench/SWE-bench_Verified", cache_dir=None):
    cache_dir = Path(cache_dir) if cache_dir else Path.home() / ".cache" / "answerleak"
    cache = cache_dir / (dataset.replace("/", "__") + ".json")
    tasks = None
    if cache.exists():
        try:
            tasks = json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            tasks = None
    if not tasks:
        tasks = _fetch_all(dataset)
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(tasks), encoding="utf-8")

    if ids is not None:
        by_id = {t["instance_id"]: t for t in tasks}
        missing = [i for i in ids if i not in by_id]
        if missing:
            raise ValueError("unknown instance ids: " + ", ".join(missing))
        tasks = [by_id[i] for i in ids]
    if repo is not None:
        tasks = [t for t in tasks if t["repo"] == repo]
    if sample is not None:
        tasks = random.Random(seed).sample(tasks, min(sample, len(tasks)))
    return tasks
