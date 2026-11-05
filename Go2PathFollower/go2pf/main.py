"""コマンドライン入口。python -m go2pf --help"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from typing import List, Optional

from .backends import DryBackend, MockBackend, MockPoseFeeder, MockWorld, RobotBackend
from .config import Config, load_config, load_route
from .geometry import Polyline, target_heading
from .keyboard import TerminalKeys
from .pose import OscPoseReceiver, PoseBuffer
from .supervisor import Supervisor

log = logging.getLogger("go2pf")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="go2pf", description="Go2 経路追従コントローラ")
    ap.add_argument("--config", default="config/default.yaml", help="YAML 設定ファイル")
    ap.add_argument("--route", help="ルート JSON（省略時は TouchDesigner から /go2pf/config で受け取る）")
    ap.add_argument("--backend", choices=["mock", "go2", "dry"], default="mock",
                    help="mock: シミュレーション（既定） / go2: 実機 / dry: OSC を受けて計算するがロボットには送らない")
    ap.add_argument("--loop", action="store_true", help="ループ走行（設定の control.loop を上書き）")
    ap.add_argument("--no-viz", action="store_true", help="可視化しない")
    ap.add_argument("--no-log", action="store_true", help="CSV ログを書かない")
    ap.add_argument("--mock-osc", action="store_true",
                    help="mock 時、シミュレーション位置を OSC で自分宛に送り、OSC 受信経路ごと確認する")
    ap.add_argument("--auto-arm", action="store_true", help="mock / dry 専用: 位置を受信したら自動でアーム")
    ap.add_argument("--duration", type=float, default=0.0, help="指定秒数で終了（0 なら無制限）")
    ap.add_argument("--port", type=int, help="受信ポート（osc.port を上書き）")
    ap.add_argument("--status-port", type=int, help="状態の送り返し先ポート（remote.status_port を上書き）")
    ap.add_argument("--interface", help="Go2 につながる NIC 名（go2.interface を上書き）")
    ap.add_argument("--controller-timeout", type=float,
                    help="操作元から この秒数なにも届かなければ停止して終了（remote.controller_timeout を上書き）")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap.parse_args(argv)


def check_route(cfg: Config, path: Polyline) -> None:
    fence = cfg.safety.geofence.to_geofence()
    for i, p in enumerate(path.points):
        if not fence.contains(p.x, p.y):
            raise ValueError(f"ルートの点 {i} ({p.x}, {p.y}) がジオフェンスの外です")


def mock_start(cfg: Config, path: Polyline):
    """mock のロボットの初期位置: 設定 mock.start か、ルートの始点で最初の区間を向く。"""
    start = cfg.mock.start or {}
    p0, p1 = path.points[0], path.points[1]
    return (start.get("x", p0.x), start.get("y", p0.y),
            start.get("yaw", target_heading(p0.x, p0.y, p1.x, p1.y)))


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if args.auto_arm and args.backend == "go2":
        log.error("--auto-arm は mock / dry でのみ使えます")
        return 2

    cfg = load_config(args.config)
    if args.loop:
        cfg.control.loop = True
    if args.port is not None:
        cfg.osc.port = args.port
    if args.status_port is not None:
        cfg.remote.status_port = args.status_port
    if args.interface:
        cfg.go2.interface = args.interface
    if args.controller_timeout is not None:
        cfg.remote.controller_timeout = args.controller_timeout
    path = None
    if args.route:
        path = Polyline(load_route(args.route, cfg.control.speed_max), loop=cfg.control.loop)
        check_route(cfg, path)
        log.info("ルート: %d 点, 全長 %.2f m, loop=%s", len(path.points), path.length, path.loop)
    elif not cfg.remote.enabled:
        log.error("--route を指定するか、remote.enabled を true にしてください")
        return 2
    else:
        log.info("ルートは TouchDesigner から受け取ります")

    # SIGTERM でも finally の停止処理を通す
    def on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_sigterm)

    stop_event = threading.Event()
    pose_buf = PoseBuffer()
    backend: Optional[RobotBackend] = None
    receiver = feeder = keys = logger = sup = remote = None
    errors: List[BaseException] = []

    try:
        # ---- 出力先
        if args.backend == "go2":
            from .backends import Go2Backend
            backend = Go2Backend(cfg.go2)
        elif args.backend == "dry":
            backend = DryBackend()
        else:
            world = MockWorld(0.0, 0.0, 0.0, cfg.mock.velocity_tau)
            backend = MockBackend(world)
            if path is not None:
                world.reset(*mock_start(cfg, path))

        # ---- 監視・ログ
        t0 = time.monotonic()
        if cfg.log.enabled and not args.no_log:
            from .csvlog import CsvLogger
            logger = CsvLogger(cfg.log.dir, t0)
            log.info("ログ: %s", logger.path)
        sup = Supervisor(cfg, path, backend, pose_buf,
                         on_snapshot=logger.write if logger else None)

        # ---- シミュレーションの位置（mock）
        if args.backend == "mock":
            if args.mock_osc:
                from pythonosc.udp_client import SimpleUDPClient
                client = SimpleUDPClient("127.0.0.1", cfg.osc.port)
                publish = lambda p: client.send_message(  # noqa: E731
                    cfg.osc.address, [p.x, p.y, p.yaw, int(p.found)])
            else:
                publish = pose_buf.update
            feeder = MockPoseFeeder(backend.world, cfg.mock, publish)
            feeder.start()

        # ---- 入力元・リモート操作
        pose_map = {cfg.osc.address: pose_buf} if (args.backend != "mock" or args.mock_osc) else {}
        handlers = {}
        if cfg.remote.enabled:
            from .remote import RemoteControl, StatusLoop

            def on_route(p: Polyline) -> None:
                # mock で一度も動いていなければ、ロボットを新しいルートの始点に置き直す
                if feeder is not None and backend.move_calls == 0:
                    feeder.teleport(*mock_start(cfg, p))

            remote = RemoteControl(sup, cfg, on_quit=stop_event.set, on_route=on_route)
            handlers = remote.handlers()
            StatusLoop(remote, cfg.remote.status_rate_hz, stop_event).start()
        receiver = OscPoseReceiver(cfg.osc.host, cfg.osc.port, pose_map, handlers)
        receiver.start()
        log.info("OSC 受信: %s:%d %s%s", cfg.osc.host, cfg.osc.port, " ".join(pose_map) or "(位置は mock)",
                 "  /go2pf/*" if handlers else "")

        # ---- キー操作
        def on_key(k: str) -> None:
            if k == " ":
                sup.estop()
            elif k in ("a", "A"):
                log.info(sup.arm())
            elif k in ("q", "Q"):
                stop_event.set()

        keys = TerminalKeys(on_key)
        keys.start()
        log.info("キー操作: [a] アーム  [space] 非常停止  [q] 終了  （起動直後は停止状態です）")

        # ---- 制御ループ
        def control_loop() -> None:
            next_t = time.monotonic()
            armed_once = False
            while not stop_event.is_set():
                period = 1.0 / sup.cfg.control.rate_hz   # TouchDesigner から変更されうる
                try:
                    sup.tick()
                    if remote is not None and remote.controller_lost():
                        log.error("操作元から %.1f s 応答がないため停止して終了します",
                                  cfg.remote.controller_timeout)
                        sup.estop("操作元との通信断")
                        stop_event.set()
                        return
                    if args.auto_arm and not armed_once and pose_buf.latest()[0] is not None:
                        log.info(sup.arm())
                        armed_once = True
                except BaseException as e:
                    errors.append(e)
                    log.exception("制御ループで例外。停止します")
                    try:
                        sup.fault(f"例外: {e}")
                    finally:
                        stop_event.set()
                    return
                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    stop_event.wait(delay)
                else:
                    next_t = time.monotonic()

        ctrl = threading.Thread(target=control_loop, name="control", daemon=True)
        ctrl.start()

        if args.duration > 0:
            timer = threading.Timer(args.duration, stop_event.set)
            timer.daemon = True
            timer.start()

        if cfg.viz.enabled and not args.no_viz:
            from .viz import run_viz
            run_viz(sup, stop_event, on_key, cfg.viz.fps, cfg.viz.trail)
            stop_event.set()
        else:
            while not stop_event.wait(0.2):
                pass
        ctrl.join(timeout=2.0)
    except KeyboardInterrupt:
        log.info("中断されました")
    finally:
        stop_event.set()
        # 停止指令は何より先に
        if sup is not None:
            try:
                sup.shutdown()
            except Exception:
                log.exception("終了時の停止に失敗")
        elif backend is not None:
            backend.stop()
        if sup is not None and sup.last.pose is not None:
            log.info("最終状態: %s（%s）", sup.state.value, sup.reason)
        for close in (keys and keys.stop, feeder and feeder.stop, receiver and receiver.stop,
                      logger and logger.close, backend and backend.close):
            if close:
                try:
                    close()
                except Exception:
                    log.exception("終了処理で例外")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
