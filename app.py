import io
import os
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import requests
from flask import Flask, Response, jsonify, request
from PIL import Image

app = Flask(__name__)


INFO_API = "https://out-bar-info.vercel.app/info?uid={uid}"
ICON_API = "https://iconapi.wasmer.app/{}"
CONNECT_TIMEOUT = 5
READ_TIMEOUT = 25

DEFAULT_BANNER_ID = "900000014"
DEFAULT_AVATAR_ID = "900000013"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRAME_PATH = os.path.join(BASE_DIR, "frame.png")
OUTPUT_WIDTH = 1900

AVATAR_BOX = (66, 91, 570, 704)
BANNER_BOX = (600, 91, 1850, 704)
UA = {"User-Agent": "BannerAPI/1.1"}


def _norm(key: Any) -> str:
    return str(key).lower().replace("_", "").replace("-", "")


def _scalar_id(value: Any):
    """Normalize an icon ID from strings/numbers and common object wrappers."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        value = str(value).strip()
        if value and value != "0" and value.lower() not in {"none", "null", "false", ""}:
            return value
    if isinstance(value, dict):
        # APIs sometimes wrap IDs as {"id": 123} or {"value": 123}.
        for k, v in value.items():
            if _norm(k) in {"id", "value", "itemid", "resourceid"}:
                found = _scalar_id(v)
                if found:
                    return found
    return None


def find_value(data: Any, names: set[str]):
    """Find known fields recursively, including JSON encoded inside strings."""
    if isinstance(data, str):
        stripped = data.strip()
        if stripped.startswith(("{", "[")):
            try:
                import json
                return find_value(json.loads(stripped), names)
            except (ValueError, TypeError):
                return None
        return None
    if isinstance(data, dict):
        # Exact known keys first, so generic keys like `banner` don't win early.
        for key, value in data.items():
            if _norm(key) in names:
                found = _scalar_id(value)
                if found is not None:
                    return found
        # Some APIs use keys such as equippedBanner or accountAvatarId.
        for key, value in data.items():
            nk = _norm(key)
            if any(nk.endswith(name) or nk.startswith(name) for name in names if len(name) >= 6):
                found = _scalar_id(value)
                if found is not None:
                    return found
        for value in data.values():
            found = find_value(value, names)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_value(value, names)
            if found is not None:
                return found
    return None


BANNER_KEYS = {
    "bannerid", "accountbannerid", "currentbannerid", "equippedbannerid",
    "banner", "accountbanner", "currentbanner", "equippedbanner",
    "banneridvalue", "bannerresourceid", "profilebannerid", "selectedbannerid"
}
AVATAR_KEYS = {
    "avatarid", "accountavatarid", "currentavatarid", "equippedavatarid",
    "headpic", "headpicid", "avatar", "accountavatar", "currentavatar",
    "equippedavatar", "profilepic", "profilepicid", "avatarresourceid",
    "selectedavatarid", "characteravatarid", "portraitid"
}
NAME_KEYS = {"nickname", "accountname", "playername", "name"}
LEVEL_KEYS = {"level", "accountlevel", "playerlevel"}


def extract_info(data: Any) -> dict:
    banner = find_value(data, BANNER_KEYS)
    avatar = find_value(data, AVATAR_KEYS)
    return {
        "banner_id": banner or DEFAULT_BANNER_ID,
        "avatar_id": avatar or DEFAULT_AVATAR_ID,
        "name": find_value(data, NAME_KEYS) or "",
        "level": find_value(data, LEVEL_KEYS) or "",
        "banner_default": banner is None,
        "avatar_default": avatar is None,
    }


@lru_cache(maxsize=512)
def _cached_icon_bytes(item_id: str):
    """Cache only successful image bytes; failures are never cached."""
    try:
        response = requests.get(
            ICON_API.format(item_id),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            headers=UA,
        )
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "").lower()
        if "image" not in content_type and not response.content.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF")):
            return None
        with Image.open(io.BytesIO(response.content)) as check:
            check.verify()
        return response.content
    except (requests.RequestException, OSError, ValueError):
        return None


def fetch_icon_bytes(item_id: str):
    # Don't cache temporary failures: retry on the next API request.
    result = _cached_icon_bytes(str(item_id))
    if result is None:
        _cached_icon_bytes.cache_clear()
    return result


def get_icon(item_id: str, fallback_id: str):
    """item_id না পেলে default id দিয়ে আবার চেষ্টা করে।"""
    for iid in dict.fromkeys((str(item_id), str(fallback_id))):
        raw = fetch_icon_bytes(str(iid))
        if raw:
            try:
                with Image.open(io.BytesIO(raw)) as im:
                    return im.convert("RGBA"), str(iid)
            except (OSError, ValueError):
                pass
    return None, str(item_id)


@lru_cache(maxsize=1)
def load_frame() -> Image.Image:
    with Image.open(FRAME_PATH) as im:
        return im.convert("RGBA")


@lru_cache(maxsize=1)
def frame_bbox():
    return load_frame().getchannel("A").point(lambda v: 255 if v > 40 else 0).getbbox()


def cover(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """ছবি crop করে পুরো বক্স ভরে ফেলে (CSS 'cover')।"""
    w, h = size
    scale = max(w / img.width, h / img.height)
    nw, nh = int(img.width * scale) + 1, int(img.height * scale) + 1
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def make_banner(info: dict):
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_banner = pool.submit(get_icon, info["banner_id"], DEFAULT_BANNER_ID)
        f_avatar = pool.submit(get_icon, info["avatar_id"], DEFAULT_AVATAR_ID)
        banner_img, banner_used = f_banner.result()
        avatar_img, avatar_used = f_avatar.result()

    frame = load_frame()
    canvas = Image.new("RGBA", frame.size, (0, 0, 0, 0))

    for box, img in ((AVATAR_BOX, avatar_img), (BANNER_BOX, banner_img)):
        if img is None:
            continue
        x0, y0, x1, y1 = box
        canvas.alpha_composite(cover(img, (x1 - x0, y1 - y0)), (x0, y0))

    canvas.alpha_composite(frame)
    canvas = canvas.crop(frame_bbox())

    if OUTPUT_WIDTH and OUTPUT_WIDTH != canvas.width:
        h = round(canvas.height * OUTPUT_WIDTH / canvas.width)
        canvas = canvas.resize((OUTPUT_WIDTH, h), Image.Resampling.LANCZOS)

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue(), banner_used, avatar_used


@app.get("/")
def home():
    return jsonify({
        "status": "online",
        "endpoint": "/api?uid=PLAYER_UID",
        "legacy_endpoint": "/banner?uid=PLAYER_UID",
        "json_debug": "/api?uid=PLAYER_UID&format=json",
        "icon_debug": "/icon-check?id=ICON_ID",
        "default_banner_id": DEFAULT_BANNER_ID,
        "default_avatar_id": DEFAULT_AVATAR_ID,
        "info_api_timeout_seconds": READ_TIMEOUT,
        "icon_api_timeout_seconds": READ_TIMEOUT,
    })


@app.get("/icon-check")
def icon_check():
    """Check what the icon provider returns for a specific ID."""
    item_id = (request.args.get("id") or "").strip()
    if not item_id or len(item_id) > 40:
        return jsonify({"error": "Provide an icon ID using ?id=..."}), 400
    url = ICON_API.format(item_id)
    try:
        response = requests.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), headers=UA)
        content_type = response.headers.get("Content-Type", "")
        valid_image = False
        if response.ok:
            try:
                with Image.open(io.BytesIO(response.content)) as im:
                    im.verify()
                valid_image = True
            except (OSError, ValueError):
                pass
        return jsonify({
            "id": item_id,
            "url": url,
            "http_status": response.status_code,
            "content_type": content_type,
            "valid_image": valid_image,
            "bytes": len(response.content),
        }), (200 if response.ok else 502)
    except requests.RequestException as exc:
        return jsonify({"id": item_id, "url": url, "error": str(exc)}), 502


@app.get("/api")
@app.get("/banner")
def banner():
    uid = (request.args.get("uid") or "").strip()
    if not uid:
        return jsonify({"error": "Missing required query parameter: uid"}), 400
    if not uid.isdigit() or len(uid) > 24:
        return jsonify({"error": "UID must contain 1-24 digits"}), 400

    # Info API is optional: if it fails or returns unusable data, use default IDs.
    try:
        r = requests.get(INFO_API.format(uid=uid), timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), headers=UA)
        r.raise_for_status()
        data = r.json()
        # The source payload can be inspected with ?debug=source if field names differ.
        info = extract_info(data)
    except (requests.RequestException, ValueError) as exc:
        app.logger.warning("Info API unavailable for UID %s; using default icons: %s", uid, exc)
        info = {
            "banner_id": DEFAULT_BANNER_ID,
            "avatar_id": DEFAULT_AVATAR_ID,
            "name": "",
            "level": "",
            "banner_default": True,
            "avatar_default": True,
        }

    # Diagnostic mode: reveals the upstream JSON structure when the source API
    # uses undocumented field names. Keep this for local debugging only.
    if request.args.get("debug") == "source":
        try:
            return jsonify({"extracted": info, "source": data})
        except UnboundLocalError:
            return jsonify({"extracted": info, "source_error": "Info API request failed"}), 502

    if request.args.get("format") == "json":
        return jsonify(info)

    try:
        image_bytes, banner_used, avatar_used = make_banner(info)
    except (OSError, ValueError, requests.RequestException) as exc:
        app.logger.exception("Could not create banner for UID %s", uid)
        return jsonify({"error": "Could not create banner"}), 500

    return Response(
        image_bytes,
        mimetype="image/png",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "X-Requested-Banner-ID": str(info["banner_id"]),
            "X-Requested-Avatar-ID": str(info["avatar_id"]),
            "Content-Disposition": f'inline; filename="banner-{uid}.png"',
            "X-Banner-ID": banner_used,
            "X-Avatar-ID": avatar_used,
        },
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False, threaded=True)
