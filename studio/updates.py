"""Is there a newer PeakCut? Compares the version running here with the latest on GitHub, once a minute.

The version here is the git commit this folder was on when PeakCut started (read straight from .git, no git
program needed). Each minute, in the background, PeakCut asks GitHub which commit the project is on now: one small
request to the same address `git` itself uses, which has no hourly allowance to run out of. Only when that commit
changes does it ask GitHub's API what was added, to show the list. Nothing about this computer or its users is
sent. The owner sees a notice in the dashboard within a minute or two, with no restart.

    PEAKCUT_UPDATE_REPO     where to look (default: the project's own repository; a fork points at itself)
    PEAKCUT_UPDATE_BRANCH   which branch (default: main)
    PEAKCUT_UPDATE_CHECK=0  never look

The owner can also update from the dashboard (**Update now**), or switch on **Update automatically** (off by default).
Updating is `git pull`, then the requirements if they changed, then PeakCut starts itself again. It only happens while
nothing is recording or making clips, and if the new version can't start, the old one is put back."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from . import config

REPO = (config.env("PEAKCUT_UPDATE_REPO") or "haziqahmedqasmi/peakcut").strip().strip("/")
BRANCH = (config.env("PEAKCUT_UPDATE_BRANCH") or "main").strip()
ON = config.env("PEAKCUT_UPDATE_CHECK", "1") != "0" and bool(re.fullmatch(r"[\w.\-]+/[\w.\-]+", REPO))
EVERY = 60
UA = {"User-Agent": "PeakCut-update-check"}
_state = {"checked": 0.0, "result": None, "seen": "", "notes": None}
_lock = threading.Lock()
_started = False


def current() -> str:
    """The commit this folder is on now, or "" when it was not installed with git (a downloaded zip, for example)."""
    g = config.ROOT / ".git"
    try:
        if g.is_file():                      # a worktree or submodule: .git points somewhere else
            g = (config.ROOT / g.read_text(encoding="utf-8").split(":", 1)[1].strip()).resolve()
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head if re.fullmatch(r"[0-9a-f]{40}", head) else ""
        ref = head.split(":", 1)[1].strip()
        places = [g]
        if (g / "commondir").is_file():      # a worktree keeps its branches in the main repository's folder
            places.append((g / (g / "commondir").read_text(encoding="utf-8").strip()).resolve())
        for d in places:
            f = d / ref
            if f.is_file():
                return f.read_text(encoding="utf-8").strip()
            packed = d / "packed-refs"
            if packed.is_file():
                for line in packed.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0]
    except (OSError, IndexError):
        pass
    return ""


RUNNING = current()       # the code in memory: what PeakCut is actually running until it is restarted


def newest() -> str:
    """The commit the project is on right now, from the address git itself asks. "" if it cannot be told."""
    r = httpx.get(f"https://github.com/{REPO}.git/info/refs", params={"service": "git-upload-pack"}, headers=UA, timeout=15)
    if r.status_code != 200:
        return ""                            # a private repository, or GitHub is having a moment
    m = re.search(rb"([0-9a-f]{40}) refs/heads/" + re.escape(BRANCH).encode() + rb"[\x00\n]", r.content)
    return m.group(1).decode() if m else ""


def _what_changed(here: str):
    """(how many commits newer, their first lines, newest first), or None if GitHub's API will not say right now."""
    r = httpx.get(f"https://api.github.com/repos/{REPO}/compare/{here}...{BRANCH}", timeout=20,
                  headers={"Accept": "application/vnd.github+json", **UA})
    if r.status_code != 200:
        return None
    d = r.json()
    notes = []
    for c in reversed(d.get("commits") or []):
        msg = str((c.get("commit") or {}).get("message") or "").strip().splitlines()
        notes.append({"sha": str(c.get("sha") or "")[:7], "title": (msg[0] if msg else "")[:140],
                      "date": str(((c.get("commit") or {}).get("committer") or {}).get("date") or "")[:10]})
    return int(d.get("ahead_by") or 0), notes[:20]


