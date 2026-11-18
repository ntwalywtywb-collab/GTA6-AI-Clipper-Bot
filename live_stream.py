import os
import shutil
import subprocess
import tempfile

import yt_dlp
import imageio_ffmpeg


# Render Secret Files are read-only. We copy cookies to /tmp so yt-dlp
# can safely read/update its cookie jar without trying to write to /etc/secrets.
COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"
DENO_PATH = "/opt/render/project/src/.deno/bin/deno"


def get_live_stream(url):
    """Extract the direct URL and headers for an active YouTube Live stream."""
    if "youtube.com" not in url and "youtu.be" not in url:
        raise ValueError("This module currently supports YouTube Live URLs only.")

    options = {
        "format": "best",
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "live_from_start": False,
    }

    if os.path.isfile(DENO_PATH) and os.access(DENO_PATH, os.X_OK):
        options["js_runtimes"] = {"deno": {"path": DENO_PATH}}
        options["remote_components"] = ["ejs:github"]

    temp_cookie_path = None

    try:
        if os.path.isfile(COOKIES_FILE) and os.access(COOKIES_FILE, os.R_OK):
            fd, temp_cookie_path = tempfile.mkstemp(
                prefix="youtube_cookies_", suffix=".txt", dir="/tmp"
            )
            os.close(fd)
            shutil.copyfile(COOKIES_FILE, temp_cookie_path)
            options["cookiefile"] = temp_cookie_path

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)

    finally:
        if temp_cookie_path and os.path.exists(temp_cookie_path):
            try:
                os.remove(temp_cookie_path)
            except OSError:
                pass

    if info.get("live_status") != "is_live":
        raise RuntimeError("The supplied URL is not an active live stream.")

    stream_url = info.get("url")
    if not stream_url:
        raise RuntimeError("Could not extract a direct live-stream URL.")

    return {
        "url": stream_url,
        "title": info.get("title") or "YouTube Live",
        "headers": info.get("http_headers") or {},
    }


def start_live_capture(url, output_dir, segment_seconds=60):
    """
    Start capturing an active live stream into consecutive 60-second files.
    Returns a dictionary with the FFmpeg process and output pattern.
    """
    if int(segment_seconds) != 60:
        raise ValueError("Live capture segments are configured for 60 seconds.")

    os.makedirs(output_dir, exist_ok=True)

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    live = get_live_stream(url)

    output_pattern = os.path.join(output_dir, "live_%05d.ts")

    headers = live.get("headers", {})
    header_text = "".join(
        f"{key}: {value}\\r\\n" for key, value in headers.items()
    )

    command = [ffmpeg, "-hide_banner", "-loglevel", "warning"]

    if header_text:
        command.extend(["-headers", header_text])

    command.extend([
        "-i", live["url"],
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", "60",
        "-reset_timestamps", "1",
        "-segment_format", "mpegts",
        output_pattern,
    ])

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    return {
        "process": process,
        "output_pattern": output_pattern,
        "title": live["title"],
        "segment_seconds": 60,
    }


def stop_live_capture(process):
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
