"""Pure Pursuit による経路追従と、指令値の変化量制限。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from .config import ControlConfig
from .geometry import (Polyline, clamp, left_vector, target_heading,
                       wrap_angle)


@dataclass
class Command:
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0

    def as_tuple(self) -> Tuple[float, float, float]:
        return self.vx, self.vy, self.vyaw

    def is_zero(self, eps: float = 1e-3) -> bool:
        return abs(self.vx) < eps and abs(self.vy) < eps and abs(self.vyaw) < eps


@dataclass
class TrackOutput:
    cmd: Command
    tx: float              # 目標点
    ty: float
    s: float               # 進捗（弧長、[0, L)）
    heading_error: float   # e = wrap(θt - yaw)
    cross_track: float     # 横ずれ（進行方向の左が正）
    finished: bool
    laps: int = 0


class PurePursuit:
    """折れ線ルートを Pure Pursuit で追従する。

    進捗 s を覚えておき、次の最近点探索を [s - search_back, s + search_ahead] に限定する。
    """

    def __init__(self, path: Polyline, cfg: ControlConfig):
        self.path = path
        self.cfg = cfg
        self.reset()

    def reset(self) -> None:
        self.s: Optional[float] = None
        self.laps = 0

    def update(self, x: float, y: float, yaw: float) -> TrackOutput:
        cfg, path = self.cfg, self.path
        if self.s is None:
            proj = path.project(x, y)
            self.s = path.normalize_s(proj.s)
        else:
            proj = path.project(x, y, self.s - cfg.search_back, self.s + cfg.search_ahead)
            # 進捗は戻さない（角やノイズで最近点が前後に揺れて目標点がぶれるのを防ぐ）
            s = max(self.s, proj.s)
            if path.loop and s >= path.length:
                self.laps += 1
            self.s = path.normalize_s(s)

        s_target = self.s + cfg.lookahead
        if not path.loop:
            s_target = min(s_target, path.length)
        tx, ty = path.point_at(s_target)

        end_x, end_y = path.points[-1].x, path.points[-1].y
        finished = (not path.loop
                    and self.s >= path.length - cfg.lookahead - cfg.goal_radius
                    and math.hypot(end_x - x, end_y - y) <= cfg.goal_radius)

        if math.hypot(tx - x, ty - y) > 1e-3:
            theta_t = target_heading(x, y, tx, ty)
        else:
            # 目標点がほぼ真上にあるときは、ルートの接線方向を向く
            ux, uy = path.tangent_at(self.s)
            theta_t = math.atan2(-ux, uy)
        e = wrap_angle(theta_t - yaw)

        speed = clamp(path.speed_at(self.s, cfg.speed), 0.0, cfg.speed_max)
        vx = speed * max(0.0, math.cos(e))
        vyaw = clamp(cfg.k_yaw * e, -cfg.vyaw_max, cfg.vyaw_max)

        vy = 0.0
        if cfg.k_lateral > 0.0:
            # 横ずれを戻す方向（ルートの左法線の逆向き）を、ロボットの左右成分に変換
            ux, uy = path.tangent_at(self.s)
            nx, ny = -uy, ux
            corr = -cfg.k_lateral * proj.cross_track
            lx, ly = left_vector(yaw)
            vy = clamp(corr * (nx * lx + ny * ly), -cfg.vy_max, cfg.vy_max)

        return TrackOutput(Command(vx, vy, vyaw), tx, ty, self.s, e,
                           proj.cross_track, finished, self.laps)


class RateLimiter:
    """1 ステップあたりの変化量を加速度上限 × dt に制限する。"""

    def __init__(self, accel_vx: float, accel_vy: float, accel_vyaw: float):
        self.accel = (accel_vx, accel_vy, accel_vyaw)
        self.reset()

    def reset(self) -> None:
        self.current = Command()

    def step(self, target: Command, dt: float) -> Command:
        out = []
        for cur, tgt, acc in zip(self.current.as_tuple(), target.as_tuple(), self.accel):
            d = acc * dt
            out.append(cur + clamp(tgt - cur, -d, d))
        self.current = Command(*out)
        return self.current
