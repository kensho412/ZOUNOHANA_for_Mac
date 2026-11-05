"""Go2 を Isaac Sim 上でラジコン操作し、足裏の力を innu.maxpat 互換の OSC で送るシミュレータ.

実機と同じ形式で送る:
    /gait/force  i i i i   (FR FL RR RL, 実機の foot_force 相当の整数)
    -> Max: [udpreceive 9871] -> [route /gait/force] -> [p thr]

操作:
    キーボード  ↑↓ 前後 / ←→ 左右平行移動 / N M 旋回 (Isaac Sim の Go2 例と同じ)
    ゲームパッド 左スティック 前後・左右 / 右スティック 旋回 (接続されていれば)

実行例 (Windows, venv 有効化後):
    python go2_osc_sim.py --host 192.168.1.20
    python go2_osc_sim.py --host 192.168.1.20 --obstacles
    python go2_osc_sim.py --host 192.168.1.20 --scene C:/path/to/venue.usd
    python go2_osc_sim.py --gain 0.7          # 自動キャリブレーションせず固定倍率

Isaac Sim 6.2 (pip) の go2_standalone.py をベースにしている.
"""

import argparse
import os
import socket
import struct

os.environ.setdefault("OMNI_KIT_ACCEPT_EULA", "YES")

from isaacsim import SimulationApp

parser = argparse.ArgumentParser(description="Go2 RC simulation -> OSC (/gait/force) for Max")
parser.add_argument("--host", default="127.0.0.1", help="Max が動いている PC の IP (Mac の IP)")
parser.add_argument("--port", type=int, default=9871, help="innu.maxpat の udpreceive のポート")
parser.add_argument("--address", default="/gait/force", help="OSC アドレス")
parser.add_argument("--send-hz", type=float, default=200.0, help="OSC 送信レート (物理は 200Hz)")
parser.add_argument("--gain", type=float, default=None, help="N -> 実機値 の固定倍率. 指定すると自動キャリブレーションしない")
parser.add_argument("--max-speed", type=float, default=1.0, help="キー/スティック最大時の速度 [m/s, rad/s]")
parser.add_argument("--obstacles", action="store_true", help="サンプルの障害物 (段差・箱・坂) を置く")
parser.add_argument("--scene", default=None, help="追加で読み込む USD (会場モデルなど)")
parser.add_argument("--headless", action="store_true", help="画面なしで実行")
parser.add_argument("--verbose", action="store_true", help="送信値を 1 秒ごとに表示")
parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
parser.add_argument("--engine", choices=["physx", "newton"], default="physx")
args, _unknown = parser.parse_known_args()

extra_args = [
    f"--/exts/isaacsim.core.simulation_manager/default_engine={args.engine}",
    "--enable",
    "isaacsim.sensors.experimental.physics",
]
if args.engine == "newton":
    extra_args.extend(["--enable", "isaacsim.physics.newton", "--enable", "isaacsim.physics.newton.tensors"])
simulation_app = SimulationApp({"headless": args.headless, "extra_args": extra_args})

import carb
import numpy as np
import omni.appwindow
import omni.timeline
import omni.usd
from isaacsim.core.experimental.utils.stage import define_prim, set_stage_units, set_stage_up_axis
from isaacsim.core.rendering_manager import RenderingManager
from isaacsim.core.simulation_manager import SimulationManager
from isaacsim.core.simulation_manager.impl.isaac_events import IsaacEvents
from isaacsim.robot.policy.examples import PolicyEnvConfig, RobotPolicyRunner, get_go2_spec
from isaacsim.sensors.experimental.physics import Contact, ContactSensor
from isaacsim.storage.native import get_assets_root_path
from pxr import Gf, Usd, UsdGeom, UsdPhysics, UsdShade

# ---------------------------------------------------------------------------
# 設定 (土曜に実機で測った値に書き換える)
# ---------------------------------------------------------------------------

