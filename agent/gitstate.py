"""Bot memory (state/) shared through the git repository.

Several runners can run the bot: the cloud task, the laptop (Windows Task Scheduler), GitLab CI.
Each of them may have an old copy of state/, so:
  refresh()  — before a run: if the repository has a newer memory than the local one, take it;
  save()     — after a run: commit state/ and push; if someone pushed meanwhile, put our memory
               on top of their commits (ours is the newest: we have just run) and push again.

Settings (environment variables, all optional):
  STATE_BRANCH  branch that holds the memory (default main)
  STATE_REMOTE  remote name or URL to fetch/push (default origin; GitLab CI passes a URL with its token)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("meta.json", "events.json", "seen.json")
IDENTITY = ["-c", "user.name=opportunity-agent", "-c", "user.email=opportunity-agent@users.noreply.github.com"]
ALMATY = timezone(timedelta(hours=5))


def _branch():
    return os.environ.get("STATE_BRANCH", "main")


def _remote():
    return os.environ.get("STATE_REMOTE", "origin")


def git(*args, check=True, timeout=120, cwd=None):
    r = subprocess.run(["git", *IDENTITY, *args], cwd=cwd or ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout,
                       env={**os.environ, "GIT_EDITOR": "true", "GIT_TERMINAL_PROMPT": "0"})
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
    return r


def is_repo(root=None):
    return os.path.isdir(os.path.join(root or ROOT, ".git"))


def freshness(meta):
    """When this copy of the memory was last written (older copies have no saved_at)."""
    return meta.get("saved_at") or meta.get("last_run") or ""


def refresh(root=None):
    """Take state/ from the repository if it is newer than ours. Never fails the run."""
    root = root or ROOT
    if not is_repo(root):
        return False
    try:
        git("fetch", "-q", _remote(), _branch(), timeout=90, cwd=root)
        remote_meta = json.loads(git("show", "FETCH_HEAD:state/meta.json", cwd=root).stdout)
    except Exception as e:
        print(f"  ! could not check memory in the repository: {e}")
        return False
    state_dir = os.path.join(root, "state")
    try:
        with open(os.path.join(state_dir, "meta.json"), encoding="utf-8") as f:
            local_meta = json.load(f)
    except Exception:
        local_meta = {}
    if freshness(remote_meta) <= freshness(local_meta):
        return False
    os.makedirs(state_dir, exist_ok=True)
    for name in FILES:
        r = git("show", f"FETCH_HEAD:state/{name}", check=False, cwd=root)
        if r.returncode == 0:
            with open(os.path.join(state_dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(r.stdout)
    print(f"  memory: took the newer copy from the repository ({freshness(remote_meta)[:16]})")
    return True


def _read_state(root):
    out = {}
    for name in FILES:
        p = os.path.join(root, "state", name)
        if os.path.exists(p):
            with open(p, "rb") as f:
                out[name] = f.read()
    return out


def _write_state(root, saved):
    for name, data in saved.items():
        with open(os.path.join(root, "state", name), "wb") as f:
            f.write(data)


def _commit_state(root, message):
    git("add", "state/", cwd=root)
    if git("diff", "--cached", "--quiet", check=False, cwd=root).returncode != 0:
        git("commit", "-q", "-m", message, cwd=root)
        return True
    return False


def _rebase_keeping_ours(root, saved):
    """Rebase our commits onto FETCH_HEAD; any conflict in state/ is solved with our memory."""
    r = git("rebase", "--autostash", "FETCH_HEAD", check=False, cwd=root)
    for _ in range(50):
        if r.returncode == 0:
            return True
        conflicted = git("diff", "--name-only", "--diff-filter=U", check=False, cwd=root).stdout.split()
        if not conflicted or any(not f.startswith("state/") for f in conflicted):
            git("rebase", "--abort", check=False, cwd=root)
            print(f"  ! rebase stopped on: {conflicted or r.stderr.strip()[:200]}")
            return False
        _write_state(root, saved)
        git("add", "state/", cwd=root)
        if git("diff", "--cached", "--quiet", check=False, cwd=root).returncode == 0:
            r = git("rebase", "--skip", check=False, cwd=root)
        else:
            r = git("rebase", "--continue", check=False, cwd=root)
    git("rebase", "--abort", check=False, cwd=root)
    return False


def save(runner="run", root=None, attempts=3):
    """Commit state/ and push it. Returns True when the memory is in the repository."""
    root = root or ROOT
    if not is_repo(root):
        print("  ! not a git repository: memory stays only here")
        return False
    saved = _read_state(root)
    stamp = datetime.now(ALMATY).strftime("%Y-%m-%d %H:%M")
    _commit_state(root, f"state: {runner} run {stamp}")
    for attempt in range(1, attempts + 1):
        r = git("push", "-q", _remote(), f"HEAD:{_branch()}", check=False, timeout=120, cwd=root)
        if r.returncode == 0:
            print(f"  memory saved to the repository ({runner})")
            return True
        print(f"  ! push attempt {attempt}: {r.stderr.strip()[:200]}")
        try:
            git("fetch", "-q", _remote(), _branch(), timeout=90, cwd=root)
        except Exception as e:
            print(f"  ! fetch: {e}")
            continue
        if not _rebase_keeping_ours(root, saved):
            break
        _write_state(root, saved)
        _commit_state(root, f"state: {runner} run {stamp} (on top of newer commits)")
    return False


def main():
    runner = sys.argv[1] if len(sys.argv) > 1 else "run"
    ok = save(runner)
    if not ok:
        from .main import notify_failure, load_env_file
        load_env_file()
        notify_failure(store.load("meta.json", {}),
                       "память бота не сохранилась в репозиторий. Следующий запуск может повторить "
                       "уже присланное. Я (Claude) посмотрю, в чём дело.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
