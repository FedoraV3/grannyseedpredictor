"""
updater.py

Launcher with a built-in auto-updater. run.bat and run_debug.bat start this
instead of gui.py.

On every launch it asks GitHub for the newest commit on `main`. If that is not
the commit installed here, it downloads the new code, puts it in place of the
old code, and restarts so the new code is what runs. Then it opens gui.py.
Any failure (offline, GitHub down, git missing, ...) is written to
logs/updater.log and the program starts as it is -- the updater never stops
the tool from opening.

Two kinds of install are handled:

* A plain copy (downloaded ZIP, copied folder -- no .git): the repo is cloned
  into a temporary folder (or downloaded as a ZIP when git isn't installed)
  and its files replace the installed ones. Files the previous version
  shipped but the new one doesn't are deleted. Anything the repo never
  shipped -- dumps/, logs/, backups/, ... -- is left alone.
  `.update_state.json` records which commit and files are installed.

* A git checkout (has .git): the new commit is fetched and the checkout is
  fast-forwarded to it. This is only done when the checkout is on `main`,
  has no uncommitted changes to tracked files, and has no commits that
  aren't on GitHub -- so a developer's work is never thrown away.

Set GSP_NO_UPDATE=1 or pass --no-update to skip the check.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path

OWNER_REPO = "FedoraV3/grannyseedpredictor"
BRANCH = "main"
REPO_URL = f"https://github.com/{OWNER_REPO}.git"
API_URL = f"https://api.github.com/repos/{OWNER_REPO}/commits/{BRANCH}"
ZIP_URL = f"https://codeload.github.com/{OWNER_REPO}/zip/{{sha}}"

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / ".update_state.json"
CHECK_TIMEOUT = 10      # seconds for "is there a new commit?"
DOWNLOAD_TIMEOUT = 180  # seconds for fetching the new code

log = logging.getLogger("updater")

# No console windows flashing up when launched through pythonw.exe, and never
# let git sit waiting for a credential prompt nobody can see.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
_HTTP_HEADERS = {"User-Agent": "grannyseedpredictor-updater"}


def _setup_logging() -> None:
    log.setLevel(logging.INFO)
    try:
        (HERE / "logs").mkdir(exist_ok=True)
        fh = logging.FileHandler(HERE / "logs" / "updater.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(fh)
    except OSError:
        pass
    # Under pythonw.exe there is no console and sys.stderr is None.
    if sys.stderr is not None:
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("[updater] %(message)s"))
        log.addHandler(sh)


def _find_git() -> str | None:
    git = shutil.which("git")
    if git:
        return git
    for base in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            for cand in (Path(base) / "Git" / "cmd" / "git.exe",
                         Path(base) / "Programs" / "Git" / "cmd" / "git.exe"):
                if cand.is_file():
                    return str(cand)
    return None


def _git(git: str, *args: str, cwd: Path | None = None, timeout: float = DOWNLOAD_TIMEOUT) -> str:
    r = subprocess.run(
        [git, *args], cwd=cwd or HERE, env=_GIT_ENV, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _http_get(url: str, timeout: float, accept: str | None = None) -> bytes:
    headers = dict(_HTTP_HEADERS)
    if accept:
        headers["Accept"] = accept
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
        return r.read()


def _remote_commit(git: str | None) -> str:
    """The commit `main` points to on GitHub."""
    if git:
        out = _git(git, "ls-remote", REPO_URL, f"refs/heads/{BRANCH}", timeout=CHECK_TIMEOUT)
        if not out:
            raise RuntimeError(f"branch {BRANCH!r} not found at {REPO_URL}")
        return out.split()[0]
    return _http_get(API_URL, CHECK_TIMEOUT, accept="application/vnd.github.sha").decode().strip()


# --------------------------------------------------------------------------
# git checkout
# --------------------------------------------------------------------------

def _plan_git_checkout(git: str):
    local = _git(git, "rev-parse", "HEAD")
    remote = _remote_commit(git)
    if local == remote:
        log.info("Up to date (%s).", local[:7])
        return None
    branch = _git(git, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != BRANCH:
        log.info("Update %s available, but the checkout is on %r, not %r; skipping.",
                 remote[:7], branch, BRANCH)
        return None
    if _git(git, "status", "--porcelain", "--untracked-files=no"):
        log.info("Update %s available, but tracked files have uncommitted changes; skipping.",
                 remote[:7])
        return None

    def install() -> bool:
        # Also refresh origin/main when origin is this repo, so `git status`
        # doesn't report the updated branch as "ahead of origin".
        refspec = BRANCH
        try:
            if OWNER_REPO.lower() in _git(git, "remote", "get-url", "origin").lower():
                refspec = f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}"
        except RuntimeError:
            pass
        _git(git, "fetch", "--quiet", REPO_URL, refspec)
        try:
            _git(git, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD")
        except RuntimeError:
            log.info("This checkout has commits that are not on GitHub; skipping update.")
            return False
        _git(git, "merge", "--ff-only", "--quiet", "FETCH_HEAD")
        log.info("Updated %s -> %s.", local[:7], _git(git, "rev-parse", "HEAD")[:7])
        return True

    log.info("Update available: %s -> %s.", local[:7], remote[:7])
    return install


# --------------------------------------------------------------------------
# plain copy
# --------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _download(git: str | None, sha: str, tmp: Path) -> tuple[Path, str]:
    """Put the newest code in `tmp`; return (its folder, its commit)."""
    if git:
        dest = tmp / "repo"
        _git(git, "clone", "--quiet", "--depth", "1", "--branch", BRANCH, REPO_URL, str(dest), cwd=tmp)
        return dest, _git(git, "rev-parse", "HEAD", cwd=dest)
    with zipfile.ZipFile(io.BytesIO(_http_get(ZIP_URL.format(sha=sha), DOWNLOAD_TIMEOUT))) as z:
        z.extractall(tmp)
    # GitHub's archive holds a single "<repo>-<sha>/" folder.
    (top,) = [p for p in tmp.iterdir() if p.is_dir()]
    return top, sha


def _rmtree(path: Path) -> None:
    # git marks its object files read-only, which Windows refuses to delete.
    def onerror(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, onerror=onerror)


def _remove_stale(rel: str) -> None:
    path = HERE / rel
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    # Drop folders the old version created that are now empty.
    parent = path.parent
    while parent != HERE:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def _plan_copy(git: str | None):
    state = _load_state()
    local = state.get("commit")
    remote = _remote_commit(git)
    if local == remote:
        log.info("Up to date (%s).", local[:7])
        return None

    def install() -> bool:
        tmp = Path(tempfile.mkdtemp(prefix="gsp-update-"))
        try:
            src, commit = _download(git, remote, tmp)
            files = sorted(
                p.relative_to(src).as_posix() for p in src.rglob("*")
                if p.is_file() and ".git" not in p.relative_to(src).parts
            )
            for rel in files:
                dst = HERE / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                part = dst.with_name(dst.name + ".update-part")
                shutil.copy2(src / rel, part)
                os.replace(part, dst)
            for rel in set(state.get("files", ())) - set(files):
                _remove_stale(rel)
        finally:
            _rmtree(tmp)
        STATE_FILE.write_text(json.dumps({"commit": commit, "files": files}, indent=1), encoding="utf-8")
        log.info("Updated %s -> %s.", (local or "unknown")[:7], commit[:7])
        return True

    log.info("Update available: %s -> %s.", (local or "unknown")[:7], remote[:7])
    return install


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------

def _read_requirements() -> bytes:
    try:
        return (HERE / "requirements.txt").read_bytes()
    except OSError:
        return b""


def _install_requirements() -> None:
    """Best effort, one package at a time -- like install.bat, a failing
    optional package (pyopencl) must not block the rest."""
    for line in _read_requirements().decode("utf-8", "replace").splitlines():
        req = line.split("#", 1)[0].strip()
        if not req:
            continue
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "--quiet", "--no-warn-script-location", req],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, creationflags=_NO_WINDOW,
        )
        log.info("pip install %s: %s", req, "ok" if r.returncode == 0 else r.stderr.strip()[-500:])


def check_for_update():
    """Return a function that installs the newest version (True if it did),
    or None when already up to date or this install can't be updated."""
    git = _find_git()
    if (HERE / ".git").exists():
        if not git:
            log.info("This is a git checkout but git was not found; skipping update.")
            return None
        install = _plan_git_checkout(git)
    else:
        install = _plan_copy(git)
    if install is None:
        return None

    def install_with_requirements() -> bool:
        before = _read_requirements()
        if not install():
            return False
        if _read_requirements() != before:
            log.info("requirements.txt changed; installing packages.")
            _install_requirements()
        return True

    return install_with_requirements


