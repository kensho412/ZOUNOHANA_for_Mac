#!/usr/bin/env python3
"""
*_raw.csv を記録時と同じタイミングで OSC 再生する。OSC の 0/1 で再生・停止できる。

使い方:
  python osc_replay_ctrl.py logs/20261031_184408_raw.csv
  python osc_replay_ctrl.py logs/xxx_raw.csv --start 46 --end 77 --loop

操作（OSC、既定はポート 9880）:
  /replay 1  → 先頭から再生（再生中に送ると先頭からやり直し）
  /replay 0  → 停止

Max からは例えば:
  [toggle] → [prepend /replay] → [udpsend 127.0.0.1 9880]
"""
import argparse, csv, threading, time
from pythonosc import udp_client, dispatcher, osc_server

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int, default=9871, help="再生データの送り先ポート")
ap.add_argument("--ctrl-port", type=int, default=9880, help="再生/停止を受け付けるポート")
ap.add_argument("--ctrl-addr", default="/replay")
ap.add_argument("--start", type=float, default=None)
ap.add_argument("--end", type=float, default=None)
ap.add_argument("--speed", type=float, default=1.0)
ap.add_argument("--loop", action="store_true", help="停止されるまで繰り返す")
args = ap.parse_args()


def num(s):
    for f in (int, float):
        try:
            return f(s)
        except ValueError:
            pass
    return s


msgs = []
with open(args.csv, newline="") as f:
    r = csv.reader(f)
    next(r)
    for row in r:
        if len(row) < 4 or row[2] == "/marker":
            continue
        t = float(row[0])
        if (args.start is not None and t < args.start) or (args.end is not None and t > args.end):
            continue
        msgs.append((t, row[2], [num(v) for v in row[3:] if v != ""]))
if not msgs:
    raise SystemExit("再生するメッセージがありません（範囲指定を確認してください）")

client = udp_client.SimpleUDPClient(args.host, args.port)
playing = threading.Event()      # 再生中かどうか
restart = threading.Event()      # 先頭からやり直す合図


def on_ctrl(address, *vals):
    v = int(vals[0]) if vals else 1
    if v:
        restart.set()
        playing.set()
        print("  ▶ 再生")
    else:
        playing.clear()
        print("  ■ 停止")


d = dispatcher.Dispatcher()
d.map(args.ctrl_addr, on_ctrl)
server = osc_server.ThreadingOSCUDPServer(("0.0.0.0", args.ctrl_port), d)
threading.Thread(target=server.serve_forever, daemon=True).start()

t_first = msgs[0][0]
print(f"{len(msgs)} 件（{t_first:.2f}〜{msgs[-1][0]:.2f}s）→ {args.host}:{args.port}")
print(f"操作: {args.ctrl_addr} 1 / 0 をポート {args.ctrl_port} へ。Ctrl+C で終了。")


def wait_until(target):
    """target 時刻まで待つ。停止・やり直しが来たら False を返す。"""
    while True:
        if not playing.is_set() or restart.is_set():
            return False
        remain = target - time.perf_counter()
        if remain <= 0:
            return True
        time.sleep(min(remain, 0.005))


try:
    while True:
        playing.wait()
        restart.clear()
        wall0 = time.perf_counter()
        finished = True
        for t, addr, vals in msgs:
            if not wait_until(wall0 + (t - t_first) / args.speed):
                finished = False
                break
            client.send_message(addr, vals)
        if finished:
            if args.loop and playing.is_set():
                print("  ↻ ループ")
                continue
            playing.clear()
            print("  ■ 最後まで再生しました")
except KeyboardInterrupt:
    pass
finally:
    server.shutdown()
    print("\n終了")
