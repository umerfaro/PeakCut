"""Instagram and Facebook post through Composio. Composio is replaced by a stand-in, so nothing leaves this machine."""
import json, os, sys, tempfile
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-composio-")
os.environ.update(DATABASE_URL="", PEAKCUT_ACCOUNTS="1", PEAKCUT_PUBLIC_URL="https://clips.example.com", PEAKCUT_VERIFY_EMAIL="0",
                  PEAKCUT_COMPOSIO_API_KEY="", PEAKCUT_COMPOSIO_API_KEY_FILE="",
                  GOOGLE_CLIENT_ID="", GOOGLE_CLIENT_SECRET="", GOOGLE_CLIENT_FILE="")
sys.path.insert(0, ".")
from pathlib import Path
from fastapi.testclient import TestClient
from studio import accounts, auth, composio_post, config, creators, ctx, db, jobs, posting, server
db.init()
fails = []
def check(n, got, want=True):
    ok = want(got) if callable(want) else got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f"   got {got!r}")); ok or fails.append(n)

BASE = "https://clips.example.com"
ORIGIN = {"Origin": BASE}
KEY = "composio-test-key-value"
def person(email, admin=False):
    u, _ = auth._create(email, email.split("@")[0], password_hash="x")
    if admin:
        import sqlalchemy as sa
        with db.engine.begin() as c:
            c.execute(db.users.update().where(db.users.c.id == u.id).values(is_admin=True))
    cl = TestClient(server.app, base_url=BASE, follow_redirects=False)
    cl.cookies.set(auth.COOKIE, auth.start_session(u.id))
    return u, cl
boss, a = person("boss@example.com", admin=True)
user, b = person("user@example.com")

check("nothing is connected before a key", a.get("/api/status").json()["composio"], False)
check("a normal user cannot save a key", b.put("/api/composio", json={"key": KEY}, headers=ORIGIN).status_code, 404)
check("a short key is refused", a.put("/api/composio", json={"key": "short"}, headers=ORIGIN).status_code, 400)
r = a.put("/api/composio", json={"key": KEY}, headers=ORIGIN)
check("the key is saved", (r.status_code, r.json()), (200, {"ready": True, "from_env": False}))
check("the key is never sent back", KEY in a.get("/api/composio").text, False)
check("the person who runs the server can connect", a.get("/api/status").json()["connect"]["instagram"], True)
check("everyone else still cannot use that key", b.get("/api/status").json()["connect"]["facebook"], False)

class R:
    def __init__(self, code, data=None, text=None):
        self.status_code, self._d = code, data
        self.text = text if text is not None else json.dumps(data or {})
    def json(self):
        if self._d is None:
            raise ValueError
        return self._d

calls, puts = [], []
mode = {"personal": False, "fail": False}
class Fake:
    def request(self, method, url, params=None, json=None, timeout=None, headers=None):
        calls.append((method, url, json or params or {}, headers or {}))
        if url.endswith("/connected_accounts"):
            return R(200, {"items": [
                {"id": "ca_ig_secret", "status": "ACTIVE", "is_disabled": False, "toolkit": {"slug": "instagram"},
                 "state": {"val": {"access_token": "should-not-be-saved"}}},
                {"id": "ca_fb_secret", "status": "ACTIVE", "is_disabled": False, "toolkit": {"slug": "facebook"}},
                {"id": "ca_old", "status": "EXPIRED", "is_disabled": False, "toolkit": {"slug": "instagram"}}], "next_cursor": None})
        if url.endswith("/files/upload/request"):
            return R(200, {"key": "projects/p/clip.mp4", "new_presigned_url": "https://storage.example/put",
                           "metadata": {"storage_backend": "s3"}})
        slug = url.rsplit("/", 1)[-1]
        if mode["fail"] and slug == "INSTAGRAM_POST_IG_USER_MEDIA":
            return R(200, {"successful": False, "error": "Instagram rejected the video.", "data": {}})
        answers = {
            "INSTAGRAM_GET_USER_INFO": {"id": "17841", "username": "lazy.gta6.clips",
                                        "account_type": "PERSONAL" if mode["personal"] else "MEDIA_CREATOR"},
            "FACEBOOK_LIST_MANAGED_PAGES": {"data": [
                {"id": "111", "name": "Lazy Clips", "tasks": ["CREATE_CONTENT", "MANAGE"]},
                {"id": "222", "name": "No Posting", "tasks": ["ANALYZE"]}], "paging": {"cursors": {}}},
            "INSTAGRAM_POST_IG_USER_MEDIA": {"id": "creation1"},
            "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH": {"id": "media1"},
            "INSTAGRAM_GET_IG_MEDIA": {"id": "media1", "permalink": "https://www.instagram.com/reel/abc/", "view_count": 12},
            "FACEBOOK_CREATE_VIDEO_POST": {"id": "vid9"},
            "FACEBOOK_GET_PAGE_POSTS": {"data": [{"id": "111_vid9", "permalink_url": "https://www.facebook.com/111/videos/vid9"}]},
        }
        return R(200, {"successful": True, "data": answers[slug], "error": None})
    def put(self, url, content=None, headers=None, timeout=None):
        puts.append((url, len(content or b""), headers or {}))
        return R(200, {})
