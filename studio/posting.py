"""Post queue: decides when each finished clip goes to which account, posts it, and tracks views."""
import secrets
import threading
import time
from pathlib import Path

import json

from . import accounts, alerts, config, creators, ctx, jobs, live

_lock = threading.RLock()
_FILE = config.DATA_DIR / "posts.json"
_HOLDS = config.DATA_DIR / "post_holds.json"   # account id -> unix time it may post again
try:
    _posts: list = json.loads(_FILE.read_text(encoding="utf-8")) if _FILE.exists() else []
except ValueError:
    _posts = []
try:
    _holds: dict = json.loads(_HOLDS.read_text(encoding="utf-8")) if _HOLDS.exists() else {}
except ValueError:
    _holds = {}
_holds = {str(k): float(v) for k, v in _holds.items() if float(v) > time.time()}


def _mine() -> list:
    u = ctx.uid()
    return [p for p in _posts if p.get("user") == u]
OPEN = ("scheduled", "posting")


def _save():
    with _lock:
        tmp = _FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_posts, indent=1), encoding="utf-8")
        tmp.replace(_FILE)


def _save_holds():
    tmp = _HOLDS.with_suffix(".tmp")
    tmp.write_text(json.dumps(_holds), encoding="utf-8")
    tmp.replace(_HOLDS)


def forget(uid: str):
    """Drop everything queued or recorded for a deleted account, so nothing is posted or retried for it any more."""
    with _lock:
        for p in _posts:
            if p.get("user") == uid:
                p["status"] = "deleted"          # the posting loop may be holding this one already: it then skips it
        _posts[:] = [p for p in _posts if p.get("user") != uid]
        _save()


def _day(t):
    return time.strftime("%Y-%m-%d", time.localtime(t))


def _mirror(p):
    """Copy the post's state onto its clip so the clip card can show it."""
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    if clip is not None:
        clip.setdefault("posts", {})[p["id"]] = {k: p.get(k) for k in ("account_name", "platform", "status", "url", "due_at", "error")}
        jobs.save(job)


def _next_slot(account_id, gap_min, cap):
    """Earliest time that keeps the gap between posts on this account. A daily cap of 0 means no daily limit."""
    mine = [p for p in _mine() if p["account"] == account_id and p["status"] in OPEN + ("posted",)]
    t = max(time.time(), max((p.get("posted_at") or p["due_at"] for p in mine), default=0) + gap_min * 60)
    hold = _holds.get(account_id, 0)
    if hold > t:          # this account is in a pause, so nothing new is placed inside it
        t = hold
    if not cap:
        return t
    for _ in range(30):
        if sum(1 for p in mine if _day(p.get("posted_at") or p["due_at"]) == _day(t)) < cap:
            break
        t = _morning_after(t)
    return t


def _morning_after(t):
    """09:00 the next morning, which is when a full day of posts continues."""
    lt = time.localtime(t + 86400)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 9, 0, 0, 0, 0, -1))


def reschedule(creator_id) -> int:
    """Move this creator's waiting posts onto the gap and daily limit saved for them.

    Posts that already went out, and one that is uploading now, stay where they are. The order on each
    account stays the same. The first waiting post is the later of now and one gap after the last post
    on that account. Returns how many posts moved."""
    c = creators.get(creator_id)
    if not c:
        raise KeyError(creator_id)
    gap, cap = int(c["post_gap_min"]) * 60, int(c["daily_cap"])
    moved = []
    with _lock:
        mine = _mine()
        accounts_waiting = {p["account"] for p in mine if p.get("creator") == creator_id and p["status"] == "scheduled"}
        for aid in accounts_waiting:
            rows = sorted((p for p in mine if p["account"] == aid and p.get("creator") == creator_id and p["status"] == "scheduled"),
                          key=lambda p: (p.get("due_at") or 0, p.get("id") or ""))
            taken = [p["posted_at"] for p in mine if p["account"] == aid and p["status"] == "posted" and p.get("posted_at")]
            t = max(time.time(), _holds.get(aid, 0))
            if taken:
                t = max(t, max(taken) + gap)
            for p in rows:
                slot = t
                if cap:
                    for _ in range(40):
                        if sum(1 for x in taken if _day(x) == _day(slot)) < cap:
                            break
                        slot = _morning_after(slot)
                p.update(due_at=slot, error=None, tries=0)
                taken.append(slot)
                t = slot + gap
                moved.append(p)
        if moved:
            _save()
    for p in moved:
        _mirror(p)
    if moved:
        creators.log(creator_id, f"Moved {len(moved)} waiting post{'s' if len(moved) != 1 else ''} onto a {c['post_gap_min']} minute gap.")
        ctx.bump()
    return len(moved)


