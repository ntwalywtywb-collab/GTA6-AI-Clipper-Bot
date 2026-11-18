import os
import shutil
import subprocess
import tempfile
from typing import List, Optional, Tuple

import yt_dlp
import imageio_ffmpeg

COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"
DENO_PATH = "/opt/render/project/src/.deno/bin/deno"


def _base_options(cookie_path: Optional[str] = None, player_client: Optional[str] = None) -> dict:
    options = {
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 35,
        "retries": 4,
        "fragment_retries": 4,
        "extractor_retries": 3,
        "live_from_start": False,
        "cachedir": False,
        "skip_download": True,
    }
    if cookie_path:
        options["cookiefile"] = cookie_path
    if os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK):
        options["js_runtimes"] = {"deno": {"path": DENO_PATH}}
        options["remote_components"] = ["ejs:github"]
    if player_client:
        options["extractor_args"] = {"youtube": {"player_client": [player_client], "formats": ["incomplete"]}}
    return options


def _is_video(fmt: dict) -> bool:
    return bool(fmt.get("url")) and fmt.get("vcodec") not in (None, "none")


def _is_audio(fmt: dict) -> bool:
    return bool(fmt.get("url")) and fmt.get("acodec") not in (None, "none")


def _format_score(fmt: dict) -> Tuple[int, int, float]:
    protocol = (fmt.get("protocol") or "").lower()
    protocol_score = 100 if "m3u8" in protocol else 50 if protocol in ("https", "http") else 10
    return protocol_score, int(fmt.get("height") or 0), float(fmt.get("tbr") or 0)


def _choose_formats(info: dict) -> Tuple[dict, Optional[dict]]:
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict) and f.get("url") and not f.get("has_drm")]
    combined = [f for f in formats if _is_video(f) and _is_audio(f)]
    if combined:
        return sorted(combined, key=_format_score, reverse=True)[0], None
    videos = sorted([f for f in formats if _is_video(f)], key=_format_score, reverse=True)
    audios = sorted([f for f in formats if _is_audio(f)], key=_format_score, reverse=True)
    if not videos:
        raise RuntimeError("YouTube returned no usable video stream formats. Check yt-dlp logs, cookies, and player extraction.")
    return videos[0], audios[0] if audios else None


def _copy_cookies_to_temp() -> Optional[str]:
    if not (os.path.isfile(COOKIES_FILE) and os.access(COOKIES_FILE, os.R_OK)):
        return None
    fd, temp_path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt", dir="/tmp")
    os.close(fd)
    shutil.copyfile(COOKIES_FILE, temp_path)
    return temp_path


def _extract_raw_info(url: str, cookie_path: Optional[str]) -> dict:
    attempts = [("default", None), ("tv", "tv"), ("mweb", "mweb"), ("ios", "ios"), ("web", "web")]
    errors: List[str] = []
    for label, player_client in attempts:
        try:
            print(f"[live] Extraction attempt: {label}", flush=True)
            with yt_dlp.YoutubeDL(_base_options(cookie_path, player_client)) as ydl:
                info = ydl.extract_info(url, download=False, process=False)
            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp returned an empty or invalid result.")
            if info.get("_type") == "playlist":
                info = next((entry for entry in (info.get("entries") or []) if entry), None)
            if not isinstance(info, dict):
                raise RuntimeError("No video entry was returned by YouTube.")
            usable = [f for f in (info.get("formats") or []) if isinstance(f, dict) and f.get("url") and (f.get("vcodec") not in (None, "none") or f.get("acodec") not in (None, "none"))]
            if usable:
                print(f"[live] {label}: received {len(usable)} usable stream format(s)", flush=True)
                return info
            errors.append(f"{label}: no usable formats returned")
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {exc}")
            print(f"[live] {errors[-1]}", flush=True)
    raise RuntimeError("All YouTube extraction methods failed:\n" + "\n".join(errors))


def get_live_stream(url: str) -> dict:
    if "youtube.com" not in url and "youtu.be" not in url:
        raise ValueError("This module currently supports YouTube Live URLs only.")
    temp_cookie_path = None
    try:
        temp_cookie_path = _copy_cookies_to_temp()
        info = _extract_raw_info(url, temp_cookie_path)
    finally:
        if temp_cookie_path and os.path.exists(temp_cookie_path):
            try:
                os.remove(temp_cookie_path)
            except OSError:
                pass
    live_status = info.get("live_status")
    if live_status not in ("is_live", "was_live") and not info.get("is_live"):
        raise RuntimeError(f"The supplied URL does not appear to be an active live stream (live_status={live_status!r}).")
    video_fmt, audio_fmt = _choose_formats(info)
    return {
        "url": video_fmt["url"],
        "audio_url": audio_fmt.get("url") if audio_fmt else None,
        "title": info.get("title") or "YouTube Live",
        "headers": video_fmt.get("http_headers") or info.get("http_headers") or {},
        "audio_headers": (audio_fmt or {}).get("http_headers") or {},
        "format_id": video_fmt.get("format_id"),
        "audio_format_id": audio_fmt.get("format_id") if audio_fmt else None,
    }


def _header_argument(headers: dict) -> str:
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def start_live_capture(url: str, output_dir: str, segment_seconds: int = 180) -> dict:
    """Capture three-minute source chunks; the clipper chooses the best 60-second window."""
    segment_seconds = int(segment_seconds)
    if segment_seconds < 180:
        raise ValueError("Live capture segments must be at least 180 seconds for highlight selection.")
    os.makedirs(output_dir, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    live = get_live_stream(url)
    output_pattern = os.path.join(output_dir, "live_%05d.ts")
    command = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    video_headers = _header_argument(live.get("headers") or {})
    if video_headers:
        command.extend(["-headers", video_headers])
    command.extend(["-i", live["url"]])
    if live.get("audio_url"):
        audio_headers = _header_argument(live.get("audio_headers") or {})
        if audio_headers:
            command.extend(["-headers", audio_headers])
        command.extend(["-i", live["audio_url"], "-map", "0:v:0", "-map", "1:a:0"])
    else:
        command.extend(["-map", "0:v:0", "-map", "0:a:0?"])
    command.extend(["-c", "copy", "-f", "segment", "-segment_time", str(segment_seconds), "-reset_timestamps", "1", "-segment_format", "mpegts", output_pattern])
    print(f"[live] Starting FFmpeg capture; video format={live.get('format_id')}, audio format={live.get('audio_format_id')}, segment={segment_seconds}s", flush=True)
    # Do not leave an unread PIPE attached: it can fill and stall a long-running capture.
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"process": process, "output_pattern": output_pattern, "title": live["title"], "segment_seconds": segment_seconds, "format_id": live.get("format_id")}


def stop_live_capture(process) -> bool:
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    return True
            
