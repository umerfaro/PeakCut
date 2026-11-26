"""Each user's connected social accounts and personal settings (alerts, posting pause).

Stored under users/<user>/accounts. People connect YouTube, Instagram and TikTok with one click: they sign in on the
platform's own page and come back. For that, whoever runs the server registers one app with each platform once
(its id and secret), in .env or in the dashboard (Settings, Connected apps, Set up). Those belong to the server,
never to users.

Instagram and Facebook can instead use accounts already connected in Composio. That needs only a Composio API key,
not a Meta developer app. See composio_post.py."""
import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx

from . import config, ctx

_lock = threading.RLock()
_states: dict = {}   # one-time values for the connect redirects: state -> (user id, when, platform)

SECRET_KEYS = ("discord_webhook", "telegram_token")
SETTING_DEFAULTS = {"discord_webhook": "", "telegram_token": "", "telegram_chat": "", "paused": False,
                    "digest_hour": 8, "last_digest": "", "tour_done": False, "os_seen": False, "rated": False, "day_limit": 3}

YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
IG = "https://graph.instagram.com"
FB = "https://graph.facebook.com/v21.0"      # Instagram through Facebook login lives here
IG_AUDIO = "https://graph.facebook.com/v22.0"
IG_UPLOAD = "https://rupload.facebook.com/ig-api-upload/v21.0"
IG_SCOPES = "instagram_business_basic,instagram_business_content_publish"
TT = "https://open.tiktokapis.com/v2"
TT_SCOPES = "user.info.basic,video.publish"
PLATFORM_NAMES = {"youtube": "YouTube", "instagram": "Instagram", "facebook": "Facebook", "tiktok": "TikTok"}


def _dir() -> Path:
    d = config.user_dir(ctx.uid()) / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read(name, default):
    f = _dir() / name
    if not f.exists():
        return default
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except ValueError:
        return default


