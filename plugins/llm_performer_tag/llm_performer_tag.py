#!/usr/bin/env python3
"""
LLM Performer Tag Plugin

Uses a vision-capable LLM to identify performers in scenes or images.
For images: sends the single image to the LLM.
For scenes: extracts frames via ffmpeg's scene detection + evenly-spaced frames
and sends them to the LLM.

Matches against the existing performer catalog (including non-default performer
images as visual context) and suggests performers to tag on the entity.

Self-contained; does not require the CommunityScrapers repo at runtime.
"""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from typing import Any, Optional, List, Dict

try:
    from PIL import Image
except ImportError:
    Image = None

try:
    from StashPluginHelper import StashPluginHelper, taskQueue  # type: ignore
except Exception:
    from stash_helper_fallback import StashPluginHelper, taskQueue  # type: ignore

# ----------------------------
# Configuration and utilities
# ----------------------------

DEFAULT_BASE_URL = "http://localhost:11434/v1"
DEFAULT_MODEL = "gemma3:4b-it-q8_0"
DEFAULT_TEMP = 0.7
DEFAULT_MAX_TOKENS = -1
DEFAULT_TIMEOUT = 3600.0
DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_MAX_FRAMES = 20
DEFAULT_FRAME_WIDTH = 640

PROMPT_DEFAULT = (
    "You are a performer-identification assistant. You will be shown media content "
    "(either an image or video frames) and a catalog of existing performers in the database. "
    "Your job is to identify which performers from the catalog appear in the media. "
    "Return ONLY a JSON array of performer names (strings) that you can see in the content. "
    "Put the names exactly as they appear in the catalog (case-sensitive, matching the 'name' field). "
    "If you see a performer who is NOT in the provided catalog, include their name in the array as well "
    "(these will be suggested as new performers to create). "
    "If no performers are visible or you cannot identify anyone, return an empty JSON array []. "
    "Do NOT return any explanation, code fences, or extra text — ONLY the JSON array."
)

settings = {
    "llmModel": DEFAULT_MODEL,
    "llmTemp": DEFAULT_TEMP,
    "llmMaxTokens": DEFAULT_MAX_TOKENS,
    "llmTimeout": DEFAULT_TIMEOUT,
    "sceneThreshold": DEFAULT_SCENE_THRESHOLD,
    "maxFrames": DEFAULT_MAX_FRAMES,
    "frameWidth": DEFAULT_FRAME_WIDTH,
    "zzdebugTracing": False,
}

try:
    from llm_performer_tag_settings import config  # type: ignore
except Exception:
    config = {}

stash = StashPluginHelper(settings=settings, config=config, maxbytes=10 * 1024 * 1024)

PNG_MAGIC = b'\x89PNG\r\n\x1a\n'

TAG = "[LLMPerformerTag]"


def _fetch_plugin_setting(name: str) -> Optional[str]:
    try:
        query = """
            query($ids: [ID!]) {
                configuration {
                    plugins(include: $ids)
                }
            }
        """
        variables = {"ids": ["llm_performer_tag", "LLMPerformerTag"]}
        resp = stash._graphql(query, variables)  # type: ignore[attr-defined]
        if not isinstance(resp, dict):
            return None
        plugins_map = (((resp.get("data") or {}).get("configuration") or {}).get("plugins")) or {}
        if not isinstance(plugins_map, dict):
            return None
        for pid in variables["ids"]:
            settings_map = plugins_map.get(pid)
            if isinstance(settings_map, dict):
                v = settings_map.get(name)
                if v is not None:
                    return str(v)
        return None
    except Exception:
        return None


def _resolve_base_url() -> str:
    try:
        arg_url = ((stash.JSON_INPUT or {}).get("args") or {}).get("llmBaseUrl") if isinstance(stash.JSON_INPUT, dict) else None
        if isinstance(arg_url, str) and arg_url.strip():
            return arg_url.strip().rstrip("/")
    except Exception:
        pass

    try:
        ui_url = stash.Setting("llmBaseUrl", None)
        if isinstance(ui_url, str) and ui_url.strip():
            return ui_url.strip().rstrip("/")
    except Exception:
        pass

    raw_url = None
    try:
        if isinstance(stash.JSON_INPUT, dict):
            settings_src = stash.JSON_INPUT.get("settings") or {}
            if isinstance(settings_src, dict):
                raw_url = settings_src.get("llmBaseUrl")
            elif isinstance(settings_src, list):
                for item in settings_src:
                    if isinstance(item, dict) and item.get("key") == "llmBaseUrl":
                        raw_url = item.get("value")
                        break
            if not raw_url:
                alt_src = stash.JSON_INPUT.get("pluginSettings") or {}
                if isinstance(alt_src, dict):
                    raw_url = alt_src.get("llmBaseUrl")
                elif isinstance(alt_src, list):
                    for item in alt_src:
                        if isinstance(item, dict) and item.get("key") == "llmBaseUrl":
                            raw_url = item.get("value")
                            break
        if isinstance(raw_url, str) and raw_url.strip():
            return raw_url.strip().rstrip("/")
    except Exception:
        pass

    fetched = _fetch_plugin_setting("llmBaseUrl")
    if isinstance(fetched, str) and fetched.strip():
        return fetched.strip().rstrip("/")

    env_url = os.getenv("LLM_BASE_URL")
    if isinstance(env_url, str) and env_url.strip():
        return env_url.strip().rstrip("/")

    return DEFAULT_BASE_URL.rstrip("/")


def _env_or_setting(name: str, env: str, default: Any) -> Any:
    v = stash.Setting(name, None)
    if v is None:
        v = os.getenv(env, None)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return v


