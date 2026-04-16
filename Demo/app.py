import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
TRAFFIC_CAMERA_DATA_FILE = BASE_DIR / "Traffic+Cameras.json"
USERS_FILE = BASE_DIR / "users.json"
MANAGED_CAMERAS_FILE = BASE_DIR / "managed_cameras.json"
REPLAY_ROOT = BASE_DIR / "recordings"
EASTERN_TZ = ZoneInfo("America/New_York")
UTC = timezone.utc

app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "ui"),
    static_folder=str(BASE_DIR / "static"),
    static_url_path="/static",
)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-me-in-production")

DEFAULT_STREAM_URL = "https://itsvideo.arlingtonva.us:8013/live/cam221.stream/playlist.m3u8"
app.config["DEFAULT_STREAM_URL"] = DEFAULT_STREAM_URL

REPLAY_WINDOW_HOURS = max(1, int(os.getenv("REPLAY_WINDOW_HOURS", "72")))
REPLAY_SEGMENT_SECONDS = max(5, int(os.getenv("REPLAY_SEGMENT_SECONDS", "60")))
REPLAY_DEFAULT_LOOKBACK_MINUTES = max(
    1, int(os.getenv("REPLAY_DEFAULT_LOOKBACK_MINUTES", "30"))
)
REPLAY_ACTIVE_PLAYLIST_NAME = os.getenv("REPLAY_ACTIVE_PLAYLIST_NAME", "live.m3u8")
REPLAY_CLIPS_SUBDIR = os.getenv("REPLAY_CLIPS_SUBDIR", "clips")
REPLAY_GENERATED_SUBDIR = os.getenv("REPLAY_GENERATED_SUBDIR", "generated")
REPLAY_GENERATED_KEEP_HOURS = max(
    1, int(os.getenv("REPLAY_GENERATED_KEEP_HOURS", "24"))
)
REPLAY_GENERATED_MIN_BYTES = max(
    1024, int(os.getenv("REPLAY_GENERATED_MIN_BYTES", "65536"))
)
REPLAY_FFMPEG_BIN = os.getenv("FFMPEG_BIN", "ffmpeg")
REPLAY_COMBINE_FFMPEG_LOGLEVEL = os.getenv(
    "REPLAY_COMBINE_FFMPEG_LOGLEVEL",
    os.getenv("REPLAY_FFMPEG_LOGLEVEL", "warning"),
)
REPLAY_COMBINE_FALLBACK_PRESET = os.getenv(
    "REPLAY_COMBINE_FALLBACK_PRESET",
    "veryfast",
)
REPLAY_ACTIVE_STALENESS_SECONDS = max(
    REPLAY_SEGMENT_SECONDS * 3,
    int(os.getenv("REPLAY_ACTIVE_STALENESS_SECONDS", str(REPLAY_SEGMENT_SECONDS * 3))),
)
REPLAY_MONITOR_INTERVAL_SECONDS = max(
    2, int(os.getenv("REPLAY_MONITOR_INTERVAL_SECONDS", "10"))
)

# test, delete later
DEFAULT_CAMERAS = [
    {
        "id": 221,
        "name": "Camera 221",
        "stream_url": "https://itsvideo.arlingtonva.us:8013/live/cam221.stream/playlist.m3u8",
        "location": "Arlington, VA",
    },
    {
        "id": 223,
        "name": "Camera 223",
        "stream_url": "https://itsvideo.arlingtonva.us:8013/live/cam223.stream/playlist.m3u8",
        "location": "Arlington, VA",
    },
    {
        "id": 225,
        "name": "Camera 225",
        "stream_url": "https://itsvideo.arlingtonva.us:8013/live/cam225.stream/playlist.m3u8",
        "location": "Arlington, VA",
    },
]
# -------------------------
# admin account
# -------------------------
ADMIN = {
    "username": "admin",
    "password": "123456",
}

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OFFSET_NO_COLON_PATTERN = re.compile(r"([+-]\d{2})(\d{2})$")


@dataclass(frozen=True)
class ReplayClip:
    relative_path: str
    duration: float
    start_at: datetime | None
    end_at: datetime | None
    size_bytes: int


'''
JSON document helpers:
get cameras' info (location, stree name, etc.)
get users' info
'''
def load_json_document(path: Path, default):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default
    return payload


def save_json_document(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temp_path.replace(path)


def load_users():
    users = load_json_document(USERS_FILE, {})
    return users if isinstance(users, dict) else {}


def save_users(users) -> None:
    save_json_document(USERS_FILE, users)


# only accepts http or https
def is_allowed_video_url(value: str) -> bool:
    if not value:
        return False

    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)



