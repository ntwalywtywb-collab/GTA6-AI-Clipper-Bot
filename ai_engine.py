"""Lightweight highlight scoring for GTA clips; OpenCV/NumPy only."""
from __future__ import annotations

import math
import os
import subprocess
from typing import Dict, List, Optional

import cv2
import numpy as np


def _duration(path: str) -> float:
    cap = cv2.VideoCapture(path)
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0
        if fps > 0 and frames > 0:
            return frames / fps
        return 0.0
    finally:
        cap.release()


def analyze_video(path: str, clip_length: int = 60, sample_every: float = 2.0) -> Dict:
    """Return the highest-scoring full window in a closed video file.

    Scores visual motion and scene-change intensity. This is a lightweight
    heuristic, not semantic recognition of explosions, chases, or wins.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    duration = _duration(path)
    if duration < clip_length:
        return {"start": 0.0, "duration": duration, "score": 0.0,
                "reasons": ["الفيديو أقصر من مدة المقطع المطلوبة"]}

    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step_frames = max(1, int(fps * sample_every))
    times: List[float] = []
    motion: List[float] = []
    prev: Optional[np.ndarray] = None
    index = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if index % step_frames == 0:
            small = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if prev is not None:
                diff = cv2.absdiff(gray, prev)
                motion.append(float(np.mean(diff)))
                times.append(index / fps)
            prev = gray
        index += 1
    cap.release()

    if not motion:
        return {"start": 0.0, "duration": duration, "score": 0.0, "reasons": []}

    values = np.asarray(motion, dtype=np.float32)
    # Rolling windows; each sample represents approximately sample_every seconds.
    window = max(1, int(math.ceil(clip_length / sample_every)))
    if len(values) <= window:
        best_index = 0
        best_score = float(np.mean(values))
    else:
        sums = np.convolve(values, np.ones(window, dtype=np.float32) / window, mode="valid")
        best_index = int(np.argmax(sums))
        best_score = float(sums[best_index])

    start = max(0.0, times[min(best_index, len(times) - 1)] - sample_every)
    start = min(start, max(0.0, duration - clip_length))
    return {
        "start": round(start, 2),
        "duration": float(clip_length),
        "score": round(best_score, 3),
        "reasons": ["تم اختيار الفترة ذات أعلى حركة بصرية نسبياً"],
    }


def extract_best_clip(source: str, destination: str, clip_length: int = 60,
                      ffmpeg: Optional[str] = None) -> Dict:
    """Extract the best full-length window; caller supplies FFmpeg executable."""
    result = analyze_video(source, clip_length=clip_length)
    if result["duration"] < clip_length:
        raise ValueError("الفيديو لا يحتوي على مدة كاملة تكفي للمقطع المطلوب.")
    if not ffmpeg:
        raise ValueError("يجب تمرير مسار FFmpeg.")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-ss",
               str(result["start"]), "-i", source, "-t", str(clip_length),
               "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-y", destination]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr[-2000:] or "فشل قص المقطع")
    return result
