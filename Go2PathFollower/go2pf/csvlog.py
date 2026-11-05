"""制御ステップごとの CSV ログ。"""
from __future__ import annotations

import csv
import math
import time
from pathlib import Path

from .supervisor import Snapshot

COLUMNS = [
    "wall_time", "t", "state", "reason",
    "x", "y", "yaw", "found", "pose_age",
    "s", "tx", "ty", "heading_error", "cross_track",
    "raw_vx", "raw_vy", "raw_vyaw", "vx", "vy", "vyaw", "sent",
]


def _f(v: float, nd: int = 4) -> str:
    return "" if v is None or (isinstance(v, float) and not math.isfinite(v)) else f"{v:.{nd}f}"


class CsvLogger:
    def __init__(self, directory: str, t0: float):
        Path(directory).mkdir(parents=True, exist_ok=True)
        self.path = Path(directory) / time.strftime("run_%Y%m%d_%H%M%S.csv")
        self._f = open(self.path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(COLUMNS)
        self._t0 = t0
        self._last_flush = time.monotonic()

    def write(self, s: Snapshot) -> None:
        p = s.pose
        tx, ty = s.target if s.target else (None, None)
        self._w.writerow([
            f"{time.time():.3f}", _f(s.t - self._t0, 3), s.state.value, s.reason,
            _f(p.x if p else None), _f(p.y if p else None), _f(p.yaw if p else None),
            "" if p is None else int(p.found), _f(s.pose_age, 3),
            _f(s.s), _f(tx), _f(ty), _f(s.heading_error), _f(s.cross_track),
            _f(s.raw.vx), _f(s.raw.vy), _f(s.raw.vyaw),
            _f(s.cmd.vx), _f(s.cmd.vy), _f(s.cmd.vyaw), int(s.sent),
        ])
        if time.monotonic() - self._last_flush > 1.0:
            self._f.flush()
            self._last_flush = time.monotonic()

    def close(self) -> None:
        self._f.close()