def _write(name, data):
    with _lock:
        d = _dir()
        tmp = d / (name + ".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        try:
            tmp.chmod(0o600)                 # these hold posting tokens: readable by the app only
        except OSError:
            pass
        tmp.replace(d / name)
        if name == "settings.json":
            _settings_mem[ctx.uid()] = {**SETTING_DEFAULTS, **data}
    ctx.bump()


# ------------------------------------------------------------------ the server's apps with each platform
# name -> (what the platform calls the id, what it calls the secret, .env names for both)
APPS = {"google": ("Client ID", "Client secret", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"),
        "instagram": ("Instagram app ID", "Instagram app secret", "PEAKCUT_IG_APP_ID", "PEAKCUT_IG_APP_SECRET"),
        "tiktok": ("Client key", "Client secret", "PEAKCUT_TIKTOK_CLIENT_KEY", "PEAKCUT_TIKTOK_CLIENT_SECRET")}
APP_FOR = {"youtube": "google", "instagram": "instagram", "tiktok": "tiktok"}
_apps_seen = {"at": None, "data": {}}


def _apps_file() -> Path:
    return config.DATA_DIR / "apps.json"


def _apps_saved() -> dict:
    f = _apps_file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _apps_seen["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _apps_seen.update(at=at, data=data if isinstance(data, dict) else {})
    return _apps_seen["data"]


def app_keys(name: str) -> tuple:
    """(id, secret) of the server's app with this platform: what the dashboard saved, or else .env."""
    saved = _apps_saved().get(name) or {}
    if saved.get("id") and saved.get("secret"):
        return saved["id"], saved["secret"]
    if name == "google":
        return config.google_client()
    _l1, _l2, id_env, secret_env = APPS[name]
    return (config.env(id_env) or "").strip(), (config.env(secret_env) or "").strip()


def _server_admin() -> bool:
    """The Composio key belongs to whoever runs the server, so only they may post with it."""
    if not config.ACCOUNTS:
        return True
    from . import db
    import sqlalchemy as sa
    with db.engine.connect() as c:
        row = c.execute(sa.select(db.users.c.is_admin).where(db.users.c.id == ctx.uid())).first()
    return bool(row and row[0])


def composio_for_me() -> bool:
    from . import composio_post
    return composio_post.ready() and _server_admin()


def ig_config() -> str:
    """The Facebook login configuration id for Instagram (set in the dashboard or PEAKCUT_IG_CONFIG_ID). When there
    is one, Instagram connects through Facebook login; without it, through Instagram's own login."""
    return str((_apps_saved().get("instagram") or {}).get("config") or config.env("PEAKCUT_IG_CONFIG_ID") or "").strip()


def app_open(platform: str) -> bool:
    """Is this platform's app open to everyone? Yes unless an admin unticked it: until a platform approves an app
    only its testers can sign in, so ordinary users are then not offered a Connect button that would fail; admins
    always see it to test."""
    return bool((_apps_saved().get(APP_FOR.get(platform)) or {}).get("open", True))


def can_connect(platform: str) -> bool:
    if platform == "facebook":
        return composio_for_me()
    if platform == "instagram" and composio_for_me():
        return True
    app = APP_FOR.get(platform)
    return bool(app) and all(app_keys(app))


def redirect_for(platform: str) -> str:
    return f"{config.PUBLIC_URL}/oauth/{platform}/callback"


def apps_public() -> dict:
    """For the setup panel: whether each app is set, never its secret."""
    out = {}
    for platform, name in APP_FOR.items():
        cid, secret = app_keys(name)
        out[platform] = {"ready": bool(cid and secret), "id_label": APPS[name][0], "secret_label": APPS[name][1],
                         "id_hint": cid[:6] + "…" if len(cid) > 10 else cid, "redirect": redirect_for(platform),
                         "https": config.PUBLIC_URL.startswith("https://"),
                         "from_dashboard": bool((_apps_saved().get(name) or {}).get("id")), "open": app_open(platform)}
    out["instagram"].update(config_label="Login configuration ID (Facebook login, newer Meta apps)", facebook=bool(ig_config()),
                            config_hint=ig_config()[:6] + "…" if len(ig_config()) > 10 else ig_config())
    return out


def save_app(platform: str, cid: str, secret: str, config_id=None, opened=None) -> dict:
    """Save the server's app for a platform, from the dashboard. An empty secret keeps the saved one."""
    if platform not in APP_FOR:
        raise ValueError("Unknown platform.")
    name = APP_FOR[platform]
    cid, secret = str(cid or "").strip()[:200], str(secret or "").strip()[:300]
    old = _apps_saved().get(name) or {}
    if not cid and not secret:
        raise ValueError(f"Paste the {APPS[name][0]} and {APPS[name][1]}.")
    cid = cid or old.get("id", "")                # only a new secret typed: keep the saved id
    if not re.fullmatch(r"[\w.\-]{4,200}", cid):
        raise ValueError(f"Paste the {APPS[name][0]} exactly as the platform shows it.")
    secret = secret or (old.get("secret") if old.get("id") == cid else "")
    if not secret:
        raise ValueError(f"Paste the {APPS[name][1]} too.")
    cfg = str(config_id if config_id is not None else old.get("config", "")).strip()[:60]
    if cfg and not re.fullmatch(r"\d{5,30}", cfg):
        raise ValueError("The login configuration ID is only digits. Paste it exactly as Meta shows it.")
    with _lock:
        data = dict(_apps_saved())
        was_open = (_apps_saved().get(name) or {}).get("open", True)
        data[name] = {"id": cid, "secret": secret, "open": bool(opened) if opened is not None else was_open,
                      **({"config": cfg} if cfg and platform == "instagram" else {})}
        tmp = _apps_file().with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(_apps_file())
        _apps_seen["at"] = None
    return apps_public()


def _new_state(platform: str) -> str:
    state = secrets.token_urlsafe(16)
    for k in [k for k, v in list(_states.items()) if time.time() - v[1] > 900]:
        _states.pop(k, None)
    _states[state] = (ctx.uid(), time.time(), platform)
    return state


def _take_state(state: str, platform: str):
    got = _states.pop(state or "", None)
    if not got or got[0] != ctx.uid() or got[2] != platform or time.time() - got[1] > 900:
        raise ValueError("This sign-in has expired. Press Connect again.")


def connect_url(platform: str) -> str:
    """Where the Connect button sends the browser: the platform's own sign-in page."""
    if platform == "facebook":
        raise ValueError("Facebook connects through Composio. Add the API key in Settings, then press Connect.")
    if platform not in APP_FOR:
        raise ValueError("Unknown platform.")
    if not can_connect(platform):
        raise ValueError(f"Connecting {PLATFORM_NAMES[platform]} isn't set up on this server yet.")
    cid, _ = app_keys(APP_FOR[platform])
    state, redirect = _new_state(platform), redirect_for(platform)
    if platform == "youtube":
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": YT_SCOPES,
            "access_type": "offline", "prompt": "consent", "state": state})
    if platform == "instagram" and ig_config():
        # exactly the login link Meta's dashboard gives for an "Instagram Login Compatible" configuration: the page
        # leads with "Continue with Instagram", so people sign in with Instagram, not a Facebook account
        return "https://www.facebook.com/dialog/oauth?" + urlencode({
            "client_id": cid, "config_id": ig_config(), "display": "popup", "override_default_response_type": "true",
            "redirect_uri": redirect, "response_type": "code", "state": state, "prompt": "login"})
    if platform == "instagram":
        return "https://www.instagram.com/oauth/authorize?" + urlencode({
            "client_id": cid, "redirect_uri": redirect, "response_type": "code", "scope": IG_SCOPES, "state": state})
    return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": cid, "redirect_uri": redirect, "response_type": "code", "scope": TT_SCOPES, "state": state})


def connect_finish(platform: str, code: str, state: str) -> dict:
    _take_state(state, platform)
    code = (code or "").split("#")[0]           # Instagram adds "#_" to the code
    if not code:
        raise ValueError("The platform did not send a sign-in code. Try again.")
    return {"youtube": _youtube_finish, "instagram": _instagram_finish, "tiktok": _tiktok_finish}[platform](code)


# ------------------------------------------------------------------ links the platforms can fetch a clip from
# Instagram takes a video by address, not as an upload. Each address works for one clip, for one hour, and is
# signed, so it cannot be guessed or changed to reach anything else.
def _link_key() -> bytes:
    f = config.DATA_DIR / "link.key"
    if not f.exists():
        with _lock:
            if not f.exists():
                f.write_bytes(secrets.token_bytes(32))
                try:
                    f.chmod(0o600)
                except OSError:
                    pass
    return f.read_bytes()


def clip_link(job_id: str, file_name: str, seconds: int = 3600) -> str:
    body = json.dumps([ctx.uid(), job_id, file_name, int(time.time() + seconds)], separators=(",", ":")).encode()
    b = base64.urlsafe_b64encode(body).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(_link_key(), b.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")
    return f"{config.PUBLIC_URL}/pub/{b}.{sig}.mp4"


def read_clip_link(token: str):
    """(user id, job id, file name) for a valid, unexpired link, else None."""
    try:
        b, sig = token.split(".")
        want = base64.urlsafe_b64encode(hmac.new(_link_key(), b.encode(), hashlib.sha256).digest()[:18]).decode().rstrip("=")
        if not hmac.compare_digest(sig, want):
            return None
        uid, job_id, name, until = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)))
        if time.time() > until or not re.fullmatch(r"[\w\-]+\.mp4", name):
            return None
        return uid, job_id, name
    except (ValueError, TypeError):
        return None