def to_float(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    return number if number == number else None



def extract_camera_site(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""

    stream_match = re.search(r"(cam\d+)(?:\.stream)?", text)
    if stream_match:
        return stream_match.group(1)

    numeric_match = re.fullmatch(r"\d+", text)
    if numeric_match:
        return f"cam{numeric_match.group(0)}"

    return ""

'''
Example link: https://itsvideo.arlingtonva.us:8013/live/cam221.stream/playlist.m3u8
the port may change, try 8002, 8011, 8012, 8013
'''

def build_stream_url(site: str, port: str) -> str:
    site = str(site or "").strip().lower()
    port = str(port or "").strip()

    if not site:
        return ""

    port_match = re.fullmatch(r"800(\d)", port)
    if not port_match:
        return ""

    return f"https://itsvideo.arlingtonva.us:801{port_match.group(1)}/live/{site}.stream/playlist.m3u8"


# converts both values (lat, lng) to floats
def is_reasonable_camera_coordinate(lat, lng) -> bool:
    lat = to_float(lat)
    lng = to_float(lng)
    if lat is None or lng is None:
        return False

    return 38.7 <= lat <= 39.0 and -77.3 <= lng <= -76.9


def slugify_identifier(value: str, default: str = "") -> str:
    text = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    text = re.sub(r"-{2,}", "-", text).strip("-_")
    if not text:
        return default
    return text[:64]


def build_recording_id(
    stream_url: str = "",
    camera_id=None,
    camera_site: str = "",
    explicit: str = "",
) -> str:
    explicit_text = slugify_identifier(explicit)
    if explicit_text:
        return explicit_text

    site = extract_camera_site(camera_site or stream_url or camera_id)
    if site:
        return site

    raw_id = slugify_identifier(camera_id)
    if raw_id:
        return raw_id

    stream_url = str(stream_url or "").strip()
    if stream_url:
        digest = hashlib.sha1(stream_url.encode("utf-8")).hexdigest()[:10]
        return f"camera-{digest}"

    return ""



'''
Camera lookup:
loads rows from Traffic+Cameras.json
find camera site, numeric ID, location, coordinates, port, status, and derived stream URL
'''
def load_camera_lookup():
    raw_rows = load_json_document(TRAFFIC_CAMERA_DATA_FILE, [])
    lookup = []

    for row in raw_rows if isinstance(raw_rows, list) else []:
        if not isinstance(row, dict):
            continue

        site = str(row.get("Camera Site") or "").strip().lower()
        if not site:
            continue

        site = extract_camera_site(site) or site
        id_match = re.search(r"(\d+)", site)
        camera_id = id_match.group(1) if id_match else ""
        location = str(row.get("Camera EncoderB2") or "").strip() or "Location unavailable"
        latitude = to_float(row.get("Latitude"))
        longitude = to_float(row.get("Longitude"))

        lookup.append(
            {
                "site": site,
                "id": camera_id,
                "name": f"Camera {camera_id}" if camera_id else site,
                "location": location,
                "lat": latitude,
                "lng": longitude,
                "port": str(row.get("port") or "").strip(),
                "status": str(row.get("STATUS") or "").strip(),
                "stream_url": build_stream_url(site, row.get("port")),
            }
        )

    return lookup


CAMERA_LOOKUP = load_camera_lookup()
CAMERA_LOOKUP_BY_SITE = {
    camera["site"]: camera for camera in CAMERA_LOOKUP if camera.get("site")
}
CAMERA_LOOKUP_BY_ID = {
    str(camera["id"]): camera for camera in CAMERA_LOOKUP if camera.get("id")
}
CAMERA_LOOKUP_BY_STREAM_URL = {
    camera["stream_url"]: camera
    for camera in CAMERA_LOOKUP
    if camera.get("stream_url")
}



def camera_sort_key(camera: dict):
    raw_id = str(camera.get("id") or "").strip()
    try:
        return (0, int(raw_id), str(camera.get("name") or "").lower())
    except ValueError:
        pass

    return (1, raw_id.lower(), str(camera.get("name") or "").lower())


def normalize_managed_camera(camera):
    normalized = serialize_camera(camera)
    normalized["recording_id"] = build_recording_id(
        stream_url=normalized.get("stream_url"),
        camera_id=normalized.get("id"),
        camera_site=normalized.get("camera_site"),
        explicit=(camera or {}).get("recording_id"),
    )
    if not normalized.get("id"):
        normalized["id"] = normalized["recording_id"]

    added_at = str((camera or {}).get("added_at") or "").strip()
    if added_at:
        normalized["added_at"] = added_at

    return normalized


def load_managed_cameras():
    payload = load_json_document(MANAGED_CAMERAS_FILE, [])
    cameras = []
    seen = set()

    for camera in payload if isinstance(payload, list) else []:
        if not isinstance(camera, dict):
            continue

        normalized = normalize_managed_camera(camera)
        dedupe_key = (
            normalized.get("recording_id")
            or normalized.get("stream_url")
            or normalized.get("favorite_key")
        )
        if not dedupe_key or dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        cameras.append(normalized)

    cameras.sort(key=camera_sort_key)
    return cameras


def save_managed_cameras(cameras) -> None:
    normalized_cameras = []
    seen = set()

    for camera in cameras if isinstance(cameras, list) else []:
        if not isinstance(camera, dict):
            continue

        normalized = normalize_managed_camera(camera)
        dedupe_key = (
            normalized.get("recording_id")
            or normalized.get("stream_url")
            or normalized.get("favorite_key")
        )
        if not dedupe_key or dedupe_key in seen:
            continue

        seen.add(dedupe_key)
        normalized_cameras.append(normalized)

    save_json_document(MANAGED_CAMERAS_FILE, normalized_cameras)


def find_managed_camera(camera_ref: str):
    ref = str(camera_ref or "").strip()
    if not ref:
        return None

    extracted_site = extract_camera_site(ref)
    for camera in load_managed_cameras():
        if ref in {
            str(camera.get("recording_id") or ""),
            str(camera.get("camera_site") or ""),
            str(camera.get("id") or ""),
            str(camera.get("stream_url") or ""),
            str(camera.get("favorite_key") or ""),
        }:
            return camera

        if extracted_site and extracted_site == str(camera.get("camera_site") or "").strip().lower():
            return camera

    return None


'''
Find camera metadata:
tries to resolve a camera using stream URL, extracted site, and raw ID
'''
def find_camera_metadata(stream_url: str = "", camera_id=None, camera_site: str = ""):
    stream_url = str(stream_url or "").strip()
    if stream_url and stream_url in CAMERA_LOOKUP_BY_STREAM_URL:
        return CAMERA_LOOKUP_BY_STREAM_URL[stream_url]

    site = extract_camera_site(camera_site or stream_url or camera_id)
    if site and site in CAMERA_LOOKUP_BY_SITE:
        return CAMERA_LOOKUP_BY_SITE[site]

    raw_id = str(camera_id or "").strip()
    if raw_id in CAMERA_LOOKUP_BY_ID:
        return CAMERA_LOOKUP_BY_ID[raw_id]

    return None


'''
looks in the managed-camera list 
then lookup metadata
then extracted site
return normalized camera object or None
'''
def resolve_camera_reference(camera_ref: str):
    ref = str(camera_ref or "").strip()
    if not ref:
        return None

    managed_camera = find_managed_camera(ref)
    if managed_camera:
        return managed_camera

    metadata = find_camera_metadata(stream_url=ref, camera_id=ref, camera_site=ref)
    if metadata:
        return serialize_camera(metadata)

    site = extract_camera_site(ref)
    if site and site in CAMERA_LOOKUP_BY_SITE:
        return serialize_camera(CAMERA_LOOKUP_BY_SITE[site])

    return None


'''
Identity string used for favorites/history
site, url, or id
'''
def build_camera_identity(camera) -> str:
    if not isinstance(camera, dict):
        return ""

    site = extract_camera_site(
        camera.get("camera_site")
        or camera.get("stream_url")
        or camera.get("id")
        or camera.get("camera_id")
    )
    if site:
        return f"site:{site}"

    stream_url = str(
        camera.get("stream_url") or camera.get("video_url") or camera.get("url") or ""
    ).strip()
    if stream_url:
        return f"url:{stream_url}"

    raw_id = str(camera.get("id") or camera.get("camera_id") or "").strip()
    if raw_id:
        return f"id:{raw_id}"

    return ""



def enrich_camera(camera):
    base_camera = dict(camera or {})
    stream_url = str(
        base_camera.get("stream_url")
        or base_camera.get("video_url")
        or base_camera.get("url")
        or ""
    ).strip()
    raw_id = base_camera.get("id") or base_camera.get("camera_id")
    raw_name = str(base_camera.get("name") or base_camera.get("title") or "").strip()
    raw_location = str(
        base_camera.get("location") or base_camera.get("address") or ""
    ).strip()
    metadata = find_camera_metadata(
        stream_url=stream_url,
        camera_id=raw_id,
        camera_site=base_camera.get("camera_site"),
    )

    if metadata:
        base_camera["camera_site"] = metadata["site"]
        base_camera["id"] = str(base_camera.get("id") or metadata["id"] or "").strip()
        base_camera["name"] = raw_name or metadata["name"]
        base_camera["location"] = metadata["location"] or raw_location or "Location unavailable"
        base_camera["lat"] = metadata["lat"] if metadata["lat"] is not None else to_float(base_camera.get("lat"))
        base_camera["lng"] = metadata["lng"] if metadata["lng"] is not None else to_float(base_camera.get("lng"))
        base_camera["stream_url"] = stream_url or metadata["stream_url"]
        base_camera["status"] = str(metadata.get("status") or "").strip()
    else:
        resolved_id = str(raw_id or "").strip()
        base_camera["id"] = resolved_id
        base_camera["camera_site"] = (
            str(base_camera.get("camera_site") or "").strip().lower()
            or extract_camera_site(stream_url or resolved_id)
        )
        base_camera["name"] = raw_name or (f"Camera {resolved_id}" if resolved_id else "Camera")
        base_camera["location"] = raw_location or "Location unavailable"
        base_camera["lat"] = to_float(base_camera.get("lat"))
        base_camera["lng"] = to_float(base_camera.get("lng"))
        base_camera["stream_url"] = stream_url
        base_camera["status"] = str(base_camera.get("status") or "").strip()

    base_camera["favorite_key"] = build_camera_identity(base_camera)
    return base_camera




def serialize_camera(camera):
    enriched = enrich_camera(camera)
    recording_id = build_recording_id(
        stream_url=enriched.get("stream_url"),
        camera_id=enriched.get("id"),
        camera_site=enriched.get("camera_site"),
        explicit=enriched.get("recording_id"),
    )
    camera_id = str(enriched.get("id") or "").strip() or recording_id

    if not str(enriched.get("name") or "").strip():
        if recording_id.startswith("camera-"):
            enriched["name"] = f"Custom Camera {recording_id.split('-', 1)[-1].upper()}"
        elif camera_id:
            enriched["name"] = f"Camera {camera_id}"
        else:
            enriched["name"] = "Camera"

    return {
        "id": camera_id,
        "recording_id": recording_id,
        "camera_site": str(enriched.get("camera_site") or "").strip().lower(),
        "favorite_key": build_camera_identity(enriched),
        "name": str(enriched.get("name") or "Camera").strip() or "Camera",
        "location": str(enriched.get("location") or "Location unavailable").strip() or "Location unavailable",
        "stream_url": str(enriched.get("stream_url") or "").strip(),
        "lat": to_float(enriched.get("lat")),
        "lng": to_float(enriched.get("lng")),
        "status": str(enriched.get("status") or "").strip(),
    }


# Loads managed cameras
def get_map_cameras():
    valid_cameras = []

    for camera in load_managed_cameras():
        normalized = serialize_camera(camera)
        if not normalized.get("stream_url"):
            continue
        if not is_reasonable_camera_coordinate(normalized.get("lat"), normalized.get("lng")):
            continue
        valid_cameras.append(normalized)

    valid_cameras.sort(key=camera_sort_key)
    return valid_cameras


def get_admin_cameras():
    return load_managed_cameras()


'''
Auth/session helpers

return:
401: guests
403: other roles
'''
def session_role() -> str:
    return str(session.get("role") or "").strip().lower()


def session_can_watch_replay() -> bool:
    return session_role() in {"admin", "user"}


def replay_api_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        current_role = session_role()
        if current_role in {"admin", "user"}:
            return view_func(*args, **kwargs)

        if not current_role:
            return jsonify({
                "error": "Login required to watch recorded footage. Guests can only watch live video."
            }), 401

        return jsonify({"error": "Forbidden."}), 403

    return wrapped_view


def replay_file_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        current_role = session_role()
        if current_role in {"admin", "user"}:
            return view_func(*args, **kwargs)

        if not current_role:
            return Response(
                "Login required to watch recorded footage. Guests can only watch live video.",
                status=401,
                mimetype="text/plain",
            )

        return Response("Forbidden.", status=403, mimetype="text/plain")

    return wrapped_view


def current_user_key() -> str:
    return str(session.get("user_key") or session.get("user") or "").strip().lower()


def current_user_record(users=None):
    users = users if users is not None else load_users()
    if session_role() != "user":
        return "", None, users

    user_key = current_user_key()
    record = users.get(user_key)
    return user_key, record, users


def redirect_for_session():
    role = session_role()
    if role == "admin":
        return redirect("/adminPage.html")
    if role == "user":
        return redirect("/userPage.html")
    return None



def html_role_required(required_role):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            current_role = session_role()

            if current_role == required_role:
                return view_func(*args, **kwargs)

            if current_role == "admin":
                return redirect("/adminPage.html")
            if current_role == "user":
                return redirect("/userPage.html")

            flash("Please log in first.", "error")
            return redirect(url_for("login"))

        return wrapped_view

    return decorator


def api_role_required(required_role):
    def decorator(view_func):
        @wraps(view_func)
        def wrapped_view(*args, **kwargs):
            current_role = session_role()
            if current_role == required_role:
                return view_func(*args, **kwargs)

            if not current_role:
                session.clear()
                return jsonify({"error": "Authentication required."}), 401

            return jsonify({"error": "Forbidden."}), 403

        return wrapped_view

    return decorator


def api_user_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        user_key, record, users = current_user_record()
        if not user_key or record is None:
            session.clear()
            return jsonify({"error": "Authentication required."}), 401

        return view_func(user_key, record, users, *args, **kwargs)

    return wrapped_view


def normalize_username(value: str) -> str:
    return str(value or "").strip()


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_PATTERN.fullmatch(normalize_email(value)))



def is_valid_username(value: str) -> bool:
    return bool(USERNAME_PATTERN.fullmatch(normalize_username(value)))


'''
User favorites/history helper:
ensures each entry has camera_id, camera_site, name, location, stream_url, and timestamp
'''
def normalize_history_entries(entries):
    normalized_entries = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue

        camera = serialize_camera(entry)
        raw_camera_id = str(entry.get("camera_id") or camera.get("id") or "").strip()
        camera_id = raw_camera_id or str(camera.get("id") or "").strip()
        timestamp = str(entry.get("timestamp") or "").strip()
        if not timestamp:
            continue

        normalized_entries.append(
            {
                "camera_id": camera_id,
                "camera_site": str(entry.get("camera_site") or camera.get("camera_site") or "").strip().lower(),
                "name": str(entry.get("name") or camera.get("name") or "Camera").strip() or "Camera",
                "location": str(entry.get("location") or camera.get("location") or "Location unavailable").strip() or "Location unavailable",
                "stream_url": str(entry.get("stream_url") or camera.get("stream_url") or "").strip(),
                "timestamp": timestamp,
            }
        )

    return normalized_entries[:20]


def format_visit_timestamp() -> str:
    return datetime.now(EASTERN_TZ).strftime("%Y-%m-%d %I:%M:%S %p %Z")


'''
Replay helpers
filename: cam221_20260413T120000Z.mp4
'''
MP4_CLIP_FILENAME_PATTERN = re.compile(
    r"^(?P<site>cam\d+)_(?P<timestamp>\d{8}T\d{6}Z)\.mp4$",
    re.IGNORECASE,
)


def replay_camera_dir(camera_site: str) -> Path:
    recording_id = build_recording_id(explicit=camera_site, camera_site=camera_site)
    if not recording_id:
        recording_id = slugify_identifier(camera_site, default="camera")
    return REPLAY_ROOT / recording_id

def replay_clips_dir(camera_site: str) -> Path:
    return replay_camera_dir(camera_site) / REPLAY_CLIPS_SUBDIR


def replay_status_path(camera_site: str) -> Path:
    return replay_camera_dir(camera_site) / "status.json"


def replay_generated_dir(camera_site: str) -> Path:
    return replay_camera_dir(camera_site) / REPLAY_GENERATED_SUBDIR


def generated_replay_filename(camera_site: str, start_at: datetime, end_at: datetime, cache_key: str) -> str:
    camera_prefix = slugify_identifier(camera_site, default="camera")
    start_stamp = start_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    end_stamp = end_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{camera_prefix}_{start_stamp}_{end_stamp}_{cache_key}.mp4"


def cleanup_generated_replays(camera_site: str) -> None:
    generated_dir = replay_generated_dir(camera_site)
    if not generated_dir.exists():
        return

    cutoff = datetime.now(UTC) - timedelta(hours=REPLAY_GENERATED_KEEP_HOURS)
    for path in generated_dir.glob("*"):
        if not path.is_file():
            continue

        try:
            modified_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            continue

        if modified_at >= cutoff:
            continue

        try:
            path.unlink()
        except OSError:
            continue


def build_generated_replay_cache_key(camera_site: str, start_at: datetime, end_at: datetime, selected_clips: list[ReplayClip]) -> str:
    digest = hashlib.sha1()
    digest.update(b"combined-replay-exact-v2")
    digest.update(str(camera_site or "").encode("utf-8"))
    digest.update(isoformat_z(start_at).encode("utf-8"))
    digest.update(isoformat_z(end_at).encode("utf-8"))

    camera_dir = replay_camera_dir(camera_site)
    for clip in selected_clips:
        clip_path = camera_dir / clip.relative_path
        digest.update(str(clip.relative_path).encode("utf-8"))
        digest.update(str(clip.duration).encode("utf-8"))
        digest.update(str(clip.size_bytes).encode("utf-8"))
        digest.update(isoformat_z(clip.start_at).encode("utf-8"))
        digest.update(isoformat_z(clip.end_at).encode("utf-8"))
        try:
            stat = clip_path.stat()
        except OSError:
            continue

        digest.update(str(stat.st_size).encode("utf-8"))
        digest.update(str(stat.st_mtime_ns).encode("utf-8"))

    return digest.hexdigest()[:16]


'''
figures out which portion of a saved clip overlaps the requested replay window
return:
inpoint_seconds
outpoint_seconds
actual overlapping duration
'''
def clip_contribution_bounds(clip: ReplayClip, start_at: datetime, end_at: datetime) -> tuple[float, float, float]:
    if clip.start_at is None or clip.end_at is None:
        return 0.0, 0.0, 0.0

    contribution_start = max(start_at, clip.start_at)
    contribution_end = min(end_at, clip.end_at)
    if contribution_end <= contribution_start:
        return 0.0, 0.0, 0.0

    inpoint_seconds = max(0.0, (contribution_start - clip.start_at).total_seconds())
    outpoint_seconds = max(inpoint_seconds, (contribution_end - clip.start_at).total_seconds())
    duration_seconds = max(0.0, (contribution_end - contribution_start).total_seconds())
    return inpoint_seconds, outpoint_seconds, duration_seconds


def build_ffconcat_file(camera_site: str, cache_stem: str, start_at: datetime, end_at: datetime, selected_clips: list[ReplayClip]) -> tuple[Path, float]:
    camera_dir = replay_camera_dir(camera_site)
    generated_dir = replay_generated_dir(camera_site)
    generated_dir.mkdir(parents=True, exist_ok=True)
    concat_path = generated_dir / f"{cache_stem}.ffconcat"

    def ffconcat_path_line(path: Path) -> str:
        escaped = path.resolve().as_posix().replace("'", r"'\''")
        return f"file '{escaped}'"

    lines = ["ffconcat version 1.0"]
    total_duration = 0.0

    for clip in selected_clips:
        clip_path = camera_dir / clip.relative_path
        inpoint_seconds, outpoint_seconds, duration_seconds = clip_contribution_bounds(
            clip,
            start_at,
            end_at,
        )
        if duration_seconds <= 0:
            continue

        lines.append(ffconcat_path_line(clip_path))
        if inpoint_seconds > 0.0005:
            lines.append(f"inpoint {inpoint_seconds:.6f}")
        lines.append(f"outpoint {outpoint_seconds:.6f}")
        lines.append(f"duration {duration_seconds:.6f}")
        total_duration += duration_seconds

    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return concat_path, round(total_duration, 3)


def run_ffmpeg_for_generated_replay(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            cwd=str(BASE_DIR),
        )
    except FileNotFoundError as error:
        raise RuntimeError(
            f"Could not find ffmpeg binary '{REPLAY_FFMPEG_BIN}'. Install ffmpeg or set FFMPEG_BIN."
        ) from error


def build_generated_replay(camera_site: str, start_at: datetime, end_at: datetime, selected_clips: list[ReplayClip]) -> dict:
    if not selected_clips:
        raise ValueError("No clips were selected for the requested replay window.")

    cleanup_generated_replays(camera_site)

    generated_dir = replay_generated_dir(camera_site)
    generated_dir.mkdir(parents=True, exist_ok=True)

    cache_key = build_generated_replay_cache_key(camera_site, start_at, end_at, selected_clips)
    filename = generated_replay_filename(camera_site, start_at, end_at, cache_key)
    final_path = generated_dir / filename
    relative_path = final_path.relative_to(replay_camera_dir(camera_site)).as_posix()

    if final_path.exists():
        try:
            existing_stat = final_path.stat()
            existing_size = existing_stat.st_size
        except OSError:
            existing_size = 0
            existing_stat = None

        if existing_size >= REPLAY_GENERATED_MIN_BYTES:
            return {
                "filename": filename,
                "relative_path": relative_path,
                "duration_seconds": round(max(0.0, (end_at - start_at).total_seconds()), 3),
                "clip_count": len(selected_clips),
                "render_mode": "cached",
                "cached": True,
                "size_bytes": existing_size,
                "generated_at": isoformat_z(datetime.fromtimestamp(existing_stat.st_mtime, UTC)) if existing_stat else "",
            }

        try:
            final_path.unlink()
        except OSError:
            pass

    cache_stem = Path(filename).stem
    concat_path, exact_duration_seconds = build_ffconcat_file(
        camera_site,
        cache_stem,
        start_at,
        end_at,
        selected_clips,
    )
    if exact_duration_seconds <= 0:
        try:
            concat_path.unlink()
        except OSError:
            pass
        raise RuntimeError("No archived MP4 clip content matched the requested replay window.")

    temp_path = generated_dir / f"{cache_stem}.tmp.mp4"

    if temp_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            pass

    copy_command = [
        REPLAY_FFMPEG_BIN,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        REPLAY_COMBINE_FFMPEG_LOGLEVEL,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_path),
        "-an",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-fflags",
        "+genpts",
        "-avoid_negative_ts",
        "make_zero",
        str(temp_path),
    ]

    completed = run_ffmpeg_for_generated_replay(copy_command)
    render_mode = "copy-exact"

    if completed.returncode != 0 or not temp_path.exists() or temp_path.stat().st_size < REPLAY_GENERATED_MIN_BYTES:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

        fallback_command = [
            REPLAY_FFMPEG_BIN,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            REPLAY_COMBINE_FFMPEG_LOGLEVEL,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            REPLAY_COMBINE_FALLBACK_PRESET,
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
        completed = run_ffmpeg_for_generated_replay(fallback_command)
        render_mode = "reencoded-exact"

    try:
        concat_path.unlink()
    except OSError:
        pass

    if completed.returncode != 0 or not temp_path.exists():
        stderr_tail = (completed.stderr or completed.stdout or "").strip()[-1200:]
        raise RuntimeError(
            "ffmpeg could not build the replay video."
            + (f" Details: {stderr_tail}" if stderr_tail else "")
        )

    try:
        output_size = temp_path.stat().st_size
    except OSError as error:
        raise RuntimeError("ffmpeg finished but the merged replay file is missing.") from error

    if output_size < REPLAY_GENERATED_MIN_BYTES:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise RuntimeError("ffmpeg created a replay file, but it was too small to use.")

    temp_path.replace(final_path)
    generated_at = datetime.now(UTC)

    return {
        "filename": filename,
        "relative_path": relative_path,
        "duration_seconds": exact_duration_seconds,
        "clip_count": len(selected_clips),
        "render_mode": render_mode,
        "cached": False,
        "size_bytes": output_size,
        "generated_at": isoformat_z(generated_at),
    }


def isoformat_z(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_request_datetime(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    elif OFFSET_NO_COLON_PATTERN.search(text):
        text = OFFSET_NO_COLON_PATTERN.sub(r"\1:\2", text)

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN_TZ)

    return parsed.astimezone(UTC)



def parse_clip_start_from_name(filename: str) -> datetime | None:
    match = MP4_CLIP_FILENAME_PATTERN.fullmatch(str(filename or "").strip())
    if not match:
        return None

    try:
        return datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None

'''

'''
def load_replay_clips(camera_site: str):
    camera_dir = replay_camera_dir(camera_site)
    clips_dir = replay_clips_dir(camera_site)

    if not clips_dir.exists():
        return {
            "camera_dir": camera_dir,
            "clips_dir": clips_dir,
            "clips": [],
        }

    raw_clips = []
    for clip_path in sorted(clips_dir.rglob("*.mp4")):
        if not clip_path.is_file():
            continue

        start_at = parse_clip_start_from_name(clip_path.name)
        if start_at is None:
            try:
                start_at = datetime.fromtimestamp(clip_path.stat().st_mtime, UTC) - timedelta(
                    seconds=REPLAY_SEGMENT_SECONDS
                )
            except OSError:
                continue

        try:
            size_bytes = clip_path.stat().st_size
        except OSError:
            continue

        try:
            relative_path = clip_path.relative_to(camera_dir).as_posix()
        except ValueError:
            relative_path = clip_path.name

        raw_clips.append(
            {
                "relative_path": relative_path,
                "start_at": start_at,
                "size_bytes": size_bytes,
            }
        )

    raw_clips.sort(
        key=lambda clip: (
            clip["start_at"],
            clip["relative_path"],
        )
    )

    clips: list[ReplayClip] = []
    for index, clip in enumerate(raw_clips):
        start_at = clip["start_at"]
        duration_seconds = float(REPLAY_SEGMENT_SECONDS)

        if index + 1 < len(raw_clips):
            next_start = raw_clips[index + 1]["start_at"]
            gap_seconds = max(0.0, (next_start - start_at).total_seconds())
            if gap_seconds > 0:
                duration_seconds = min(duration_seconds, gap_seconds)

        duration_seconds = max(0.001, duration_seconds)
        end_at = start_at + timedelta(seconds=duration_seconds)

        clips.append(
            ReplayClip(
                relative_path=clip["relative_path"],
                duration=duration_seconds,
                start_at=start_at,
                end_at=end_at,
                size_bytes=clip["size_bytes"],
            )
        )

    return {
        "camera_dir": camera_dir,
        "clips_dir": clips_dir,
        "clips": clips,
    }


def load_replay_status_file(camera_site: str) -> dict:
    payload = load_json_document(replay_status_path(camera_site), {})
    return payload if isinstance(payload, dict) else {}


def replay_archive_status(camera_site: str):
    archive = load_replay_clips(camera_site)
    clips: list[ReplayClip] = archive["clips"]
    now = datetime.now(UTC)
    status_payload = load_replay_status_file(camera_site)

    earliest = clips[0].start_at if clips else None
    latest = clips[-1].end_at if clips else None

    status_updated_at = parse_request_datetime(status_payload.get("updated_at"))
    raw_state = str(status_payload.get("state") or "").strip().lower()
    recording_active = bool(
        status_updated_at
        and raw_state in {"running", "capturing", "sleeping"}
        and (now - status_updated_at) <= timedelta(seconds=REPLAY_ACTIVE_STALENESS_SECONDS)
    )

    default_end = latest or now
    if earliest is not None:
        default_start = max(
            earliest,
            default_end - timedelta(minutes=REPLAY_DEFAULT_LOOKBACK_MINUTES),
        )
    else:
        default_start = default_end - timedelta(minutes=REPLAY_DEFAULT_LOOKBACK_MINUTES)

    clips_dir = archive["clips_dir"]
    clips_dir_display = str(clips_dir.relative_to(BASE_DIR)) if clips_dir.exists() else str(clips_dir)

    return {
        "camera_site": camera_site,
        "has_archive": bool(clips),
        "recording_active": recording_active,
        "clips_directory": clips_dir_display,
        "status_file": str(replay_status_path(camera_site).relative_to(BASE_DIR))
        if replay_status_path(camera_site).exists()
        else str(replay_status_path(camera_site)),
        "clip_count": len(clips),
        "target_duration": REPLAY_SEGMENT_SECONDS,
        "earliest_available": isoformat_z(earliest),
        "latest_available": isoformat_z(latest),
        "default_start": isoformat_z(default_start),
        "default_end": isoformat_z(default_end),
        "window_hours": REPLAY_WINDOW_HOURS,
        "recorder_state": raw_state or "unknown",
        "recorder_updated_at": isoformat_z(status_updated_at),
    }


def clamp_replay_window(status: dict, requested_start: datetime | None, requested_end: datetime | None):
    latest = parse_request_datetime(status.get("latest_available"))
    earliest = parse_request_datetime(status.get("earliest_available"))

    if latest is None and earliest is None:
        return None, None

    end_at = requested_end or latest or datetime.now(UTC)
    start_at = requested_start or (
        end_at - timedelta(minutes=REPLAY_DEFAULT_LOOKBACK_MINUTES)
    )

    if latest is not None and end_at > latest:
        end_at = latest
    if earliest is not None and start_at < earliest:
        start_at = earliest

    max_window = timedelta(hours=REPLAY_WINDOW_HOURS)
    if end_at - start_at > max_window:
        start_at = end_at - max_window
        if earliest is not None and start_at < earliest:
            start_at = earliest

    if start_at >= end_at:
        if earliest is not None and latest is not None and earliest < latest:
            start_at = earliest
            end_at = latest
        else:
            end_at = start_at + timedelta(minutes=1)

    return start_at, end_at


def select_replay_clips(camera_site: str, start_at: datetime, end_at: datetime):
    archive = load_replay_clips(camera_site)
    selected: list[ReplayClip] = []

    for clip in archive["clips"]:
        if clip.start_at is None or clip.end_at is None:
            continue
        if clip.end_at <= start_at:
            continue
        if clip.start_at >= end_at:
            continue
        selected.append(clip)

    return archive, selected



def camera_for_replay_or_404(camera_ref: str):
    metadata = resolve_camera_reference(camera_ref)
    if not metadata:
        abort(404, description="Unknown camera.")

    camera = serialize_camera(metadata)
    if not camera.get("recording_id"):
        abort(404, description="This camera does not have a replay archive identifier.")

    return camera


'''
HTML routes:
/ ------------------------- map_homepage.html
/admin -------------------- adminPage.html
/login -------------------- login.html
/register ----------------- registerPage.html
/user --------------------- userPage.html
/replay/<camera_ref> ------ replay.html
'''
@app.route("/")
def home():
    return render_template(
        "map_homepage.html",
        cameras=get_map_cameras(),
        camera_lookup=CAMERA_LOOKUP,
        can_watch_replay=session_can_watch_replay(),
    )


@app.route("/admin")
@app.route("/adminPage.html")
@html_role_required("admin")
def admin():
    requested_stream_url = str(request.args.get("url") or "").strip()
    return render_template(
        "adminPage.html",
        stream_url=requested_stream_url,
        default_url="",
        cameras=get_admin_cameras(),
        camera_lookup=CAMERA_LOOKUP,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    existing_session_redirect = redirect_for_session()
    if existing_session_redirect is not None:
        return existing_session_redirect

    username = ""
    if request.method == "POST":
        username = normalize_username(request.form.get("username"))
        password = str(request.form.get("password") or "")

        if username.lower() == ADMIN["username"].lower() and password == ADMIN["password"]:
            session.clear()
            session["user"] = ADMIN["username"]
            session["user_key"] = ADMIN["username"].lower()
            session["role"] = "admin"
            return redirect("/adminPage.html")

        users = load_users()
        user_key = username.lower()
        record = users.get(user_key)
        password_hash = str(record.get("password_hash") or "") if isinstance(record, dict) else ""
        if record and password_hash and check_password_hash(password_hash, password):
            session.clear()
            session["user"] = record.get("username") or username
            session["user_key"] = user_key
            session["role"] = "user"
            return redirect("/userPage.html")

        flash("Invalid username or password.", "error")

    return render_template("login.html", username=username)


@app.route("/register", methods=["GET", "POST"])
@app.route("/registerPage.html", methods=["GET", "POST"])
def register():
    existing_session_redirect = redirect_for_session()
    if existing_session_redirect is not None:
        return existing_session_redirect

    form_data = {"email": "", "username": ""}
    if request.method == "POST":
        email = normalize_email(request.form.get("email") or request.form.get("userEmail"))
        username = normalize_username(request.form.get("username"))
        password = str(request.form.get("password") or "")
        form_data = {"email": email, "username": username}
        users = load_users()

        has_error = False
        if not is_valid_email(email):
            flash("Please enter a valid email address.", "error")
            has_error = True

        if not is_valid_username(username):
            flash(
                "Username must be 3 to 32 characters and can contain letters, numbers, underscores, periods, or hyphens.",
                "error",
            )
            has_error = True

        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "error")
            has_error = True

        user_key = username.lower()
        if user_key == ADMIN["username"].lower():
            flash("That username is reserved.", "error")
            has_error = True
        elif user_key in users:
            flash("That username is already taken.", "error")
            has_error = True

        if any(str(user.get("email") or "").lower() == email for user in users.values()):
            flash("That email address is already registered.", "error")
            has_error = True

        if not has_error:
            users[user_key] = {
                "username": username,
                "email": email,
                "password_hash": generate_password_hash(password),
                "favorites": [],
                "history": [],
            }
            save_users(users)
            flash("Account created successfully. Please log in.", "success")
            return redirect(url_for("login"))

    return render_template("registerPage.html", form_data=form_data)


@app.route("/user")
@app.route("/userPage.html")
@html_role_required("user")
def user_page():
    user_key, user_record, _users = current_user_record()
    if not user_key or user_record is None:
        session.clear()
        flash("Please log in again.", "error")
        return redirect(url_for("login"))

    favorites = [serialize_camera(item) for item in user_record.get("favorites", [])]
    history = normalize_history_entries(user_record.get("history", []))

    return render_template(
        "userPage.html",
        cameras=get_map_cameras(),
        camera_lookup=CAMERA_LOOKUP,
        favorites=favorites,
        history=history,
    )


@app.route("/replay/<camera_ref>")
def replay_page(camera_ref):
    camera = camera_for_replay_or_404(camera_ref)
    can_watch_replay = session_can_watch_replay()
    replay_status = replay_archive_status(camera["recording_id"]) if can_watch_replay else {}
    return render_template(
        "replay.html",
        camera=camera,
        replay_status=replay_status,
        replay_window_hours=REPLAY_WINDOW_HOURS,
        can_watch_replay=can_watch_replay,
        replay_cameras=get_map_cameras(),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# User APIs
@app.post("/api/user/favorites")
@api_user_required
def add_favorite_api(user_key, user_record, users):
    payload = request.get_json(silent=True) or {}
    camera = serialize_camera(payload)
    favorite_key = camera.get("favorite_key") or build_camera_identity(payload)
    if not favorite_key:
        return jsonify({"error": "A camera identifier is required."}), 400

    favorites = [serialize_camera(item) for item in user_record.get("favorites", [])]
    if not any(item.get("favorite_key") == favorite_key for item in favorites):
        camera["favorite_key"] = favorite_key
        favorites.insert(0, camera)

    user_record["favorites"] = favorites
    users[user_key] = user_record
    save_users(users)
    return jsonify({"favorites": favorites, "favorite_key": favorite_key})


@app.post("/api/user/favorites/remove")
@api_user_required
def remove_favorite_api(user_key, user_record, users):
    payload = request.get_json(silent=True) or {}
    favorite_key = str(payload.get("favorite_key") or build_camera_identity(payload)).strip()
    if not favorite_key:
        return jsonify({"error": "A camera identifier is required."}), 400

    favorites = [serialize_camera(item) for item in user_record.get("favorites", [])]
    favorites = [item for item in favorites if item.get("favorite_key") != favorite_key]

    user_record["favorites"] = favorites
    users[user_key] = user_record
    save_users(users)
    return jsonify({"favorites": favorites, "favorite_key": favorite_key})


@app.post("/api/user/history")
@api_user_required
def add_history_api(user_key, user_record, users):
    payload = request.get_json(silent=True) or {}
    camera = serialize_camera(payload)
    camera_id = str(payload.get("camera_id") or camera.get("id") or "").strip()
    camera_site = str(payload.get("camera_site") or camera.get("camera_site") or "").strip().lower()
    stream_url = str(payload.get("stream_url") or camera.get("stream_url") or "").strip()

    if not any([camera_id, camera_site, stream_url]):
        return jsonify({"error": "A camera identifier is required."}), 400

    entry = {
        "camera_id": camera_id or str(camera.get("id") or "").strip(),
        "camera_site": camera_site or str(camera.get("camera_site") or "").strip().lower(),
        "name": str(camera.get("name") or "Camera").strip() or "Camera",
        "location": str(camera.get("location") or "Location unavailable").strip() or "Location unavailable",
        "stream_url": stream_url or str(camera.get("stream_url") or "").strip(),
        "timestamp": format_visit_timestamp(),
    }

    history = normalize_history_entries([entry] + user_record.get("history", []))
    user_record["history"] = history
    users[user_key] = user_record
    save_users(users)
    return jsonify({"history": history, "entry": entry})


@app.post("/api/user/history/clear")
@api_user_required
def clear_history_api(user_key, user_record, users):
    user_record["history"] = []
    users[user_key] = user_record
    save_users(users)
    return jsonify({"history": []})


@app.get("/api/user/data")
@api_user_required
def user_data_api(user_key, user_record, users):
    return jsonify(
        {
            "favorites": [serialize_camera(item) for item in user_record.get("favorites", [])],
            "history": normalize_history_entries(user_record.get("history", [])),
        }
    )


# Camera management APIs
@app.get("/api/cameras")
def public_cameras_api():
    return jsonify({"cameras": get_map_cameras()})


@app.get("/api/admin/cameras")
@api_role_required("admin")
def admin_cameras_api():
    return jsonify({"cameras": get_admin_cameras()})


@app.post("/api/admin/cameras")
@api_role_required("admin")
def admin_add_camera_api():
    payload = request.get_json(silent=True) or request.form or {}
    video_url = str(payload.get("video_url") or payload.get("stream_url") or "").strip()

    if not is_allowed_video_url(video_url):
        return jsonify({"error": "Please enter a valid http:// or https:// camera URL."}), 400

    new_camera = serialize_camera({"stream_url": video_url})
    existing_cameras = load_managed_cameras()

    for existing_camera in existing_cameras:
        if (
            new_camera.get("recording_id")
            and new_camera.get("recording_id") == existing_camera.get("recording_id")
        ) or (
            new_camera.get("stream_url")
            and new_camera.get("stream_url") == existing_camera.get("stream_url")
        ):
            return jsonify(
                {
                    "added": False,
                    "camera": existing_camera,
                    "cameras": existing_cameras,
                    "recording_will_start": True,
                    "map_visible": is_reasonable_camera_coordinate(
                        existing_camera.get("lat"), existing_camera.get("lng")
                    ),
                    "message": f"{existing_camera.get('name') or 'Camera'} is already in the managed list.",
                }
            )

    new_camera["added_at"] = isoformat_z(datetime.now(UTC))
    existing_cameras.append(new_camera)
    save_managed_cameras(existing_cameras)
    updated_cameras = load_managed_cameras()

    message = (
        f"{new_camera.get('name') or 'Camera'} added. "
        f"If recorder_service.py is running, recording will start automatically "
        f"within about {REPLAY_MONITOR_INTERVAL_SECONDS} seconds."
    )
    if not is_reasonable_camera_coordinate(new_camera.get("lat"), new_camera.get("lng")):
        message += " This camera has no known coordinates yet, so it will not appear on the map."
    elif str(new_camera.get("status") or "").strip().upper() == "OFFLINE":
        message += " The lookup data currently marks this camera offline, so capture may fail until the feed comes back."

    return jsonify(
        {
            "added": True,
            "camera": new_camera,
            "cameras": updated_cameras,
            "recording_will_start": True,
            "map_visible": is_reasonable_camera_coordinate(
                new_camera.get("lat"), new_camera.get("lng")
            ),
            "message": message,
        }
    )


@app.post("/api/admin/cameras/remove")
@api_role_required("admin")
def admin_remove_camera_api():
    payload = request.get_json(silent=True) or request.form or {}
    camera_ref = str(
        payload.get("recording_id")
        or payload.get("camera_site")
        or payload.get("id")
        or payload.get("stream_url")
        or payload.get("favorite_key")
        or ""
    ).strip()

    if not camera_ref:
        return jsonify({"error": "A camera identifier is required."}), 400

    existing_cameras = load_managed_cameras()
    remaining_cameras = []
    removed_camera = None
    extracted_site = extract_camera_site(camera_ref)

    for camera in existing_cameras:
        matches = camera_ref in {
            str(camera.get("recording_id") or ""),
            str(camera.get("camera_site") or ""),
            str(camera.get("id") or ""),
            str(camera.get("stream_url") or ""),
            str(camera.get("favorite_key") or ""),
        }
        if not matches and extracted_site:
            matches = extracted_site == str(camera.get("camera_site") or "").strip().lower()

        if matches and removed_camera is None:
            removed_camera = camera
            continue

        remaining_cameras.append(camera)

    if removed_camera is None:
        return jsonify({"error": "Camera not found."}), 404

    save_managed_cameras(remaining_cameras)
    return jsonify(
        {
            "removed": True,
            "removed_camera": removed_camera,
            "cameras": load_managed_cameras(),
            "message": (
                f"{removed_camera.get('name') or 'Camera'} removed. "
                "The recorder service will stop capturing it automatically."
            ),
        }
    )


# Replay APIs
@app.get("/api/replay/status/<camera_ref>")
@replay_api_required
def replay_status_api(camera_ref):
    camera = camera_for_replay_or_404(camera_ref)
    status = replay_archive_status(camera["recording_id"])
    return jsonify({"camera": camera, "status": status})


@app.get("/api/replay/clips/<camera_ref>")
@replay_api_required
def replay_clips_api(camera_ref):
    camera = camera_for_replay_or_404(camera_ref)
    status = replay_archive_status(camera["recording_id"])

    if not status.get("has_archive"):
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "clips": [],
                "error": "No archived video exists for this camera yet.",
            }
        ), 404

    requested_start = parse_request_datetime(request.args.get("start"))
    requested_end = parse_request_datetime(request.args.get("end"))
    start_at, end_at = clamp_replay_window(status, requested_start, requested_end)
    if start_at is None or end_at is None:
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "clips": [],
                "error": "Archive metadata is not available yet.",
            }
        ), 404

    _archive, selected_clips = select_replay_clips(camera["recording_id"], start_at, end_at)
    if not selected_clips:
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "clips": [],
                "error": "No archived MP4 clips matched the requested time range.",
            }
        ), 404

    return jsonify(
        {
            "camera": camera,
            "status": status,
            "requested_start": isoformat_z(start_at),
            "requested_end": isoformat_z(end_at),
            "clips": [
                {
                    "filename": Path(clip.relative_path).name,
                    "path": clip.relative_path,
                    "url": url_for(
                        "replay_file",
                        camera_site=camera["recording_id"],
                        clip_path=clip.relative_path,
                    ),
                    "start_at": isoformat_z(clip.start_at),
                    "end_at": isoformat_z(clip.end_at),
                    "duration": clip.duration,
                    "size_bytes": clip.size_bytes,
                }
                for clip in selected_clips
            ],
        }
    )


