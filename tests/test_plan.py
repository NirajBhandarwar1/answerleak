import json

import pytest

from answerleak import cli, plan


def tasks():
    out = []
    for repo, n in (("a/x", 5), ("b/y", 3), ("c/z", 2), ("d/w", 4)):
        out += [{"instance_id": "%s-%d" % (repo.split("/")[1], i), "repo": repo} for i in range(n)]
    return out


def test_select_excludes_and_filters():
    ids = [t["instance_id"] for t in plan.select(tasks(), exclude=["a/x", "d/w"])]
    assert ids == ["y-0", "y-1", "y-2", "z-0", "z-1"]
    assert len(plan.select(tasks(), repos=["b/y"])) == 3


def test_limit_spreads_across_repos():
    ids = [t["instance_id"] for t in plan.select(tasks(), limit=6)]
    assert ids == ["x-0", "y-0", "z-0", "w-0", "x-1", "y-1"]


def test_matrix_shards_cover_every_task_once():
    m = plan.matrix(tasks(), exclude=["d/w"], shards=4)
    all_ids = [i for row in m["include"] for i in row["ids"].split()]
    assert sorted(all_ids) == sorted(t["instance_id"] for t in tasks() if t["repo"] != "d/w")
    assert len(m["include"]) == 4 and [r["shard"] for r in m["include"]] == ["0", "1", "2", "3"]
    assert plan.summary(m) == "10 task(s) in 4 shard(s)"


def test_fewer_tasks_than_shards_and_limits():
    m = plan.matrix(tasks(), repos=["c/z"], shards=40)
    assert len(m["include"]) == 2
    assert plan.matrix(tasks(), repos=["none/none"])["include"] == []
    with pytest.raises(ValueError):
        plan.matrix(tasks(), shards=300)


def test_named_repo_wins_over_exclude():
    ids = [t["instance_id"] for t in plan.select(tasks(), repos=["b/y"], exclude=["a/x", "b/y"])]
    assert ids == ["y-0", "y-1", "y-2"]


def test_cli_plan_fails_clearly_when_nothing_selected(monkeypatch, tmp_path, capsys):
    from answerleak import dataset
    monkeypatch.setattr(dataset, "load_tasks", lambda **kw: tasks())
    gh = tmp_path / "gh_output"
    assert cli.main(["plan", "--repos", "no/such", "--github-output", str(gh)]) == 1
    assert "no tasks selected" in capsys.readouterr().err
    assert not gh.exists()


def test_cli_plan_writes_github_output(monkeypatch, tmp_path, capsys):
    from answerleak import dataset
    monkeypatch.setattr(dataset, "load_tasks", lambda **kw: tasks())
    gh = tmp_path / "gh_output"
    assert cli.main(["plan", "--exclude", "a/x", "--limit", "4", "--shards", "2", "--github-output", str(gh)]) == 0
    line = gh.read_text().strip()
    assert line.startswith("matrix=")
    m = json.loads(line[len("matrix="):])
    assert sum(len(r["ids"].split()) for r in m["include"]) == 4
    assert json.loads(capsys.readouterr().out) == m