def enqueue(job, clip, account_ids, now=False) -> list:
    c = creators.get(job["creator"]) or creators.DEFAULTS
    made = []
    with _lock:
        for aid in account_ids:
            acc = accounts.get(aid)
            if not acc:
                continue
            if any(p["job"] == job["id"] and p["clip"] == clip["id"] and p["account"] == aid
                   and p["status"] in OPEN + ("posted",) for p in _mine()):
                continue  # never post the same clip to the same account twice
            due = time.time() if now else _next_slot(aid, c["post_gap_min"], c["daily_cap"])
            p = {"id": secrets.token_hex(8), "user": ctx.uid(), "job": job["id"], "clip": clip["id"], "creator": job["creator"],
                 "account": aid, "account_name": acc["name"], "platform": acc["platform"], "status": "scheduled",
                 "due_at": due, "created": time.time(), "title": clip.get("title") or clip.get("hook") or "Clip",
                 "score": clip.get("score", 0), "tries": 0, "url": "", "remote_id": "", "error": None, "views": None}
            _posts.append(p)
            made.append(p)
        _save()
    for p in made:
        _mirror(p)
    return made


def on_clip_ready(job, clip):
    """Called once when a clip first finishes rendering."""
    c = creators.get(job["creator"])
    if not c or not c["autopost"] or not c["accounts"]:
        return
    if clip.get("score", 0) < c["post_min_score"]:
        return  # includes hand-made clips (score 0): those wait for you to press Post
    made = enqueue(job, clip, c["accounts"])
    if made:
        creators.log(c["id"], f"Queued for {len(made)} account(s): {clip.get('title') or clip.get('hook')}")


def _caption(clip, creator):
    # A description the person wrote themselves is posted as it is, without the AI's hashtags added to it, and
    # one that already ends with its hashtags does not get them twice.
    tags = "" if clip.get("fixed_description") or clip.get("hashtags_in_description") else " ".join("#" + h for h in clip.get("hashtags", []))
    return "\n\n".join(x for x in (clip.get("description", ""), tags, creator.get("post_footer", "")) if x)


def _instagram_limited(error: str) -> bool:
    """Instagram's own 'too many actions' (code 9): the account has published as much as Meta allows."""
    text = (error or "").lower()
    return "too many actions" in text or "code: 9" in text


def _retry_wait(error: str) -> int:
    """Seconds before a failed post is tried again. A plain failure is tried again in 10 minutes."""
    return 600


def _pause_instagram(p, now=None):
    """Stop this Instagram account for 24 hours from the first limit error, and move every waiting post past it.

    A later error inside that window does not move the end. The order of the queue stays. Each post keeps
    the gap already set for the creator, so they do not all go out together when the pause ends.
    Returns the posts that moved, and the time posting may start again."""
    now = time.time() if now is None else now
    until = _holds.get(p["account"], 0)
    if until <= now:
        until = now + 24 * 3600
        _holds[p["account"]] = until
        _save_holds()
    c = creators.get(p.get("creator")) or creators.DEFAULTS
    gap = int(c.get("post_gap_min") or 60) * 60
    with _lock:
        rows = sorted((row for row in _posts if row.get("account") == p["account"] and row.get("status") == "scheduled"),
                      key=lambda row: (row.get("due_at") or 0, row.get("id") or ""))
        t = until
        moved = []
        for row in rows:
            if (row.get("due_at") or 0) < t or _instagram_limited(row.get("error") or ""):
                row["due_at"] = t
                if _instagram_limited(row.get("error") or ""):
                    row["error"] = None
                    row["tries"] = 0
                moved.append(row)
                t += gap
            else:
                t = row["due_at"] + gap
    return moved, until


