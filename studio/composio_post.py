"""Post finished clips through Composio, to the Instagram and Facebook accounts connected there.

Composio holds the sign-in. PeakCut never talks to Meta's own developer app for these accounts: it uploads the
clip to Composio, and Composio publishes it. The API key belongs to the server (the environment, or a file in
the data folder). It is never sent back to the browser.

A project key (ak_...) uses Composio's API directly. A Connect key (ck_...), the one from Composio's own
Connect page, uses the same connection the person already made there.
"""
import hashlib
import json
import re
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config

API = "https://backend.composio.dev/api/v3.1"
MCP = "https://connect.composio.dev/mcp"


def _saved_file() -> Path:
    return config.DATA_DIR / "composio.key"


def from_env() -> bool:
    return bool(config.composio_key())


def key() -> str:
    got = config.composio_key()
    if got:
        return got
    try:
        return _saved_file().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def ready() -> bool:
    return bool(key())


def _connect_key() -> bool:
    """True for a Composio Connect key. Those reach the accounts connected in Composio's own Connect page."""
    return key().startswith("ck_")


def save_key(value: str) -> None:
    """Remember a key pasted in Settings. A key already set in the environment is left as it is."""
    if from_env():
        raise ValueError("The Composio key is set in the .env file. Change it there.")
    value = str(value or "").strip()
    if not re.fullmatch(r"\S{12,300}", value):
        raise ValueError("Paste the API key from the Composio dashboard, under API keys.")
    path = _saved_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(value, encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def clear_key() -> None:
    if from_env():
        raise ValueError("The Composio key is set in the .env file. Remove it there.")
    _saved_file().unlink(missing_ok=True)


def _error_text(body, response) -> str:
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err.get("slug") or "Composio refused that.")
    if isinstance(err, str) and err:
        return err
    text = (response.text or "").strip()
    return text[:240] or f"Composio answered {response.status_code}."


def _request(method: str, path: str, *, params=None, body=None, timeout=60) -> dict:
    if not key():
        raise RuntimeError("Composio isn't set up on this server yet.")
    response = httpx.request(method, API + path, params=params, json=body, timeout=timeout,
                             headers={"x-api-key": key()})
    try:
        got = response.json()
    except ValueError:
        got = {}
    if response.status_code >= 400:
        raise RuntimeError(f"Composio failed ({response.status_code}): {_error_text(got, response)}")
    return got if isinstance(got, dict) else {}


def _tool_data(body: dict) -> dict:
    """The useful part of an execute answer. Composio sometimes wraps the platform's answer once more."""
    if body.get("successful") is False:
        raise RuntimeError(str(body.get("error") or "Composio could not finish that."))
    data = body.get("data")
    if isinstance(data, dict) and data.get("successful") is False:
        raise RuntimeError(str(data.get("error") or "The platform refused that."))
    if isinstance(data, dict) and "successful" in data and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else {}


def _hide(text: str) -> str:
    got = key()
    return text.replace(got, "[key]") if got else text