BASE_URL: str = _resolve_base_url()
MODEL: str = str(_env_or_setting("llmModel", "LLM_MODEL", DEFAULT_MODEL))
TEMP: float = float(_env_or_setting("llmTemp", "LLM_TEMP", DEFAULT_TEMP))
MAX_TOKENS: int = int(_env_or_setting("llmMaxTokens", "LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS))
TIMEOUT: float = float(_env_or_setting("llmTimeout", "LLM_TIMEOUT", DEFAULT_TIMEOUT))
ENABLE_THINKING: bool = _env_or_setting("enableThinking", "LLM_ENABLE_THINKING", True)
if isinstance(ENABLE_THINKING, str):
    ENABLE_THINKING = ENABLE_THINKING.strip().lower() in ("true", "1", "yes", "on")
_ENABLE_THINKING_RAW = stash.Setting("enableThinking", None)

if ENABLE_THINKING:
    try:
        fetched = _fetch_plugin_setting("enableThinking")
        _ENABLE_THINKING_GQL = fetched
        if fetched is not None:
            fetched_str = str(fetched).strip().lower()
            if fetched_str in ("false", "0", "no", "off"):
                ENABLE_THINKING = False
    except Exception as e:
        _ENABLE_THINKING_GQL = f"ERROR:{e}"
else:
    _ENABLE_THINKING_GQL = "skipped"
API_KEY: str = os.getenv("LLM_API_KEY", "none")
PROMPT: str = os.getenv("LLM_PERFORMER_PROMPT", PROMPT_DEFAULT)
SCENE_THRESHOLD: float = float(_env_or_setting("sceneThreshold", "LLM_SCENE_THRESHOLD", DEFAULT_SCENE_THRESHOLD))
MAX_FRAMES: int = int(_env_or_setting("maxFrames", "LLM_MAX_FRAMES", DEFAULT_MAX_FRAMES))
FRAME_WIDTH: int = int(_env_or_setting("frameWidth", "LLM_FRAME_WIDTH", DEFAULT_FRAME_WIDTH))

INCLUDE_PERFORMER_DETAILS: bool = _env_or_setting("includePerformerDescriptions", "LLM_INCLUDE_PERFORMER_DETAILS", True)
if isinstance(INCLUDE_PERFORMER_DETAILS, str):
    INCLUDE_PERFORMER_DETAILS = INCLUDE_PERFORMER_DETAILS.strip().lower() in ("true", "1", "yes", "on")

INCLUDE_PERFORMER_IMAGES: bool = _env_or_setting("includePerformerImages", "LLM_INCLUDE_PERFORMER_IMAGES", False)
if isinstance(INCLUDE_PERFORMER_IMAGES, str):
    INCLUDE_PERFORMER_IMAGES = INCLUDE_PERFORMER_IMAGES.strip().lower() in ("true", "1", "yes", "on")

MAX_PERFORMER_IMAGES: int = int(_env_or_setting("maxPerformerImages", "LLM_MAX_PERFORMER_IMAGES", 50))

SAVE_DEBUG_LOG: bool = _env_or_setting("saveDebugLog", "LLM_SAVE_DEBUG_LOG", False)
if isinstance(SAVE_DEBUG_LOG, str):
    SAVE_DEBUG_LOG = SAVE_DEBUG_LOG.strip().lower() in ("true", "1", "yes", "on")

if not SAVE_DEBUG_LOG:
    try:
        fetched = _fetch_plugin_setting("saveDebugLog")
        if fetched is not None:
            fetched_str = str(fetched).strip().lower()
            if fetched_str in ("true", "1", "yes", "on"):
                SAVE_DEBUG_LOG = True
    except Exception:
        pass


# ----------------------------
# HTTP helpers
# ----------------------------

def _http_get(url: str, timeout: float = TIMEOUT) -> tuple[int, Dict[str, str], bytes]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            headers = {k: v for k, v in resp.headers.items()}
            return (getattr(resp, "status", 200), headers, body)
    except urllib.error.HTTPError as e:
        return (e.code, {k: v for k, v in e.headers.items()} if e.headers else {}, e.read() if hasattr(e, "read") else b"")
    except Exception as e:
        raise RuntimeError(f"HTTP GET failed for {url}: {e}") from e


def _sanitize_payload_for_log(payload: Dict[str, Any]) -> Dict[str, Any]:
    import copy
    safe = copy.deepcopy(payload)
    for m in safe.get("messages", []):
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    img = part.get("image_url", {})
                    url = img.get("url", "")
                    if url.startswith("data:"):
                        img["url"] = f"data:...;base64,<{len(url)} bytes>"
    return safe


def _save_debug_log(payload: Dict[str, Any]) -> None:
    if not SAVE_DEBUG_LOG:
        return
    try:
        results_dir = os.path.join(_plugin_dir(), "results")
        os.makedirs(results_dir, exist_ok=True)
        sanitized = _sanitize_payload_for_log(payload)
        path = os.path.join(results_dir, "debug_last_run.json")
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(sanitized, handle, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        pass


def _http_post_json(url: str, json_body: Dict[str, Any], headers: Optional[Dict[str, str]] = None, timeout: float = TIMEOUT) -> Dict[str, Any]:
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(json_body).encode("utf-8")
    try:
        stash.Error(f"{TAG} LLM Request Payload: {json.dumps(_sanitize_payload_for_log(json_body), indent=2)}")
    except Exception:
        pass
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8", errors="ignore"))
            except Exception as e:
                raise RuntimeError(f"Non-JSON response from {url}: {raw[:500]!r} ({e})") from e
    except urllib.error.HTTPError as e:
        detail = getattr(e, "read", lambda: b"")()
        raise RuntimeError(f"HTTP {e.code} {e.reason} from {url}: {detail[:500].decode('utf-8', errors='ignore')}") from e
    except Exception as e:
        raise RuntimeError(f"HTTP POST failed for {url}: {e}") from e


# ----------------------------
# ffmpeg frame extraction (for scenes)
# ----------------------------

def _check_ffmpeg() -> None:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found on PATH. Please install ffmpeg to tag scenes.")
    except subprocess.CalledProcessError:
        raise RuntimeError("ffmpeg is installed but returned an error. Check your ffmpeg installation.")
    try:
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffprobe not found on PATH. It is part of the ffmpeg package.")


def _get_video_info(video_path: str) -> tuple[float, float]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration,r_frame_rate",
         "-of", "json", video_path],
        capture_output=True, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {proc.stderr.strip()}")
    info = json.loads(proc.stdout)
    streams = info.get("streams", [])
    if not streams:
        raise RuntimeError("No video stream found in file")
    stream = streams[0]
    duration = float(stream.get("duration", 0))
    if duration <= 0:
        raise RuntimeError(f"Video duration is zero or unknown: {duration}")
    fr_str = stream.get("r_frame_rate", "24/1")
    num, den = fr_str.split("/")
    fps = float(num) / float(den) if float(den) != 0 else 24.0
    return duration, fps


def _extract_frames(video_path: str, max_frames: int, scene_threshold: float, frame_width: int) -> List[str]:
    _check_ffmpeg()

    duration, fps = _get_video_info(video_path)
    stash.Log(f"{TAG} Video duration={duration:.1f}s fps={fps:.1f}")

    max_frames = max(1, min(max_frames, 50))
    scene_threshold = max(0.01, min(1.0, scene_threshold))
    stash.Log(f"{TAG} Extracting up to {max_frames} frames, threshold={scene_threshold}")

    step = max(1, int(duration * fps / max_frames))

    scale_filter = ""
    if frame_width > 0:
        scale_filter = f",scale={frame_width}:-1"

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"select='gt(scene\\,{scene_threshold})+not(mod(n\\,{step}))'{scale_filter}",
        "-vsync", "vfr", "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        "-loglevel", "error",
        "-y",
    ]
    stash.Trace(f"{TAG} ffmpeg: {' '.join(cmd)}")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw_data = proc.stdout.read()
        _, stderr_data = proc.communicate(timeout=300)
        if proc.returncode != 0 and proc.returncode is not None:
            err = stderr_data.decode("utf-8", errors="ignore").strip()[:500]
            stash.Warn(f"{TAG} ffmpeg exited with code {proc.returncode}: {err}")
    except FileNotFoundError:
        raise RuntimeError("ffmpeg not found on PATH")
    except subprocess.TimeoutExpired:
        proc.kill()
        raw_data = proc.stdout.read() if proc.stdout else b""
        stash.Warn(f"{TAG} ffmpeg timed out after 300s")

    if not raw_data:
        fallback = _extract_frames_fallback(video_path, max_frames, frame_width)
        if fallback:
            return fallback
        raise RuntimeError("ffmpeg produced no frame data")

    frames_parts = raw_data.split(PNG_MAGIC)
    frames = [PNG_MAGIC + part for part in frames_parts if part]

    stash.Log(f"{TAG} Got {len(frames)} raw frames from ffmpeg")

    if not frames:
        fallback = _extract_frames_fallback(video_path, max_frames, frame_width)
        if fallback:
            return fallback
        raise RuntimeError("No frames extracted from video")

    if len(frames) > max_frames:
        indices = [int(i * len(frames) / max_frames) for i in range(max_frames)]
        frames = [frames[i] for i in indices]
        stash.Log(f"{TAG} Downsampled to {len(frames)} frames")

    b64_frames = []
    for i, png_data in enumerate(frames):
        b64 = base64.b64encode(png_data).decode("utf-8")
        b64_frames.append(b64)
        stash.Trace(f"{TAG} Frame {i + 1}: {len(png_data)} bytes raw, {len(b64)} bytes b64")

    return b64_frames


def _extract_frames_fallback(video_path: str, max_frames: int, frame_width: int) -> List[str]:
    stash.Log(f"{TAG} Using fallback: evenly-spaced frame extraction via fps filter")

    duration, _ = _get_video_info(video_path)
    target_fps = max_frames / duration if duration > 0 else 1.0

    scale_filter = ""
    if frame_width > 0:
        scale_filter = f",scale={frame_width}:-1"

    cmd = [
        "ffmpeg", "-i", video_path,
        "-vf", f"fps={target_fps}{scale_filter}",
        "-f", "image2pipe", "-vcodec", "png", "pipe:1",
        "-loglevel", "error",
        "-y",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        raw_data = proc.stdout.read()
        _, stderr_data = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        raw_data = proc.stdout.read() if proc.stdout else b""
        return []

    frames_parts = raw_data.split(PNG_MAGIC)
    frames = [PNG_MAGIC + part for part in frames_parts if part]

    stash.Log(f"{TAG} Fallback produced {len(frames)} frames")

    if len(frames) > max_frames:
        indices = [int(i * len(frames) / max_frames) for i in range(max_frames)]
        frames = [frames[i] for i in indices]

    return [base64.b64encode(f).decode("utf-8") for f in frames]


# ----------------------------
# Image reading
# ----------------------------

def _read_image_bytes(path_or_url: str) -> tuple[bytes, str]:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        status, headers, body = _http_get(path_or_url, timeout=TIMEOUT)
        if status < 200 or status >= 300:
            raise RuntimeError(f"Failed to fetch image URL {path_or_url}: HTTP {status}")
        content_type = headers.get("Content-Type") or "application/octet-stream"
        data, mime = body, content_type
    else:
        mime = mimetypes.guess_type(os.path.basename(path_or_url))[0] or "image/jpeg"
        with open(path_or_url, "rb") as f:
            data = f.read()

    if mime == "image/webp" and Image is not None:
        try:
            img = Image.open(io.BytesIO(data))
            with io.BytesIO() as out:
                img.save(out, format="PNG")
                data = out.getvalue()
                mime = "image/png"
        except Exception as e:
            stash.Error(f"{TAG} Failed to convert WebP to PNG: {e}")

    return data, mime


def _message_content_to_str(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        parts: list[str] = []
        for part in msg:
            if isinstance(part, str):
                parts.append(part)
                continue
            if isinstance(part, dict):
                txt = part.get("text") or part.get("content")
                if txt:
                    parts.append(str(txt))
        if parts:
            return "\n".join(parts)
    if msg is None:
        return ""
    try:
        return json.dumps(msg)
    except Exception:
        return str(msg)


def _server_base_url() -> str:
    sc = stash.JSON_INPUT.get("server_connection") or stash.JSON_INPUT.get("serverConnection") or {}
    if isinstance(sc, dict):
        scheme = sc.get("Scheme") or sc.get("scheme") or "http"
        host = sc.get("endpoint") or sc.get("Endpoint") or sc.get("host")
        if host:
            return f"{scheme}://{host}"
    return "http://localhost:9999"


# ----------------------------
# Performer catalog
# ----------------------------

def _existing_performers() -> list[dict[str, str]]:
    try:
        query = """
            query($filter: FindFilterType) {
              findPerformers(filter: $filter) {
                performers {
                  id
                  name
                  disambiguation
                  alias_list
                  gender
                  image_path
                  ignore_auto_tag
                }
              }
            }
        """
        variables = {"filter": {"per_page": -1}}
        resp = stash._graphql(query, variables)  # type: ignore[attr-defined]
        entries: list[dict[str, str]] = []
        sample_real: list[str] = []
        sample_default: list[str] = []
        if isinstance(resp, dict):
            performers = (((resp.get("data") or {}).get("findPerformers") or {}).get("performers")) or []
            for p in performers or []:
                if p.get("ignore_auto_tag"):
                    continue
                name = p.get("name")
                disambiguation = p.get("disambiguation") or ""
                alias_list = p.get("alias_list") or []
                gender = p.get("gender") or ""
                image_path = p.get("image_path")
                performer_id = p.get("id")
                if name:
                    entries.append({
                        "id": str(performer_id),
                        "name": str(name),
                        "disambiguation": str(disambiguation),
                        "gender": str(gender),
                        "image_path": image_path,
                        "type": "performer",
                    })
                    if image_path and isinstance(image_path, str):
                        if "default=true" in image_path:
                            if len(sample_default) < 3:
                                sample_default.append(f"{name}: {image_path[:100]}...")
                        else:
                            if len(sample_real) < 3:
                                sample_real.append(f"{name}: {image_path[:100]}...")
                for alias in alias_list or []:
                    entries.append({
                        "id": str(performer_id),
                        "name": str(alias),
                        "disambiguation": str(disambiguation),
                        "gender": str(gender),
                        "image_path": image_path,
                        "type": "alias",
                    })
        seen = set()
        uniq: list[dict[str, str]] = []
        real_count = 0
        for entry in entries:
            name = entry["name"]
            if name not in seen:
                seen.add(name)
                uniq.append(entry)
                ip = entry.get("image_path")
                if ip and isinstance(ip, str) and "default=true" not in ip:
                    real_count += 1
        stash.Log(f"{TAG} Performers fetched: {len(uniq)} total, {real_count} with custom images")
        if sample_real:
            stash.Log(f"{TAG} Sample custom image performers: {sample_real}")
        if sample_default:
            stash.Log(f"{TAG} Sample default image performers: {sample_default}")
        return uniq
    except Exception as e:
        stash.Error(f"{TAG} Failed to fetch existing performers: {e}")
        return []


def _format_performers_for_prompt(performers: list[dict[str, str]], include_details: bool) -> tuple[str, str]:
    if include_details:
        intro = (
            "The following input is a JSON array of existing performers already in the database. "
            "Each entry has: name (the performer's primary name), disambiguation (to distinguish performers "
            "with the same name), and gender. Use this context to identify which performers appear in the media. "
            "Choose from these performers by name where possible, but you may suggest additional "
            "performers not in this list."
        )
        payload = []
        for e in performers:
            entry = {"name": e["name"]}
            disambiguation = e.get("disambiguation", "")
            gender = e.get("gender", "")
            if disambiguation:
                entry["disambiguation"] = disambiguation
            if gender:
                entry["gender"] = gender
            payload.append(entry)
    else:
        intro = (
            "The following input is a JSON array of existing performers already in the database. "
            "Choose from these by name where possible, but you may suggest additional performers "
            "not in this list."
        )
        payload = [entry["name"] for entry in performers]
    return intro, payload


def _resolve_use_performer_images() -> bool:
    use_images = INCLUDE_PERFORMER_IMAGES or bool(_env_or_setting("includePerformerImages", "LLM_INCLUDE_PERFORMER_IMAGES", False))
    if isinstance(use_images, str):
        use_images = use_images.strip().lower() in ("true", "1", "yes", "on")
    if not use_images:
        gql_val = _fetch_plugin_setting("includePerformerImages")
        if gql_val is not None:
            use_images = gql_val.strip().lower() in ("true", "1", "yes", "on")
            stash.Log(f"{TAG} includePerformerImages from GraphQL: {gql_val!r} -> use_images={use_images}")
    return use_images


def _fetch_performer_image_base64(image_path, performer_name):
    if not image_path:
        return None
    if "default=true" in image_path:
        return None

    base_url = _server_base_url()
    full_url = image_path if image_path.startswith("http") else base_url + image_path

    try:
        req = urllib.request.Request(full_url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            content_type = resp.headers.get("Content-Type") or "image/png"
            b64 = base64.b64encode(body).decode("utf-8")
            stash.Log(f"{TAG} Fetched performer image for '{performer_name}' ({len(body)} bytes, {content_type})")
            return b64, content_type
    except Exception as e:
        stash.Error(f"{TAG} Failed to fetch image for performer '{performer_name}': {e}")
        return None


def _append_performer_images(content, existing_performers):
    content.append({"type": "text", "text": "Note: the images below show the performer's representative image. This does NOT guarantee that the performer appears in the media — use your own judgment based on the actual visual content."})
    seen_ids: set[str] = set()
    performer_images_added = 0

    id_to_aliases: dict[str, list[str]] = {}
    for p in existing_performers:
        if p.get("type") == "alias":
            pid = p.get("id")
            if pid:
                id_to_aliases.setdefault(pid, []).append(p.get("name", ""))

    for performer in existing_performers:
        if MAX_PERFORMER_IMAGES > 0 and performer_images_added >= MAX_PERFORMER_IMAGES:
            break
        pid = performer.get("id", "")
        if pid in seen_ids:
            continue
        seen_ids.add(pid)

        image_path = performer.get("image_path") or performer.get("imagePath")
        name = performer.get("name", "")
        result = _fetch_performer_image_base64(image_path, name)
        if result:
            b64_p, mime_p = result
            aliases = id_to_aliases.get(pid, [])
            label = f"Image for performer '{name}'"
            if aliases:
                label += f" (also known as: {', '.join(aliases)})"
            label += ":"
            content.append({"type": "text", "text": label})
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime_p};base64,{b64_p}"}})
            performer_images_added += 1
    if performer_images_added:
        stash.Log(f"{TAG} Added {performer_images_added} performer image(s) to prompt")
    else:
        stash.Log(f"{TAG} No custom performer images found to include")


# ----------------------------
# LLM integration
# ----------------------------

def _log_prompt(messages: list[dict[str, Any]], label: str = "Prompt") -> None:
    try:
        parts = [f"{TAG} {label} (text only):"]
        image_count = 0
        for m in messages:
            content = m.get("content")
            if isinstance(content, str):
                parts.append(f"  {m.get('role')}: {content}")
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        parts.append(f"  {m.get('role')}: {part.get('text','')}")
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        image_count += 1
        parts.append(f"  (images: {image_count})")
        stash.Error("\n".join(parts))
    except Exception:
        pass


def _extract_reasoning_from_content(content: str) -> str:
    think_match = re.search(r"<think>(.*?)</think>", content, flags=re.DOTALL | re.IGNORECASE)
    if think_match:
        return think_match.group(1).strip()
    return ""


def _strip_think_blocks(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)


def _parse_performers(text: str) -> List[str]:
    text = text.strip()
    performers: List[str] = []
    start = text.find("["); end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        maybe_json = text[start : end + 1]
        try:
            arr = json.loads(maybe_json)
            if isinstance(arr, list):
                performers = [str(x) for x in arr]
        except Exception:
            pass
    if not performers:
        sep = "," if "," in text else "\n"
        performers = [t.strip() for t in text.split(sep)]

    cleaned: List[str] = []
    for t in performers:
        t = t.strip().strip("#").strip().strip('"').strip("'").strip()
        if 1 <= len(t) <= 100:
            cleaned.append(t)
    seen = set()
    uniq: List[str] = []
    for t in cleaned:
        if t and t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# ----------------------------
# Non-streaming LLM calls
# ----------------------------

def _call_llm_b64_image_nonstreaming(b64: str, mime: str, existing_performers: Optional[list[dict[str, str]]] = None) -> str:
    url = f"{BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY and API_KEY != "none" else {}
    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
    use_images = _resolve_use_performer_images()

    if existing_performers:
        intro, payload = _format_performers_for_prompt(existing_performers, INCLUDE_PERFORMER_DETAILS)
        performer_text = json.dumps(payload, ensure_ascii=False)
        content: list[dict[str, Any]] = [{"type": "text", "text": f"EXISTING PERFORMERS (provided as input context, these are NOT your output):\n\n{intro}\n\n{performer_text}"}]
        if use_images:
            _append_performer_images(content, existing_performers)
        messages.append({"role": "user", "content": content})

    messages.append({"role": "user", "content": [{"type": "text", "text": "The following is the image to analyze. Identify which performers from the catalog are visible:"}]})
    messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]})

    _log_prompt(messages, "Image Prompt")

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS}
    if not ENABLE_THINKING:
        payload["enable_thinking"] = False
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["reasoning_control"] = True
        payload["reasoning_format"] = "auto"
    _save_debug_log(payload)
    data = _http_post_json(url, payload, headers=headers, timeout=TIMEOUT)
    try:
        msg = (data["choices"][0]["message"]) or {}
        content = _message_content_to_str(msg.get("content"))
        if not content:
            content = _message_content_to_str(msg)
        return content
    except Exception:
        raise RuntimeError(f"Unexpected LLM response: {data!r}")


