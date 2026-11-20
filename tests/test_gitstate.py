"""Two runners (say, laptop and cloud) share the bot memory through one repository."""
import json
import os
import subprocess

import pytest

from agent import gitstate


def sh(cwd, *args):
    subprocess.run(["git", *gitstate.IDENTITY, *args], cwd=cwd, check=True, capture_output=True)


def write_meta(root, **meta):
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    for name in gitstate.FILES:
        p = os.path.join(root, "state", name)
        data = meta if name == "meta.json" else ([] if name == "events.json" else {})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)


def read_meta(root):
    with open(os.path.join(root, "state", "meta.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_BRANCH", "main")
    monkeypatch.setenv("STATE_REMOTE", "origin")
    origin = tmp_path / "origin.git"
    sh(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    sh(tmp_path, "clone", "-q", str(origin), str(seed))
    sh(seed, "checkout", "-q", "-b", "main")
    write_meta(seed, saved_at="2026-11-19T13:00", last_run="2026-11-19T13:00")
    (seed / "code.py").write_text("v1\n")
    sh(seed, "add", ".")
    sh(seed, "commit", "-q", "-m", "seed")
    sh(seed, "push", "-q", "origin", "main")
    laptop, cloud = tmp_path / "laptop", tmp_path / "cloud"
    for r in (laptop, cloud):
        sh(tmp_path, "clone", "-q", str(origin), str(r))
    return origin, laptop, cloud


def test_refresh_takes_newer_memory_only(repos):
    origin, laptop, cloud = repos
    write_meta(cloud, saved_at="2026-11-20T04:10", last_run="2026-11-20T04:10")
    assert gitstate.save("cloud", root=str(cloud))
    assert gitstate.refresh(root=str(laptop))
    assert read_meta(laptop)["last_run"] == "2026-11-20T04:10"
    assert not gitstate.refresh(root=str(laptop))  # already the newest


def test_refresh_keeps_local_memory_when_it_is_newer(repos):
    origin, laptop, cloud = repos
    write_meta(laptop, saved_at="2026-11-20T13:00", last_run="2026-11-20T13:00")
    assert not gitstate.refresh(root=str(laptop))
    assert read_meta(laptop)["last_run"] == "2026-11-20T13:00"


def test_save_after_someone_else_pushed_keeps_our_memory_and_their_code(repos):
    origin, laptop, cloud = repos
    # cloud runs and saves memory; meanwhile a code change lands too
    write_meta(cloud, saved_at="2026-11-20T04:10", runner="cloud")
    (cloud / "code.py").write_text("v2\n")
    sh(cloud, "add", "code.py")
    sh(cloud, "commit", "-q", "-m", "code v2")
    assert gitstate.save("cloud", root=str(cloud))
    # the laptop has its own older clone, runs later and saves: conflict in state/ must resolve to ours
    write_meta(laptop, saved_at="2026-11-20T04:40", runner="pc")
    assert gitstate.save("pc", root=str(laptop))
    check = origin.parent / "check"
    sh(origin.parent, "clone", "-q", str(origin), str(check))
    assert read_meta(check)["runner"] == "pc"
    assert (check / "code.py").read_text() == "v2\n"


def test_save_without_changes_is_fine(repos):
    origin, laptop, cloud = repos
    assert gitstate.save("pc", root=str(laptop))