# ------------------------------------------------------------------ settings
_settings_mem: dict = {}


def settings() -> dict:
    u = ctx.uid()
    if u not in _settings_mem:
        _settings_mem[u] = {**SETTING_DEFAULTS, **_read("settings.json", {})}
    return dict(_settings_mem[u])


def save_settings(patch: dict) -> dict:
    with _lock:
        s = settings()
        for k, v in patch.items():
            if k not in SETTING_DEFAULTS:
                continue
            if k in ("paused", "tour_done", "os_seen", "rated"):
                v = bool(v)
            elif k == "digest_hour":
                v = max(0, min(23, int(v)))
            elif k == "day_limit":
                v = max(1, min(20, int(v)))  # most posts the schedule takes on one day
            else:
                v = str(v).strip()[:300]
                if k == "discord_webhook" and v and not v.startswith("https://discord.com/api/webhooks/"):
                    raise ValueError("That doesn't look like a Discord webhook link.")
                if k in SECRET_KEYS and v == "":
                    continue  # blank means "keep what is saved"
            s[k] = v
        _write("settings.json", s)
        return s


def public_settings() -> dict:
    s = settings()
    return {k: (bool(v) if k in SECRET_KEYS else v) for k, v in s.items()}


# ------------------------------------------------------------------ accounts
def _all() -> list:
    return [a for a in _read("accounts.json", []) if a.get("platform") in PLATFORM_NAMES]


def get(aid):
    return next((a for a in _all() if a["id"] == aid), None)


def list_accounts() -> list:
    return [{"id": a["id"], "platform": a["platform"], "label": PLATFORM_NAMES[a["platform"]], "name": a["name"],
             "added": a["added"], "detail": a.get("detail", ""), "reels_audio": reels_audio_enabled(a)} for a in _all()]


def _put(acc):
    with _lock:
        rest = [a for a in _all() if a["id"] != acc["id"]]
        _write("accounts.json", rest + [acc])


def remove(aid):
    with _lock:
        _write("accounts.json", [a for a in _all() if a["id"] != aid])


def _check(r, what):
    if r.status_code >= 400:
        raise RuntimeError(f"{what} failed ({r.status_code}): {r.text[:300]}")
    return r


# ------------------------------------------------------------------ YouTube
def youtube_ready() -> bool:
    return can_connect("youtube")


def _youtube_finish(code: str) -> dict:
    cid, secret = app_keys("google")
    tok = _check(httpx.post("https://oauth2.googleapis.com/token", data={
        "code": code, "client_id": cid, "client_secret": secret,
        "redirect_uri": redirect_for("youtube"), "grant_type": "authorization_code"}, timeout=30), "Google sign-in").json()
    if "refresh_token" not in tok:
        raise ValueError("Google did not return a long-lived sign-in. Remove PeakCut's access in your Google account and connect again.")
    ch = _check(httpx.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                          headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=30), "Reading the channel").json()
    if not ch.get("items"):
        raise ValueError("That Google account has no YouTube channel. Create one on YouTube, then connect again.")
    item = ch["items"][0]
    acc = {"id": f"yt_{item['id']}", "platform": "youtube", "name": item["snippet"]["title"], "added": time.time(),
           "refresh_token": tok["refresh_token"], "access_token": tok["access_token"],
           "expires": time.time() + tok.get("expires_in", 3600) - 60}
    _put(acc)
    return acc


def _yt_token(acc) -> str:
    if acc.get("expires", 0) > time.time():
        return acc["access_token"]
    cid, secret = app_keys("google")
    tok = _check(httpx.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": secret,
        "refresh_token": acc["refresh_token"], "grant_type": "refresh_token"}, timeout=30),
        "Refreshing the YouTube sign-in").json()
    acc.update(access_token=tok["access_token"], expires=time.time() + tok.get("expires_in", 3600) - 60)
    _put(acc)
    return acc["access_token"]


def youtube_upload(acc, path: Path, title: str, description: str, tags: list, privacy: str):
    token = _yt_token(acc)
    meta = {"snippet": {"title": re.sub(r"[<>]", "", title)[:100] or "Clip", "description": re.sub(r"[<>]", "", description)[:4900],
                        "tags": tags[:15], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False, "embeddable": True,
                       "publicStatsViewable": True, "license": "youtube", "containsSyntheticMedia": False}}
    size = path.stat().st_size
    start = _check(httpx.post("https://www.googleapis.com/upload/youtube/v3/videos",
                              params={"uploadType": "resumable", "part": "snippet,status"}, json=meta,
                              headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                                       "X-Upload-Content-Length": str(size)}, timeout=60), "Starting the YouTube upload")
    with path.open("rb") as f:
        done = _check(httpx.put(start.headers["Location"], content=f.read(),
                                headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, timeout=900),
                      "Uploading to YouTube").json()
    return done["id"], f"https://www.youtube.com/shorts/{done['id']}"


