import os
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


def detect_facecam_region(path, max_samples=10):
    """
    يقدّر مستطيل نافذة Facecam اعتماداً على ثبات اكتشاف الوجه في موضع متقارب
    عبر عدة إطارات. يعيد (x, y, w, h) أو None عند عدم كفاية الثقة.
    """
    try:
        cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        detector = cv2.CascadeClassifier(cascade_path)
        if detector.empty():
            return None
    except Exception:
        return None

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return None

    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if width < 160 or height < 120 or frame_count < 1:
        cap.release()
        return None

    # نأخذ عينات موزعة بدلاً من فحص كل الإطارات لتقليل استهلاك Render.
    positions = np.linspace(0, max(0, frame_count - 1), num=min(max_samples, frame_count), dtype=int)
    detections = []
    for pos in positions:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(pos))
        ok, frame = cap.read()
        if not ok:
            continue
        scale = min(1.0, 640.0 / max(width, height))
        if scale < 1.0:
            frame = cv2.resize(frame, (int(width * scale), int(height * scale)))
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = detector.detectMultiScale(
            gray, scaleFactor=1.12, minNeighbors=5,
            minSize=(max(24, int(28 * scale)), max(24, int(28 * scale)))
        )
        for (x, y, w, h) in faces:
            # تجاهل الوجوه الكبيرة جداً التي غالباً تملأ الشاشة وليست نافذة Facecam.
            if w > gray.shape[1] * 0.35 or h > gray.shape[0] * 0.55:
                continue
            detections.append((x / scale, y / scale, w / scale, h / scale))
    cap.release()

    if len(detections) < 2:
        return None

    # تجميع الاكتشافات المتقاربة مكانياً؛ الوجه الثابت عبر الإطارات أرجح أن يكون Facecam.
    clusters = []
    for box in detections:
        cx = box[0] + box[2] / 2
        cy = box[1] + box[3] / 2
        matched = None
        for cluster in clusters:
            mx = np.mean([b[0] + b[2] / 2 for b in cluster])
            my = np.mean([b[1] + b[3] / 2 for b in cluster])
            if abs(cx - mx) <= width * 0.16 and abs(cy - my) <= height * 0.16:
                matched = cluster
                break
        if matched is None:
            clusters.append([box])
        else:
            matched.append(box)

    clusters.sort(key=lambda c: (len(c), np.mean([b[2] * b[3] for b in c])), reverse=True)
    best = clusters[0]
    if len(best) < 2:
        return None

    fx = int(np.median([b[0] for b in best]))
    fy = int(np.median([b[1] for b in best]))
    fw = int(np.median([b[2] for b in best]))
    fh = int(np.median([b[3] for b in best]))

    # توسيع حدود الوجه لتقدير نافذة الكاميرا كاملة (تقريب لاكتشاف حدود overlay).
    roi_w = int(fw * 2.8)
    roi_h = int(fh * 2.5)
    x = int(fx + fw / 2 - roi_w / 2)
    y = int(fy + fh / 2 - roi_h * 0.48)
    x = max(0, min(width - 1, x))
    y = max(0, min(height - 1, y))
    roi_w = max(1, min(width - x, roi_w))
    roi_h = max(1, min(height - y, roi_h))
    if roi_w < width * 0.10 or roi_h < height * 0.10:
        return None
    return (x, y, roi_w, roi_h)
        
