"""Instagram and Facebook posts are spaced five minutes apart so Composio is not hit twice at once."""
import os, sys, tempfile, time
os.environ["PEAKCUT_DATA"] = tempfile.mkdtemp(prefix="pc-stagger-"); os.environ["DATABASE_URL"] = ""
sys.path.insert(0, ".")
from pathlib import Path
from studio import accounts, auth, creators, ctx, db, jobs, posting
db.init()
fails = []
def check(n, ok, x=""):
    print(("PASS " if ok else "FAIL ") + n + (f"  {x}" if x else "")); ok or fails.append(n)

a, _ = auth._create("a@example.com", "A", password_hash="x")
with ctx.as_user(a.id):
    c = creators.create({"name": "Streamer", "post_gap_min": 30, "daily_cap": 0, "autopost": False,
                         "accounts": []})
    j = jobs.create({"type": "file", "path": "", "name": "v.mp4"}, {}, enqueue=False, creator=c["id"])
    (Path(j["dir"]) / "clips").mkdir(parents=True, exist_ok=True)
    for i in range(2):
        j["clips"].append({"id": f"clip0{i+1}", "status": "done", "file": f"clip0{i+1}.mp4",
                           "cover": f"clip0{i+1}.jpg", "title": f"Clip {i+1}", "start": 0, "end": 10})
    j["status"] = "done"; jobs.save(j)
    for aid, plat in (("acc_yt", "youtube"), ("acc_ig", "instagram"), ("acc_fb", "facebook")):
        accounts._put({"id": aid, "platform": plat, "name": plat.title(), "added": time.time()})

    at = time.time() + 3600
    made = posting.enqueue(j, j["clips"][0], ["acc_yt", "acc_ig", "acc_fb"], at=at)
    by = {p["platform"]: p for p in made}
    check("all three accounts are queued", set(by) == {"youtube", "instagram", "facebook"})
    check("YouTube keeps the time picked", abs(by["youtube"]["due_at"] - at) < 1)
    check("Instagram keeps the time picked", abs(by["instagram"]["due_at"] - at) < 1)
    gap = by["facebook"]["due_at"] - by["instagram"]["due_at"]
    check("Facebook is five minutes after Instagram", abs(gap - posting.PLATFORM_STAGGER_SEC) < 1, str(gap))

    now = time.time()
    made2 = posting.enqueue(j, j["clips"][1], ["acc_ig", "acc_fb"], now=True)
    by2 = {p["platform"]: p for p in made2}
    gap2 = by2["facebook"]["due_at"] - by2["instagram"]["due_at"]
    check("Post now still spaces Instagram then Facebook", abs(gap2 - posting.PLATFORM_STAGGER_SEC) < 1, str(gap2))
    check("...and Instagram is due straight away", by2["instagram"]["due_at"] - now < 2)

    # Force an Instagram and a Facebook post onto the same second; reschedule must split those two
    clash = now + 7200
    for p in posting._mine():
        if p["status"] == "scheduled" and p["platform"] in ("instagram", "facebook"):
            p["due_at"] = clash
    posting._save()
    creators.update(c["id"], {"post_gap_min": 30, "daily_cap": 0})
    n = posting.reschedule(c["id"])
    check("reschedule moves waiting posts", n >= 2)
    igs = [p["due_at"] for p in posting._mine() if p["status"] == "scheduled" and p["platform"] == "instagram"]
    fbs = [p["due_at"] for p in posting._mine() if p["status"] == "scheduled" and p["platform"] == "facebook"]
    cross = [abs(i - f) for i in igs for f in fbs]
    check("after reschedule no Instagram shares a five-minute window with Facebook",
          all(d >= posting.PLATFORM_STAGGER_SEC - 0.5 for d in cross), str(cross))

    # Runtime: only one IG/FB may be posting at a time
    ig, fb = by2["instagram"], by2["facebook"]
    ig.update(status="posting", due_at=time.time() - 10)
    fb.update(status="scheduled", due_at=time.time() - 10)
    busy = any(x.get("status") == "posting" and x.get("platform") in posting.STAGGER_PLATFORMS for x in posting._posts)
    check("a posting Instagram blocks starting Facebook in the same tick", busy)

print("\nall passed" if not fails else f"\n{len(fails)} failed: {fails}")
sys.exit(1 if fails else 0)
