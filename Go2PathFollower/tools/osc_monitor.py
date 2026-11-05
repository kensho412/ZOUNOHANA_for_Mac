"""TouchDesigner からの /dog/pose が届いているかを確認する（ロボットには何も送らない）。

    python tools/osc_monitor.py            # ポート 9000
    python tools/osc_monitor.py --port 9000 --address /dog/pose
"""
import argparse
import math
import time

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

ap = argparse.ArgumentParser()
ap.add_argument("--host", default="0.0.0.0")
ap.add_argument("--port", type=int, default=9000)
ap.add_argument("--address", default="/dog/pose")
args = ap.parse_args()

state = {"n": 0, "t0": time.monotonic(), "last": None}


def on_pose(address, *a):
    if state["last"] is None:
        state["t0"] = time.monotonic()  # 最初の受信から周波数を数える
    state["n"] += 1
    state["last"] = a
    now = time.monotonic()
    if now - state["t0"] >= 0.5:
        hz = state["n"] / (now - state["t0"])
        try:
            x, y, yaw, found = a[:4]
            print(f"{hz:5.1f} Hz  x={x:+.3f} y={y:+.3f} yaw={yaw:+.3f} ({math.degrees(yaw):+6.1f}°) found={found}")
        except (ValueError, TypeError):
            print(f"{hz:5.1f} Hz  不正な引数: {a!r}")
        state["n"], state["t0"] = 0, now


def on_other(address, *a):
    print(f"(別アドレス) {address} {a!r}")


d = Dispatcher()
d.map(args.address, on_pose)
d.set_default_handler(on_other)
print(f"{args.host}:{args.port} で {args.address} を待っています（Ctrl-C で終了）")
BlockingOSCUDPServer((args.host, args.port), d).serve_forever()