def _call_llm_b64_frames_nonstreaming(b64_frames: List[str], existing_performers: Optional[list[dict[str, str]]] = None) -> str:
    url = f"{BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}"} if API_KEY and API_KEY != "none" else {}
    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
    use_images = _resolve_use_performer_images()

    if existing_performers:
        intro, payload = _format_performers_for_prompt(existing_performers, INCLUDE_PERFORMER_DETAILS)
        performer_text = json.dumps(payload, ensure_ascii=False)
        content: list[dict[str, Any]] = [{"type": "text", "text": f"EXISTING PERFORMERS (provided as input context, these are NOT your output):\n\n{intro}\n\n{performer_text}"}]
        if use_images:
            _append_performer_images(content, existing_performers)
        messages.append({"role": "user", "content": content})

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "The following are frames from the video to analyze. Identify which performers from the catalog are visible:"}],
    })

    for i, b64 in enumerate(b64_frames):
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Frame {i + 1}:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        })

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "Based on the frames above, return a JSON array of performer names visible in the video."}],
    })

    _log_prompt(messages, "Scene Prompt")

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS}
    if not ENABLE_THINKING:
        payload["enable_thinking"] = False
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["reasoning_control"] = True
        payload["reasoning_format"] = "auto"
    _save_debug_log(payload)
    data = _http_post_json(url, payload, headers=headers, timeout=TIMEOUT)
    try:
        msg = (data["choices"][0]["message"]) or {}
        content = _message_content_to_str(msg.get("content"))
        if not content:
            content = _message_content_to_str(msg)
        return content
    except Exception:
        raise RuntimeError(f"Unexpected LLM response: {data!r}")


