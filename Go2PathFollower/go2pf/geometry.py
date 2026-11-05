"""幾何計算（部屋座標系）。

部屋座標系（TouchDesigner から届く座標）:
  - 原点は区画の中心、単位 m
  - x: 右が正、y: 前が正（上から見た図）
  - yaw: +y を向いているとき 0、反時計回りが正、[-π, π)

ロボット座標系（Go2 SportClient.Move の引数）:
  - vx: 前進、vy: 左、vyaw: 反時計回りが正
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

TWO_PI = 2.0 * math.pi


def wrap_angle(a: float) -> float:
    """角度を [-π, π) に正規化する。"""
    a = math.fmod(a + math.pi, TWO_PI)
    if a < 0.0:
        a += TWO_PI
    return a - math.pi


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


def target_heading(x: float, y: float, tx: float, ty: float) -> float:
    """(x, y) から (tx, ty) を向くときの yaw。θt = atan2(-(tx - x), (ty - y))"""
    return math.atan2(-(tx - x), ty - y)


def forward_vector(yaw: float) -> Tuple[float, float]:
    """yaw の向きの前方単位ベクトル（部屋座標）。yaw=0 → (0, 1)"""
    return -math.sin(yaw), math.cos(yaw)


def left_vector(yaw: float) -> Tuple[float, float]:
    """yaw の向きの左方向単位ベクトル（部屋座標）。yaw=0 → (-1, 0)"""
    return -math.cos(yaw), -math.sin(yaw)


def robot_to_world(vx: float, vy: float, yaw: float) -> Tuple[float, float]:
    fx, fy = forward_vector(yaw)
    lx, ly = left_vector(yaw)
    return vx * fx + vy * lx, vx * fy + vy * ly


def world_to_robot(wx: float, wy: float, yaw: float) -> Tuple[float, float]:
    fx, fy = forward_vector(yaw)
    lx, ly = left_vector(yaw)
    return wx * fx + wy * fy, wx * lx + wy * ly


@dataclass(frozen=True)
class Waypoint:
    x: float
    y: float
    speed: Optional[float] = None  # この点から次の点までの区間の前進速度


@dataclass(frozen=True)
class Projection:
    s: float          # 折れ線上の弧長（ループ時は窓に合わせて展開された値、[0, L) とは限らない）
    x: float
    y: float
    dist: float       # 現在位置からの距離
    seg: int          # 区間番号
    cross_track: float  # 進行方向に対して左にずれていると正


class Polyline:
    """折れ線ルート。loop=True なら最後の点から最初の点へ戻る区間を追加する。"""

    def __init__(self, waypoints: Sequence[Waypoint], loop: bool = False):
        pts: List[Waypoint] = []
        for w in waypoints:
            # 長さ 0 の区間は取り除く
            if pts and math.hypot(w.x - pts[-1].x, w.y - pts[-1].y) < 1e-9:
                continue
            pts.append(w)
        if loop and len(pts) >= 2:
            if math.hypot(pts[0].x - pts[-1].x, pts[0].y - pts[-1].y) < 1e-9:
                pts.pop()
            pts.append(Waypoint(pts[0].x, pts[0].y, pts[0].speed))
        if len(pts) < 2:
            raise ValueError("ルートには異なる 2 点以上が必要です")

        self.loop = loop
        self.points = pts
        self.seg_speed: List[Optional[float]] = [p.speed for p in pts[:-1]]
        self.cum = [0.0]
        for a, b in zip(pts[:-1], pts[1:]):
            self.cum.append(self.cum[-1] + math.hypot(b.x - a.x, b.y - a.y))
        self.length = self.cum[-1]

    @property
    def num_segments(self) -> int:
        return len(self.points) - 1

    def normalize_s(self, s: float) -> float:
        if self.loop:
            return s % self.length
        return clamp(s, 0.0, self.length)

    def segment_at(self, s: float) -> int:
        s = self.normalize_s(s)
        i = bisect.bisect_right(self.cum, s) - 1
        return int(clamp(i, 0, self.num_segments - 1))

    def point_at(self, s: float) -> Tuple[float, float]:
        s = self.normalize_s(s)
        i = self.segment_at(s)
        a, b = self.points[i], self.points[i + 1]
        seg_len = self.cum[i + 1] - self.cum[i]
        t = (s - self.cum[i]) / seg_len
        return a.x + t * (b.x - a.x), a.y + t * (b.y - a.y)

    def tangent_at(self, s: float) -> Tuple[float, float]:
        i = self.segment_at(s)
        a, b = self.points[i], self.points[i + 1]
        seg_len = self.cum[i + 1] - self.cum[i]
        return (b.x - a.x) / seg_len, (b.y - a.y) / seg_len

    def speed_at(self, s: float, default: float) -> float:
        sp = self.seg_speed[self.segment_at(s)]
        return default if sp is None else sp

    def project(self, x: float, y: float,
                s_lo: float = -math.inf, s_hi: float = math.inf) -> Projection:
        """(x, y) に最も近い折れ線上の点を、弧長 [s_lo, s_hi] の範囲内で探す。

        範囲を絞るのは、ルートが自分自身と交差・接近するときに、
        遠い区間へ「飛ぶ」のを防ぐため。ループ時は範囲が 0/L をまたいでもよい。
        """
        L = self.length
        shifts = (-L, 0.0, L) if self.loop else (0.0,)
        best: Optional[Projection] = None
        for i in range(self.num_segments):
            a, b = self.points[i], self.points[i + 1]
            dx, dy = b.x - a.x, b.y - a.y
            seg_len = self.cum[i + 1] - self.cum[i]
            t_free = ((x - a.x) * dx + (y - a.y) * dy) / (seg_len * seg_len)
            for sh in shifts:
                s0 = self.cum[i] + sh
                lo = max(s0, s_lo)
                hi = min(s0 + seg_len, s_hi)
                if lo > hi:
                    continue
                t = clamp(t_free, (lo - s0) / seg_len, (hi - s0) / seg_len)
                px, py = a.x + t * dx, a.y + t * dy
                d = math.hypot(x - px, y - py)
                if best is None or d < best.dist - 1e-12:
                    # 左法線 n = (-ty, tx)
                    ct = ((x - px) * (-dy) + (y - py) * dx) / seg_len
                    best = Projection(s0 + t * seg_len, px, py, d, i, ct)
        if best is None:
            # 窓が完全に範囲外のとき（通常起きない）は全体から探す
            return self.project(x, y)
        return best


@dataclass(frozen=True)
class Geofence:
    x_min: float
    x_max: float
    y_min: float
    y_max: float

    def contains(self, x: float, y: float) -> bool:
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max