def _post(p):
    job = jobs.get(p["job"])
    clip = next((c for c in job["clips"] if c["id"] == p["clip"]), None) if job else None
    acc = accounts.get(p["account"])
    try:
        if not clip or clip.get("status") != "done":
            raise RuntimeError("The clip no longer exists or is not finished.")
        if not acc:
            raise RuntimeError("That account was disconnected.")
        path = Path(job["dir"]) / "clips" / clip["file"]
        creator = creators.get(job["creator"]) or dict(creators.DEFAULTS, name="clips")
        title, cap = clip.get("title") or clip.get("hook") or "Clip", _caption(clip, creator)
        text = f"{title}\n\n{cap}"
        if acc.get("via") == "composio":
            from . import composio_post
            if acc["platform"] == "instagram":
                rid, url = composio_post.post_instagram(acc, path, text)
            elif acc["platform"] == "facebook":
                rid, url = composio_post.post_facebook(acc, path, title, text)
            else:
                raise RuntimeError("That kind of account is no longer supported.")
        elif acc["platform"] == "youtube":
            rid, url = accounts.youtube_upload(acc, path, title, cap + "\n\n#Shorts", clip.get("hashtags", []), creator["privacy"])
        elif acc["platform"] == "instagram":
            rid, url = accounts.instagram_upload(acc, path, text, job["id"])
        elif acc["platform"] == "tiktok":
            rid, url = accounts.tiktok_upload(acc, path, text, creator["privacy"])
        else:
            raise RuntimeError("That kind of account is no longer supported.")
        p.update(status="posted", posted_at=time.time(), remote_id=rid, url=url, error=None)
        creators.log(p["creator"], f"Posted to {acc['name']}: {title}")
    except Exception as e:  # noqa: BLE001
        p["error"] = str(e)[-300:]
        if _instagram_limited(p["error"]) and clip and acc and p.get("platform") == "instagram":
            # The first of these starts a 24 hour pause. Every waiting post on this account moves past it,
            # so the next one in the queue is not tried while Instagram is still refusing.
            p.update(status="scheduled", tries=0)
            moved, until = _pause_instagram(p)
            when = time.strftime("%b %d, %H:%M", time.localtime(until))
            creators.log(p["creator"], f"Instagram blocked posting until {when}, so {len(moved)} waiting posts were moved forward.")
            _save()
            for row in moved:
                _mirror(row)
            return
        wait = _retry_wait(p["error"])
        p["tries"] += 1
        if p["tries"] < 3 and clip and acc:
            p.update(status="scheduled", due_at=time.time() + wait)  # try again in 10 minutes
        else:
            p["status"] = "failed"
            creators.log(p["creator"], f"Post failed on {p['account_name']}: {p['error']}")
            alerts.notify(f"post failed on {p['account_name']} ({p['title']}): {p['error'][:200]}", kind="post")
    _save()
    _mirror(p)


def _run():
    while True:
        time.sleep(15)
        due = [p for p in list(_posts) if p["status"] == "scheduled" and p["due_at"] <= time.time() and p.get("user")]
        for p in due:
            try:
                with ctx.as_user(p["user"]):
                    if accounts.settings()["paused"]:
                        continue
                    with _lock:
                        if p["status"] != "scheduled":
                            continue
                        p["status"] = "posting"
                    _post(p)
            except Exception:  # noqa: BLE001
                pass


def _stats_for(recent):
    for aid in {p["account"] for p in recent if p["platform"] == "youtube"}:
        acc = accounts.get(aid)
        if acc:
            views = accounts.youtube_views(acc, [p["remote_id"] for p in recent if p["account"] == aid])
            for p in recent:
                if p["account"] == aid and p["remote_id"] in views:
                    p["views"] = views[p["remote_id"]]
    for p in recent:
        acc = accounts.get(p["account"]) if p["platform"] == "instagram" else None
        if acc:
            v = accounts.instagram_views(acc, p["remote_id"])
            if v is not None:
                p["views"] = v


def _stats():
    while True:
        for uid in {p.get("user") for p in _posts if p.get("user")}:
            try:
                with ctx.as_user(uid):
                    _stats_for([p for p in _mine() if p["status"] == "posted" and time.time() - p["posted_at"] < 14 * 86400])
            except Exception:  # noqa: BLE001
                pass
        _save()
        time.sleep(1800)


