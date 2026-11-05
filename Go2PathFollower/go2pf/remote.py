"""外部（TouchDesigner）からの操作と、状態の送り返し。

受信（osc.port、/dog/pose と同じポート）:
  /go2pf/config  seq(int) json(str)   設定とルート。約 1 秒ごと＋変更時に送られてくる
                                      {"control": {...}, "safety": {...}, "route": [{"x","y","speed"?}, ...]}
  /go2pf/arm                          アーム
  /go2pf/estop                        非常停止（どのホストからでも受け付ける）
  /go2pf/quit                         停止して終了

送信（操作元の IP の remote.status_port へ）:
  /go2pf/status  state reason s length laps pose_age
  /go2pf/pose    x y yaw found
  /go2pf/target  tx ty               （走行中のみ）
  /go2pf/cmd     vx vy vyaw sent
  /go2pf/ack     seq ok(int) message  /go2pf/config と /go2pf/arm への応答
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from typing import Callable, Optional

from .config import Config, merge_remote, parse_route_points
from .geometry import Polyline
from .supervisor import Supervisor

log = logging.getLogger(__name__)


class RemoteControl:
    def __init__(self, sup: Supervisor, base_cfg: Config, on_quit: Callable[[], None],
                 on_route: Optional[Callable[[Polyline], None]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 send: Optional[Callable[[str, int, str, list], None]] = None):
        self.sup = sup
        self.base_cfg = base_cfg
        self.rcfg = base_cfg.remote
        self.on_quit = on_quit
        self.on_route = on_route
        self.clock = clock
        self._send = send or _UdpSender().send
        self.controller: Optional[str] = None
        self.last_contact = clock()
        self._last_json: Optional[str] = None
        self._last_result = (True, "")
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 受信

    def handlers(self):
        """{OSC アドレス: handler(client_address, address, *args)}"""
        return {
            "/go2pf/config": self._on_config,
            "/go2pf/arm": self._on_arm,
            "/go2pf/estop": self._on_estop,
            "/go2pf/quit": self._on_quit,
        }

    def _allowed(self, ip: str, claim: bool = False) -> bool:
        with self._lock:
            if ip in self.rcfg.allow_hosts or ip == self.controller:
                ok = True
            elif self.controller is None and claim and not self.rcfg.allow_hosts:
                self.controller = ip
                log.info("操作元: %s", ip)
                ok = True
            else:
                ok = False
            if ok:
                self.last_contact = self.clock()
                if self.controller is None:
                    self.controller = ip
            return ok

    def _on_config(self, client, address, *args):
        ip = client[0]
        if not self._allowed(ip, claim=True):
            log.warning("操作元以外 (%s) からの %s を無視", ip, address)
            return
        if len(args) < 2 or not isinstance(args[1], str):
            self._ack(ip, -1, False, "引数は seq, json です")
            return
        seq, text = args[0], args[1]
        if text != self._last_json:
            self._last_json = text
            self._last_result = self.apply_config_json(text)
        self._ack(ip, seq, *self._last_result)

    def apply_config_json(self, text: str):
        """設定とルートを適用する。(ok, message) を返す。

        - 数値の設定は走行中でも反映する（検証に通らなければ全部適用しない）
        - ルートとループの切り替えは停止中のみ。走行中は無視して理由を返す
        - 新しいジオフェンスが現在のルートを含まないなら、設定は適用しない
        """
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("JSON はオブジェクトである必要があります")
            route = data.pop("route", None)
            cfg = merge_remote(self.sup.cfg, data)
            new_path = None
            if route is not None:
                wps = parse_route_points(route, cfg.control.speed_max)
                if len(wps) < 2:
                    raise ValueError("ルートには 2 点以上が必要です")
                new_path = Polyline(wps, loop=cfg.control.loop)
            elif self.sup.path is not None and cfg.control.loop != self.sup.path.loop:
                # ルートなしでループだけ切り替わった: 今のルートの点で作り直す
                pts = self.sup.path.points[:-1] if self.sup.path.loop else self.sup.path.points
                new_path = Polyline(pts, loop=cfg.control.loop)
        except (ValueError, TypeError, KeyError) as e:
            log.warning("設定を適用できません: %s", e)
            return False, f"設定エラー: {e}"

        old_path = self.sup.path
        route_msg = ""
        if new_path is not None and not _same_path(new_path, old_path):
            if self.sup.is_moving_state:
                route_msg = "走行中はルート・ループを変えられません（停止後に反映）"
                new_path = None
                cfg.control.loop = self.sup.cfg.control.loop
                self._last_json = None  # 停止後に同じ JSON が来たら適用し直す
        else:
            new_path = None

        path = new_path or old_path
        fence = cfg.safety.geofence.to_geofence()
        if path is not None:
            for i, p in enumerate(path.points):
                if not fence.contains(p.x, p.y):
                    return False, f"ルートの点 {i} ({p.x:.2f}, {p.y:.2f}) がジオフェンスの外です。未適用"

        if cfg != self.sup.cfg:
            self.sup.set_config(cfg)
            log.info("設定を更新しました")
        if new_path is not None:
            self.sup.set_path(new_path)
            if self.on_route:
                self.on_route(new_path)
            log.info("ルート更新: %d 点, %.2f m, loop=%s", len(new_path.points), new_path.length, new_path.loop)
        if route_msg:
            return False, route_msg
        return True, "OK"

    def _on_arm(self, client, address, *args):
        ip = client[0]
        if not self._allowed(ip):
            log.warning("操作元以外 (%s) からの %s を無視", ip, address)
            return
        msg = self.sup.arm()
        log.info("リモート: %s", msg)
        self._ack(ip, -1, self.sup.state.value == "RUNNING", msg)

    def _on_estop(self, client, address, *args):
        if self.sup.state.value != "ESTOP":
            log.warning("リモート非常停止 (%s)", client[0])
        self.sup.estop(f"リモート非常停止 ({client[0]})")
        if self._allowed(client[0]):
            self._ack(client[0], -1, True, "ESTOP")

    def _on_quit(self, client, address, *args):
        if not self._allowed(client[0]):
            log.warning("操作元以外 (%s) からの %s を無視", client[0], address)
            return
        log.info("リモートから終了要求")
        self.on_quit()

    # ------------------------------------------------------------ 送信

    def _ack(self, ip, seq, ok, message):
        self._send(ip, self.rcfg.status_port, "/go2pf/ack", [int(seq), int(bool(ok)), str(message)])

    def send_status(self) -> None:
        ip = self.controller
        if ip is None:
            return
        port = self.rcfg.status_port
        s = self.sup.last
        length = self.sup.path.length if self.sup.path else 0.0
        self._send(ip, port, "/go2pf/status", [
            s.state.value, s.reason, _num(s.s), float(length), int(s.laps), _num(s.pose_age, 99.0)])
        if s.pose is not None:
            self._send(ip, port, "/go2pf/pose", [s.pose.x, s.pose.y, s.pose.yaw, int(s.pose.found)])
        if s.target is not None:
            self._send(ip, port, "/go2pf/target", [float(s.target[0]), float(s.target[1])])
        self._send(ip, port, "/go2pf/cmd", [s.cmd.vx, s.cmd.vy, s.cmd.vyaw, int(s.sent)])

    def controller_lost(self) -> bool:
        t = self.rcfg.controller_timeout
        return t > 0 and self.clock() - self.last_contact > t


def _num(v: float, default: float = 0.0) -> float:
    return float(v) if v is not None and math.isfinite(v) else default


def _same_path(a: Polyline, b: Optional[Polyline]) -> bool:
    return (b is not None and a.loop == b.loop and
            [(p.x, p.y, p.speed) for p in a.points] == [(p.x, p.y, p.speed) for p in b.points])


class _UdpSender:
    def __init__(self):
        self._clients = {}

    def send(self, ip, port, address, args):
        from pythonosc.udp_client import SimpleUDPClient
        key = (ip, port)
        if key not in self._clients:
            self._clients[key] = SimpleUDPClient(ip, port)
        try:
            self._clients[key].send_message(address, args)
        except OSError as e:
            log.debug("状態の送信に失敗: %s", e)


class StatusLoop:
    def __init__(self, remote: RemoteControl, rate_hz: float, stop_event: threading.Event):
        self.remote, self.period, self.stop_event = remote, 1.0 / rate_hz, stop_event
        self._thread = threading.Thread(target=self._run, name="status", daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while not self.stop_event.wait(self.period):
            try:
                self.remote.send_status()
            except Exception:
                log.exception("状態送信で例外")
