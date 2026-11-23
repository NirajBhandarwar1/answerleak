"""Split selected tasks into shards for parallel scanning (e.g. a GitHub Actions job matrix)."""
import json
from collections import OrderedDict


def select(tasks, repos=None, exclude=None, limit=0):
    """Tasks of `repos` (all repos if empty) minus `exclude`, in dataset order. With a limit,
    take tasks round-robin across repos so even a small test run covers many repos.
    A repo named in `repos` is always scanned, even if it is also in `exclude`."""
    repos = set(repos or [])
    exclude = set(exclude or []) - repos
    picked = [t for t in tasks if (not repos or t["repo"] in repos) and t["repo"] not in exclude]
    if not limit or limit >= len(picked):
        return picked
    by_repo = OrderedDict()
    for t in picked:
        by_repo.setdefault(t["repo"], []).append(t)
    out = []
    while len(out) < limit:
        for repo in list(by_repo):
            if by_repo[repo] and len(out) < limit:
                out.append(by_repo[repo].pop(0))
    return out


def shard(ids, n):
    """Deal ids into at most n non-empty shards (round-robin, so each repo is spread out)."""
    n = max(1, min(n, len(ids))) if ids else 0
    shards = [[] for _ in range(n)]
    for i, x in enumerate(ids):
        shards[i % n].append(x)
    return shards


def matrix(tasks, repos=None, exclude=None, limit=0, shards=40):
    """GitHub Actions matrix: {"include": [{"shard": "00", "ids": "id1 id2 ..."}, ...]}."""
    if shards > 256:
        raise ValueError("GitHub Actions allows at most 256 jobs per matrix")
    ids = [t["instance_id"] for t in select(tasks, repos, exclude, limit)]
    parts = shard(ids, shards)
    width = len(str(max(len(parts) - 1, 0)))
    return {"include": [{"shard": str(k).zfill(width), "ids": " ".join(p)} for k, p in enumerate(parts)]}


def summary(m):
    ids = [i for row in m["include"] for i in row["ids"].split()]
    return "%d task(s) in %d shard(s)" % (len(ids), len(m["include"]))


def to_json(m):
    return json.dumps(m, separators=(",", ":"))
