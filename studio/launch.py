"""Open PeakCut like an app: start it if it isn't running, then show it in its own window.

This is what the PeakCut shortcut runs (Settings, App). `python -m studio.launch` does the same by hand."""
import os
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

import httpx

# the folders the icon was made with must be set before the settings are read
for _flag, _name in (("--data", "PEAKCUT_DATA"), ("--models", "PEAKCUT_MODELS")):
    if __name__ == "__main__" and _flag in sys.argv[1:-1]:
        os.environ[_name] = sys.argv[sys.argv.index(_flag) + 1]

from . import config  # noqa: E402


def _address(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _running(port: int) -> bool:
    try:
        return httpx.get(_address(port) + "/healthz", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def _python() -> str:
    """python.exe rather than pythonw.exe: the server writes messages, and pythonw has nowhere to put them."""
    p = Path(sys.executable)
    plain = p.with_name("python.exe")
    return str(plain if p.name.lower() == "pythonw.exe" and plain.is_file() else p)


def _start(port: int):
    env = {**os.environ, "PEAKCUT_PORT": str(port), "PYTHONIOENCODING": "utf-8"}
    log = open(config.DATA_DIR / "peakcut.log", "ab")  # noqa: SIM115 - handed to the server, which keeps it open
    how = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    subprocess.Popen([_python(), "-m", "studio.server"], cwd=config.ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **how)


def _window(url: str):
    """Its own window without tabs or an address bar, when Chrome or Edge is there; otherwise the usual browser."""
    if os.name == "nt":
        roots = [os.environ.get(k) for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        found = [Path(r) / p for p in ("Google/Chrome/Application/chrome.exe", "Microsoft/Edge/Application/msedge.exe") for r in roots if r]
    elif sys.platform == "darwin":
        found = [Path(f"/Applications/{n}.app/Contents/MacOS/{n}") for n in ("Google Chrome", "Microsoft Edge")]
    else:
        found = [Path(p) for p in map(shutil.which, ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")) if p]
    program = next((p for p in found if p.is_file()), None)
    if program:
        subprocess.Popen([str(program), f"--app={url}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        webbrowser.open(url)


def _say(text: str):
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "PeakCut", 0x10)
    else:
        print(text)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[argv.index("--port") + 1]) if "--port" in argv else config.PORT
    if not _running(port):
        _start(port)
        until = time.time() + 120
        while not _running(port):
            if time.time() > until:
                return _say(f"PeakCut didn't start. What went wrong is written in:\n{config.DATA_DIR / 'peakcut.log'}")
            time.sleep(1)
    _window(_address(port) + "/app")


if __name__ == "__main__":
    main()