composio_post.httpx = Fake()

with ctx.as_user(boss.id):
    try:
        mode["personal"] = True
        accounts.composio_import("instagram")
        personal = "accepted"
    except ValueError as e:
        personal = str(e)
    mode["personal"] = False
    check("a personal Instagram account is refused", "Creator or Business" in personal, True)
    names = accounts.composio_import("instagram")
    check("Instagram comes in from Composio", names, ["@lazy.gta6.clips"])
    pages = accounts.composio_import("facebook")
    check("only a Page that can post is brought in", pages, ["Lazy Clips"])
    listed = accounts.list_accounts()
    stored = json.dumps(listed) + (config.user_dir(boss.id) / "accounts" / "accounts.json").read_text(encoding="utf-8")
    check("the Composio sign-in and the API key stay on the server",
          ("should-not-be-saved" in stored or KEY in stored or "ca_ig_secret" in json.dumps(listed)), False)
    ig = accounts.get("ig_17841")
    check("the account remembers which Composio connection to use", ig["composio_account"], "ca_ig_secret")

jd = config.user_dir(boss.id) / "jobs" / "job-12345678" / "clips"
jd.mkdir(parents=True)
clip = {"id": "c1", "status": "done", "file": "clip01.mp4", "title": "Chopper chase", "description": "He panics", "hashtags": ["gta"], "score": 80}
(jd.parent / "job.json").write_text(json.dumps({
    "id": "job-12345678", "user": boss.id, "dir": str(jd.parent), "clips": [clip], "status": "done",
    "creator": None, "created": 1, "options": {}, "source": {"type": "file"}}), encoding="utf-8")
(jd / "clip01.mp4").write_bytes(b"\x00" * 32)
jobs.load_user(boss.id)
with ctx.as_user(boss.id):
    calls.clear(); puts.clear(); mode["fail"] = True
    p = {"id": "p1", "user": boss.id, "job": "job-12345678", "clip": "c1", "creator": None, "account": "ig_17841",
         "account_name": "@lazy.gta6.clips", "platform": "instagram", "status": "posting", "title": "Chopper chase",
         "tries": 0, "url": "", "remote_id": "", "error": None}
    posting._post(p)
    check("a refused video is tried again later", (p["status"], p["tries"], "rejected" in (p["error"] or "")), ("scheduled", 1, True))
    mode["fail"] = False
    calls.clear(); puts.clear()
    p.update(status="posting", tries=1, error=None)
    posting._post(p)
    made = next(c for c in calls if str(c[1]).endswith("INSTAGRAM_POST_IG_USER_MEDIA") and c[0] == "POST")
    check("the clip is uploaded, then published as a Reel",
          (puts[0][0], puts[0][1], made[2]["arguments"]["media_type"], made[2]["arguments"]["video_file"]["s3key"],
           made[2]["connected_account_id"]),
          ("https://storage.example/put", 32, "REELS", "projects/p/clip.mp4", "ca_ig_secret"))
    check("Instagram: posted, with its link", (p["status"], p["remote_id"], p["url"]),
          ("posted", "media1", "https://www.instagram.com/reel/abc/"))
    check("the key went out only as a header", made[3].get("x-api-key") == KEY, True)
    views = accounts.instagram_views(accounts.get("ig_17841"), "media1")
    check("views are read back through Composio", views, 12)

    calls.clear(); puts.clear()
    fp = dict(p, id="p2", account="fb_111", account_name="Lazy Clips", platform="facebook", status="posting",
              remote_id="", url="", error=None, tries=0)
    posting._post(fp)
    made = next(c for c in calls if str(c[1]).endswith("FACEBOOK_CREATE_VIDEO_POST"))
    check("Facebook gets the Page and the same uploaded clip",
          (fp["status"], fp["url"], made[2]["arguments"]["page_id"], made[2]["arguments"]["video"]["s3key"]),
          ("posted", "https://www.facebook.com/111/videos/vid9", "111", "projects/p/clip.mp4"))