# ----------------------------
# Streaming + result files
# ----------------------------

def _plugin_dir() -> str:
    sc = stash.JSON_INPUT.get("server_connection") or stash.JSON_INPUT.get("serverConnection") or {}
    if isinstance(sc, dict):
        for key in ("plugin_dir", "PluginDir", "pluginDir"):
            val = sc.get(key)
            if isinstance(val, str) and val:
                return val
    return os.path.dirname(os.path.abspath(__file__))


def _write_stream_progress(entity_id, request_id, reasoning, output, done, entity_type, error=None):
    if not entity_id or not request_id:
        return
    results_dir = os.path.join(_plugin_dir(), "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(request_id).strip())
    payload = {
        "entity_id": int(entity_id),
        "entity_type": entity_type,
        "done": bool(done),
        "reasoning": str(reasoning) if reasoning else "",
        "output": str(output) if output else "",
        "error": str(error) if error else None,
    }
    tmp_path = os.path.join(results_dir, f"{entity_type}_{entity_id}_{safe_request_id}_stream.json.tmp")
    final_path = os.path.join(results_dir, f"{entity_type}_{entity_id}_{safe_request_id}_stream.json")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, final_path)


def _write_result(entity_id: int, entity_type: str, performers: List[str], error: Optional[str] = None, request_id: Optional[str] = None, reasoning: str = "", output: str = "") -> None:
    results_dir = os.path.join(_plugin_dir(), "results")
    os.makedirs(results_dir, exist_ok=True)
    safe_request_id = None
    if isinstance(request_id, str) and request_id.strip():
        safe_request_id = re.sub(r"[^A-Za-z0-9_-]", "_", request_id.strip())
    payload = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "performers": performers,
        "error": error,
        "request_id": safe_request_id,
        "reasoning": reasoning,
        "output": output,
    }
    suffix = f"_{safe_request_id}" if safe_request_id else ""
    tmp_path = os.path.join(results_dir, f"{entity_type}_{entity_id}{suffix}.json.tmp")
    final_path = os.path.join(results_dir, f"{entity_type}_{entity_id}{suffix}.json")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    os.replace(tmp_path, final_path)
    if safe_request_id:
        stream_path = os.path.join(results_dir, f"{entity_type}_{entity_id}_{safe_request_id}_stream.json")
        try:
            os.remove(stream_path)
        except OSError:
            pass


