"""ロボットへの出力部分。RobotBackend を差し替えて実機 / シミュレーションを切り替える。"""
from __future__ import annotations

import abc
import logging
import math
import random
import threading
import time
from collections import deque
from typing import Callable, Optional

from .config import Go2Config, MockConfig
from .geometry import robot_to_world, wrap_angle
from .pose import Pose

log = logging.getLogger(__name__)


class RobotError(RuntimeError):
    pass


class RobotBackend(abc.ABC):
    @abc.abstractmethod
    def move(self, vx: float, vy: float, vyaw: float) -> None:
        """ロボット座標系の速度指令。vx 前進 [m/s]、vy 左 [m/s]、vyaw 反時計回り [rad/s]"""

    @abc.abstractmethod
    def stop(self) -> None:
        """停止。例外を投げずに、できる限り停止指令を届けること。"""

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- 実機

class Go2Backend(RobotBackend):
    """unitree_sdk2_python の SportClient を使う。

    確認した API（unitreerobotics/unitree_sdk2_python, unitree_sdk2py/go2/sport/sport_client.py）:
      - ChannelFactoryInitialize(domain_id, network_interface)
      - SportClient(); SetTimeout(sec); Init()
      - Move(vx, vy, vyaw) -> int   … 応答を待たない送信（_CallNoReply）。0 なら送信成功
      - StopMove() -> int           … 応答を待つ RPC。0 なら成功
    """

    def __init__(self, cfg: Go2Config):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.go2.sport.sport_client import SportClient

        ChannelFactoryInitialize(cfg.domain_id, cfg.interface)
        self._lock = threading.Lock()
        self._client = SportClient()
        self._client.SetTimeout(cfg.rpc_timeout)
        self._client.Init()
        log.info("SportClient 初期化完了（interface=%s, domain=%d）", cfg.interface, cfg.domain_id)

        if cfg.check_motion_mode:
            self._check_motion_mode(cfg.rpc_timeout)

    @staticmethod
    def _check_motion_mode(timeout: float) -> None:
        """現在のモーションモードを表示するだけ（モードの切り替えはしない）。"""
        try:
            from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
            msc = MotionSwitcherClient()
            msc.SetTimeout(timeout)
            msc.Init()
            code, data = msc.CheckMode()
        except Exception as e:  # SDK のバージョン差などで失敗しても致命的ではない
            log.warning("モーションモードを確認できませんでした: %s", e)
            return
        if code != 0:
            log.warning("CheckMode が失敗しました（code=%s）。ロボットと通信できているか確認してください", code)
        elif not data or not data.get("name"):
            log.warning("モーションモードが空です（%r）。sport サービスが動いていない可能性があります。"
                        "アプリまたはリモコンで通常の歩行モードにしてください", data)
        else:
            log.info("モーションモード: %r", data)

    def move(self, vx: float, vy: float, vyaw: float) -> None:
        with self._lock:
            code = self._client.Move(float(vx), float(vy), float(vyaw))
        if code != 0:
            raise RobotError(f"Move の送信に失敗しました（code={code}）")

    def stop(self) -> None:
        with self._lock:
            for attempt in range(2):
                try:
                    self._client.Move(0.0, 0.0, 0.0)
                    code = self._client.StopMove()
                except Exception as e:
                    log.error("StopMove で例外: %s", e)
                    continue
                if code == 0:
                    return
                log.error("StopMove が失敗しました（code=%s, 試行 %d）", code, attempt + 1)
        log.critical("停止指令が確認できません。リモコンで停止してください")


class DryBackend(RobotBackend):
    """何も送らない。TouchDesigner からの実際の位置で、計算結果だけを確認するとき用。"""

    def move(self, vx: float, vy: float, vyaw: float) -> None:
        log.debug("dry move vx=%+.2f vy=%+.2f vyaw=%+.2f", vx, vy, vyaw)

    def stop(self) -> None:
        log.debug("dry stop")


# ---------------------------------------------------------------- シミュレーション

