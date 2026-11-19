import re
import threading
import time
from urllib.parse import urlparse, parse_qs


def extract_video_id(url):
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.endswith("youtu.be"):
        return parsed.path.strip("/").split("/")[0]
    if "youtube.com" in host:
        if parsed.path == "/watch":
            return parse_qs(parsed.query).get("v", [None])[0]
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("live", "shorts", "embed"):
            return parts[1]
    return None


def _elapsed_seconds(value):
    if not value:
        return None
    parts = str(value).split(":")
    try:
        total = 0
        for part in parts:
            total = total * 60 + int(part)
        return total
    except (TypeError, ValueError):
        return None


class LiveChatMonitor:
    """مراقب اختياري للتعليقات؛ فشل الدردشة لا يوقف معالجة الفيديو."""
    def __init__(self, url):
        self.video_id = extract_video_id(url)
        self.events = []
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.available = False

    def start(self):
        if not self.video_id:
            self.error = "تعذر استخراج معرف فيديو YouTube"
            return self
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        try:
            import pytchat
            chat = pytchat.create(video_id=self.video_id, interruptable=False)
            self.available = True
            while not self.stop_event.is_set() and chat.is_alive():
                data = chat.get()
                items = data.sync_items() if data else []
                if items:
                    now_events = []
                    for item in items:
                        elapsed = _elapsed_seconds(getattr(item, "elapsedTime", None))
                        if elapsed is not None:
                            now_events.append({"time": elapsed, "count": 1})
                    if now_events:
                        with self.lock:
                            self.events.extend(now_events)
                time.sleep(2)
            try:
                chat.terminate()
            except Exception:
                pass
        except Exception as exc:
            self.error = str(exc)
            self.available = False

    def snapshot(self):
        with self.lock:
            return list(self.events)

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)


def start_chat_monitor(url):
    return LiveChatMonitor(url).start()
    