def _cleanup_old_results(max_age_seconds: int = 3600) -> None:
    results_dir = os.path.join(_plugin_dir(), "results")
    if not os.path.isdir(results_dir):
        return
    now = time.time()
    count = 0
    for fname in os.listdir(results_dir):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(results_dir, fname)
        try:
            if now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
                count += 1
        except OSError:
            pass
    if count:
        stash.Log(f"{TAG} Cleaned up {count} old result file(s)")


# ----------------------------
# Streaming LLM calls
# ----------------------------

def _call_llm_b64_image_streaming(b64, mime, existing_performers, request_id, entity_id, entity_type):
    url = f"{BASE_URL}/chat/completions"
    h = {"Content-Type": "application/json"}
    if API_KEY and API_KEY != "none":
        h["Authorization"] = f"Bearer {API_KEY}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
    use_images = _resolve_use_performer_images()

    if existing_performers:
        intro, payload = _format_performers_for_prompt(existing_performers, INCLUDE_PERFORMER_DETAILS)
        performer_text = json.dumps(payload, ensure_ascii=False)
        content: list[dict[str, Any]] = [{"type": "text", "text": f"EXISTING PERFORMERS (provided as input context, these are NOT your output):\n\n{intro}\n\n{performer_text}"}]
        if use_images:
            _append_performer_images(content, existing_performers)
        messages.append({"role": "user", "content": content})

    messages.append({"role": "user", "content": [{"type": "text", "text": "The following is the image to analyze. Identify which performers from the catalog are visible:"}]})
    messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}]})

    _log_prompt(messages, "Image Streaming Prompt")

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS, "stream": True}
    if not ENABLE_THINKING:
        payload["enable_thinking"] = False
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["reasoning_control"] = True
        payload["reasoning_format"] = "auto"

    _save_debug_log(payload)

    try:
        stash.Error(f"{TAG} LLM Request Payload: {json.dumps(_sanitize_payload_for_log(payload), indent=2)}")
    except Exception:
        pass
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")

    reasoning_parts: list[str] = []
    output_parts: list[str] = []
    partial_line = ""

    _write_stream_progress(entity_id, request_id, "", "", done=False, entity_type=entity_type)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            while True:
                byte = resp.read(1)
                if not byte:
                    break
                partial_line += byte.decode("utf-8", errors="replace")

                if not partial_line.endswith("\n"):
                    continue

                line = partial_line.strip()
                partial_line = ""
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices")
                if not choices or not isinstance(choices, list):
                    continue
                delta = (choices[0] or {}).get("delta", {})
                if not isinstance(delta, dict):
                    continue

                rc = delta.get("reasoning_content")
                c = delta.get("content")

                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
                if isinstance(c, str) and c:
                    output_parts.append(c)

                _write_stream_progress(
                    entity_id, request_id,
                    "".join(reasoning_parts),
                    "".join(output_parts),
                    done=False,
                    entity_type=entity_type,
                )
    except Exception as e:
        _write_stream_progress(
            entity_id, request_id,
            "".join(reasoning_parts),
            "".join(output_parts),
            done=True, entity_type=entity_type, error=str(e),
        )
        raise

    reasoning_text = "".join(reasoning_parts)
    output_text = "".join(output_parts)

    if not output_text and not reasoning_text:
        raise RuntimeError("No content received from LLM stream")

    _write_stream_progress(entity_id, request_id, reasoning_text, output_text, done=True, entity_type=entity_type)

    return reasoning_text, output_text


