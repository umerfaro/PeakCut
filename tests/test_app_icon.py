"""The app icon and the "updated" message. The icon is made in a throwaway folder, not on the real desktop."""
import os, sys, tempfile
from pathlib import Path

os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-app-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="0", PEAKCUT_PUBLIC_URL="http://127.0.0.1:8765", PEAKCUT_UPDATE_CHECK="0")
sys.path.insert(0, ".")
from fastapi.testclient import TestClient

from studio import db as database, server, shortcut, updates

database.init()
fails = []


def check(n, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)


folder = tempfile.mkdtemp(prefix="pc app icon ")
made = shortcut.create(8765, folder)
check("an icon is made", (len(made), Path(made[0]).is_file()), (1, True))
check("the icon opens the same data folder", ("--data" in shortcut._arguments(8765), str(Path(os.environ["PEAKCUT_DATA"]).resolve()) in shortcut._arguments(8765)), (True, True))
check("the icon picture is there", shortcut.ICON.is_file(), True)
H = {"Origin": "http://127.0.0.1:8765"}
away = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("203.0.113.9", 5000))
check("another computer can't make icons or close PeakCut",
      [away.post(p, headers=H).status_code in (403, 409) for p in ("/api/app/shortcut", "/api/app/quit")], [True, True])
here = TestClient(server.app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 5000))
check("a website can't close PeakCut", here.post("/api/app/quit", headers={"Origin": "https://evil.example"}).status_code, 403)

updates.RUNNING = "a" * 40
updates.note_version(used_before=False)
check("a first install says nothing", updates.updated(), None)
updates.RUNNING = "b" * 40
updates.note_version(used_before=True)
check("a new version is announced", (updates.updated() or {}).get("version"), "b" * 7)
updates.note_version(used_before=True)
check("and stays announced after another start", bool(updates.updated()), True)

print("ALL PASSED" if not fails else f"{len(fails)} FAILED")
sys.exit(1 if fails else 0)