r = b.get("/oauth/composio/instagram/start")
check("someone else cannot connect the server's Composio accounts", "connect_error" in r.headers.get("location", ""), True)
r = a.get("/oauth/composio/instagram/start")
check("Connect lands back on Connected apps", r.headers.get("location", "").startswith("/app/settings/apps?connected=instagram"), True)
check("a normal user still cannot see the key", b.get("/api/composio").status_code, 404)
a.put("/api/composio", json={"remove": True}, headers=ORIGIN)
check("removing the key turns Composio off", composio_post.ready(), False)

# A Connect key (ck_) talks to Composio Connect, where the person's own Instagram and Facebook logins live.
class Connect:
    def post(self, url, headers=None, json=None, timeout=None):
        calls.append(("MCP", url, json, headers or {}))
        name = json["params"]["name"]
        code = (json["params"].get("arguments") or {}).get("code_to_execute") or ""
        if name == "COMPOSIO_MANAGE_CONNECTIONS":
            payload = {"successful": True, "data": {"results": {"instagram": {"accounts": [
                {"id": "instagram_demo", "status": "active"}]}}}}
        elif name == "COMPOSIO_MULTI_EXECUTE_TOOL":
            payload = {"successful": True, "data": {"results": [{"response": {"successful": True, "data": {
                "id": "17841", "username": "lazy.gta6.clips", "account_type": "MEDIA_CREATOR"}}}]}}
        elif "base64.b64decode" in code:
            payload = {"successful": True, "data": {"stdout": "32"}}
        elif "MEDIA_PUBLISH" in code:
            payload = {"successful": True, "data": {"stdout": 'PEAKCUT {"media_id": "media9", "permalink": "https://www.instagram.com/reel/z/", "error": ""}'}}
        else:
            payload = {"successful": True, "data": {"stdout": 'PEAKCUT {"creation_id": "c9", "error": ""}'}}
        text = json_mod.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": json_mod.dumps(payload)}]}})
        return R(200, text="event: message\ndata: " + text)
import json as json_mod
composio_post.save_key("ck_testkeyvalue123456")
calls.clear()
composio_post.httpx = Connect()
check("a Connect key sees the Instagram login", composio_post.connections("instagram")[0]["id"], "instagram_demo")
rid, url = composio_post.post_instagram({"composio_account": "instagram_demo", "user_id": "17841"}, jd / "clip01.mp4", "Hi")
check("a Connect key publishes through Composio Connect", (rid, url), ("media9", "https://www.instagram.com/reel/z/"))
sent = next(c for c in calls if c[0] == "MCP")
check("the Connect key is sent as the consumer key", sent[3].get("x-consumer-api-key") == "ck_testkeyvalue123456" and "x-api-key" not in sent[3], True)

import time as time_mod
now = time_mod.time()
with ctx.as_user(boss.id):
    # Keep the three posts on the same day as the next slot. Late at night, "now plus 90 minutes"
    # is already tomorrow, and a daily cap would then see an empty day.
    base = now
    if time_mod.strftime("%Y-%m-%d", time_mod.localtime(now)) != time_mod.strftime("%Y-%m-%d", time_mod.localtime(now + 3 * 1800)):
        nxt = time_mod.localtime(now + 86400)
        base = time_mod.mktime((nxt.tm_year, nxt.tm_mon, nxt.tm_mday, 10, 0, 0, 0, 0, -1))
    for i in range(3):
        posting._posts.append({"user": boss.id, "account": "gap-test", "status": "scheduled", "due_at": base + i * 1800})
    capped = posting._next_slot("gap-test", 30, 1)
    open_cap = posting._next_slot("gap-test", 30, 0)