def youtube_states(acc, ids: list) -> dict:
    """{video id: (state, why)} for uploaded videos: "processed" once YouTube has finished with it and it can be
    watched, "uploaded" while YouTube is still processing, or "failed" / "rejected" / "deleted" (why: YouTube's reason)."""
    out = {}
    for i in range(0, len(ids), 50):
        r = _check(httpx.get("https://www.googleapis.com/youtube/v3/videos",
                             params={"part": "status,processingDetails", "id": ",".join(ids[i:i + 50])},
                             headers={"Authorization": f"Bearer {_yt_token(acc)}"}, timeout=30), "Checking the YouTube upload").json()
        seen = set()
        for it in r.get("items", []):
            st = it.get("status") or {}
            seen.add(it["id"])
            state = st.get("uploadStatus") or "uploaded"
            if (it.get("processingDetails") or {}).get("processingStatus") == "failed":
                state = "failed"
            out[it["id"]] = (state, st.get("failureReason") or st.get("rejectionReason") or "")
        for v in ids[i:i + 50]:
            if v not in seen:
                out[v] = ("deleted", "")
    return out


def youtube_views(acc, ids: list) -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        r = _check(httpx.get("https://www.googleapis.com/youtube/v3/videos",
                             params={"part": "statistics", "id": ",".join(ids[i:i + 50])},
                             headers={"Authorization": f"Bearer {_yt_token(acc)}"}, timeout=30), "Reading YouTube views").json()
        for it in r.get("items", []):
            out[it["id"]] = int(it["statistics"].get("viewCount", 0))
    return out


# ------------------------------------------------------------------ Instagram (Instagram Login, professional accounts)
def _instagram_finish_fb(code: str) -> dict:
    """Facebook login: the code becomes the person's access token (swapped for a 60-day one and renewed weekly), which
    names the Instagram accounts they shared; every one of them is connected, and the first is returned."""
    cid, secret = app_keys("instagram")
    tok = _check(httpx.get(f"{FB}/oauth/access_token", params={
        "client_id": cid, "client_secret": secret, "redirect_uri": redirect_for("instagram"), "code": code}, timeout=30),
        "Instagram sign-in").json()
    try:        # a person's token is swapped for a 60-day one; a business (system user) token never expires and stays as it is
        token = _fb_long(tok["access_token"], cid, secret)
    except Exception:  # noqa: BLE001
        token = tok["access_token"]
    ids, fb_user, scopes, page_ids = [], "", set(), []
    try:        # what was shared, from the token itself: Instagram accounts and Pages
        info = _check(httpx.get(f"{FB}/debug_token", params={"input_token": token, "access_token": f"{cid}|{secret}"}, timeout=30),
                      "Reading what was shared").json().get("data", {})
        fb_user = str(info.get("user_id") or "")
        scopes = set(info.get("scopes") or [])
        for g in info.get("granular_scopes") or []:
            sc = str(g.get("scope", ""))
            scopes.add(sc)
            if sc.startswith("instagram"):
                ids += [str(i) for i in g.get("target_ids") or []]
            elif sc.startswith("pages_"):
                page_ids += [str(i) for i in g.get("target_ids") or []]
    except Exception:  # noqa: BLE001
        pass
    fields = "instagram_business_account{id},connected_instagram_account{id}"
    pages = []
    try:        # the Pages the person manages, with the Instagram account linked to each
        pages = _check(httpx.get(f"{FB}/me/accounts", params={"fields": "id," + fields, "limit": 100, "access_token": token}, timeout=30),
                       "Reading the Instagram accounts").json().get("data", []) or []
    except Exception:  # noqa: BLE001
        pass
    seen = {str(p.get("id")) for p in pages}
    for pid in [p for p in dict.fromkeys(page_ids) if p not in seen][:20]:   # Pages shared through a business, not listed above
        try:
            pages.append(_check(httpx.get(f"{FB}/{pid}", params={"fields": "id," + fields, "access_token": token}, timeout=30),
                                "Reading a Page").json())
        except Exception:  # noqa: BLE001
            pass
    for p in pages:
        for k in ("instagram_business_account", "connected_instagram_account"):
            if (p.get(k) or {}).get("id"):
                ids.append(str(p[k]["id"]))
    ids = list(dict.fromkeys(ids))
    notes = []
    if not ids:     # a business (system user) sign-in: the Instagram account is an asset of that user, not behind a Page
        ids, notes = _ig_probe(token)
        if ids and ids[0] == "ig":      # only Instagram's own API knows this sign-in
            return _instagram_save(token)
    # what came back, for working out a failed connect (counts and permission names only, nothing personal)
    print(f"instagram connect: scopes={sorted(scopes)} pages={len(pages)} instagram={len(ids)} probe={notes}", flush=True)
    if not ids:
        why = f" (Meta shared: {', '.join(sorted(scopes)) or 'no permissions'}; Pages: {len(pages)}; {' '.join(notes)})"
        if scopes and not {"instagram_basic", "instagram_content_publish"} <= scopes:
            raise ValueError("Instagram was not shared. Connect again and leave every permission ticked." + why)
        if not pages:
            raise ValueError("No Instagram account was shared. Connect again and pick your Instagram account when Meta asks." + why)
        raise ValueError("The shared Facebook Page has no Instagram account linked. Link your Instagram Business or Creator "
                         "account to that Page (Instagram: Settings, Account type and tools, or the Page's settings), then connect again." + why)
    first = None
    for ig_id in ids[:5]:
        acc = _instagram_save_fb(token, ig_id, fb_user)
        first = first or acc
    return first


