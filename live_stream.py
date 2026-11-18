
import os
import subprocess
import shutil
import tempfile
import traceback

import yt_dlp
import imageio_ffmpeg


COOKIES_FILE = "/etc/secrets/m.youtube.com_cookies.txt"

DENO_PATH = "/opt/render/project/src/.deno/bin/deno"


def get_live_stream(url):
    """
    استخراج رابط البث المباشر ومعلوماته.
    يبدأ الالتقاط من اللحظة الحالية.
    """

    if "youtube.com" not in url and "youtu.be" not in url:
        raise ValueError("هذا الملف مخصص لبثوث YouTube فقط")

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

    if os.path.isfile(DENO_PATH):
        options["js_runtimes"] = {
            "deno": {
                "path": DENO_PATH
            }
        }
        options["remote_components"] = ["ejs:github"]

    if os.path.isfile(COOKIES_FILE):
        options["cookiefile"] = COOKIES_FILE

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(
            url,
            download=False
        )

    if info.get("live_status") != "is_live":
        raise Exception(
            "الرابط ليس بثاً مباشراً نشطاً حالياً"
        )

    stream_url = info.get("url")

    if not stream_url:
        raise Exception(
            "تعذر استخراج رابط البث"
        )

    return {
        "url": stream_url,
        "title": info.get("title", "YouTube Live"),
        "headers": info.get("http_headers", {}),
        "status": info.get("live_status")
    }


def start_live_capture(
    url,
    output_dir,
    segment_seconds=30
):
    """
    بدء التقاط البث وتقسيمه إلى ملفات
    قصيرة مدة كل منها 30 ثانية.
    """

    os.makedirs(
        output_dir,
        exist_ok=True
    )

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()

    live = get_live_stream(url)

    output_pattern = os.path.join(
        output_dir,
        "live_%05d.ts"
    )

    headers = live.get("headers", {})

    header_text = "".join(
        f"{key}: {value}\r\n"
        for key, value in headers.items()
    )

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning"
    ]

    if header_text:
        command.extend([
            "-headers",
            header_text
        ])

    command.extend([
        "-i", live["url"],
        "-map", "0:v:0",
        "-map", "0:a?",
        "-c", "copy",
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-reset_timestamps", "1",
        "-segment_format", "mpegts",
        output_pattern
    ])

    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    return {
        "process": process,
        "output_pattern": output_pattern,
        "title": live["title"],
        "segment_seconds": segment_seconds
    }


def stop_live_capture(process):
    """
    إيقاف التقاط البث.
    """

    if process and process.poll() is None:
        process.terminate()

        try:
            process.wait(timeout=10)

        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

        return True

    return False