def cancel(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] != "scheduled":
            raise ValueError("Only posts that are still waiting can be cancelled.")
        p["status"] = "cancelled"
        _save()
    _mirror(p)


def retry(pid):
    with _lock:
        p = next((p for p in _mine() if p["id"] == pid), None)
        if not p or p["status"] not in ("failed", "cancelled"):
            raise ValueError("Only failed or cancelled posts can be retried.")
        p.update(status="scheduled", due_at=time.time(), tries=0, error=None)
        _save()
    _mirror(p)


def overview() -> dict:
    today = _day(time.time())
    _posts = _mine()  # everything below is about the signed-in user only
    rows = []
    all_cs, all_js = creators.all_creators(), jobs.list_jobs()
    for c in all_cs:
        js = [j for j in all_js if j["creator"] == c["id"]]
        clips = [k for j in js for k in j["clips"] if k.get("status") == "done"]
        ps = [p for p in _posts if p["creator"] == c["id"]]
        views = sum(p["views"] or 0 for p in ps)
        rows.append({
            "id": c["id"], "name": c["name"],
            "state": "recording" if live.active_for(c["id"]) else "waiting" if c["auto_watch"] and c["channel"] else "idle",
            "autopost": c["autopost"] and bool(c["accounts"]),
            "clips_today": sum(1 for k in clips if _day(k.get("made_at", 0)) == today), "clips_total": len(clips),
            "posted_today": sum(1 for p in ps if p["status"] == "posted" and _day(p["posted_at"]) == today),
            "posted_total": sum(1 for p in ps if p["status"] == "posted"),
            "queued": sum(1 for p in ps if p["status"] in OPEN), "failed": sum(1 for p in ps if p["status"] == "failed"),
            "views": views, "earnings": round(views / 1000 * c["rate_per_1k"], 2)})
    names = {c["id"]: c["name"] for c in all_cs}
    queue = sorted((p for p in _posts if p["status"] in OPEN), key=lambda p: p["due_at"])
    done = sorted((p for p in _posts if p["status"] in ("posted", "failed")), key=lambda p: -(p.get("posted_at") or p["due_at"]))[:15]
    show = [{**{k: v for k, v in p.items() if k != "user"}, "creator_name": names.get(p["creator"], "")} for p in queue + done]
    # last 14 days, oldest first, for the home graph
    days = [_day(time.time() - 86400 * i) for i in range(13, -1, -1)]
    made, posted, views = {d: 0 for d in days}, {d: 0 for d in days}, {d: 0 for d in days}
    for j in all_js:
        for k in j["clips"]:
            d = _day(k.get("made_at") or j["created"])
            if k.get("status") == "done" and d in made:
                made[d] += 1
    for p in _posts:
        if p["status"] == "posted" and _day(p["posted_at"]) in posted:
            posted[_day(p["posted_at"])] += 1
            views[_day(p["posted_at"])] += p["views"] or 0
    daily = [{"day": d, "clips": made[d], "posted": posted[d], "views": views[d]} for d in days]
    return {"paused": accounts.settings()["paused"], "creators": rows, "posts": show, "daily": daily,
            "alerts": alerts.configured(), "recording": len(live.active_mine())}


def digest_text() -> str:
    o = overview()
    lines = ["PeakCut daily summary"]
    for r in o["creators"]:
        lines.append(f"- {r['name']}: {r['state']}, {r['clips_today']} clips today, {r['posted_today']} posted today, "
                     f"{r['queued']} waiting, {r['failed']} failed, {r['views']} views")
    if not o["creators"]:
        lines.append("No creators set up yet.")
    if o["paused"]:
        lines.append("Posting is paused.")
    return "\n".join(lines)


def start():
    with _lock:
        for p in _posts:
            if p["status"] == "posting":  # interrupted mid-upload last time
                p.update(status="failed", error="The studio closed during this upload. Check the account, then retry.")
        _save()
    jobs.on_clip_ready.append(on_clip_ready)
    threading.Thread(target=_run, daemon=True).start()
    threading.Thread(target=_stats, daemon=True).start()
    threading.Thread(target=alerts.digest_loop, args=(digest_text,), daemon=True).start()
    threading.Thread(target=alerts.keep_awake_loop,
                     args=(lambda: bool(live.active()) or any(p["status"] in OPEN for p in _posts),), daemon=True).start()