def _ig_probe(token: str):
    """Other places a sign-in can name its Instagram accounts. Returns (ids, notes): ids is ["ig"] when only
    Instagram's own API answers; notes say what each place returned (codes and counts only, nothing personal)."""
    ids, notes = [], []
    sub = "id,instagram_business_account{id},connected_instagram_account{id}"
    tries = [("me", f"{FB}/me", {"fields": "id"}),
             # where Meta's Unified login (every app made since late 2026) lists the Instagram accounts it shared
             ("assigned_ig", f"{FB}/me/assigned_instagram_accounts", {"fields": "id", "limit": 100}),
             ("assigned_pages", f"{FB}/me/assigned_pages", {"fields": sub}),
             ("ig_accounts", f"{FB}/me/instagram_accounts", {"fields": "id"}),
             ("ig_business", f"{FB}/me/instagram_business_accounts", {"fields": "id"})]
    for name, url, params in tries:
        try:
            r = httpx.get(url, params={**params, "access_token": token}, timeout=20)
        except Exception:  # noqa: BLE001
            notes.append(f"{name}:x"); continue
        try:
            j = r.json() or {}
        except Exception:  # noqa: BLE001
            j = {}
        if r.status_code >= 400:
            notes.append(f"{name}:{(j.get('error') or {}).get('code', r.status_code)}"); continue
        rows = j.get("data") if isinstance(j.get("data"), list) else None
        notes.append(f"{name}:{len(rows) if rows is not None else 'ok'}")
        for row in rows or []:
            if name == "assigned_pages":
                ids += [str((row.get(k) or {}).get("id")) for k in ("instagram_business_account", "connected_instagram_account")
                        if (row.get(k) or {}).get("id")]
            elif row.get("id"):
                ids.append(str(row["id"]))
    if not ids:
        try:
            r = httpx.get(f"{IG}/me", params={"fields": "user_id"}, headers={"Authorization": f"Bearer {token}"}, timeout=20)
            notes.append(f"instagram_api:{r.status_code}")
            if r.status_code < 400 and r.json().get("user_id"):
                return ["ig"], notes
        except Exception:  # noqa: BLE001
            notes.append("instagram_api:x")
    return list(dict.fromkeys(ids)), notes


def _fb_long(token: str, cid: str = "", secret: str = "") -> str:
    """A short-lived Facebook token swapped for a long-lived (60 day) one; the same call renews a long-lived one."""
    if not cid:
        cid, secret = app_keys("instagram")
    r = _check(httpx.get(f"{FB}/oauth/access_token", params={"grant_type": "fb_exchange_token", "client_id": cid,
                                                            "client_secret": secret, "fb_exchange_token": token}, timeout=30),
               "Keeping the Instagram sign-in").json()
    return r.get("access_token") or token


def _instagram_save_fb(token: str, ig_id: str, fb_user: str = "") -> dict:
    # account_type only exists on Instagram's own API; asking Facebook's for it fails the whole read
    me = _check(httpx.get(f"{FB}/{ig_id}", params={"fields": "id,username", "access_token": token}, timeout=30),
                "Reading the Instagram account").json()
    kind = str(me.get("account_type") or "")
    acc = {"id": f"ig_{me.get('id') or ig_id}", "platform": "instagram", "api": "facebook", "name": "@" + str(me.get("username") or ig_id),
           "added": time.time(), "user_id": str(me.get("id") or ig_id), "token": token, "token_time": time.time(),
           "fb_user": fb_user, "detail": {"BUSINESS": "Business account", "MEDIA_CREATOR": "Creator account"}.get(kind.upper(), "")}
    _put(acc)
    return acc


def meta_user(signed_request: str) -> str:
    """The Facebook user id inside a signed request from Meta (the removal and deletion callbacks), after checking
    it was signed with this app's secret. Raises ValueError if it was not."""
    try:
        sig, payload = (signed_request or "").split(".", 1)
        want = base64.urlsafe_b64encode(hmac.new(app_keys("instagram")[1].encode(), payload.encode(), hashlib.sha256).digest()).rstrip(b"=")
        if not hmac.compare_digest(sig.encode(), want):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return str(data["user_id"])
    except Exception:  # noqa: BLE001
        raise ValueError("That request was not signed by Meta.") from None


def forget_facebook_user(fb_user: str) -> int:
    """Remove every Instagram account connected by this Facebook person, for whoever in PeakCut connected it."""
    if not fb_user:
        return 0
    n = 0
    users = config.DATA_DIR / "users"
    for d in users.iterdir() if users.is_dir() else []:
        f = d / "accounts" / "accounts.json"
        try:
            items = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        keep = [a for a in items if not (a.get("platform") == "instagram" and a.get("fb_user") == fb_user)]
        if len(keep) != len(items):
            with _lock:
                f.write_text(json.dumps(keep, indent=1), encoding="utf-8")
            n += len(items) - len(keep)
    return n


def _instagram_finish(code: str) -> dict:
    if ig_config():
        return _instagram_finish_fb(code)
    cid, secret = app_keys("instagram")
    short = _check(httpx.post("https://api.instagram.com/oauth/access_token", data={
        "client_id": cid, "client_secret": secret, "grant_type": "authorization_code",
        "redirect_uri": redirect_for("instagram"), "code": code}, timeout=30), "Instagram sign-in").json()
    if isinstance(short.get("data"), list) and short["data"]:
        short = short["data"][0]
    if "instagram_business_content_publish" not in str(short.get("permissions", "instagram_business_content_publish")):
        raise ValueError("PeakCut needs permission to publish. Connect again and leave every box ticked.")
    long = _check(httpx.get(f"{IG}/access_token", params={"grant_type": "ig_exchange_token", "client_secret": secret,
                                                         "access_token": short["access_token"]}, timeout=30),
                  "Keeping the Instagram sign-in").json()
    return _instagram_save(long["access_token"])


