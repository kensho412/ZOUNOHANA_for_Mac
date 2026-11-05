"""Go2 と通信できるかを確かめる（読むだけ。ロボットは動かさない）。

    .venv/bin/python tools/go2_status.py enP8p1s0

  - ロボットの状態（rt/sportmodestate）を 2 秒受信して、受信数・モード・歩容・体の高さ・位置を表示
  - モーションモード（MotionSwitcherClient.CheckMode）を表示（空なら sport サービスが動いていない）
"""
import sys
import time

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_

iface = sys.argv[1] if len(sys.argv) > 1 else "eth0"
ChannelFactoryInitialize(0, iface)

got = {}


def handler(topic):
    def h(msg: SportModeState_):
        got.setdefault(topic, []).append(msg)
    return h


subs = []
for topic in ("rt/sportmodestate", "rt/lf/sportmodestate"):
    s = ChannelSubscriber(topic, SportModeState_)
    s.Init(handler(topic), 10)
    subs.append(s)
time.sleep(2.0)

if not got:
    print("NG: ロボットの状態が届きません。NIC 名、ロボットの電源、ケーブルを確認してください")
for topic, msgs in got.items():
    m = msgs[-1]
    print(f"{topic}: {len(msgs)} 件 / 2 s  mode={m.mode} gait_type={m.gait_type} "
          f"body_height={m.body_height:.3f} position=({m.position[0]:+.2f}, {m.position[1]:+.2f}) "
          f"foot_force={list(m.foot_force)} error_code={m.error_code}")

try:
    from unitree_sdk2py.comm.motion_switcher.motion_switcher_client import MotionSwitcherClient
    msc = MotionSwitcherClient()
    msc.SetTimeout(3.0)
    msc.Init()
    code, data = msc.CheckMode()
    print(f"モーションモード: code={code} data={data}"
          + ("  ← 空: sport サービスが動いていない（Move が効かない）" if code == 0 and not (data or {}).get("name") else ""))
except Exception as e:
    print("モーションモードを確認できません:", e)
