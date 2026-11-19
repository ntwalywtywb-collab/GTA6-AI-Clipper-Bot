import os
import shutil
import tempfile
import subprocess
import time

import yt_dlp
import imageio_ffmpeg

COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"
DENO_PATH = "/opt/render/project/src/.deno/bin/deno"


def _cookie_copy(options):
    if not (os.path.isfile(COOKIES_FILE) and os.access(COOKIES_FILE, os.R_OK)):
        return None
    fd, path = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt", dir="/tmp")
    os.close(fd)
    shutil.copyfile(COOKIES_FILE, path)
    options["cookiefile"] = path
    return path


def _choose_format(info):
    """السماح بسحب بث يصل إلى 1080p/30fps للحفاظ على الجودة العالية."""
    direct = info.get("url")
    formats = info.get("formats") or []

    candidates = []
    for fmt in formats:
        if not fmt.get("url"):
            continue
        if fmt.get("vcodec") in (None, "none"):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        height = int(fmt.get("height") or 0)
        fps = float(fmt.get("fps") or 0)
        protocol = str(fmt.get("protocol") or "")
        
        # السماح بارتفاع حتى 1080p كحد أقصى للحفاظ على دقة عالية
        within_target = 1 if (height == 0 or height <= 1080) else 0
        fps_target = 1 if (fps == 0 or fps <= 30) else 0
        hls = 1 if ("m3u8" in protocol or "http" in protocol) else 0
        candidates.append((
            within_target,
            fps_target,
            hls,
            min(height or 1080, 1080),
            min(fps or 30, 30),
            fmt
        ))

    if candidates:
        candidates.sort(key=lambda item: item[:-1], reverse=True)
        chosen = candidates[0][-1]
        return {
            "url": chosen["url"],
            "headers": chosen.get("http_headers") or info.get("http_headers") or {},
            "format_id": chosen.get("format_id"),
            "height": chosen.get("height"),
        }

    if direct:
        return {
            "url": direct,
            "headers": info.get("http_headers") or {},
            "format_id": info.get("format_id"),
            "height": info.get("height"),
        }

    video_only = [
        f for f in formats
        if f.get("url") and f.get("vcodec") not in (None, "none")
    ]
    if video_only:
        video_only.sort(
            key=lambda f: min(int(f.get("height") or 1080), 1080),
            reverse=True
        )
        chosen = video_only[0]
        return {
            "url": chosen["url"],
            "headers": chosen.get("http_headers") or info.get("http_headers") or {},
            "format_id": chosen.get("format_id"),
            "height": chosen.get("height"),
        }

    raise RuntimeError("YouTube لم يُرجع رابط بث مباشر قابلاً للاستخدام.")


def get_live_stream(url):
    if "youtube.com" not in url and "youtu.be" not in url:
        raise ValueError("هذا الإصدار يدعم روابط YouTube Live فقط.")

    options = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "cachedir": False,
        "socket_timeout": 35,
        "retries": 4,
        "fragment_retries": 4,
        "extractor_retries": 3,
        "live_from_start": False,
        "skip_download": True,
    }

    if os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK):
        options["js_runtimes"] = {"deno": {"path": DENO_PATH}}
        options["remote_components"] = ["ejs:github"]

    cookie_path = None
    try:
        cookie_path = _cookie_copy(options)
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False, process=False)
    finally:
        if cookie_path and os.path.exists(cookie_path):
            try:
                os.remove(cookie_path)
            except OSError:
                pass

    if not info:
        raise RuntimeError("لم يتمكن yt-dlp من قراءة معلومات البث.")
    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((entry for entry in info["entries"] if entry), None)
    if not info:
        raise RuntimeError("لم يتم العثور على معلومات البث.")

    live_status = info.get("live_status")
    if live_status not in ("is_live", "is_upcoming") and not info.get("is_live"):
        raise RuntimeError(
            f"الرابط ليس بثاً مباشراً نشطاً حالياً (live_status={live_status})."
        )

    selected = _choose_format(info)
    return {
        "url": selected["url"],
        "title": info.get("title") or "YouTube Live",
        "headers": selected.get("headers") or {},
        "format_id": selected.get("format_id"),
        "height": selected.get("height"),
    }


LIVE_SEGMENT_SECONDS = 180


def start_live_capture(url, output_dir, segment_seconds=LIVE_SEGMENT_SECONDS):
    """Capture closed 180-second segments for later 60-second clip analysis."""
    segment_seconds = int(segment_seconds)
    if segment_seconds != LIVE_SEGMENT_SECONDS:
        raise ValueError(
            f"مدة أجزاء البث يجب أن تكون {LIVE_SEGMENT_SECONDS} ثانية."
        )

    os.makedirs(output_dir, exist_ok=True)
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    live = get_live_stream(url)

    output_pattern = os.path.join(output_dir, "live_%05d.ts")
    headers = live.get("headers") or {}
    header_text = "".join(
        f"{key}: {value}\r\n" for key, value in headers.items()
    )

    command = [ffmpeg, "-hide_banner", "-loglevel", "warning"]
    if header_text:
        command.extend(["-headers", header_text])

    command.extend([
        "-rw_timeout", "20000000",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-i", live["url"],
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-segment_time_delta", "1",
        "-reset_timestamps", "1",
        "-segment_format", "mpegts",
        "-segment_start_number", "0",
        output_pattern,
    ])

    log_path = os.path.join(output_dir, "capture_ffmpeg.log")
    log_file = open(log_path, "ab")
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
        )
    finally:
        log_file.close()

    return {
        "process": process,
        "output_pattern": output_pattern,
        "title": live["title"],
        "segment_seconds": segment_seconds,
        "format_id": live.get("format_id"),
        "height": live.get("height"),
        "log_path": log_path,
    }


def stop_live_capture(process):
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return True

    