def _call_llm_b64_frames_streaming(b64_frames, existing_performers, request_id, entity_id, entity_type):
    url = f"{BASE_URL}/chat/completions"
    h = {"Content-Type": "application/json"}
    if API_KEY and API_KEY != "none":
        h["Authorization"] = f"Bearer {API_KEY}"

    messages: list[dict[str, Any]] = [{"role": "system", "content": PROMPT}]
    use_images = _resolve_use_performer_images()

    if existing_performers:
        intro, payload = _format_performers_for_prompt(existing_performers, INCLUDE_PERFORMER_DETAILS)
        performer_text = json.dumps(payload, ensure_ascii=False)
        content: list[dict[str, Any]] = [{"type": "text", "text": f"EXISTING PERFORMERS (provided as input context, these are NOT your output):\n\n{intro}\n\n{performer_text}"}]
        if use_images:
            _append_performer_images(content, existing_performers)
        messages.append({"role": "user", "content": content})

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "The following are frames from the video to analyze. Identify which performers from the catalog are visible:"}],
    })

    for i, b64 in enumerate(b64_frames):
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": f"Frame {i + 1}:"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        })

    messages.append({
        "role": "user",
        "content": [{"type": "text", "text": "Based on the frames above, return a JSON array of performer names visible in the video."}],
    })

    _log_prompt(messages, "Scene Streaming Prompt")

    payload = {"model": MODEL, "messages": messages, "temperature": TEMP, "max_tokens": MAX_TOKENS, "stream": True}
    if not ENABLE_THINKING:
        payload["enable_thinking"] = False
        payload["chat_template_kwargs"] = {"enable_thinking": False}
        payload["reasoning_control"] = True
        payload["reasoning_format"] = "auto"

    _save_debug_log(payload)

    try:
        stash.Error(f"{TAG} LLM Request Payload: {json.dumps(_sanitize_payload_for_log(payload), indent=2)}")
    except Exception:
        pass
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")

    reasoning_parts: list[str] = []
    output_parts: list[str] = []
    partial_line = ""

    _write_stream_progress(entity_id, request_id, "", "", done=False, entity_type=entity_type)

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            while True:
                byte = resp.read(1)
                if not byte:
                    break
                partial_line += byte.decode("utf-8", errors="replace")

                if not partial_line.endswith("\n"):
                    continue

                line = partial_line.strip()
                partial_line = ""
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[len("data: "):]
                if data_str == "[DONE]":
                    break
                try:
                    event = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choices = event.get("choices")
                if not choices or not isinstance(choices, list):
                    continue
                delta = (choices[0] or {}).get("delta", {})
                if not isinstance(delta, dict):
                    continue

                rc = delta.get("reasoning_content")
                c = delta.get("content")

                if isinstance(rc, str) and rc:
                    reasoning_parts.append(rc)
                if isinstance(c, str) and c:
                    output_parts.append(c)

                _write_stream_progress(
                    entity_id, request_id,
                    "".join(reasoning_parts),
                    "".join(output_parts),
                    done=False,
                    entity_type=entity_type,
                )
    except Exception as e:
        _write_stream_progress(
            entity_id, request_id,
            "".join(reasoning_parts),
            "".join(output_parts),
            done=True, entity_type=entity_type, error=str(e),
        )
        raise

    reasoning_text = "".join(reasoning_parts)
    output_text = "".join(output_parts)

    if not output_text and not reasoning_text:
        raise RuntimeError("No content received from LLM stream")

    _write_stream_progress(entity_id, request_id, reasoning_text, output_text, done=True, entity_type=entity_type)

    return reasoning_text, output_text