def check(force: bool = False):
    """Look once. Returns what the dashboard should say, or None when this copy is up to date (or it cannot be told)."""
    if not ON:
        return None
    with _lock:
        now = time.time()
        if not force and now - _state["checked"] < EVERY:
            return _state["result"]
        _state["checked"] = now
        here, on_disk = RUNNING, current()
        try:
            top = newest() if here else ""
        except Exception:  # noqa: BLE001 - offline: what was known stays, and it is asked again in a minute
            return _state["result"]
        if not top or top == here:
            _state.update(result=None, seen=top)
            return None
        if top != _state["seen"] or _state["notes"] is None:      # a commit not seen before: ask what it brought, once
            _state["seen"] = top
            try:
                _state["notes"] = _what_changed(here)
            except Exception:  # noqa: BLE001
                _state["notes"] = None
        got = _state["notes"]
        if got and got[0] == 0:
            _state["result"] = None          # this copy has its own newer commits: nothing to fetch
            return None
        _state["result"] = {"current": here[:7], "latest": top[:7], "behind": got[0] if got else None,
                            "notes": got[1] if got else [], "url": f"https://github.com/{REPO}/commits/{BRANCH}",
                            # the new files are already in the folder (git pull was run): only a restart is left
                            "restart": on_disk == top}
        return _state["result"]


def latest():
    """For the pages: what the last look found. Never waits on GitHub."""
    return _state["result"] if ON else None


# ---------------------------------------------------------------- updating from the dashboard
_inst = {"doing": "", "error": "", "failed": ""}    # what the update is doing now, why the last one failed, and which one
_idle = lambda: True                                 # noqa: E731 - set by the server: is nothing recording or being made?
_inst_lock = threading.Lock()


def _settings_file():
    return config.DATA_DIR / "update.json"


def auto_on() -> bool:
    try:
        return bool(json.loads(_settings_file().read_text(encoding="utf-8")).get("auto"))
    except (OSError, ValueError, AttributeError):
        return False


def set_auto(on: bool):
    _settings_file().write_text(json.dumps({"auto": bool(on)}), encoding="utf-8")
    if on:
        threading.Thread(target=_act_now, daemon=True).start()      # an update already waiting is installed now


def _act_now():
    try:
        found = check(force=True)
        if found and not _inst["doing"] and can_install()[0]:
            install()
    except Exception:  # noqa: BLE001 - the once-a-minute check tries again
        pass


# ---- "PeakCut was updated": the version that last ran is remembered, so a new one is noticed however it got here
_updated = {}


def note_version(used_before: bool):
    """Called once at start. `used_before`: this copy already has someone's work, so no record means it was updated
    from a version that kept none."""
    if not RUNNING:
        return
    f = config.DATA_DIR / "version.json"
    try:
        had = json.loads(f.read_text(encoding="utf-8"))
        had = had if isinstance(had, dict) else {}
    except (OSError, ValueError):
        had = {}
    if had.get("version") != RUNNING:
        had = {"version": RUNNING, "updated": time.time() if had.get("version") or used_before else 0}
        try:
            f.write_text(json.dumps(had), encoding="utf-8")
        except OSError:
            pass
    _updated.clear()
    if had.get("updated") and time.time() - had["updated"] < 7 * 86400:
        _updated.update(version=RUNNING[:7], at=had["updated"])


def updated():
    return dict(_updated) or None


def _git(*args, timeout=300) -> str:
    r = subprocess.run(["git", *args], cwd=config.ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "git failed")
    return r.stdout.strip()


def can_install():
    """(can this copy update itself, and if not, why in plain words)."""
    if not ON:
        return False, "Update checks are switched off on this copy."
    if Path("/.dockerenv").exists():
        return False, "This copy runs in Docker. Update it with: git pull && docker compose up -d --build"
    if not current() or not shutil.which("git"):
        return False, "This copy wasn't installed with git, so it can't update itself. Download the new version from GitHub."
    try:
        if _git("rev-parse", "--abbrev-ref", "HEAD", timeout=30) != BRANCH:
            return False, f"This folder is on another branch than {BRANCH}, so it is left alone."
        if _git("status", "--porcelain", "--untracked-files=no", timeout=60):
            return False, "Files in the PeakCut folder were changed by hand, so updating by itself could lose them."
    except (RuntimeError, OSError, subprocess.SubprocessError):
        return False, "git didn't answer in the PeakCut folder."
    return True, ""