@app.get("/api/replay/video/<camera_ref>")
@replay_api_required
def replay_video_api(camera_ref):
    camera = camera_for_replay_or_404(camera_ref)
    status = replay_archive_status(camera["recording_id"])

    if not status.get("has_archive"):
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "video": None,
                "error": "No archived video exists for this camera yet.",
            }
        ), 404

    requested_start = parse_request_datetime(request.args.get("start"))
    requested_end = parse_request_datetime(request.args.get("end"))
    start_at, end_at = clamp_replay_window(status, requested_start, requested_end)
    if start_at is None or end_at is None:
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "video": None,
                "error": "Archive metadata is not available yet.",
            }
        ), 404

    _archive, selected_clips = select_replay_clips(camera["recording_id"], start_at, end_at)
    if not selected_clips:
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "video": None,
                "error": "No archived MP4 clips matched the requested time range.",
            }
        ), 404

    try:
        generated_video = build_generated_replay(
            camera["recording_id"],
            start_at,
            end_at,
            selected_clips,
        )
    except RuntimeError as error:
        return jsonify(
            {
                "camera": camera,
                "status": status,
                "video": None,
                "error": str(error),
            }
        ), 500

    return jsonify(
        {
            "camera": camera,
            "status": status,
            "requested_start": isoformat_z(start_at),
            "requested_end": isoformat_z(end_at),
            "video": {
                "filename": generated_video["filename"],
                "path": generated_video["relative_path"],
                "url": url_for(
                    "generated_replay_file",
                    camera_site=camera["recording_id"],
                    filename=generated_video["filename"],
                ),
                "duration_seconds": generated_video["duration_seconds"],
                "clip_count": generated_video["clip_count"],
                "render_mode": generated_video["render_mode"],
                "cached": generated_video["cached"],
                "size_bytes": generated_video["size_bytes"],
                "generated_at": generated_video["generated_at"],
            },
        }
    )