def _mcp(name: str, arguments: dict, timeout: int = 120) -> dict:
    """One call on Composio Connect, which is where a ck_ key's Instagram and Facebook logins live."""
    response = httpx.post(MCP, timeout=timeout, headers={
        "x-consumer-api-key": key(), "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream"}, json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    if response.status_code >= 400:
        raise RuntimeError(f"Composio failed ({response.status_code}): {_hide(response.text[:240])}")
    payload = None
    for line in response.text.splitlines():
        if line.startswith("data:"):
            try:
                payload = json.loads(line[5:].strip())
            except ValueError:
                payload = None
    if not isinstance(payload, dict):
        raise RuntimeError("Composio did not answer.")
    if payload.get("error"):
        raise RuntimeError(_hide(str(payload["error"])[:300]))
    texts = [c.get("text", "") for c in ((payload.get("result") or {}).get("content") or []) if c.get("type") == "text"]
    if not texts:
        raise RuntimeError("Composio returned nothing.")
    try:
        body = json.loads(texts[0])
    except ValueError:
        raise RuntimeError(_hide(texts[0][:300]))
    if isinstance(body, dict) and body.get("successful") is False:
        raise RuntimeError(_hide(str(body.get("error") or "Composio could not finish that."))[:300])
    return body if isinstance(body, dict) else {}


def execute(slug: str, arguments: dict, account_id: str, timeout: int = 120, *, user_id: str = "") -> dict:
    if _connect_key():
        body = _mcp("COMPOSIO_MULTI_EXECUTE_TOOL", {
            "tools": [{"tool_slug": slug, "arguments": arguments, "account": account_id}],
            "sync_response_to_workbench": False, "thought": "PeakCut is using the connected account.",
            "current_step": "POSTING"}, timeout=timeout)
        results = (body.get("data") or {}).get("results") if isinstance(body.get("data"), dict) else None
        if not results:
            raise RuntimeError("Composio did not return a result.")
        first = results[0] if isinstance(results[0], dict) else {}
        if first.get("error"):
            raise RuntimeError(_hide(str(first["error"]))[:300])
        response = first.get("response") if isinstance(first.get("response"), dict) else first
        return _tool_data(response)
    if not user_id:
        user_id = str(_request("GET", f"/connected_accounts/{account_id}").get("user_id") or "")
    if not user_id:
        raise RuntimeError("Composio did not provide the connected account's user ID. Refresh the connection in PeakCut.")
    body = _request("POST", f"/tools/execute/{slug}", timeout=timeout, body={
        "connected_account_id": account_id, "user_id": user_id, "arguments": arguments})
    return _tool_data(body)


def _pick_id(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    if data.get("id"):
        return str(data["id"])
    inner = data.get("data")
    if isinstance(inner, dict) and inner.get("id"):
        return str(inner["id"])
    return ""


def connections(platform: str) -> list:
    """Active Composio connections for instagram or facebook."""
    if _connect_key():
        body = _mcp("COMPOSIO_MANAGE_CONNECTIONS", {"toolkits": [{"name": platform, "action": "list"}]})
        info = ((body.get("data") or {}).get("results") or {}).get(platform) or {}
        return [{"id": acc["id"], "status": "ACTIVE", "is_disabled": False, "toolkit": {"slug": platform}}
                for acc in (info.get("accounts") or [])
                if str(acc.get("status", "")).lower() == "active" and acc.get("id")]
    found, cursor = [], None
    for _ in range(10):
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        page = _request("GET", "/connected_accounts", params=params)
        for item in page.get("items") or []:
            slug = str((item.get("toolkit") or {}).get("slug") or "").lower()
            if slug == platform and item.get("status") == "ACTIVE" and not item.get("is_disabled") and item.get("id"):
                found.append(item)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return found


def instagram_profile(account_id: str, *, user_id: str = "") -> dict:
    return execute("INSTAGRAM_GET_USER_INFO", {"ig_user_id": "me"}, account_id, user_id=user_id)


def facebook_pages(account_id: str, *, user_id: str = "") -> list:
    pages, seen = [], set()
    after = None
    for _ in range(10):
        args = {"user_id": "me", "limit": 25, "fields": "id,name,tasks,link"}
        if after:
            args["after"] = after
        got = execute("FACEBOOK_LIST_MANAGED_PAGES", args, account_id, user_id=user_id)
        rows = got.get("data") if isinstance(got.get("data"), list) else []
        pages.extend(rows)
        after = ((got.get("paging") or {}).get("cursors") or {}).get("after")
        if not rows or not after or after in seen:
            break
        seen.add(after)
    return pages


def upload(path: Path, toolkit: str, tool: str) -> dict:
    """Put the clip where Composio can hand it to Instagram or Facebook. Returns the file reference they expect."""
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    meta = _request("POST", "/files/upload/request", body={
        "toolkit_slug": toolkit, "tool_slug": tool, "filename": path.name,
        "mimetype": "video/mp4", "md5": digest.hexdigest()})
    url, s3key = meta.get("new_presigned_url"), meta.get("key")
    if not url or not s3key:
        raise RuntimeError("Composio did not accept the video file.")
    headers = {"Content-Type": "video/mp4"}
    if (meta.get("metadata") or {}).get("storage_backend") == "azure_blob_storage":
        headers["x-ms-blob-type"] = "BlockBlob"
    with path.open("rb") as handle:
        sent = httpx.put(url, content=handle.read(), headers=headers, timeout=900)
    if sent.status_code >= 400:
        raise RuntimeError(f"Sending the video to Composio failed ({sent.status_code}).")
    return {"name": path.name, "mimetype": "video/mp4", "s3key": s3key}


class ConnectVideoSourceError(RuntimeError):
    """The server needs a reachable URL before a Connect account can publish video."""


def _video_source(path, job_id, toolkit, tool, file_field, url_field):
    if not _connect_key():
        return {file_field: upload(path, toolkit, tool)}

    from .accounts import clip_link

    address = urlsplit(config.PUBLIC_URL)
    host = (address.hostname or "").rstrip(".").lower()
    try:
        public_host = ip_address(host).is_global
    except ValueError:
        public_host = "." in host and not host.endswith((".local", ".localhost", ".test", ".internal"))
    if not job_id or address.scheme != "https" or not public_host:
        raise ConnectVideoSourceError(
            "Composio Connect needs a public HTTPS PeakCut address to fetch the clip. "
            "For local uploads, use a Composio project API key and connect your Instagram and Facebook accounts in that project."
        )
    return {url_field: clip_link(job_id, path.name)}


def post_instagram(acc: dict, path: Path, caption: str, job_id: str = "") -> tuple:
    """A Reel on the connected Instagram account. Returns (media id, link)."""
    account_id = acc["composio_account"]
    user_id = acc.get("composio_user_id") or ""
    source = _video_source(path, job_id, "instagram", "INSTAGRAM_POST_IG_USER_MEDIA", "video_file", "video_url")
    created = execute("INSTAGRAM_POST_IG_USER_MEDIA", {
        "ig_user_id": acc["user_id"], "caption": caption[:2200], **source,
        "media_type": "REELS", "share_to_feed": True}, account_id, user_id=user_id)
    creation = _pick_id(created)
    if not creation:
        raise RuntimeError("Instagram did not accept the video.")
    published = execute("INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", {
        "ig_user_id": acc["user_id"], "creation_id": creation, "max_wait_seconds": 300}, account_id, timeout=360, user_id=user_id)
    media_id = _pick_id(published) or creation
    link = ""
    try:
        info = execute("INSTAGRAM_GET_IG_MEDIA", {"ig_media_id": media_id, "fields": "id,permalink"}, account_id, user_id=user_id)
        link = str(info.get("permalink") or "")
    except Exception:  # noqa: BLE001 - the post is up; the link is only for the clip card
        pass
    return media_id, link


def instagram_views(acc: dict, media_id: str):
    try:
        info = execute("INSTAGRAM_GET_IG_MEDIA", {"ig_media_id": media_id, "fields": "id,view_count"},
                       acc["composio_account"], user_id=acc.get("composio_user_id") or "")
        return int(info["view_count"]) if info.get("view_count") is not None else None
    except Exception:  # noqa: BLE001
        return None


def post_facebook(acc: dict, path: Path, title: str, description: str, job_id: str = "") -> tuple:
    """A video on a Facebook Page. Returns (post id, link)."""
    account_id = acc["composio_account"]
    user_id = acc.get("composio_user_id") or ""
    source = _video_source(path, job_id, "facebook", "FACEBOOK_CREATE_VIDEO_POST", "video", "file_url")
    made = execute("FACEBOOK_CREATE_VIDEO_POST", {
        "page_id": acc["page_id"], "title": (title or "Clip")[:250], "description": description[:5000],
        **source, "published": True}, account_id, timeout=360, user_id=user_id)
    if made.get("unsuccessful"):
        raise RuntimeError("Facebook did not publish the video.")
    remote = _pick_id(made)
    if not remote:
        raise RuntimeError("Facebook did not publish the video.")
    link = str(made.get("permalink_url") or "")
    if not link:
        try:
            posts = execute("FACEBOOK_GET_PAGE_POSTS", {
                "page_id": acc["page_id"], "limit": 5, "fields": "id,permalink_url"}, account_id, user_id=user_id)
            rows = posts.get("data") if isinstance(posts.get("data"), list) else []
            match = next((row for row in rows if remote in str(row.get("id", ""))), None)
            link = str((match or (rows[0] if rows else {})).get("permalink_url") or "")
        except Exception:  # noqa: BLE001
            link = ""
    if link.startswith("/"):
        link = "https://www.facebook.com" + link
    return remote, link