STEPS = (("Getting ready", 8), ("Waiting", 12), ("Downloading", 30), ("Installing", 55), ("Checking", 75), ("Restarting", 90))


def state() -> dict:
    ok, why = can_install()
    return {"can": ok, "why": why, "auto": auto_on(), "doing": _inst["doing"], "error": _inst["error"],
            "percent": next((n for start, n in STEPS if _inst["doing"].startswith(start)), 0),
            "version": (RUNNING or "")[:7]}


def _starts_ok():
    """Does the new code at least load? Tried in a separate process with an empty, throwaway data folder."""
    with tempfile.TemporaryDirectory(prefix="pc-update-", ignore_cleanup_errors=True) as d:
        r = subprocess.run([sys.executable, "-c", "import studio.server"], cwd=config.ROOT, capture_output=True, text=True,
                           timeout=180, env={**os.environ, "PEAKCUT_DATA": d, "PEAKCUT_UPDATE_CHECK": "0", "DATABASE_URL": ""})
    if r.returncode != 0:
        raise RuntimeError("The new version did not start, so the one you had was kept.")


def _download():
    """Fetch the new version and what it needs. Puts the old one back if anything fails. True when something changed."""
    old = _git("rev-parse", "HEAD", timeout=30)
    _inst["doing"] = "Downloading the new version"
    _git("pull", "--ff-only", "--quiet")
    new = _git("rev-parse", "HEAD", timeout=30)
    if new == old:
        return new != RUNNING                       # already downloaded earlier: only a restart is left
    try:
        if _git("diff", "--name-only", old, new, "--", "requirements.txt", timeout=60):
            _inst["doing"] = "Installing what it needs"
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], cwd=config.ROOT,
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0:
                raise RuntimeError("Installing the requirements failed: " + (r.stderr or "").strip()[-200:])
        _inst["doing"] = "Checking the new version"
        _starts_ok()
    except Exception:
        _git("reset", "--keep", old)                # back to the version that works; keeps anything changed by hand
        raise
    return True


def _restart():
    """Start PeakCut again, with the same Python, and end this one."""
    if config.env("PEAKCUT_AUTO_RELOAD") == "1":
        return                                      # it restarts itself when the files change
    args = [sys.executable, "-m", "studio.server"]
    env = {**os.environ, "PEAKCUT_RESTARTING": "1"}
    if os.name != "nt":
        os.chdir(config.ROOT)
        os.execve(sys.executable, args, env)
    import ctypes
    seen = bool(ctypes.windll.kernel32.GetConsoleWindow()) and bool(sys.stdout and sys.stdout.isatty())
    subprocess.Popen(args, cwd=config.ROOT, env=env,
                     creationflags=subprocess.CREATE_NEW_CONSOLE if seen else subprocess.CREATE_NO_WINDOW)
    os._exit(0)


def install():
    """Update now. One at a time; waits for recordings and videos to finish before restarting."""
    if not _inst_lock.acquire(blocking=False):
        return
    target = (_state["result"] or {}).get("latest", "")
    try:
        _inst.update(error="", doing="Getting ready")
        ok, why = can_install()
        if not ok:
            raise RuntimeError(why)
        while not _idle():
            _inst["doing"] = "Waiting for recordings and videos to finish"
            time.sleep(15)
        if _download():
            _inst["doing"] = "Restarting PeakCut"
            while not _idle():
                time.sleep(10)
            time.sleep(2)
            _restart()
        _inst["doing"] = ""
    except Exception as e:  # noqa: BLE001
        _inst.update(doing="", error=str(e)[:300], failed=target)
    finally:
        _inst_lock.release()


def install_soon():
    _inst.update(error="", doing="Getting ready")
    threading.Thread(target=install, daemon=True).start()


def start(idle=None):
    """Look every minute, in the background, for as long as PeakCut runs; update by itself if the owner asked for that."""
    global _started, _idle
    if idle:
        _idle = idle
    if not ON or _started:
        return
    _started = True

    def loop():
        while True:
            try:
                found = check()
                if found and auto_on() and found["latest"] != _inst["failed"] and not _inst["doing"] and _idle() and can_install()[0]:
                    install()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(EVERY)
    threading.Thread(target=loop, daemon=True).start()