@app.get("/api/replay/playlist/<camera_ref>.m3u8")
@replay_api_required
def replay_playlist_api(camera_ref):
    return Response(
        "#EXTM3U\n# This replay backend now serves merged MP4 replay videos from /api/replay/video/<camera>.\n",
        status=410,
        mimetype="application/vnd.apple.mpegurl",
    )


@app.get("/replay/generated/<camera_site>/<path:filename>")
@replay_file_required
def generated_replay_file(camera_site, filename):
    recording_id = extract_camera_site(camera_site) or str(camera_site or "").strip().lower()
    if not recording_id:
        abort(404)

    generated_dir = replay_generated_dir(recording_id)
    if not generated_dir.exists():
        abort(404)

    try:
        response = send_from_directory(
            str(generated_dir),
            filename,
            mimetype="video/mp4",
            conditional=True,
            max_age=0,
        )
    except Exception:
        abort(404)

    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


@app.get("/replay/file/<camera_site>/<path:clip_path>")
@replay_file_required
def replay_file(camera_site, clip_path):
    site = extract_camera_site(camera_site) or str(camera_site or "").strip().lower()
    if not site:
        abort(404)

    camera_dir = replay_camera_dir(site)
    if not camera_dir.exists():
        abort(404)

    try:
        response = send_from_directory(
            str(camera_dir),
            clip_path,
            mimetype="video/mp4" if clip_path.lower().endswith(".mp4") else None,
            conditional=True,
            max_age=0,
        )
    except Exception:
        abort(404)

    response.headers["Cache-Control"] = "private, no-store, max-age=0"
    return response


# Admin watch route
@app.post("/watch")
@html_role_required("admin")
def watch():
    video_url = request.form.get("video_url", "").strip()

    if not is_allowed_video_url(video_url):
        return render_template(
            "adminPage.html",
            stream_url=app.config["DEFAULT_STREAM_URL"],
            default_url=app.config["DEFAULT_STREAM_URL"],
            error="Please enter a valid http:// or https:// video URL.",
            cameras=get_admin_cameras(),
            camera_lookup=CAMERA_LOOKUP,
        )

    return redirect(f"/adminPage.html?url={video_url}")


if __name__ == "__main__":
    app.run(debug=True)
