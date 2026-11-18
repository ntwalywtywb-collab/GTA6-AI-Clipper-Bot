import os
import shutil
import subprocess
import tempfile
from typing import Dict, List, Optional, Tuple

import yt_dlp
import imageio_ffmpeg


COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"
DENO_PATH = "/opt/render/project/src/.deno/bin/deno"


def _base_options(cookie_path: Optional[str] = None, player_client: Optional[str] = None) -> dict:
    """Build yt-dlp options without forcing a format selector."""
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

    # Try alternate YouTube player clients on later attempts.
    if player_client:
        options["extractor_args"] = {
            "youtube": {
                "player_client": [player_client],
                "formats": ["incomplete"],
            }
        }
    return options


def _is_video(fmt: dict) -> bool:
    return bool(fmt.get("url")) and fmt.get("vcodec") not in (None, "none")


def _is_audio(fmt: dict) -> bool:
    return bool(fmt.get("url")) and fmt.get("acodec") not in (None, "none")


def _format_score(fmt: dict) -> Tuple[int, int, float]:
    protocol = (fmt.get("protocol") or "").lower()
    # HLS manifests are generally the most practical input for a live capture.
    protocol_score = 100 if "m3u8" in protocol else 50 if protocol in ("https", "http") else 10
    height = int(fmt.get("height") or 0)
    bitrate = float(fmt.get("tbr") or 0)
    return protocol_score, height, bitrate


def _choose_formats(info: dict) -> Tuple[dict, Optional[dict]]:
    """Return (video_or_combined_format, separate_audio_format_or_none)."""
    formats = [
        fmt for fmt in (info.get("formats") or [])
        if isinstance(fmt, dict) and fmt.get("url") and not fmt.get("has_drm")
    ]

    combined = [
        fmt for fmt in formats
        if _is_video(fmt) and _is_audio(fmt)
    ]
    if combined:
        combined.sort(key=_format_score, reverse=True)
        return combined[0], None

    videos = [fmt for fmt in formats if _is_video(fmt)]
    audios = [fmt for fmt in formats if _is_audio(fmt)]

    if not videos:
        raise RuntimeError(
            "YouTube returned no usable video stream formats. "
            "Check the yt-dlp logs, cookies, and YouTube player extraction."
        )

    videos.sort(key=_format_score, reverse=True)
    video = videos[0]

    if audios:
        audios.sort(key=_format_score, reverse=True)
        return video, audios[0]

    return video, None


def _copy_cookies_to_temp() -> Optional[str]:
    """Copy Render's read-only secret file to writable /tmp."""
    if not (os.path.isfile(COOKIES_FILE) and os.access(COOKIES_FILE, os.R_OK)):
        return None

    fd, temp_path = tempfile.mkstemp(
        prefix="youtube_cookies_", suffix=".txt", dir="/tmp"
    )
    os.close(fd)
    shutil.copyfile(COOKIES_FILE, temp_path)
    return temp_path


def _extract_raw_info(url: str, cookie_path: Optional[str]) -> dict:
    """
    Retry extraction with different YouTube player clients.
    process=False is intentional: it prevents yt-dlp from applying a format
    selector before we can inspect the formats returned by YouTube.
    """
    attempts = [
        ("default", None),
        ("tv", "tv"),
        ("mweb", "mweb"),
        ("ios", "ios"),
        ("web", "web"),
    ]
    errors: List[str] = []

    for label, player_client in attempts:
        try:
            print(f"[live] Extraction attempt: {label}", flush=True)
            options = _base_options(cookie_path, player_client)

            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False, process=False)

            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp returned an empty or invalid result.")

            if info.get("_type") == "playlist":
                entries = info.get("entries") or []
                info = next((entry for entry in entries if entry), None)
                if not isinstance(info, dict):
                    raise RuntimeError("No video entry was returned by YouTube.")

            formats = info.get("formats") or []
            usable = [
                fmt for fmt in formats
                if isinstance(fmt, dict)
                and fmt.get("url")
                and (fmt.get("vcodec") not in (None, "none")
                     or fmt.get("acodec") not in (None, "none"))
            ]

            if usable:
                print(
                    f"[live] {label}: received {len(usable)} usable stream format(s)",
                    flush=True,
                )
                return info

            errors.append(f"{label}: no usable formats returned")
        except Exception as exc:
            message = f"{label}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"[live] {message}", flush=True)

    raise RuntimeError(
        "All YouTube extraction methods failed:\n" + "\n".join(errors)
    )


def get_live_stream(url: str) -> dict:
    """Extract a usable direct/manifest URL and HTTP headers for YouTube Live."""
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
    is_live = info.get("is_live")
    if live_status not in ("is_live", "was_live") and not is_live:
        raise RuntimeError(
            f"The supplied URL does not appear to be an active live stream "
            f"(live_status={live_status!r})."
        )

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
    """Format HTTP headers exactly as FFmpeg expects."""
    return "".join(f"{key}: {value}\r\n" for key, value in headers.items())


def start_live_capture(url: str, output_dir: str, segment_seconds: int = 60) -> dict:
    """
    Start capturing a live stream into consecutive 60-second MPEG-TS files.
    Returns a dictionary containing the FFmpeg process and output pattern.
    """
    if int(segment_seconds) != 60:
        raise ValueError("Live capture segments are configured for 60 seconds.")

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
        command.extend(["-i", live["audio_url"]])
        command.extend([
            "-map", "0:v:0",
            "-map", "1:a:0",
        ])
    else:
        command.extend([
            "-map", "0:v:0",
            "-map", "0:a:0?",
        ])

    command.extend([
        "-c", "copy",
        "-f", "segment",
        "-segment_time", "60",
        "-reset_timestamps", "1",
        "-segment_format", "mpegts",
        output_pattern,
    ])

    print(
        f"[live] Starting FFmpeg capture; video format={live.get('format_id')}, "
        f"audio format={live.get('audio_format_id')}",
        flush=True,
    )

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    return {
        "process": process,
        "output_pattern": output_pattern,
        "title": live["title"],
        "segment_seconds": 60,
        "format_id": live.get("format_id"),
    }


def stop_live_capture(process) -> bool:
    """Stop the FFmpeg live-capture process safely."""
    if process is None or process.poll() is not None:
        return False

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()

    return True