# ----------------------------
# Orchestrators
# ----------------------------

def _call_llm_b64_image(b64: str, mime: str, existing_performers: Optional[list[dict[str, str]]] = None, request_id: Optional[str] = None, entity_id: Optional[int] = None, entity_type: str = "image") -> tuple[str, str]:
    if request_id and entity_id:
        try:
            reasoning, content = _call_llm_b64_image_streaming(b64, mime, existing_performers, request_id, entity_id, entity_type)
            return reasoning, content
        except Exception as e:
            stash.Error(f"{TAG} Streaming failed ({e}), falling back to non-streaming")

    content = _call_llm_b64_image_nonstreaming(b64, mime, existing_performers)
    reasoning = _extract_reasoning_from_content(content)
    return reasoning, content


def _call_llm_b64_frames(b64_frames: List[str], existing_performers: Optional[list[dict[str, str]]] = None, request_id: Optional[str] = None, entity_id: Optional[int] = None, entity_type: str = "scene") -> tuple[str, str]:
    if request_id and entity_id:
        try:
            reasoning, content = _call_llm_b64_frames_streaming(b64_frames, existing_performers, request_id, entity_id, entity_type)
            return reasoning, content
        except Exception as e:
            stash.Error(f"{TAG} Streaming failed ({e}), falling back to non-streaming")

    content = _call_llm_b64_frames_nonstreaming(b64_frames, existing_performers)
    reasoning = _extract_reasoning_from_content(content)
    return reasoning, content


# ----------------------------
# Entity path fetching
# ----------------------------

def _fetch_image_path(image_id: int) -> Optional[str]:
    try:
        query = """
            query($id: ID!) {
              findImage(id: $id) {
                paths { image }
                files { path }
              }
            }
        """
        resp = stash._graphql(query, {"id": str(image_id)})  # type: ignore[attr-defined]
        img = (resp or {}).get("data", {}).get("findImage") or {}
        path = None
        paths = img.get("paths") or {}
        if isinstance(paths, dict):
            path = paths.get("image")
        if not path:
            files = img.get("files") or []
            if isinstance(files, list) and files:
                path = (files[0] or {}).get("path")
        return path
    except Exception as e:
        stash.Error(f"{TAG} GraphQL path lookup failed for image {image_id}: {e}")
        return None


def _fetch_scene_path(scene_id: int) -> Optional[str]:
    try:
        query = """
            query($id: ID!) {
              findScene(id: $id) {
                files { path }
              }
            }
        """
        resp = stash._graphql(query, {"id": str(scene_id)})  # type: ignore[attr-defined]
        scene = (resp or {}).get("data", {}).get("findScene") or {}
        files = scene.get("files") or []
        if isinstance(files, list) and files:
            return (files[0] or {}).get("path")
        return None
    except Exception as e:
        stash.Error(f"{TAG} GraphQL path lookup failed for scene {scene_id}: {e}")
        return None


# ----------------------------
# Main tagging functions
# ----------------------------

def performers_from_image(image_id: int, request_id: Optional[str] = None) -> tuple[Optional[List[str]], str, str]:
    path = _fetch_image_path(image_id)
    if not path:
        stash.Error(f"{TAG} No image path found for id={image_id}")
        return None, "", ""

    try:
        existing = _existing_performers()
    except Exception:
        existing = []

    try:
        data, mime = _read_image_bytes(path)
        b64 = base64.b64encode(data).decode("utf-8")
        reasoning, content = _call_llm_b64_image(b64, mime, existing_performers=existing, request_id=request_id, entity_id=image_id, entity_type="image")
        stash.Error(f"{TAG} LLM raw output: {content}")
        cleaned = _strip_think_blocks(content)
        performers = _parse_performers(cleaned)
        return performers, reasoning, content
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"{TAG} Performer tagging failed for image {image_id}: {e}\n{tb}")
        return [], "", ""


