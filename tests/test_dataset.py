import pytest

from answerleak import dataset


def _rows(n):
    out = []
    for i in range(n):
        repo = "pydata/xarray" if i % 2 == 0 else "psf/requests"
        out.append({"instance_id": "r__%d" % i, "repo": repo, "base_commit": "c%d" % i,
                    "image": "img%d" % i, "patch": "p", "test_patch": "t", "extra": "drop"})
    return out


@pytest.fixture
def fake(monkeypatch):
    calls = []
    rows = _rows(250)

    def fetch(ds, offset, length):
        calls.append((ds, offset, length))
        return rows[offset:offset + length], len(rows)

    monkeypatch.setattr(dataset, "_fetch_rows", fetch)
    return calls


def test_paginates_and_keeps_fields(fake, tmp_path):
    tasks = dataset.load_tasks(cache_dir=tmp_path)
    assert len(tasks) == 250
    assert [c[1] for c in fake] == [0, 100, 200]
    assert set(tasks[0]) == set(dataset.FIELDS)


def test_cache_reused(fake, tmp_path):
    dataset.load_tasks(cache_dir=tmp_path)
    n = len(fake)
    dataset.load_tasks(cache_dir=tmp_path)
    assert len(fake) == n
    assert (tmp_path / "SWE-bench__SWE-bench_Verified.json").exists()


def test_ids_preserve_order_and_unknown(fake, tmp_path):
    t = dataset.load_tasks(ids=["r__5", "r__1"], cache_dir=tmp_path)
    assert [x["instance_id"] for x in t] == ["r__5", "r__1"]
    with pytest.raises(ValueError, match="nope"):
        dataset.load_tasks(ids=["r__1", "nope"], cache_dir=tmp_path)


def test_repo_filter(fake, tmp_path):
    t = dataset.load_tasks(repo="psf/requests", cache_dir=tmp_path)
    assert len(t) == 125 and all(x["repo"] == "psf/requests" for x in t)


def test_image_fallback_when_column_missing(monkeypatch, tmp_path):
    rows = [{"instance_id": "pydata__xarray-6992", "repo": "pydata/xarray"}]
    monkeypatch.setattr(dataset, "_fetch_rows", lambda ds, o, n: (rows[o:o + n], 1))
    t = dataset.load_tasks(cache_dir=tmp_path)
    assert t[0]["image"] == "swebench/sweb.eval.x86_64.pydata_1776_xarray-6992:latest"


def test_rate_limit_is_retried(monkeypatch):
    import io
    import json as _json
    import urllib.error

    calls = []

    def urlopen(req, timeout=None):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {}, None)
        return io.BytesIO(_json.dumps({"rows": [{"row": {"a": 1}}], "num_rows_total": 1}).encode())

    monkeypatch.setattr(dataset.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(dataset.time, "sleep", lambda s: None)
    assert dataset._fetch_rows("x/y", 0, 1) == ([{"a": 1}], 1)
    assert len(calls) == 2


def test_sample_deterministic_after_repo(fake, tmp_path):
    a = dataset.load_tasks(repo="pydata/xarray", sample=5, seed=3, cache_dir=tmp_path)
    b = dataset.load_tasks(repo="pydata/xarray", sample=5, seed=3, cache_dir=tmp_path)
    c = dataset.load_tasks(repo="pydata/xarray", sample=5, seed=4, cache_dir=tmp_path)
    assert a == b and len(a) == 5
    assert all(x["repo"] == "pydata/xarray" for x in a)
    assert a != c