# 実機 Go2 を静止立ちさせたときの foot_force (FR FL RR RL).
# 仮の値: innu.maxpat のメッセージボックスに残っていた値.
# 実機は FL のセンサ値が低い (パッチで *2 している) ので, それも含めてここで再現する.
REAL_STANDING = [23.0, 17.0, 36.0, 26.0]

# --gain 指定時の脚ごとの補正 (FL の感度が約半分なのを真似る)
FOOT_SCALE = [1.0, 0.5, 1.0, 1.0]

LEGS = ["FR", "FL", "RR", "RL"]  # 実機 foot_force の並び順
SETTLE_SEC = 1.5  # 立ち上がりを待つ時間
CALIB_SEC = 2.0  # 静止立ちの平均を取る時間
GAMEPAD_DEADZONE = 0.1


# ---------------------------------------------------------------------------
# OSC (標準ライブラリだけで送る)
# ---------------------------------------------------------------------------


def _osc_pad(b: bytes) -> bytes:
    return b + b"\0" * (4 - len(b) % 4)


def osc_message_ints(address: str, values: list[int]) -> bytes:
    """int 引数だけの OSC メッセージを組み立てる."""
    msg = _osc_pad(address.encode())
    msg += _osc_pad(("," + "i" * len(values)).encode())
    msg += b"".join(struct.pack(">i", int(v)) for v in values)
    return msg


sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
osc_target = (args.host, args.port)


def send_force(values: list[int]) -> None:
    try:
        sock.sendto(osc_message_ints(args.address, values), osc_target)
    except OSError as error:  # 一時的な送信エラーで止めない
        carb.log_warn(f"OSC send failed: {error}")


# ---------------------------------------------------------------------------
# シーン
# ---------------------------------------------------------------------------

set_stage_up_axis("Z")
set_stage_units(meters_per_unit=1.0)
assets_root_path = get_assets_root_path()
if assets_root_path is None:
    carb.log_error("Could not find Isaac Sim assets folder")

ground = define_prim("/World/Ground", "Xform")
ground.GetReferences().AddReference(assets_root_path + "/Isaac/Environments/Grid/default_environment.usd")
define_prim("/World/PhysicsScene", "PhysicsScene")

if args.scene:
    define_prim("/World/Scene", "Xform").GetReferences().AddReference(args.scene)

SimulationManager.set_physics_sim_device(args.device)

spec = get_go2_spec()
go2 = RobotPolicyRunner(spec, prim_path="/World/Go2", position=[0.0, 0.0, 0.5])
go2.spawn()

timing = PolicyEnvConfig.from_file(spec.engines[args.engine].env_config_path).timing
RenderingManager.set_dt(timing.render_interval * timing.physics_dt)
SimulationManager.set_physics_dt(go2.physics_dt)

stage = omni.usd.get_context().get_stage()
while omni.usd.get_context().get_stage_loading_status()[2] > 0:
    simulation_app.update()


def apply_ground_friction() -> None:
    """学習時と同じ床の摩擦 (Isaac Sim の Go2 例と同じ値)."""
    material = UsdShade.Material.Define(stage, "/World/Ground/Looks/PhysicsMaterial")
    physics_material = UsdPhysics.MaterialAPI.Apply(material.GetPrim())
    physics_material.CreateStaticFrictionAttr().Set(1.0)
    physics_material.CreateDynamicFrictionAttr().Set(1.0)
    physics_material.CreateRestitutionAttr().Set(0.0)
    plane = stage.GetPrimAtPath("/World/Ground/GroundPlane/CollisionPlane")
    if plane.IsValid():
        UsdShade.MaterialBindingAPI.Apply(plane).Bind(material)