def _instagram_save(token: str) -> dict:
    me = _check(httpx.get(f"{IG}/me", params={"fields": "user_id,username,account_type"},
                          headers={"Authorization": f"Bearer {token}"}, timeout=30), "Reading the Instagram account").json()
    kind = str(me.get("account_type") or "")
    acc = {"id": f"ig_{me['user_id']}", "platform": "instagram", "name": "@" + me["username"], "added": time.time(),
           "user_id": str(me["user_id"]), "token": token, "token_time": time.time(),
           "detail": {"BUSINESS": "Business account", "MEDIA_CREATOR": "Creator account"}.get(kind.upper(), "")}
    _put(acc)
    return acc


def instagram_add(token: str) -> dict:
    """An account added with a token pasted from the Meta app dashboard (older way; Connect is the normal one)."""
    return _instagram_save(token.strip())


def _ig_host(acc) -> str:
    return FB if acc.get("api") == "facebook" else IG


def _ig_token(acc) -> str:
    if acc.get("api") == "facebook":
        if time.time() - acc.get("token_time", 0) > 7 * 86400:      # renewed weekly, so it never lapses while PeakCut is used
            try:
                acc.update(token=_fb_long(acc["token"]), token_time=time.time())
                _put(acc)
            except Exception:  # noqa: BLE001 - keep using the current token until it truly expires
                pass
        return acc["token"]
    if time.time() - acc.get("token_time", 0) > 7 * 86400:  # tokens last 60 days; refresh weekly
        try:
            r = _check(httpx.get(f"{IG}/refresh_access_token", params={"grant_type": "ig_refresh_token"},
                                 headers={"Authorization": f"Bearer {acc['token']}"}, timeout=30), "Refreshing").json()
            acc.update(token=r["access_token"], token_time=time.time())
            _put(acc)
        except Exception:  # noqa: BLE001 - keep using the current token until it truly expires
            pass
    return acc["token"]


def reels_audio_enabled(acc):
    return bool(acc and acc.get("platform") == "instagram" and acc.get("api") == "facebook"
                and not acc.get("via") and acc.get("token"))


def _reels_audio_get(acc, endpoint, params=None):
    if not reels_audio_enabled(acc):
        raise ValueError("Instagram audio requires a direct Instagram account connected through Facebook Login.")
    try:
        response = httpx.get(f"{IG_AUDIO}/{endpoint}", params={"user_id": acc["user_id"], **(params or {})},
                             headers={"Authorization": f"Bearer {_ig_token(acc)}"}, timeout=12)
        result = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError("Instagram’s audio catalog is temporarily unavailable.") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Instagram’s audio catalog returned an unexpected response.")
    if response.status_code >= 400 or result.get("error"):
        error = result.get("error") or {}
        if response.status_code in (401, 403) or isinstance(error, dict) and error.get("code") in (10, 190, 200):
            raise ValueError("Instagram denied access to audio. Check the Facebook Login connection and its publishing permissions.")
        raise RuntimeError("Instagram could not load audio for this account.")
    return result


def _reels_audio_item(row):
    if not isinstance(row, dict):
        return None
    audio_id = str(row.get("audio_id") or "")
    if not re.fullmatch(r"[0-9]{1,25}", audio_id):
        return None
    preview = preview_audio = ""
    try:
        raw = str(row.get("on_platform_audio_preview_link") or "")
        url = urlsplit(raw)
        if url.scheme == "https" and url.netloc in ("www.instagram.com", "instagram.com"):
            preview = raw
        raw_audio = str(row.get("download_url") or "")
        audio_url = urlsplit(raw_audio)
        if audio_url.scheme == "https" and audio_url.netloc.endswith((".cdninstagram.com", ".fbcdn.net")):
            preview_audio = raw_audio
    except ValueError:
        pass
    return {"audio_id": audio_id, "title": str(row.get("title") or "Original audio")[:150],
            "artist": str(row.get("display_artist") or row.get("ig_username") or "")[:100],
            "preview": preview, "preview_audio": preview_audio}


def reels_audio_search(acc, kind, query):
    if kind not in ("music", "original_sound"):
        raise ValueError("Choose music or original sounds.")
    params = {"audio_type": kind}
    if query.strip():
        params["search_query"] = query.strip()[:100]
    data = _reels_audio_get(acc, "ig_audio", params)
    rows = data.get("audio")
    return [item for row in (rows[:40] if isinstance(rows, list) else []) if (item := _reels_audio_item(row))]


def reels_audio_choice(acc, selection):
    audio_id = selection.get("audio_id")
    if not isinstance(audio_id, str) or not re.fullmatch(r"[0-9]{1,25}", audio_id):
        raise ValueError("Choose an audio track from Instagram.")
    levels = {}
    for key, default in (("audio_volume", 20), ("video_volume", 100)):
        value = selection.get(key, default)
        if type(value) is not int or not 1 <= value <= 100:
            raise ValueError("Instagram audio levels must be between 1 and 100.")
        levels[key] = value
    info = _reels_audio_item(_reels_audio_get(acc, audio_id))
    if not info or info["audio_id"] != audio_id:
        raise ValueError("Instagram did not confirm that this track is available for your account.")
    return {"account": acc["id"], "audio_id": audio_id, "title": info["title"], "artist": info["artist"], **levels}