class MockWorld:
    """速度指令を一次遅れで追従し、積分して位置を更新する簡易モデル。"""

    def __init__(self, x: float, y: float, yaw: float, velocity_tau: float):
        self._lock = threading.Lock()
        self.x, self.y, self.yaw = x, y, wrap_angle(yaw)
        self.tau = velocity_tau
        self.cmd = (0.0, 0.0, 0.0)
        self.vel = [0.0, 0.0, 0.0]

    def reset(self, x: float, y: float, yaw: float) -> None:
        with self._lock:
            self.x, self.y, self.yaw = x, y, wrap_angle(yaw)
            self.vel = [0.0, 0.0, 0.0]
            self.cmd = (0.0, 0.0, 0.0)

    def set_cmd(self, vx: float, vy: float, vyaw: float) -> None:
        with self._lock:
            self.cmd = (vx, vy, vyaw)

    def step(self, dt: float) -> None:
        with self._lock:
            a = 1.0 if self.tau <= 0 else min(1.0, dt / self.tau)
            for i in range(3):
                self.vel[i] += (self.cmd[i] - self.vel[i]) * a
            vx, vy, vyaw = self.vel
            self.yaw = wrap_angle(self.yaw + vyaw * dt)
            wx, wy = robot_to_world(vx, vy, self.yaw)
            self.x += wx * dt
            self.y += wy * dt

    def pose(self) -> Pose:
        with self._lock:
            return Pose(self.x, self.y, self.yaw, True)


class MockBackend(RobotBackend):
    def __init__(self, world: MockWorld):
        self.world = world
        self.move_calls = 0
        self.stop_calls = 0

    def move(self, vx: float, vy: float, vyaw: float) -> None:
        self.move_calls += 1
        self.world.set_cmd(vx, vy, vyaw)

    def stop(self) -> None:
        self.stop_calls += 1
        self.world.set_cmd(0.0, 0.0, 0.0)


class MockPoseFeeder:
    """MockWorld を時間発展させ、ノイズ・遅延・見失いを加えた位置を publish に渡す。

    publish は PoseBuffer.update（直接）か、OSC 送信関数（OSC 経路ごと確認したいとき）。
    """

    def __init__(self, world: MockWorld, cfg: MockConfig,
                 publish: Callable[[Optional[Pose]], None]):
        self.world = world
        self.cfg = cfg
        self.publish = publish
        self.rng = random.Random(cfg.seed)
        self._delayed: deque = deque()   # (公開時刻, Pose)
        self._qlock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mock", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def teleport(self, x: float, y: float, yaw: float) -> None:
        """ロボットを置き直す。遅延中の古い位置は捨て、新しい位置をすぐ公開する
        （古い位置のままアームされると、ルートの別の場所から追従を始めてしまう）。"""
        self.world.reset(x, y, yaw)
        with self._qlock:
            self._delayed.clear()
        self.publish(self.world.pose())

    def _run(self) -> None:
        cfg = self.cfg
        dt = 1.0 / cfg.physics_rate_hz
        pub_period = 1.0 / cfg.pose_rate_hz
        next_pub = time.monotonic()
        dropout_until = -math.inf
        last = time.monotonic()
        while not self._stop.wait(dt):
            now = time.monotonic()
            self.world.step(min(now - last, 0.1))
            last = now
            if now < next_pub:
                continue
            next_pub += pub_period
            if cfg.dropout_per_s > 0 and now >= dropout_until \
                    and self.rng.random() < cfg.dropout_per_s * pub_period:
                dropout_until = now + cfg.dropout_duration
            p = self.world.pose()
            noisy = Pose(p.x + self.rng.gauss(0, cfg.noise_pos),
                         p.y + self.rng.gauss(0, cfg.noise_pos),
                         wrap_angle(p.yaw + self.rng.gauss(0, cfg.noise_yaw)),
                         now >= dropout_until)
            with self._qlock:
                self._delayed.append((now + cfg.latency, noisy))
                due = []
                while self._delayed and self._delayed[0][0] <= now:
                    due.append(self._delayed.popleft()[1])
            for pose in due:
                if not pose.found and cfg.dropout_mode == "silent":
                    continue
                self.publish(pose)