def add_box(path: str, center, size, rotate_y_deg: float = 0.0, mass: float | None = None) -> None:
    """箱の障害物. mass を指定すると押せる箱, None なら固定."""
    cube = UsdGeom.Cube.Define(stage, path)
    cube.CreateSizeAttr(1.0)
    xform = UsdGeom.XformCommonAPI(cube)
    xform.SetTranslate(Gf.Vec3d(*center))
    xform.SetRotate(Gf.Vec3f(0.0, rotate_y_deg, 0.0))
    xform.SetScale(Gf.Vec3f(*size))
    cube.CreateDisplayColorAttr([Gf.Vec3f(0.6, 0.6, 0.65)])
    UsdPhysics.CollisionAPI.Apply(cube.GetPrim())
    if mass is not None:
        UsdPhysics.RigidBodyAPI.Apply(cube.GetPrim())
        UsdPhysics.MassAPI.Apply(cube.GetPrim()).CreateMassAttr(mass)


def add_demo_obstacles() -> None:
    add_box("/World/Obstacles/step_5cm", (1.5, 0.0, 0.025), (0.6, 1.2, 0.05))
    add_box("/World/Obstacles/slope", (3.0, 0.0, 0.0), (1.5, 1.2, 0.1), rotate_y_deg=-8.0)
    add_box("/World/Obstacles/box_2kg", (1.5, 1.2, 0.15), (0.3, 0.3, 0.3), mass=2.0)
    add_box("/World/Obstacles/wall", (0.0, -2.0, 0.25), (4.0, 0.1, 0.5))


apply_ground_friction()
if args.obstacles:
    add_demo_obstacles()


def find_leg_body(leg: str) -> str:
    """Go2 の足先 (なければすね) の prim パスを探す."""
    robot_root = stage.GetPrimAtPath("/World/Go2")
    for name in (f"{leg}_foot", f"{leg}_calf"):
        for prim in Usd.PrimRange(robot_root):
            if prim.GetName() == name and not prim.IsInstanceProxy():
                return str(prim.GetPath())
    raise RuntimeError(f"Go2 の {leg} の足が見つからない (/World/Go2 以下を確認)")


foot_sensors = []
for leg in LEGS:
    body_path = find_leg_body(leg)
    foot_sensors.append(
        ContactSensor(Contact.create(f"{body_path}/contact_sensor", min_threshold=0.0, max_threshold=1e7, radius=-1.0))
    )
    print(f"[go2_osc] {leg}: contact sensor on {body_path}")

# ---------------------------------------------------------------------------
# 入力 (キーボード + ゲームパッド)
# ---------------------------------------------------------------------------

s = args.max_speed
KEY_MAP = {
    "UP": [s, 0, 0], "NUMPAD_8": [s, 0, 0],
    "DOWN": [-s, 0, 0], "NUMPAD_2": [-s, 0, 0],
    "LEFT": [0, s, 0], "NUMPAD_4": [0, s, 0],
    "RIGHT": [0, -s, 0], "NUMPAD_6": [0, -s, 0],
    "N": [0, 0, s], "NUMPAD_7": [0, 0, s],
    "M": [0, 0, -s], "NUMPAD_9": [0, 0, -s],
}  # fmt: skip
key_command = np.zeros(3, dtype=np.float32)
pad_axes: dict[str, float] = {}

input_iface = carb.input.acquire_input_interface()
appwindow = omni.appwindow.get_default_app_window()


def on_keyboard(event, *_args, **_kwargs) -> bool:
    delta = KEY_MAP.get(event.input.name)
    if delta is None:
        return True
    if event.type == carb.input.KeyboardEventType.KEY_PRESS:
        key_command[:] += delta
    elif event.type == carb.input.KeyboardEventType.KEY_RELEASE:
        key_command[:] -= delta
    return True


def on_gamepad(event, *_args, **_kwargs) -> bool:
    pad_axes[event.input.name] = float(event.value)
    return True


def gamepad_command() -> np.ndarray:
    def axis(pos: str, neg: str) -> float:
        v = pad_axes.get(pos, 0.0) - pad_axes.get(neg, 0.0)
        return 0.0 if abs(v) < GAMEPAD_DEADZONE else v

    return s * np.array(
        [
            axis("LEFT_STICK_UP", "LEFT_STICK_DOWN"),
            axis("LEFT_STICK_LEFT", "LEFT_STICK_RIGHT"),
            axis("RIGHT_STICK_LEFT", "RIGHT_STICK_RIGHT"),
        ],
        dtype=np.float32,
    )


