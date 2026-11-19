"""Optional, fail-safe YouTube live-chat activity sampler using pytchat."""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Deque, Dict, Optional


class LiveChatAnalyzer:
    def __init__(self, video_id: str, window_seconds: int = 30):
        self.video_id = video_id
        self.window_seconds = max(5, int(window_seconds))
        self.events: Deque[tuple] = deque(maxlen=2000)
        self._chat = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.available = False
        self.error = None

    def start(self) -> bool:
        """Start optional chat polling. Returns False if pytchat is unavailable."""
        try:
            import pytchat  # optional dependency
            self._chat = pytchat.create(video_id=self.video_id, interruptable=False)
            self.available = True
            self._thread = threading.Thread(target=self._poll, daemon=True)
            self._thread.start()
            return True
        except Exception as exc:
            self.error = str(exc)
            self.available = False
            return False

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                if self._chat is None or not self._chat.is_alive():
                    self.error = "الدردشة غير متاحة أو انتهى البث"
                    break
                data = self._chat.get()
                now = time.time()
                for item in data.sync_items():
                    self.events.append((now, getattr(item, "message", "")))
            except Exception as exc:
                self.error = str(exc)
                time.sleep(2)
            self._prune()
            time.sleep(1)

    def _prune(self) -> None:
        cutoff = time.time() - max(self.window_seconds * 4, 120)
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def activity_score(self, at_time: Optional[float] = None) -> Dict:
        """Return recent message rate; timestamps are local poll times, not video PTS."""
        now = at_time or time.time()
        cutoff = now - self.window_seconds
        count = sum(1 for stamp, _ in self.events if stamp >= cutoff)
        return {"messages": count,
                "messages_per_minute": round(count * 60 / self.window_seconds, 1),
                "available": self.available,
                "error": self.error}

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._chat is not None:
                self._chat.terminate()
        except Exception:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
