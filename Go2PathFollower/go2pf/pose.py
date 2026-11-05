"""位置入力（OSC 受信）と、最新の位置を保持するバッファ。"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from .geometry import wrap_angle

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pose:
    x: float
    y: float
    yaw: float
    found: bool


class PoseBuffer:
    """最新の 1 件だけを保持する。受信時刻は time.monotonic() で記録する。"""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._pose: Optional[Pose] = None
        self._t: float = -math.inf
        self._clock = clock
        self.count = 0

    def update(self, pose: Pose) -> None:
        t = self._clock()
        with self._lock:
            self._pose, self._t = pose, t
            self.count += 1

    def latest(self) -> Tuple[Optional[Pose], float]:
        """(pose, 受信時刻) を返す。未受信なら (None, -inf)。"""
        with self._lock:
            return self._pose, self._t


def parse_pose_args(args) -> Optional[Pose]:
    """OSC 引数 x, y, yaw, found を Pose に変換する。不正なら None。"""
    if len(args) < 4:
        return None
    try:
        x, y, yaw = float(args[0]), float(args[1]), float(args[2])
        found = int(round(float(args[3]))) == 1
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, yaw)):
        return None
    return Pose(x, y, wrap_angle(yaw), found)


def _guard(handler: Callable) -> Callable:
    """受信スレッドで例外が出ても受信を止めない。"""
    def wrapped(*args):
        try:
            handler(*args)
        except Exception:
            log.exception("OSC ハンドラで例外")
    return wrapped


class OscPoseReceiver:
    """OSC を受信して PoseBuffer を更新する。

    buffers は {アドレス: PoseBuffer}。将来 /human/pose を足すときもここに追加するだけでよい。
    """

    def __init__(self, host: str, port: int, buffers: Dict[str, PoseBuffer],
                 extra_handlers: Optional[Dict[str, Callable]] = None):
        """extra_handlers は {アドレス: handler(client_address, address, *args)}（操作コマンド用）"""
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import BlockingOSCUDPServer

        self.bad_messages = 0
        disp = Dispatcher()
        for address, buf in buffers.items():
            disp.map(address, self._make_handler(buf))
        for address, handler in (extra_handlers or {}).items():
            disp.map(address, _guard(handler), needs_reply_address=True)
        # 1 スレッドで届いた順に処理する（設定が古い順に上書きされる競合を防ぐ）
        self._server = BlockingOSCUDPServer((host, port), disp)
        self._server.max_packet_size = 65507  # 既定の 8192 バイトでは長いルートの設定が切れる
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="osc", daemon=True)

    def _make_handler(self, buf: PoseBuffer):
        def handler(address, *args):
            pose = parse_pose_args(args)
            if pose is None:
                self.bad_messages += 1
                if self.bad_messages <= 5 or self.bad_messages % 100 == 0:
                    log.warning("不正な OSC メッセージ %s %r（累計 %d）", address, args, self.bad_messages)
                return
            buf.update(pose)
        return handler

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