keyboard_sub = None
gamepad_sub = None
if not args.headless:
    keyboard_sub = input_iface.subscribe_to_keyboard_events(appwindow.get_keyboard(), on_keyboard)
    try:
        gamepad = appwindow.get_gamepad(0)
        if gamepad is not None:
            gamepad_sub = input_iface.subscribe_to_gamepad_events(gamepad, on_gamepad)
    except Exception as error:  # noqa: BLE001 - ゲームパッドは任意
        carb.log_warn(f"gamepad unavailable: {error}")

# ---------------------------------------------------------------------------
# 物理ステップごと: 歩行ポリシー -> 足裏の力 -> OSC
# ---------------------------------------------------------------------------

first_step = True
reset_needed = False
policy_failed = False
sim_time = 0.0
send_accum = 0.0
calib_sum = np.zeros(4)
calib_n = 0
gains = None if args.gain is None else np.array(FOOT_SCALE) * args.gain
provisional_gains = np.array(FOOT_SCALE) * 0.7
last_values = [0, 0, 0, 0]
last_print = 0.0


def read_forces() -> np.ndarray:
    return np.array([sensor.get_data()["force"] for sensor in foot_sensors], dtype=np.float64)


def on_physics_step(step_size: float, _context: object) -> None:
    global first_step, reset_needed, policy_failed, sim_time, send_accum
    global calib_sum, calib_n, gains, last_values, last_print
    if policy_failed:
        return
    try:
        calibrating = gains is None
        command = np.zeros(3, dtype=np.float32) if calibrating else key_command + gamepad_command()
        if first_step:
            go2.restart_from_default_state(command)
            first_step = False
            sim_time = 0.0
            return
        if reset_needed:
            reset_needed = False
            first_step = True
            return
        go2.step(step_size, command)
        sim_time += step_size

        forces = read_forces()

        if calibrating and SETTLE_SEC <= sim_time < SETTLE_SEC + CALIB_SEC:
            calib_sum += forces
            calib_n += 1
        elif calibrating and sim_time >= SETTLE_SEC + CALIB_SEC and calib_n > 0:
            sim_standing = calib_sum / calib_n
            gains = np.array(REAL_STANDING) / np.maximum(sim_standing, 1.0)
            print(f"[go2_osc] calibrated. sim standing [N] = {np.round(sim_standing, 1).tolist()}")
            print(f"[go2_osc] gains = {np.round(gains, 3).tolist()}  (--gain で固定も可)")
            print("[go2_osc] 操作できます")

        send_accum += step_size
        if send_accum >= 1.0 / args.send_hz:
            send_accum = 0.0
            g = gains if gains is not None else provisional_gains
            last_values = [max(0, int(round(v))) for v in forces * g]
            send_force(last_values)

        if args.verbose and sim_time - last_print >= 1.0:
            last_print = sim_time
            print(f"[go2_osc] {args.address} {last_values}")
    except Exception as error:  # noqa: BLE001 - physics callback must not raise
        policy_failed = True
        carb.log_error(f"go2_osc_sim: stopped: {error}")


physics_callback_id = SimulationManager.register_callback(on_physics_step, IsaacEvents.POST_PHYSICS_STEP)

print(f"[go2_osc] sending {args.address} (FR FL RR RL) -> {args.host}:{args.port}")
if gains is None:
    print(f"[go2_osc] {SETTLE_SEC + CALIB_SEC:.1f} 秒間キャリブレーション中は操作しないでください")

timeline = omni.timeline.get_timeline_interface()
timeline.play()
simulation_app.update()

while simulation_app.is_running():
    simulation_app.update()
    if not SimulationManager.is_simulating():
        reset_needed = True

timeline.stop()
SimulationManager.deregister_callback(physics_callback_id)
if keyboard_sub is not None:
    input_iface.unsubscribe_to_keyboard_events(appwindow.get_keyboard(), keyboard_sub)
go2.close()
sock.close()
simulation_app.close()
