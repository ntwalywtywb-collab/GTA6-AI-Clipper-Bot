import cv2
import numpy as np


def _video_info(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError("تعذر فتح الفيديو للتحليل.")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
    duration = frames / fps if fps > 0 else 0
    cap.release()
    return duration, fps


def analyze_video(path, clip_seconds=60, chat_events=None, timeline_offset=0):
    """
    يحلل الحركة بصرياً بمعدل عينة يقارب إطاراً واحداً في الثانية.
    chat_events: قائمة أحداث بالشكل {"time": seconds_from_live_start, "count": n}.
    """
    duration, fps = _video_info(path)
    if duration <= 0:
        raise RuntimeError("مدة الفيديو غير صالحة للتحليل.")
    cap = cv2.VideoCapture(path)
    sample_step = max(1, int(round(fps))) if fps > 0 else 1
    scores = []
    previous = None
    frame_no = 0
    second = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_no % sample_step == 0:
            small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            if previous is None:
                motion = 0.0
            else:
                motion = float(cv2.absdiff(gray, previous).mean()) / 255.0
            # تخفيف أثر اهتزاز الكاميرا/الضجيج البصري
            scores.append((second, motion))
            previous = gray
            second += 1
        frame_no += 1
    cap.release()
    if not scores:
        return {"start": 0, "duration": min(clip_seconds, duration), "score": 0.0,
                "motion": 0.0, "chat": 0}
    max_start = max(0, int(duration - clip_seconds))
    starts = list(range(0, max_start + 1, 5))
    if not starts or starts[-1] != max_start:
        starts.append(max_start)
    chat_events = chat_events or []
    best = None
    for start in starts:
        end = start + clip_seconds
        motion_values = [v for t, v in scores if start <= t < end]
        motion_score = float(np.mean(motion_values)) if motion_values else 0.0
        chat_score = 0
        for event in chat_events:
            t = int(event.get("time", -1))
            if timeline_offset + start <= t < timeline_offset + end:
                chat_score += int(event.get("count", 1))
        # الحركة هي الأساس؛ التعليقات إشارة مساعدة وليست دليلاً على جودة المشهد.
        normalized_chat = min(1.0, chat_score / 30.0)
        total = motion_score * 0.85 + normalized_chat * 0.15
        candidate = {
            "start": start, "duration": min(clip_seconds, max(1, duration - start)),
            "score": total, "motion": motion_score, "chat": chat_score
        }
        if best is None or candidate["score"] > best["score"]:
            best = candidate
    return best


def select_best_clips(path, clip_seconds=60, max_clips=10):
    """يرتب نوافذ الفيديو حسب الحركة ويختار نوافذ غير متداخلة."""
    duration, fps = _video_info(path)
    if duration <= 0:
        raise RuntimeError("مدة الفيديو غير صالحة.")
    if duration <= clip_seconds:
        return [{"start": 0, "duration": duration, "score": 0.0}]
    # نحلل نوافذ متتابعة متداخلة كل 10 ثوانٍ، ثم نختار الأفضل دون تداخل.
    candidates = []
    step = max(1, int(round(fps))) if fps > 0 else 1
    cap = cv2.VideoCapture(path)
    previous = None
    frame_no = 0
    sec = 0
    per_second = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_no % step == 0:
            small = cv2.resize(frame, (320, 180), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
            motion = 0.0 if previous is None else float(cv2.absdiff(gray, previous).mean()) / 255.0
            per_second.append((sec, motion))
            previous = gray
            sec += 1
        frame_no += 1
    cap.release()
    max_start = max(0, int(duration - clip_seconds))
    starts = list(range(0, max_start + 1, 10))
    if not starts or starts[-1] != max_start:
        starts.append(max_start)
    for start in starts:
        vals = [v for t, v in per_second if start <= t < start + clip_seconds]
        candidates.append({"start": start, "duration": clip_seconds,
                           "score": float(np.mean(vals)) if vals else 0.0})
    candidates.sort(key=lambda x: x["score"], reverse=True)
    chosen = []
    for item in candidates:
        if all(abs(item["start"] - old["start"]) >= clip_seconds for old in chosen):
            chosen.append(item)
            if len(chosen) >= max_clips:
                break
    return sorted(chosen, key=lambda x: x["start"])
    
