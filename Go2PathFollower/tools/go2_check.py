"""実機で一度だけ行う確認: 速度指令の符号と、指令が途絶えたときの挙動。

周囲 1.5 m 以上を空け、リモコンを手に持った状態で実行すること。

    python tools/go2_check.py eth0

  1. 左旋回: Move(0, 0, +0.3) を 1 秒 → 上から見て反時計回りに回れば vyaw の符号は仕様どおり
  2. 前進:   Move(+0.2, 0, 0) を 1 秒 → 前に進めば OK
  3. 左移動: Move(0, +0.15, 0) を 1 秒 → 左に動けば OK
  4. （任意）Move(+0.15, 0, 0) を 1 回だけ送り、その後 2 秒間何も送らない
     → 自分で止まるか、歩き続けるかを確認（README の「指令が途絶えたときの挙動」に記録）
各ステップの後に StopMove を送る。Ctrl-C でも StopMove を送ってから終了する。
"""
import sys
import time

from unitree_sdk2py.core.channel import ChannelFactoryInitialize
from unitree_sdk2py.go2.sport.sport_client import SportClient


def stream(client, vx, vy, vyaw, seconds, rate=20.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        client.Move(vx, vy, vyaw)
        time.sleep(1.0 / rate)


def main():
    iface = sys.argv[1] if len(sys.argv) > 1 else "eth0"
    if input("周囲の安全を確認しましたか？ yes と入力: ").strip() != "yes":
        return
    ChannelFactoryInitialize(0, iface)
    client = SportClient()
    client.SetTimeout(1.0)
    client.Init()
    steps = [
        ("左旋回 vyaw=+0.3（反時計回りになるはず）", (0.0, 0.0, 0.3)),
        ("前進 vx=+0.2", (0.2, 0.0, 0.0)),
        ("左移動 vy=+0.15", (0.0, 0.15, 0.0)),
    ]
    try:
        for label, v in steps:
            input(f"\n[Enter] {label} を 1 秒実行")
            stream(client, *v, seconds=1.0)
            print("StopMove:", client.StopMove())
        if input("\n指令途絶テストをしますか？ (y/N): ").strip().lower() == "y":
            client.Move(0.15, 0.0, 0.0)
            print("Move を 1 回だけ送りました。2 秒間何も送りません…")
            time.sleep(2.0)
            print("StopMove:", client.StopMove())
            print("→ 2 秒の間に止まったか、歩き続けたかを記録してください")
    finally:
        client.Move(0.0, 0.0, 0.0)
        print("StopMove:", client.StopMove())


if __name__ == "__main__":
    main()