def instagram_upload(acc, path: Path, caption: str, job_id: str = "", *, audio=None):
    """A Reel. On a public site Instagram fetches the clip from a one-hour signed link; on a computer it cannot
    reach, the file is sent instead (only possible for some app types)."""
    if audio and (not reels_audio_enabled(acc) or audio.get("account") != acc["id"]):
        raise RuntimeError("This Instagram connection can no longer use the selected Reel audio.")
    token = _ig_token(acc)
    auth = {"Authorization": f"Bearer {token}"}
    uid, host = acc["user_id"], _ig_host(acc)
    data = {"media_type": "REELS", "caption": caption[:2200], "share_to_feed": "true"}
    upload = IG_UPLOAD
    if audio:
        host, upload = IG_AUDIO, IG_UPLOAD.replace("/v21.0", "/v22.0")
        data["audio_configuration"] = json.dumps({k: audio[k] for k in ("audio_id", "audio_volume", "video_volume")})
    reachable = config.PUBLIC_URL.startswith("https://") and not any(h in config.PUBLIC_URL for h in ("localhost", "127.0.0.1", "[::1]", ".local", ".test"))
    if reachable and job_id:
        data["video_url"] = clip_link(job_id, path.name)
        box = _check(httpx.post(f"{host}/{uid}/media", data=data, headers=auth, timeout=60), "Starting the Instagram post").json()
    else:
        data["upload_type"] = "resumable"
        box = _check(httpx.post(f"{host}/{uid}/media", data=data, headers=auth, timeout=60), "Starting the Instagram upload").json()
        with path.open("rb") as f:
            _check(httpx.post(f"{upload}/{box['id']}", content=f.read(),
                              headers={"Authorization": f"OAuth {token}", "offset": "0",
                                       "file_size": str(path.stat().st_size)}, timeout=900),
                   "Uploading to Instagram (posting from a computer needs a public https address)")
    for _ in range(90):
        st = _check(httpx.get(f"{host}/{box['id']}", params={"fields": "status_code,status"}, headers=auth, timeout=30),
                    "Checking the Instagram upload").json()
        if st.get("status_code") in ("FINISHED", "PUBLISHED"):
            break
        if st.get("status_code") in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram rejected the video: {st.get('status', st['status_code'])}")
        time.sleep(6)
    else:
        raise RuntimeError("Instagram took too long to process the video.")
    pub = _check(httpx.post(f"{host}/{uid}/media_publish", data={"creation_id": box["id"]}, headers=auth, timeout=60),
                 "Publishing on Instagram").json()
    link = ""
    try:
        link = httpx.get(f"{host}/{pub['id']}", params={"fields": "permalink"}, headers=auth, timeout=30).json().get("permalink", "")
    except Exception:  # noqa: BLE001
        pass
    return pub["id"], link


def composio_import(platform: str) -> list:
    """Copy the Instagram account, or each Facebook Page, already connected in Composio into this user's accounts."""
    from . import composio_post
    if platform not in ("instagram", "facebook"):
        raise ValueError("Unknown platform.")
    if not composio_post.ready():
        raise ValueError("Add a Composio API key in Settings, Connected apps, then press Connect again.")
    if not _server_admin():
        raise ValueError("Only the person who runs this server can connect accounts through Composio.")
    linked = composio_post.connections(platform)
    if not linked:
        name = PLATFORM_NAMES[platform]
        raise ValueError(f"No {name} account is connected in Composio yet. Connect it there, then press Connect again.")
    names = []
    if platform == "instagram":
        for conn in linked:
            me = composio_post.instagram_profile(conn["id"], user_id=conn.get("user_id") or "")
            uid, username = str(me.get("id") or ""), str(me.get("username") or "")
            if not uid or not username:
                raise RuntimeError("Composio did not return the Instagram account.")
            kind = str(me.get("account_type") or "").upper()
            if kind and kind not in ("BUSINESS", "MEDIA_CREATOR"):
                raise ValueError(f"@{username} is a personal Instagram account. Switch it to Creator or Business, then connect again.")
            _put({"id": f"ig_{uid}", "platform": "instagram", "name": "@" + username, "added": time.time(),
                  "user_id": uid, "via": "composio", "composio_account": conn["id"],
                  "composio_user_id": conn.get("user_id") or "", "detail": "Posted through Composio"})
            names.append("@" + username)
        return names
    for conn in linked:
        pages = [p for p in composio_post.facebook_pages(conn["id"], user_id=conn.get("user_id") or "") if _page_can_post(p)]
        for page in pages:
            pid, label = str(page.get("id") or ""), str(page.get("name") or "Facebook Page")
            if not pid:
                continue
            _put({"id": f"fb_{pid}", "platform": "facebook", "name": label, "added": time.time(),
                  "page_id": pid, "via": "composio", "composio_account": conn["id"],
                  "composio_user_id": conn.get("user_id") or "", "detail": "Posted through Composio"})
            names.append(label)
    if not names:
        raise ValueError("That Facebook login cannot post to any Page. In Composio, reconnect Facebook and allow the Page.")
    return names


def _page_can_post(page: dict) -> bool:
    tasks = page.get("tasks")
    if not tasks:
        return bool(page.get("id"))
    return "CREATE_CONTENT" in tasks or "MANAGE" in tasks


def instagram_views(acc, media_id: str):
    if acc.get("via") == "composio":
        from . import composio_post
        return composio_post.instagram_views(acc, media_id)
    r = httpx.get(f"{_ig_host(acc)}/{media_id}/insights", params={"metric": "views"},
                  headers={"Authorization": f"Bearer {_ig_token(acc)}"}, timeout=30)
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    return int(data[0]["values"][0]["value"]) if data else None


