"""Put a PeakCut icon on the desktop (and in the Start menu), so it opens like any app."""
import os
import subprocess
import sys
from pathlib import Path

from . import config

ICON = config.ROOT / "web" / "brand" / "peakcut.ico"

# Everything is handed to PowerShell through environment variables, so no folder name is ever read as a command.
_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
$shell = New-Object -ComObject WScript.Shell
$made = @()
foreach ($folder in $env:PC_FOLDERS.Split('|')) {
  if (-not $folder) { $folder = [Environment]::GetFolderPath('Desktop'); $menu = [Environment]::GetFolderPath('Programs'); $list = @($folder, $menu) } else { $list = @($folder) }
  foreach ($f in $list) {
    $path = Join-Path $f 'PeakCut.lnk'
    $link = $shell.CreateShortcut($path)
    $link.TargetPath = $env:PC_TARGET
    $link.Arguments = $env:PC_ARGS
    $link.WorkingDirectory = $env:PC_ROOT
    $link.IconLocation = $env:PC_ICON
    $link.Description = 'PeakCut'
    $link.Save()
    $made += $path
  }
}
$made -join "`n"
"""


def _target() -> str:
    p = Path(sys.executable)
    quiet = p.with_name("pythonw.exe")             # no black window when the icon is clicked
    return str(quiet if quiet.is_file() else p)


def _arguments(port: int) -> list:
    """What the icon runs. A data or models folder given only to this run (not written in .env or storage.json) is
    written into the icon, or it would open an empty PeakCut with none of the videos or keys."""
    args = ["-m", "studio.launch", "--port", str(int(port))]
    try:
        written = (config.ROOT / ".env").read_text(encoding="utf-8")
    except OSError:
        written = ""
    for name, flag in (("PEAKCUT_DATA", "--data"), ("PEAKCUT_MODELS", "--models")):
        value = os.environ.get(name, "")
        if value and name not in written:
            args += [flag, str(Path(value).resolve())]
    return args


def create(port: int, folder: str = "") -> list:
    """Make the icon; returns where it was put. `folder` is only for testing."""
    parts = _arguments(port)
    args = subprocess.list2cmdline(parts)
    if os.name == "nt":
        env = {**os.environ, "PC_FOLDERS": folder, "PC_TARGET": _target(), "PC_ARGS": args, "PC_ROOT": str(config.ROOT), "PC_ICON": str(ICON)}
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS], env=env, capture_output=True,
                           text=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("Windows didn't let PeakCut make the icon.")
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]
    import shlex
    run = f"cd {shlex.quote(str(config.ROOT))} && exec {shlex.quote(sys.executable)} {shlex.join(parts)}"
    if sys.platform == "darwin":
        out = Path(folder or Path.home() / "Desktop") / "PeakCut.command"
        out.write_text(f"#!/bin/sh\n{run}\n", encoding="utf-8")
    else:
        out = Path(folder or Path.home() / ".local/share/applications") / "peakcut.desktop"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("[Desktop Entry]\nType=Application\nName=PeakCut\n"
                       f"Exec=sh -c {shlex.quote(run)}\nIcon={config.ROOT / 'web/brand/favicon.svg'}\nTerminal=false\n", encoding="utf-8")
    out.chmod(0o755)
    return [str(out)]
