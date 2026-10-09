"""Small, opt-in progress protocol shared by downloader subprocesses."""
import json
import math
import os
import time

PREFIX = "__MS_PROGRESS__"


def parse_progress(line: str):
    if not line.startswith(PREFIX) or len(line) > 4096:
        return None
    try:
        value = json.loads(line[len(PREFIX):])
        if not isinstance(value, dict) or not isinstance(value.get("label"), str):
            return None
        result = {"label": value["label"][:160], "percent": None}
        percent = value.get("percent")
        if percent is not None:
            if type(percent) not in (int, float) or not math.isfinite(percent):
                return None
            # A subprocess cannot declare the task successful before it exits.
            result["percent"] = min(99, max(0, percent))
        for key in ("completed", "total", "bytes_done", "bytes_total"):
            number = value.get(key)
            if type(number) is int and 0 <= number <= 2**53 - 1:
                result[key] = number
        if value.get("unit") in ("个文件", "条内容"):
            result["unit"] = value["unit"]
        return result
    except (ValueError, TypeError):
        return None


class DownloadProgress:
    def __init__(self, total=0):
        self.enabled = os.environ.get("MS_PROGRESS") == "1"
        self.total = total
        self.completed = 0
        self.bytes_done = 0
        self.bytes_total = None
        self.label = "正在下载文件"
        self.last_emit = 0.0

    def _emit(self, percent=None, force=False):
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and now - self.last_emit < 0.2:
            return
        self.last_emit = now
        value = {
            "label": self.label, "percent": percent,
            "completed": self.completed, "total": self.total or None,
            "unit": "个文件", "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
        }
        print(PREFIX + json.dumps(value, ensure_ascii=False), flush=True)

    def stage(self, label):
        self.label = label
        self.bytes_done = 0
        self.bytes_total = None
        self._emit(force=True)

    def start_file(self, content_length=None):
        # Retried URLs start a fresh transfer; their bytes are not double counted.
        self.bytes_done = 0
        try:
            self.bytes_total = max(0, int(content_length)) or None
        except (TypeError, ValueError):
            self.bytes_total = None
        self.label = f"正在下载第 {self.completed + 1} / {self.total} 个文件"
        self._emit(self._percent(), force=True)

    def _percent(self):
        if not self.total or not self.bytes_total:
            return None
        fraction = min(1, self.bytes_done / self.bytes_total)
        return min(99, (self.completed + fraction) * 100 / self.total)

    def advance(self, size):
        self.bytes_done += size
        self._emit(self._percent())

    def finish_file(self):
        self.completed += 1
        self._emit(min(99, self.completed * 100 / self.total) if self.total else None,
                   force=True)
