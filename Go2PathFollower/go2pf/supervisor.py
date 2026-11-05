"""状態管理と安全監視。ロボットへの指令はすべてここを通る。

状態:
  DISARMED  起動直後。指令を送らない
  RUNNING   ルート追従中
  HOLD      見失い / 位置が古い → 停止中。位置が戻れば自動で RUNNING へ（設定で無効化可）
  FINISHING 最終点に到達。加速度制限に沿って 0 まで減速中
  FINISHED  停止済み。再アームで最初から
  ESTOP     非常停止（スペースキー）。再アームするまで指令を送らない
  FAULT     ジオフェンス違反・内部エラー。再アームするまで指令を送らない
"""
from __future__ import annotations

import enum
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from .backends import RobotBackend
from .config import Config
from .controller import Command, PurePursuit, RateLimiter
from .geometry import Polyline, clamp, robot_to_world
from .pose import Pose, PoseBuffer

log = logging.getLogger(__name__)


class State(str, enum.Enum):
    DISARMED = "DISARMED"
    RUNNING = "RUNNING"
    HOLD = "HOLD"
    FINISHING = "FINISHING"
    FINISHED = "FINISHED"
    ESTOP = "ESTOP"
    FAULT = "FAULT"


ACTIVE = (State.RUNNING, State.FINISHING)


@dataclass
class Snapshot:
    """1 ステップ分の記録（ログと可視化で使う）。"""
    t: float = 0.0
    state: State = State.DISARMED
    reason: str = ""
    pose: Optional[Pose] = None
    pose_age: float = math.inf
    target: Optional[Tuple[float, float]] = None
    s: float = math.nan
    laps: int = 0
    heading_error: float = math.nan
    cross_track: float = math.nan
    raw: Command = field(default_factory=Command)
    cmd: Command = field(default_factory=Command)
    sent: bool = False