# ------------------------------------------------------------------ TikTok (Login Kit + Content Posting API)
def _tt(r, what):
    """TikTok answers with an "error" object even on success; anything but "ok" is a failure."""
    try:
        data = r.json()
    except ValueError:
        data = {}
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    if r.status_code >= 400 or (err and err.get("code") not in (None, "ok")) or (isinstance(data.get("error"), str)):
        code = err.get("code") or data.get("error") or r.status_code
        msg = err.get("message") or data.get("error_description") or r.text[:200]
        e = RuntimeError(f"{what} failed ({code}): {msg}")
        e.code = code
        raise e
    return data


def _tt_tokens(form: dict) -> dict:
    cid, secret = app_keys("tiktok")
    return _tt(httpx.post(f"{TT}/oauth/token/", data={"client_key": cid, "client_secret": secret, **form},
                          headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=30), "TikTok sign-in")


def _tiktok_finish(code: str) -> dict:
    tok = _tt_tokens({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_for("tiktok")})
    if "video.publish" not in str(tok.get("scope", "")):
        raise ValueError("PeakCut needs permission to post videos. Connect again and allow posting.")
    acc = {"id": f"tt_{tok['open_id']}", "platform": "tiktok", "name": "TikTok account", "added": time.time(),
           "open_id": tok["open_id"], "access_token": tok["access_token"], "refresh_token": tok["refresh_token"],
           "expires": time.time() + int(tok.get("expires_in", 7200)) - 120}
    try:
        info = _tiktok_creator(acc)
        acc["name"] = "@" + info["creator_username"] if info.get("creator_username") else info.get("creator_nickname") or acc["name"]
    except Exception:  # noqa: BLE001 - the name is only for showing
        pass
    _put(acc)
    return acc


def _tt_token(acc) -> str:
    if acc.get("expires", 0) > time.time():
        return acc["access_token"]
    tok = _tt_tokens({"grant_type": "refresh_token", "refresh_token": acc["refresh_token"]})
    # every refresh hands out a new refresh token and retires the old one: keep the new one straight away
    acc.update(access_token=tok["access_token"], refresh_token=tok.get("refresh_token", acc["refresh_token"]),
               expires=time.time() + int(tok.get("expires_in", 7200)) - 120)
    _put(acc)
    return acc["access_token"]


def _tt_post(acc, path_, body, what):
    return _tt(httpx.post(f"{TT}{path_}", json=body, timeout=60,
                          headers={"Authorization": f"Bearer {_tt_token(acc)}", "Content-Type": "application/json; charset=UTF-8"}),
               what).get("data") or {}


def _tiktok_creator(acc) -> dict:
    return _tt_post(acc, "/post/publish/creator_info/query/", {}, "Reading the TikTok account")


# PeakCut's visibility setting -> what TikTok calls it, best first
TT_PRIVACY = {"public": ["PUBLIC_TO_EVERYONE", "FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS"],
              "unlisted": ["MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR"], "private": ["SELF_ONLY"]}


def _tt_chunks(size: int) -> tuple:
    """(chunk size, how many). Up to 64 MB goes in one piece; bigger files in 10 MB pieces, the last one taking the rest."""
    if size <= 64_000_000:
        return size, 1
    return 10_000_000, size // 10_000_000


def tiktok_upload(acc, path: Path, caption: str, privacy: str = "public"):
    info = _tiktok_creator(acc)
    options = info.get("privacy_level_options") or ["SELF_ONLY"]
    level = next((p for p in TT_PRIVACY.get(privacy, ["SELF_ONLY"]) if p in options), "SELF_ONLY" if "SELF_ONLY" in options else options[0])
    size = path.stat().st_size
    chunk, count = _tt_chunks(size)

    def init(lv):
        return _tt_post(acc, "/post/publish/video/init/", {
            "post_info": {"title": caption[:2200], "privacy_level": lv, "disable_duet": False, "disable_comment": False,
                          "disable_stitch": False, "video_cover_timestamp_ms": 1000},
            "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}},
            "Starting the TikTok post")
    note = ""
    try:
        box = init(level)
    except RuntimeError as e:
        # A TikTok app that has not passed TikTok's review can only post privately: post privately, and say so.
        if "unaudited" not in str(getattr(e, "code", "")) or level == "SELF_ONLY" or "SELF_ONLY" not in options:
            raise
        level, note = "SELF_ONLY", "Posted as private: this server's TikTok app hasn't passed TikTok's review yet."
        box = init(level)
    with path.open("rb") as f:
        for n in range(count):
            first = n * chunk
            last = size - 1 if n == count - 1 else first + chunk - 1
            part = f.read(last - first + 1)
            r = httpx.put(box["upload_url"], content=part, timeout=900, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(part)), "Content-Range": f"bytes {first}-{last}/{size}"})
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"Uploading to TikTok failed ({r.status_code}): {r.text[:200]}")
    for _ in range(120):
        st = _tt_post(acc, "/post/publish/status/fetch/", {"publish_id": box["publish_id"]}, "Checking the TikTok post")
        if st.get("status") == "PUBLISH_COMPLETE":
            break
        if st.get("status") == "FAILED":
            raise RuntimeError(f"TikTok did not publish the video: {st.get('fail_reason') or 'no reason given'}")
        time.sleep(5)
    else:
        raise RuntimeError("TikTok took too long to publish the video.")
    ids = st.get("publicaly_available_post_id") or st.get("publicly_available_post_id") or []
    user = info.get("creator_username") or ""
    url = f"https://www.tiktok.com/@{user}/video/{ids[0]}" if ids and user else ""
    if note:
        acc["detail"] = "Posts are private until this server's TikTok app is reviewed"
        _put(acc)
    return str(ids[0]) if ids else box["publish_id"], url