def _run_with_window(work) -> bool:
    """Run work() on a thread, showing a small "Updating..." window meanwhile
    (with pythonw.exe there is otherwise no sign anything is happening)."""
    result = {"ok": False}

    def worker():
        try:
            result["ok"] = work()
        except Exception:
            log.exception("Update failed; starting the current version.")

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    try:
        import tkinter as tk
        root = tk.Tk()
        root.title("Granny Legacy Seed Predictor")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", lambda: None)
        tk.Label(root, text="Updating to the newest version, please wait...",
                 padx=40, pady=25).pack()

        def poll():
            if t.is_alive():
                root.after(100, poll)
            else:
                root.destroy()
        poll()
        root.mainloop()
    except Exception:
        pass  # no display / no tkinter: just wait below
    t.join()
    return result["ok"]


def main() -> int:
    _setup_logging()
    if "--no-update" not in sys.argv and not os.environ.get("GSP_NO_UPDATE"):
        try:
            install = check_for_update()
        except Exception as e:
            log.info("Update check failed (%s); starting the current version.", e)
            install = None
        if install is not None and _run_with_window(install):
            # Rerun through the *new* updater.py, so any change to how the
            # program is launched takes effect straight away.
            log.info("Restarting into the new version.")
            return subprocess.call([sys.executable, str(HERE / "updater.py"), "--no-update"], cwd=HERE)
    return subprocess.call([sys.executable, str(HERE / "gui.py")], cwd=HERE)


if __name__ == "__main__":
    sys.exit(main())