class Supervisor:
    def __init__(self, cfg: Config, path: Optional[Polyline], backend: RobotBackend,
                 pose_buf: PoseBuffer, clock: Callable[[], float] = time.monotonic,
                 on_snapshot: Optional[Callable[[Snapshot], None]] = None):
        self.backend = backend
        self.pose_buf = pose_buf
        self.clock = clock
        self.on_snapshot = on_snapshot
        self.path: Optional[Polyline] = None
        self.tracker: Optional[PurePursuit] = None
        c = cfg.control
        self.limiter = RateLimiter(c.accel_vx, c.accel_vy, c.accel_vyaw)
        self.state = State.DISARMED
        self.reason = "起動直後"
        self._lock = threading.RLock()
        self._last_tick: Optional[float] = None
        self.last = Snapshot()
        self.set_config(cfg)
        if path is not None:
            self.set_path(path)

    # ------------------------------------------------------------ 外部操作

    @property
    def is_moving_state(self) -> bool:
        return self.state in ACTIVE or self.state == State.HOLD

    def set_config(self, cfg: Config) -> None:
        """設定を差し替える（走行中でもよい。検証済みの cfg を渡すこと）。"""
        with self._lock:
            self.cfg = cfg
            self.fence = cfg.safety.geofence.to_geofence()
            c = cfg.control
            self.limiter.accel = (c.accel_vx, c.accel_vy, c.accel_vyaw)
            if self.tracker is not None:
                self.tracker.cfg = c

    def set_path(self, path: Polyline) -> None:
        """ルートを差し替える。走行中（RUNNING / FINISHING / HOLD）は ValueError。"""
        with self._lock:
            if self.is_moving_state:
                raise ValueError(f"{self.state.value} 中はルートを変えられません。停止してから変更してください")
            for i, p in enumerate(path.points):
                if not self.fence.contains(p.x, p.y):
                    raise ValueError(f"ルートの点 {i} ({p.x:.2f}, {p.y:.2f}) がジオフェンスの外です")
            self.path = path
            self.tracker = PurePursuit(path, self.cfg.control)
            if self.state == State.FINISHED:
                self._set_state(State.DISARMED, "ルート変更")

    def arm(self) -> str:
        with self._lock:
            if self.is_moving_state:
                return f"すでに {self.state.value} です"
            if self.tracker is None:
                return "アームできません: ルートが未設定です"
            pose, ok, why = self._pose_status()
            if not ok:
                return f"アームできません: {why}"
            if not self.fence.contains(pose.x, pose.y):
                return "アームできません: ジオフェンスの外にいます"
            if self.state in (State.FINISHED, State.DISARMED):
                self.tracker.reset()
            self.limiter.reset()
            self._set_state(State.RUNNING, "アーム")
            return "アームしました"

    def estop(self, reason: str = "非常停止キー") -> None:
        """どのスレッドから呼んでもよい。直ちに停止指令を送る。"""
        with self._lock:
            self._stop_into(State.ESTOP, reason)

    def fault(self, reason: str) -> None:
        with self._lock:
            self._stop_into(State.FAULT, reason)

    def shutdown(self) -> None:
        """終了時。状態に関係なく必ず停止指令を送る。"""
        with self._lock:
            self.limiter.reset()
            self.backend.stop()
            if self.state not in (State.ESTOP, State.FAULT):
                self._set_state(State.DISARMED, "終了")

    # ------------------------------------------------------------ 制御ステップ

    def tick(self) -> Snapshot:
        with self._lock:
            now = self.clock()
            period = 1.0 / self.cfg.control.rate_hz
            dt = period if self._last_tick is None else clamp(now - self._last_tick, 0.0, 2.0 * period)
            self._last_tick = now

            pose, ok, why = self._pose_status(now)
            snap = Snapshot(t=now, pose=pose,
                            pose_age=now - self.pose_buf.latest()[1])

            if self.state == State.HOLD:
                if ok and not self.fence.contains(pose.x, pose.y):
                    self._stop_into(State.FAULT, "ジオフェンスの外")
                elif ok and self.cfg.safety.resume_after_tracking_loss:
                    self.limiter.reset()
                    self._set_state(State.RUNNING, "位置が復帰")

            if self.state in ACTIVE:
                if not ok:
                    self._stop_into(State.HOLD, why)
                elif not self.fence.contains(pose.x, pose.y):
                    self._stop_into(State.FAULT, "ジオフェンスの外")
                else:
                    self._drive(pose, dt, snap)

            snap.state, snap.reason = self.state, self.reason
            self.last = snap
            if self.on_snapshot:
                self.on_snapshot(snap)
            return snap

    def _drive(self, pose: Pose, dt: float, snap: Snapshot) -> None:
        out = self.tracker.update(pose.x, pose.y, pose.yaw)
        snap.target, snap.s, snap.laps = (out.tx, out.ty), out.s, out.laps
        snap.heading_error, snap.cross_track = out.heading_error, out.cross_track
        snap.raw = out.cmd

        if self.state == State.RUNNING and out.finished:
            self._set_state(State.FINISHING, "最終点に到達")
        target = Command() if self.state == State.FINISHING else out.cmd
        cmd = self._cap(self.limiter.step(target, dt))
        self.limiter.current = cmd

        if self.state == State.FINISHING and cmd.is_zero():
            self._stop_into(State.FINISHED, "停止完了")
            return
        if self._would_exit_fence(pose, cmd):
            self._stop_into(State.FAULT, "ジオフェンスの外へ向かう指令")
            return
        self.backend.move(*cmd.as_tuple())
        snap.cmd, snap.sent = cmd, True

    # ------------------------------------------------------------ 内部

    def _pose_status(self, now: Optional[float] = None) -> Tuple[Optional[Pose], bool, str]:
        pose, t = self.pose_buf.latest()
        now = self.clock() if now is None else now
        if pose is None:
            return None, False, "位置を未受信"
        if now - t > self.cfg.safety.pose_timeout:
            return pose, False, f"位置の受信が {now - t:.2f} s 途絶"
        if not pose.found:
            return pose, False, "マーカー見失い (found=0)"
        return pose, True, ""

    def _cap(self, cmd: Command) -> Command:
        c = self.cfg.control
        return Command(clamp(cmd.vx, 0.0, c.speed_max),
                       clamp(cmd.vy, -c.vy_max, c.vy_max),
                       clamp(cmd.vyaw, -c.vyaw_max, c.vyaw_max))

    def _would_exit_fence(self, pose: Pose, cmd: Command) -> bool:
        wx, wy = robot_to_world(cmd.vx, cmd.vy, pose.yaw)
        h = self.cfg.safety.geofence_horizon
        return not self.fence.contains(pose.x + wx * h, pose.y + wy * h)

    def _stop_into(self, state: State, reason: str) -> None:
        self.limiter.reset()
        self.backend.stop()
        self._set_state(state, reason)

    def _set_state(self, state: State, reason: str) -> None:
        if state != self.state or reason != self.reason:
            lvl = logging.WARNING if state in (State.ESTOP, State.FAULT, State.HOLD) else logging.INFO
            log.log(lvl, "%s → %s（%s）", self.state.value, state.value, reason)
        self.state, self.reason = state, reason
