"""Cost ledger (%APPDATA%/Tintr/ledger.jsonl). Amounts are the real cost reported by fal per request."""

from __future__ import annotations

import json
import threading
from datetime import datetime

from tintr.config import app_dir


class CostTracker:
    def __init__(self):
        self.file = app_dir() / "ledger.jsonl"
        self.session = 0.0
        self._lock = threading.Lock()

    def record(self, *, project: str, clip: str, layer: str, frame: int, model: str, usd: float) -> float:
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "project": project, "clip": clip, "layer": layer, "frame": frame,
            "model": model, "usd": float(usd),
        }
        with self._lock:
            self.session += float(usd)
            with self.file.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return float(usd)

    def totals(self, project: str | None = None) -> tuple[float, float, int]:
        """(all_time_usd, project_usd, project_requests)"""
        total = proj = 0.0
        n = 0
        if not self.file.exists():
            return 0.0, 0.0, 0
        with self._lock, self.file.open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                total += e.get("usd", 0.0)
                if project and e.get("project") == project:
                    proj += e.get("usd", 0.0)
                    n += 1
        return total, proj, n