def performers_from_scene(scene_id: int, request_id: Optional[str] = None) -> tuple[Optional[List[str]], str, str]:
    video_path = _fetch_scene_path(scene_id)
    if not video_path:
        stash.Error(f"{TAG} No video file path found for scene id={scene_id}")
        return None, "", ""

    if not os.path.isfile(video_path):
        stash.Error(f"{TAG} Video file does not exist: {video_path}")
        return None, "", ""

    try:
        existing = _existing_performers()
    except Exception:
        existing = []

    try:
        b64_frames = _extract_frames(video_path, MAX_FRAMES, SCENE_THRESHOLD, FRAME_WIDTH)
        stash.Log(f"{TAG} Extracted {len(b64_frames)} frames for scene {scene_id}")

        reasoning, content = _call_llm_b64_frames(b64_frames, existing_performers=existing, request_id=request_id, entity_id=scene_id, entity_type="scene")
        stash.Error(f"{TAG} LLM raw output: {content}")
        cleaned = _strip_think_blocks(content)
        performers = _parse_performers(cleaned)
        return performers, reasoning, content
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"{TAG} Performer tagging failed for scene {scene_id}: {e}\n{tb}")
        return [], "", ""


def tag_image_performers_task() -> None:
    try:
        args = stash.JSON_INPUT.get("args", {}) if stash.JSON_INPUT else {}
        image_id = args.get("image_id")
        request_id = args.get("request_id")
        if image_id is None:
            stash.Error(f"{TAG} No image_id supplied to tag_image_performers_task")
            return
        image_id = int(image_id)
        performers, reasoning, output = performers_from_image(image_id, request_id=request_id)
        error = None
        if performers is None:
            error = "No image path found."
            performers = []
        _write_result(image_id, "image", performers, error=error, request_id=request_id, reasoning=reasoning, output=output)
        if performers:
            stash.Error(f"{TAG} Suggested performers for image {image_id}: {performers}")
        else:
            stash.Error(f"{TAG} No performers returned for image {image_id}")
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"{TAG} Exception in tag_image_performers_task: {e}\nTraceBack={tb}")
        try:
            args = stash.JSON_INPUT.get("args", {}) if stash.JSON_INPUT else {}
            image_id = args.get("image_id")
            if image_id is not None:
                request_id = args.get("request_id")
                _write_result(int(image_id), "image", [], error=str(e), request_id=request_id, reasoning="", output="")
        except Exception:
            pass


def tag_scene_performers_task() -> None:
    try:
        args = stash.JSON_INPUT.get("args", {}) if stash.JSON_INPUT else {}
        scene_id = args.get("scene_id")
        request_id = args.get("request_id")
        if scene_id is None:
            stash.Error(f"{TAG} No scene_id supplied to tag_scene_performers_task")
            return
        scene_id = int(scene_id)
        performers, reasoning, output = performers_from_scene(scene_id, request_id=request_id)
        error = None
        if performers is None:
            error = "No video file path found."
            performers = []
        _write_result(scene_id, "scene", performers, error=error, request_id=request_id, reasoning=reasoning, output=output)
        if performers:
            stash.Error(f"{TAG} Suggested performers for scene {scene_id}: {performers}")
        else:
            stash.Error(f"{TAG} No performers returned for scene {scene_id}")
    except Exception as e:
        tb = traceback.format_exc()
        stash.Error(f"{TAG} Exception in tag_scene_performers_task: {e}\nTraceBack={tb}")
        try:
            args = stash.JSON_INPUT.get("args", {}) if stash.JSON_INPUT else {}
            scene_id = args.get("scene_id")
            if scene_id is not None:
                request_id = args.get("request_id")
                _write_result(int(scene_id), "scene", [], error=str(e), request_id=request_id, reasoning="", output="")
        except Exception:
            pass


# -------------
# Entry point
# -------------
try:
    _cleanup_old_results()
    if stash.Setting("zzdebugTracing", False):
        stash.Error(f"{TAG} Using BASE_URL={BASE_URL!r} model={MODEL!r} temp={TEMP} max_tokens={MAX_TOKENS} timeout={TIMEOUT}")
        stash.Error(f"{TAG} maxFrames={MAX_FRAMES} sceneThreshold={SCENE_THRESHOLD} frameWidth={FRAME_WIDTH}")
        stash.Error(f"{TAG} includePerformerDescriptions={INCLUDE_PERFORMER_DETAILS} includePerformerImages={INCLUDE_PERFORMER_IMAGES}")
    stash.Error(f"{TAG} ENABLE_THINKING raw={_ENABLE_THINKING_RAW!r} gql={_ENABLE_THINKING_GQL!r} resolved={ENABLE_THINKING!r} env={os.getenv('LLM_ENABLE_THINKING')!r}")
    if not ENABLE_THINKING:
        stash.Error(f"{TAG} Thinking DISABLED: will add chat_template_kwargs, reasoning_control, reasoning_format to all LLM requests")
    if INCLUDE_PERFORMER_IMAGES:
        stash.Log(f"{TAG} includePerformerImages is ENABLED — performer images will be sent to the LLM")

    mode = None
    if stash.PLUGIN_TASK_NAME:
        stash.Error(f"PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}")
        if stash.PLUGIN_TASK_NAME == "tag_image_performers_task":
            mode = "tag_image_performers"
        elif stash.PLUGIN_TASK_NAME == "tag_scene_performers_task":
            mode = "tag_scene_performers"
    if not mode and stash.JSON_INPUT:
        mode = (stash.JSON_INPUT.get("args", {}).get("mode") or "").strip()
    if not mode:
        mode = ""

    if mode == "tag_image_performers":
        stash.Error(f"Dispatch via mode=tag_image_performers")
        tag_image_performers_task()
    elif mode == "tag_scene_performers":
        stash.Error(f"Dispatch via mode=tag_scene_performers")
        tag_scene_performers_task()
    else:
        stash.Error(f"{TAG} Unknown or unspecified mode (mode={mode!r}, PLUGIN_TASK_NAME={stash.PLUGIN_TASK_NAME}). Nothing to do.")
except Exception as e:
    tb = traceback.format_exc()
    stash.Error(f"{TAG} Exception while running plugin: {e}\nTraceBack={tb}")

try:
    print("null")
except Exception:
    pass