check("a daily cap of 1 waits longer than the 30 minute gap", capped > open_cap + 60, True)
check("a daily cap of 0 keeps the next post 30 minutes after the last", abs(open_cap - (base + 3 * 1800)) < 5, True)
check("instagram's too many actions is the posting limit", posting._instagram_limited("User is performing too many actions (code: 9)"), True)
check("any other failure is tried again in 10 minutes", posting._retry_wait("Composio did not answer."), 600)

with ctx.as_user(boss.id):
    hold_creator = creators.create({"name": "Hold Tester"})
    creators.update(hold_creator["id"], {"post_gap_min": 60, "daily_cap": 0})
    posting._holds.pop("ig-hold", None)
    for i, due in enumerate((now - 100, now + 60, now + 120)):
        posting._posts.append({"id": f"h{i}", "user": boss.id, "creator": hold_creator["id"], "account": "ig-hold",
                               "platform": "instagram", "status": "scheduled", "due_at": due,
                               "error": "User is performing too many actions (code: 9)" if i == 0 else None, "tries": 1})
    posting._posts.append({"id": "h-yt", "user": boss.id, "creator": hold_creator["id"], "account": "yt-hold",
                           "platform": "youtube", "status": "scheduled", "due_at": now + 30})
    failed = next(p for p in posting._posts if p.get("id") == "h0")
    moved, until = posting._pause_instagram(failed, now=now)
    again, until_again = posting._pause_instagram(failed, now=now + 60)
    held = sorted((p for p in posting._posts if str(p.get("id", "")).startswith("h") and p.get("account") == "ig-hold"), key=lambda p: p["due_at"])
    youtube = next(p for p in posting._posts if p.get("id") == "h-yt")
check("the first limit error pauses instagram for 24 hours", abs(until - (now + 86400)) < 2, True)
check("a later error does not move that pause", until_again, until)
check("every waiting instagram post moved, and none twice", (len(moved), len(again)), (3, 0))
check("they start when the pause ends, one hour apart", [round(held[i + 1]["due_at"] - held[i]["due_at"]) for i in range(2)], [3600, 3600])
check("the limit error is cleared once the post has a new time", held[0].get("error") is None and held[0]["tries"] == 0, True)
check("youtube keeps its own time", abs(youtube["due_at"] - (now + 30)) < 1, True)

with ctx.as_user(boss.id):
    gap_creator = creators.create({"name": "Gap Tester"})
    creators.update(gap_creator["id"], {"post_gap_min": 60, "daily_cap": 0})
    posted_at = now - 3600
    posting._posts.append({"id": "rs-posted", "user": boss.id, "creator": gap_creator["id"], "account": "acc-rs",
                           "status": "posted", "posted_at": posted_at, "due_at": posted_at})
    posting._posts.append({"id": "rs-busy", "user": boss.id, "creator": gap_creator["id"], "account": "acc-rs",
                           "status": "posting", "due_at": 1, "tries": 1, "error": "uploading"})
    for i, due in enumerate((now - 10000, now - 5000, now - 1000)):
        posting._posts.append({"id": f"rs{i}", "user": boss.id, "job": "missing-job", "clip": "c", "creator": gap_creator["id"],
                               "account": "acc-rs", "status": "scheduled", "due_at": due, "tries": 2, "error": "old"})
    moved = posting.reschedule(gap_creator["id"])
    waiting = sorted((p for p in posting._posts if str(p.get("id", "")).startswith("rs") and p["status"] == "scheduled" and not str(p.get("id")).startswith("rs-")), key=lambda p: p["due_at"])
    busy = next(p for p in posting._posts if p.get("id") == "rs-busy")
check("three waiting posts were moved onto the new gap", moved, 3)
check("they sit one hour apart", [round(waiting[i + 1]["due_at"] - waiting[i]["due_at"]) for i in range(2)], [3600, 3600])
check("the old error is cleared", waiting[0].get("error") is None and waiting[0]["tries"] == 0, True)
check("a post that is uploading is left alone", (busy["due_at"], busy["error"]), (1, "uploading"))

print("ALL PASSED" if not fails else f"{len(fails)} failed")
import shutil
db.engine.dispose()
shutil.rmtree(os.environ["PEAKCUT_DATA"], ignore_errors=True)
sys.exit(1 if fails else 0)
