import glob
import os
import tempfile
import time
import traceback

import imageio_ffmpeg

from bot_core import *
from live_stream import start_live_capture, stop_live_capture


def convert_live_segment(chat_id, segment_file, output_file, ffmpeg):
    command = [
        ffmpeg, "-y", "-i", segment_file,
        "-map", "0:v:0", "-map", "0:a?",
        "-vf",
        "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "30",
        "-threads", "2", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart", output_file
    ]
    run_ffmpeg(chat_id, command, timeout=LIVE_CONVERT_TIMEOUT)


def process_youtube_live(url, chat_id):
    capture_process = None
    sent_count = 0
    try:
        send_message(
            chat_id,
            "\U0001f534 \u062c\u0627\u0631\u064a \u0627\u0644\u0627\u062a\u0635\u0627\u0644 \u0628\u0628\u062b YouTube Live.\n"
            "\u0633\u064a\u062a\u0645 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0648\u062a\u062d\u0648\u064a\u0644\u0647\u0627 \u0648\u0625\u0631\u0633\u0627\u0644\u0647\u0627 \u062a\u0628\u0627\u0639\u0627\u064b\u060c \u0645\u062f\u0629 \u0643\u0644 \u0645\u0642\u0637\u0639 \u0646\u062d\u0648 60 \u062b\u0627\u0646\u064a\u0629."
        )

        with tempfile.TemporaryDirectory() as folder:
            capture_dir = os.path.join(folder, "live_segments")
            os.makedirs(capture_dir, exist_ok=True)

            capture = start_live_capture(
                url, capture_dir, segment_seconds=LIVE_SEGMENT_SECONDS
            )
            capture_process = capture["process"]
            register_process(chat_id, capture_process)

            send_message(
                chat_id,
                f"\U0001f4e1 \u062a\u0645 \u0627\u0644\u0627\u062a\u0635\u0627\u0644 \u0628\u0627\u0644\u0628\u062b: {capture.get('title', 'YouTube Live')}.\n"
                "\U0001f4cb \u0627\u0644\u062d\u0627\u0644\u0629: \u0628\u0627\u0646\u062a\u0638\u0627\u0631 \u0627\u0643\u062a\u0645\u0627\u0644 \u0623\u0648\u0644 \u0645\u0642\u0637\u0639."
            )

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            processed_files = set()
            last_status_count = -1

            while True:
                if is_stopped(chat_id):
                    raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.")

                segments = sorted(
                    glob.glob(os.path.join(capture_dir, "live_*.ts"))
                )
                capture_finished = capture_process.poll() is not None

                # While capture is running, the newest TS file is still being written.
                # Once another segment appears, all earlier files are closed and safe.
                ready_segments = segments if capture_finished else segments[:-1]

                for segment_file in ready_segments:
                    if segment_file in processed_files:
                        continue
                    if is_stopped(chat_id):
                        raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.")

                    processed_files.add(segment_file)
                    segment_number = sent_count + 1
                    output_file = os.path.join(
                        folder, f"live_clip_{segment_number}.mp4"
                    )

                    send_message(
                        chat_id,
                        f"\u2699 \u0627\u0643\u062a\u0645\u0644 \u062c\u0632\u0621 \u062c\u062f\u064a\u062f \u0645\u0646 \u0627\u0644\u0628\u062b. \u062c\u0627\u0631\u064a \u062a\u062d\u0648\u064a\u0644 \u0627\u0644\u0645\u0642\u0637\u0639 {segment_number}.\n"
                        f"\u2705 \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u0645\u0631\u0633\u0644\u0629: {sent_count}."
                    )

                    convert_live_segment(
                        chat_id, segment_file, output_file, ffmpeg
                    )

                    if is_stopped(chat_id):
                        raise InterruptedError("\u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.")

                    send_document(
                        chat_id,
                        output_file,
                        f"YouTube Live \u2014 \u0627\u0644\u0645\u0642\u0637\u0639 {segment_number} \u2014 \u0646\u062d\u0648 60 \u062b\u0627\u0646\u064a\u0629"
                    )
                    sent_count += 1

                    try:
                        os.remove(segment_file)
                    except OSError:
                        pass
                    try:
                        os.remove(output_file)
                    except OSError:
                        pass

                    send_message(
                        chat_id,
                        f"\u2705 \u062a\u0645 \u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0645\u0642\u0637\u0639 {sent_count} \u0645\u0646 \u0627\u0644\u0628\u062b \u0627\u0644\u0645\u0628\u0627\u0634\u0631.\n"
                        "\U0001f4e1 \u0627\u0644\u062d\u0627\u0644\u0629: \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b \u0645\u0633\u062a\u0645\u0631 \u0648\u0627\u0646\u062a\u0638\u0627\u0631 \u0627\u0644\u062c\u0632\u0621 \u0627\u0644\u062a\u0627\u0644\u064a."
                    )

                if capture_finished:
                    # The capture process has ended; the final segment is now closed.
                    remaining_segments = [
                        path for path in segments if path not in processed_files
                    ]
                    if not remaining_segments:
                        break

                time.sleep(LIVE_POLL_SECONDS)

            if sent_count:
                send_message(
                    chat_id,
                    f"\U0001f389 \u0627\u0646\u062a\u0647\u0649 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b \u0648\u0625\u0631\u0633\u0627\u0644 \u0627\u0644\u0645\u0642\u0627\u0637\u0639.\n"
                    f"\u0625\u062c\u0645\u0627\u0644\u064a \u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u0645\u0631\u0633\u0644\u0629: {sent_count}."
                )
            else:
                raise RuntimeError("\u0627\u0646\u062a\u0647\u0649 \u0627\u0644\u0628\u062b \u062f\u0648\u0646 \u0627\u0644\u0639\u062b\u0648\u0631 \u0639\u0644\u0649 \u0645\u0642\u0627\u0637\u0639 \u0645\u0643\u062a\u0645\u0644\u0629.")

    except InterruptedError:
        send_message(
            chat_id,
            f"\u23f9 \u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.\n\u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u062a\u064a \u0623\u064f\u0631\u0633\u0644\u062a: {sent_count}."
        )
    except Exception as exc:
        traceback.print_exc()
        print(f"LIVE ERROR: {type(exc).__name__}: {exc}", flush=True)
        if is_stopped(chat_id):
            send_message(
                chat_id,
                f"\u23f9 \u062a\u0645 \u0625\u064a\u0642\u0627\u0641 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b.\n\u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u062a\u064a \u0623\u064f\u0631\u0633\u0644\u062a: {sent_count}."
            )
        else:
            send_message(
                chat_id,
                f"\u274c \u062a\u0639\u0630\u0631 \u0627\u0633\u062a\u0642\u0628\u0627\u0644 \u0627\u0644\u0628\u062b: {type(exc).__name__}: {exc}\n"
                f"\u0627\u0644\u0645\u0642\u0627\u0637\u0639 \u0627\u0644\u062a\u064a \u0623\u064f\u0631\u0633\u0644\u062a: {sent_count}."
            )
    finally:
        if capture_process is not None:
            try:
                stop_live_capture(capture_process)
            except Exception:
                traceback.print_exc()
        with JOBS_LOCK:
            JOBS.pop(chat_id, None)
