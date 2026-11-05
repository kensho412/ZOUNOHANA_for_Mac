"""TouchDesigner から Go2 経路追従（go2pf）を操作するモジュール。

build_td.py が作る COMP「go2」の中の Text DAT「go2ctl」にこのファイルが読み込まれ（Sync to File）、
各コールバック DAT から parent().op('go2ctl').module.<関数>(parent(), ...) で呼ばれる。

役割:
  - 天井カメラの画像から ArUco でロボットの位置を求め、/dog/pose を go2pf へ送る
  - カスタムパラメータとルートの表（route）を /go2pf/config で go2pf へ送る（変更時と 1 秒ごと）
  - 起動・終了（go2pf のプロセス）、アーム、非常停止
  - go2pf からの状態（/go2pf/status など）を受けて表示する
  - 俯瞰図（map）とカメラ映像への重ね描き（overlay）を Script TOP 用に作る

TD 固有の API は comp（COMP）の .par / .op() / Par.eval() / TOP.numpyArray() と、表 DAT の読み書きだけ。
それ以外は普通の Python なので、tests/test_go2ctl.py で TD なしに動作確認している。
"""
import json
import math
import os
import shlex
import socket
import struct
import subprocess
import sys
import time
from collections import deque

import numpy as np

ROOT_DEFAULT = "/Users/nibos/Desktop/ZOUNOHANA2026/Go2PathFollower"

# ---------------------------------------------------------------- パラメータ定義
# (ページ, 名前, ラベル, 種類, 既定値, 最小, 最大, go2pf の設定キー or None, 補足)
# 名前は TD の規則どおり「先頭だけ大文字」。xy は 名前x / 名前y の 2 つになる。

MENUS = {
    "Backend": (["mock", "dry", "go2"], ["Mock（シミュレーション）", "Dry（計算のみ・送信なし）", "Go2（実機）"]),
    "Dictionary": (["4X4_50", "5X5_100", "6X6_250"], ["4X4_50", "5X5_100", "6X6_250"]),
    "Routesource": (["table", "sop"], ["表（route DAT）", "SOP（Null SOP を参照）"]),
    "Sopplane": (["xy", "xz", "xzneg"], ["XY 平面（x→x, y→y）", "XZ 平面（x→x, z→y）", "XZ 平面（x→x, -z→y）"]),
}

PARAMS = [
    # ---- 運転
    ("Run", "Launch", "go2pf を起動", "pulse", None, None, None, None),
    ("Run", "Shutdown", "go2pf を終了", "pulse", None, None, None, None),
    ("Run", "Arm", "アーム（走行開始）", "pulse", None, None, None, None),
    ("Run", "Estop", "非常停止 [space]", "pulse", None, None, None, None),
    ("Run", "Backend", "バックエンド", "menu", "mock", None, None, None),
    ("Run", "Loop", "ループ走行", "toggle", False, None, None, "control.loop"),
    ("Run", "Routefile", "ルート JSON", "file", "routes/example_route.json", None, None, None),
    ("Run", "Routeload", "ルート読み込み → 表", "pulse", None, None, None, None),
    ("Run", "Routesave", "表 → ルート保存", "pulse", None, None, None, None),
    ("Run", "Autolaunch", "TD 起動時に go2pf も起動", "toggle", False, None, None, None),
    # ---- ルートの元
    ("Route", "Routesource", "ルートの元", "menu", "sop", None, None, None),
    ("Route", "Routesop", "ルートの SOP", "sop", None, None, None, None),
    ("Route", "Sopplane", "SOP の平面", "menu", "xy", None, None, None),
    ("Route", "Sopautoloop", "閉じた形ならループ", "toggle", True, None, None, None),
    ("Route", "Routereverse", "逆向きに走る", "toggle", False, None, None, None),
    ("Route", "Routeshift", "始点をずらす（ループ、0〜1）", "float", 0.0, 0.0, 1.0, None),
    ("Route", "Routespacing", "点の間隔 [m]（0 で SOP の点のまま）", "float", 0.0, 0.0, 1.0, None),
    # ---- 接続
    ("Connect", "Sshtarget", "ssh 先 user@host（空ならこの PC で起動）", "str", "", None, None, None),
    ("Connect", "Remotedir", "ssh 先の Go2PathFollower フォルダ", "str", "Go2PathFollower", None, None, None),
    ("Connect", "Host", "go2pf の IP（ssh 先があればそちらを使う）", "str", "127.0.0.1", None, None, None),
    ("Connect", "Port", "go2pf の受信ポート", "int", 9000, 1024, 65535, None),
    ("Connect", "Statusport", "状態の受信ポート", "int", 9001, 1024, 65535, None),
    ("Connect", "Projectdir", "Go2PathFollower フォルダ", "folder", ROOT_DEFAULT, None, None, None),
    ("Connect", "Python", "Python（空なら .venv）", "str", "", None, None, None),
    ("Connect", "Configfile", "設定 YAML", "str", "config/default.yaml", None, None, None),
    ("Connect", "Go2interface", "Go2 の NIC 名", "str", "eth0", None, None, None),
    ("Connect", "Launchcmd", "起動コマンド上書き（ssh など）", "str", "", None, None, None),
    # ---- カメラ
    ("Camera", "Detect", "検出して送信", "toggle", True, None, None, None),
    ("Camera", "Cameratop", "カメラ TOP", "top", "cam", None, None, None),
    ("Camera", "Sendhz", "送信周波数 [Hz]", "float", 30.0, 5.0, 60.0, None),
    ("Camera", "Cameraheight", "カメラの高さ [m]", "float", 3.0, 0.5, 10.0, None),
    ("Camera", "Markerheight", "マーカーの高さ [m]", "float", 0.0, 0.0, 1.5, None),
    ("Camera", "Nadirauto", "カメラ真下 = 画像中心", "toggle", True, None, None, None),
    ("Camera", "Nadir", "カメラ真下 [m]", "xy", (0.0, 0.0), -5.0, 5.0, None),
    ("Camera", "Yawoffset", "向きの補正 [deg]", "float", 0.0, -180.0, 180.0, None),
    ("Camera", "Robotid", "ロボットのマーカー ID", "int", 10, 0, 249, None),
    ("Camera", "Dictionary", "ArUco 辞書", "menu", "4X4_50", None, None, None),
    ("Camera", "Recalibrate", "キャリブレーションをやり直す", "pulse", None, None, None, None),
    # ---- 区画
    ("Area", "Ref0", "基準 ID0 の位置 [m]", "xy", (-2.0, -2.0), -6.0, 6.0, None),
    ("Area", "Ref1", "基準 ID1 の位置 [m]", "xy", (2.0, -2.0), -6.0, 6.0, None),
    ("Area", "Ref2", "基準 ID2 の位置 [m]", "xy", (2.0, 2.0), -6.0, 6.0, None),
    ("Area", "Ref3", "基準 ID3 の位置 [m]", "xy", (-2.0, 2.0), -6.0, 6.0, None),
    ("Area", "Fencemin", "ジオフェンス 最小 x, y [m]", "xy", (-2.3, -2.3), -6.0, 0.0, None),
    ("Area", "Fencemax", "ジオフェンス 最大 x, y [m]", "xy", (2.3, 2.3), 0.0, 6.0, None),
    ("Area", "Geofencehorizon", "外へ向かう予測 [s]", "float", 0.5, 0.0, 2.0, "safety.geofence_horizon"),
    ("Area", "Mapmargin", "俯瞰図の余白 [m]", "float", 0.4, 0.0, 3.0, None),
    # ---- 追従
    ("Control", "Ratehz", "制御周期 [Hz]", "float", 30.0, 10.0, 100.0, "control.rate_hz"),
    ("Control", "Lookahead", "先読み距離 [m]", "float", 0.5, 0.1, 2.0, "control.lookahead"),
    ("Control", "Speed", "既定の前進速度 [m/s]", "float", 0.3, 0.0, 1.0, "control.speed"),
    ("Control", "Speedmax", "前進速度の上限 [m/s]", "float", 0.6, 0.05, 1.0, "control.speed_max"),
    ("Control", "Kyaw", "旋回ゲイン", "float", 1.5, 0.0, 5.0, "control.k_yaw"),
    ("Control", "Vyawmax", "旋回速度の上限 [rad/s]", "float", 1.0, 0.1, 2.0, "control.vyaw_max"),
    ("Control", "Klateral", "横ずれ補正ゲイン", "float", 0.0, 0.0, 3.0, "control.k_lateral"),
    ("Control", "Vymax", "横移動の上限 [m/s]", "float", 0.2, 0.0, 0.5, "control.vy_max"),
    ("Control", "Accelvx", "加速度 vx [m/s²]", "float", 0.5, 0.05, 3.0, "control.accel_vx"),
    ("Control", "Accelvy", "加速度 vy [m/s²]", "float", 0.5, 0.05, 3.0, "control.accel_vy"),
    ("Control", "Accelvyaw", "加速度 vyaw [rad/s²]", "float", 2.0, 0.1, 10.0, "control.accel_vyaw"),
    ("Control", "Goalradius", "到達判定半径 [m]", "float", 0.25, 0.05, 1.0, "control.goal_radius"),
    ("Control", "Searchback", "最近点探索 後ろ [m]", "float", 0.2, 0.0, 2.0, "control.search_back"),
    ("Control", "Searchahead", "最近点探索 前 [m]", "float", 1.0, 0.1, 5.0, "control.search_ahead"),
    # ---- 安全
    ("Safety", "Posetimeout", "見失いタイムアウト [s]", "float", 0.5, 0.1, 2.0, "safety.pose_timeout"),
    ("Safety", "Resume", "見失いから自動で再開", "toggle", True, None, None, "safety.resume_after_tracking_loss"),
    # ---- 状態（表示のみ）
    ("Status", "Procstate", "go2pf プロセス", "readonly", "", None, None, None),
    ("Status", "State", "状態", "readonly", "", None, None, None),
    ("Status", "Reason", "理由", "readonly", "", None, None, None),
    ("Status", "Configack", "設定の反映", "readonly", "", None, None, None),
    ("Status", "Routeinfo", "ルート", "readonly", "", None, None, None),
    ("Status", "Calib", "キャリブレーション", "readonly", "", None, None, None),
    ("Status", "Tracking", "検出", "readonly", "", None, None, None),
    ("Status", "Posetxt", "位置", "readonly", "", None, None, None),
    ("Status", "Cmdtxt", "指令", "readonly", "", None, None, None),
]

STATUS_NAMES = {p[1] for p in PARAMS if p[0] == "Status"}
PULSE_NAMES = {p[1] for p in PARAMS if p[3] == "pulse"}


def read_vals(comp):
    """カスタムパラメータを {名前: 値} で読む（xy は (x, y)、top は OP か None）。"""
    vals = {}
    for page, name, label, kind, default, lo, hi, key in PARAMS:
        if kind in ("pulse", "readonly"):
            continue
        try:
            if kind == "xy":
                vals[name] = (float(getattr(comp.par, name + "x").eval()),
                              float(getattr(comp.par, name + "y").eval()))
            else:
                vals[name] = getattr(comp.par, name).eval()
        except AttributeError:
            vals[name] = default
    return vals


# ---------------------------------------------------------------- 設定の組み立て（純粋関数）

def detect_settings(vals):
    """aruco_pose.Tracker.process に渡す設定。"""
    return {
        "dictionary": str(vals["Dictionary"]),
        "refs": {i: tuple(vals[f"Ref{i}"]) for i in range(4)},
        "robot_id": int(vals["Robotid"]),
        "yaw_offset": math.radians(float(vals["Yawoffset"])),
        "camera_height": float(vals["Cameraheight"]),
        "marker_height": float(vals["Markerheight"]),
        "nadir": None if vals["Nadirauto"] else tuple(vals["Nadir"]),
    }


def go2pf_config(vals, route, loop=None):
    """/go2pf/config の中身（control / safety / route）。

    route が None なら route も loop も含めない（go2pf は今のルート・ループのまま）。loop は Loop パラメータの上書き。
    """
    cfg = {"control": {}, "safety": {}}
    for page, name, label, kind, default, lo, hi, key in PARAMS:
        if not key:
            continue
        section, field = key.split(".")
        v = vals[name]
        cfg[section][field] = bool(v) if kind == "toggle" else float(v)
    fmin, fmax = vals["Fencemin"], vals["Fencemax"]
    cfg["safety"]["geofence"] = {"x_min": fmin[0], "x_max": fmax[0], "y_min": fmin[1], "y_max": fmax[1]}
    if route is None:
        # ループはルートと一緒にだけ送る（ルートを送らないときに Loop だけ変わると、今のルートが開いてしまう）
        cfg["control"].pop("loop", None)
    else:
        cfg["route"] = route
        if loop is not None:
            cfg["control"]["loop"] = bool(loop)
    return cfg


# ---------------------------------------------------------------- SOP → ルート

# OSC は UDP 1 通で送る。macOS は 1 通 9216 バイトまで、python-osc の受信も既定 8192 バイトまでなので、
# 設定全体を MAX_CONFIG_BYTES 以内に収める（ルートの点が多いと超える）
MAX_ROUTE_POINTS = 200   # SOP の点がこれより多ければ、等間隔に間引く
MAX_CONFIG_BYTES = 7500


def sop_raw_points(sop):
    """SOP の最初のプリミティブの頂点順で [(x, y, z, speed or None), ...] と、閉じているか（不明なら None）。

    プリミティブがなければ点の番号順。点アトリビュート speed があれば区間速度として使う。
    """
    pts = list(sop.points)
    order, closed = None, None
    prims = list(getattr(sop, "prims", None) or [])
    if prims:
        prim = prims[0]
        try:
            order = [v.point.index for v in prim]
        except Exception:
            order = None
        for attr in ("closed", "isClosed"):
            v = getattr(prim, attr, None)
            if v is not None:
                try:
                    closed = bool(v() if callable(v) else v)
                    break
                except Exception:
                    pass
    seq = [pts[i] for i in order] if order and len(order) >= 2 else pts
    out = []
    for p in seq:
        sp = None
        try:
            a = p.speed
            sp = float(a[0] if hasattr(a, "__len__") else a)
        except Exception:
            pass
        out.append((float(p.x), float(p.y), float(p.z), sp))
    return out, closed


def resample(points, spacing, closed):
    """[(x, y, speed), ...] を弧長 spacing ごとに打ち直す。速度は元の区間の値を引き継ぐ。"""
    if spacing <= 0 or len(points) < 2:
        return points
    pts = points + [points[0]] if closed else points
    out, carry = [pts[0]], 0.0
    for a, b in zip(pts[:-1], pts[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        d = spacing - carry
        while d <= seg:
            t = d / seg
            out.append((a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2]))
            d += spacing
        carry = seg - (d - spacing)
    if closed:
        if math.hypot(out[-1][0] - pts[0][0], out[-1][1] - pts[0][1]) < spacing * 0.5:
            out.pop()
    elif math.hypot(out[-1][0] - pts[-1][0], out[-1][1] - pts[-1][1]) > 1e-6:
        out.append(pts[-1])
    return out


def reverse_points(pts, loop):
    """[(x, y, speed), ...] を逆向きにする。区間速度は「その点から次の点まで」なので 1 つずらす。"""
    r = pts[::-1]
    n = len(r)
    return [(x, y, r[(i + 1) % n][2] if (loop or i + 1 < n) else sp) for i, (x, y, sp) in enumerate(r)]


def sop_route(sop, plane="xy", autoloop=True, loop=False, reverse=False, shift=0.0, spacing=0.0):
    """SOP → (ルート [{"x", "y", "speed"?}], loop)"""
    raw, closed = sop_raw_points(sop)
    if plane == "xz":
        pts = [(x, z, sp) for x, y, z, sp in raw]
    elif plane == "xzneg":
        pts = [(x, -z, sp) for x, y, z, sp in raw]
    else:
        pts = [(x, y, sp) for x, y, z, sp in raw]
    dedup = []
    for p in pts:  # 重なった点を除く
        if not dedup or math.hypot(p[0] - dedup[-1][0], p[1] - dedup[-1][1]) > 1e-6:
            dedup.append(p)
    pts = dedup
    if len(pts) >= 3 and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-6:
        pts = pts[:-1]  # 最後が最初と同じ点なら閉じた形
        closed = True if closed is None else closed
    is_loop = bool(closed) if (autoloop and closed is not None) else bool(loop)
    if reverse:
        pts = reverse_points(pts, is_loop)
    if is_loop and pts and shift > 0:
        k = int(round(shift * len(pts))) % len(pts)
        pts = pts[k:] + pts[:k]
    if spacing <= 0 and len(pts) > MAX_ROUTE_POINTS:
        length = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts[:-1], pts[1:]))
        spacing = length / MAX_ROUTE_POINTS * 1.01
    pts = resample(pts, spacing, is_loop)
    route = []
    for x, y, sp in pts:
        p = {"x": round(x, 3), "y": round(y, 3)}
        if sp is not None:
            p["speed"] = round(sp, 3)
        route.append(p)
    return route, is_loop


def current_route(comp, vals):
    """(route, loop, 警告文 or "") を、Route > ルートの元 に従って作る。"""
    if vals.get("Routesource") == "sop":
        sop = vals.get("Routesop")
        if sop is None:
            return [], bool(vals["Loop"]), "ルートの SOP が見つかりません（Route > ルートの SOP）"
        try:
            route, loop = sop_route(sop, vals["Sopplane"], vals["Sopautoloop"], vals["Loop"],
                                    vals["Routereverse"], float(vals["Routeshift"]), float(vals["Routespacing"]))
        except Exception as e:
            return [], bool(vals["Loop"]), f"SOP を読めません: {e}"
    else:
        route, loop = read_route_table(comp.op("route")), bool(vals["Loop"])
        if vals["Routereverse"]:
            pts = reverse_points([(p["x"], p["y"], p.get("speed")) for p in route], loop)
            route = [dict({"x": x, "y": y}, **({} if sp is None else {"speed": sp})) for x, y, sp in pts]
    if len(route) < 2:
        return route, loop, "ルートの点が 2 点未満です"
    fmin, fmax = vals["Fencemin"], vals["Fencemax"]
    out = [i for i, p in enumerate(route)
           if not (fmin[0] <= p["x"] <= fmax[0] and fmin[1] <= p["y"] <= fmax[1])]
    if out:
        return route, loop, f"ルートの {len(out)} 点がジオフェンスの外です（送信しません）"
    size = len(json.dumps(route, separators=(",", ":")))
    if size > MAX_CONFIG_BYTES - 1200:   # 1200 はルート以外の設定のぶん
        return route, loop, (f"ルートの点が多すぎて送れません（{len(route)} 点, {size} バイト）。"
                             f"Route > 点の間隔 を広げてください")
    sm = float(vals["Speedmax"])
    fast = [p for p in route if p.get("speed") is not None and not 0 <= p["speed"] <= sm]
    if fast:
        return route, loop, f"speed が 0〜{sm} の範囲外の点が {len(fast)} 個あります（送信しません）"
    return route, loop, ""


def route_length(route, loop):
    pts = route + route[:1] if loop and route else route
    return sum(math.hypot(b["x"] - a["x"], b["y"] - a["y"]) for a, b in zip(pts[:-1], pts[1:]))


def read_route_table(dat):
    """表 DAT（1 行目が x, y, speed の見出し）→ [{"x", "y", "speed"?}, ...]。空行・不正な行は飛ばす。"""
    if dat is None:
        return []
    head = [dat[0, c].val.strip().lower() for c in range(dat.numCols)] if dat.numRows else []
    col = {name: head.index(name) for name in ("x", "y", "speed") if name in head}
    if "x" not in col or "y" not in col:
        return []
    pts = []
    for r in range(1, dat.numRows):
        try:
            p = {"x": float(dat[r, col["x"]].val), "y": float(dat[r, col["y"]].val)}
        except (ValueError, TypeError):
            continue
        if "speed" in col:
            sp = dat[r, col["speed"]].val.strip()
            if sp:
                try:
                    p["speed"] = float(sp)
                except ValueError:
                    pass
        pts.append(p)
    return pts


def write_route_table(dat, points):
    dat.clear()
    dat.appendRow(["x", "y", "speed"])
    for p in points:
        sp = p.get("speed")
        dat.appendRow([f"{float(p['x']):g}", f"{float(p['y']):g}", "" if sp is None else f"{float(sp):g}"])


# ---------------------------------------------------------------- OSC（最小限の実装）

def _pad(b):
    return b + b"\0" * (4 - len(b) % 4)


def osc_message(address, args):
    tags, data = ",", b""
    for a in args:
        if isinstance(a, (bool, np.bool_)):
            a = int(a)
        if isinstance(a, (int, np.integer)):
            tags += "i"
            data += struct.pack(">i", int(a))
        elif isinstance(a, (float, np.floating)):
            tags += "f"
            data += struct.pack(">f", float(a))
        else:
            tags += "s"
            data += _pad(str(a).encode("utf-8"))
    return _pad(address.encode()) + _pad(tags.encode()) + data


def parse_osc(data):
    """[(address, [args...]), ...] を返す（バンドルは展開する）。不正なら []。"""
    def rd_str(i):
        j = data.index(b"\0", i)
        return data[i:j].decode("utf-8", "replace"), (j // 4 + 1) * 4

    try:
        if data.startswith(b"#bundle\0"):
            out, i = [], 16
            while i + 4 <= len(data):
                n = struct.unpack(">i", data[i:i + 4])[0]
                out += parse_osc(data[i + 4:i + 4 + n])
                i += 4 + n
            return out
        addr, i = rd_str(0)
        tags, i = rd_str(i)
        args = []
        for t in tags[1:]:
            if t == "i":
                args.append(struct.unpack(">i", data[i:i + 4])[0]); i += 4
            elif t == "f":
                args.append(struct.unpack(">f", data[i:i + 4])[0]); i += 4
            elif t == "d":
                args.append(struct.unpack(">d", data[i:i + 8])[0]); i += 8
            elif t == "h":
                args.append(struct.unpack(">q", data[i:i + 8])[0]); i += 8
            elif t == "s":
                s, i = rd_str(i); args.append(s)
            elif t in "TF":
                args.append(t == "T")
            else:
                break
        return [(addr, args)]
    except (ValueError, struct.error, IndexError):
        return []


# ---------------------------------------------------------------- 状態
# モジュールは DAT の編集で読み直されるので、ソケットとプロセスは sys に置いて引き継ぐ

def _initial_state():
    return {
        "send_sock": None, "recv_sock": None, "recv_port": None,
        "proc": None, "proc_backend": None, "quit_at": None, "log": None,
        "tracker": None, "tracker_dir": None,
        "last_detect": 0.0, "last_config": 0.0, "config_dirty": True, "seq": 0,
        "estop_until": 0.0, "last_status": 0.0, "last_route": 0.0,
        "status": {}, "remote_pose": None, "target": None, "cmd": None, "ack": None, "ack_seq": -1,
        "pose": None, "hist": deque(maxlen=120), "trail": deque(maxlen=400),
        "gray": None, "images": {}, "last_render": 0.0,
        "route_cache": None, "loop_cache": None, "route_warn": "",
        "active_route": None, "sent_routes": {},
        "send_error": None, "launched_at": 0.0,
    }


STATE_VERSION = 3   # 状態の項目を増やしたら上げる


def _state():
    st = getattr(sys, "_go2ctl_state", None)
    if st is None:
        st = sys._go2ctl_state = _initial_state()
    if st.get("_version") != STATE_VERSION:
        # go2ctl.py を更新して読み直したとき: ソケットや起動中のプロセスは引き継ぎ、新しい項目だけ足す
        for k, v in _initial_state().items():
            st.setdefault(k, v)
        st["_version"] = STATE_VERSION
        st["config_dirty"] = True
    return st


def _sock(st):
    if st["send_sock"] is None:
        st["send_sock"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    return st["send_sock"]


def send(comp, address, args, vals=None):
    """送れたら True。失敗は黙って捨てず、Status に出す。"""
    vals = vals or read_vals(comp)
    st = _state()
    try:
        _sock(st).sendto(osc_message(address, args), (go2pf_host(vals), int(vals["Port"])))
        return True
    except OSError as e:
        st["send_error"] = (time.monotonic(), f"{address} を送れません: {e}")
        return False


def _ensure_recv(st, port):
    if st["recv_port"] == port and st["recv_sock"] is not None:
        return
    if st["recv_sock"] is not None:
        st["recv_sock"].close()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        s.setblocking(False)
    except OSError:
        s.close()
        st["recv_sock"], st["recv_port"] = None, None
        raise
    st["recv_sock"], st["recv_port"] = s, port


def _poll_status(st):
    s = st["recv_sock"]
    while True:
        try:
            data, _ = s.recvfrom(65536)
        except (BlockingIOError, OSError):
            return
        for addr, args in parse_osc(data):
            if addr == "/go2pf/status" and len(args) >= 6:
                st["status"] = {"state": args[0], "reason": args[1], "s": args[2],
                                "length": args[3], "laps": args[4], "pose_age": args[5]}
                st["last_status"] = time.monotonic()
                if args[0] not in ("RUNNING", "FINISHING", "HOLD"):
                    st["target"] = None
            elif addr == "/go2pf/pose" and len(args) >= 4:
                st["remote_pose"] = tuple(args[:4])
            elif addr == "/go2pf/target" and len(args) >= 2:
                st["target"] = tuple(args[:2])
            elif addr == "/go2pf/cmd" and len(args) >= 4:
                st["cmd"] = tuple(args[:4])
            elif addr == "/go2pf/ack" and len(args) >= 3:
                # 設定への応答（seq >= 0）だけを表示する。古い seq の応答は無視
                if args[0] >= 0 and args[0] >= st["ack_seq"]:
                    st["ack_seq"] = args[0]
                    st["ack"] = (bool(args[1]), str(args[2]))
                    if args[1] and args[0] in st["sent_routes"]:
                        st["active_route"] = st["sent_routes"][args[0]]  # go2pf が使っているルート
                    for k in [k for k in st["sent_routes"] if k <= args[0]]:
                        del st["sent_routes"][k]


# ---------------------------------------------------------------- 検出

def _tracker(st, vals):
    tddir = os.path.join(str(vals["Projectdir"]), "touchdesigner")
    if st["tracker"] is None or st["tracker_dir"] != tddir:
        if tddir not in sys.path:
            sys.path.insert(0, tddir)
        import aruco_pose
        st["tracker"] = aruco_pose.Tracker(os.path.join(str(vals["Projectdir"]), "homography.json"))
        st["tracker_dir"] = tddir
    return st["tracker"]


def to_gray(img):
    """TOP.numpyArray() の配列（下の行が先頭、RGBA）→ 通常の向きの uint8 グレースケール"""
    a = img[::-1]
    if a.ndim == 3:
        a = a[..., :3].mean(axis=2) if a.shape[2] >= 3 else a[..., 0]
    if a.dtype != np.uint8:
        a = (a * 255.0).clip(0, 255).astype(np.uint8)
    return np.ascontiguousarray(a)


def detect(comp, st, vals, now):
    top = vals["Cameratop"]
    if top is None:
        return
    try:
        img = top.numpyArray(delayed=True)
    except Exception:
        return
    if img is None or img.size == 0:
        return
    gray = to_gray(img)
    tr = _tracker(st, vals)
    x, y, yaw, found = tr.process(gray, detect_settings(vals))
    send(comp, "/dog/pose", [float(x), float(y), float(yaw), int(found)], vals)
    st["gray"] = gray
    st["pose"] = (x, y, yaw, found)
    st["hist"].append((now, x, y, yaw, found))
    if found:
        st["trail"].append((x, y))


def tracking_stats(hist, now, window=1.0):
    """直近 window 秒の 検出率, 位置の標準偏差 [m], 向きの標準偏差 [rad]"""
    rows = [h for h in hist if now - h[0] <= window]
    if not rows:
        return 0.0, float("nan"), float("nan")
    found = [h for h in rows if h[4]]
    rate = len(found) / len(rows)
    if len(found) < 3:
        return rate, float("nan"), float("nan")
    xy = np.array([(h[1], h[2]) for h in found])
    pos_std = float(np.sqrt(xy.var(axis=0).sum()))
    yaws = np.array([h[3] for h in found])
    c, s = np.cos(yaws).mean(), np.sin(yaws).mean()
    yaw_std = float(math.sqrt(max(0.0, -2.0 * math.log(max(1e-12, math.hypot(c, s))))))
    return rate, pos_std, yaw_std


# ---------------------------------------------------------------- プロセス

def go2pf_host(vals):
    """OSC の送り先。ssh 先があればそのホスト、なければ Host。"""
    target = str(vals.get("Sshtarget", "")).strip()
    if target:
        return target.split("@")[-1].split(":")[0]
    return str(vals["Host"])


def go2pf_args(vals):
    args = ["--backend", str(vals["Backend"]), "--no-viz", "--config", str(vals["Configfile"]),
            "--port", str(int(vals["Port"])), "--status-port", str(int(vals["Statusport"])),
            "--controller-timeout", "3"]
    if vals["Backend"] == "go2":
        args += ["--interface", str(vals["Go2interface"])]
    return args


def launch_command(vals):
    """(コマンド, shell を使うか)。Launchcmd > Sshtarget > この PC の .venv の順。"""
    root = str(vals["Projectdir"])
    args = {"backend": vals["Backend"], "port": int(vals["Port"]), "statusport": int(vals["Statusport"]),
            "interface": vals["Go2interface"], "config": vals["Configfile"], "root": root}
    if str(vals["Launchcmd"]).strip():
        return str(vals["Launchcmd"]).format(**args), True
    target = str(vals.get("Sshtarget", "")).strip()
    if target:
        rdir = str(vals.get("Remotedir", "Go2PathFollower")).strip() or "Go2PathFollower"
        cd = "cd ~/" + shlex.quote(rdir[2:]) if rdir.startswith("~/") else "cd " + shlex.quote(rdir)
        remote = cd + " && exec .venv/bin/python -m go2pf " + " ".join(shlex.quote(a) for a in go2pf_args(vals))
        # BatchMode: パスワードを聞かれたら待たずに失敗する（TD からは入力できないので、鍵認証にしておく）
        return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2",
                "-o", "ServerAliveCountMax=3", target, remote], False
    py = str(vals["Python"]).strip()
    if not py:
        py = os.path.join(root, ".venv", "Scripts", "python.exe") if os.name == "nt" \
            else os.path.join(root, ".venv", "bin", "python")
    return [py, "-m", "go2pf"] + go2pf_args(vals), False


def proc_alive(st):
    return st["proc"] is not None and st["proc"].poll() is None


def launch(comp):
    st, vals = _state(), read_vals(comp)
    if proc_alive(st):
        return "すでに起動しています"
    root = str(vals["Projectdir"])
    cmd, shell = launch_command(vals)
    os.makedirs(os.path.join(root, "logs"), exist_ok=True)
    if st["log"]:
        st["log"].close()
    st["log"] = open(os.path.join(root, "logs", "go2pf_td.log"), "a")
    st["log"].write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} {cmd}\n")
    st["log"].flush()
    try:
        st["proc"] = subprocess.Popen(cmd, cwd=root, shell=shell, stdin=subprocess.DEVNULL,
                                      stdout=st["log"], stderr=subprocess.STDOUT)
    except OSError as e:
        st["proc"] = None
        _set(comp, "Procstate", f"起動失敗: {e}")
        return f"起動失敗: {e}"
    st["proc_backend"] = vals["Backend"]
    st["launched_at"] = time.monotonic()
    st["quit_at"] = None
    st["status"], st["ack"], st["ack_seq"] = {}, None, -1
    st["seq"] = 0
    st["active_route"], st["sent_routes"] = None, {}
    st["config_dirty"] = True
    return "起動しました"


def shutdown(comp):
    st = _state()
    send(comp, "/go2pf/estop", [])
    send(comp, "/go2pf/quit", [])
    st["quit_at"] = time.monotonic()


def _reap(st, now):
    """終了要求のあと、終わらないプロセスを止める。"""
    p = st["proc"]
    if p is None or st["quit_at"] is None or p.poll() is not None:
        return
    if now - st["quit_at"] > 6.0:
        p.kill()
    elif now - st["quit_at"] > 3.0:
        p.terminate()


# ---------------------------------------------------------------- 操作

def arm(comp):
    st = _state()
    send_config(comp, st, read_vals(comp))  # 最新の設定を先に送る
    send(comp, "/go2pf/arm", [])


def estop(comp):
    st = _state()
    for _ in range(3):
        send(comp, "/go2pf/estop", [])
    st["estop_until"] = time.monotonic() + 1.5


def refresh_route(comp, st, vals):
    """ルートの元（表 / SOP）を読み直し、変わっていれば送信予約する。SOP のときは表にも書き出す。"""
    route, loop, warn = current_route(comp, vals)
    if route != st["route_cache"] or loop != st["loop_cache"] or warn != st["route_warn"]:
        st["route_cache"], st["loop_cache"], st["route_warn"] = route, loop, warn
        st["config_dirty"] = True
        if vals.get("Routesource") == "sop" and comp.op("route") is not None:
            write_route_table(comp.op("route"), route)


def send_config(comp, st, vals):
    if st["route_cache"] is None:
        refresh_route(comp, st, vals)
    ok = not st["route_warn"]
    route = st["route_cache"] if ok else None   # 不正なルートは送らない（go2pf は今のルートのまま）
    st["seq"] += 1
    text = json.dumps(go2pf_config(vals, route, st["loop_cache"] if ok else None),
                      ensure_ascii=False, separators=(",", ":"))
    if ok:
        st["sent_routes"][st["seq"]] = (route, st["loop_cache"])
        while len(st["sent_routes"]) > 50:
            del st["sent_routes"][min(st["sent_routes"])]
    send(comp, "/go2pf/config", [st["seq"], text], vals)
    st["last_config"] = time.monotonic()
    st["config_dirty"] = False


def load_route(comp):
    vals = read_vals(comp)
    path = _abspath(vals, vals["Routefile"])
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    write_route_table(comp.op("route"), data["points"] if isinstance(data, dict) else data)
    _state()["config_dirty"] = True


def save_route(comp):
    vals = read_vals(comp)
    path = _abspath(vals, vals["Routefile"])
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"points": read_route_table(comp.op("route"))}, f, ensure_ascii=False, indent=1)


def _abspath(vals, p):
    p = str(p)
    return p if os.path.isabs(p) else os.path.join(str(vals["Projectdir"]), p)


def _set(comp, name, value):
    par = getattr(comp.par, name, None)
    if par is not None and par.eval() != value:
        par.val = value


# ---------------------------------------------------------------- コールバックから呼ばれる入口

def on_frame(comp):
    st, now = _state(), time.monotonic()
    vals = read_vals(comp)
    try:
        _ensure_recv(st, int(vals["Statusport"]))
        _poll_status(st)
    except OSError as e:
        _set(comp, "Procstate", f"状態ポート {vals['Statusport']} を開けません: {e}")

    if vals["Detect"] and now - st["last_detect"] >= 1.0 / max(1.0, float(vals["Sendhz"])) - 0.002:
        st["last_detect"] = now
        detect(comp, st, vals, now)

    if now - st.get("last_route", 0.0) >= 1.0 / 15.0:   # 表 / SOP の変化を見る（約 15 Hz）
        st["last_route"] = now
        refresh_route(comp, st, vals)
    since = now - st["last_config"]
    if (st["config_dirty"] and since >= 0.1) or since > 1.0:   # 変更時は最大 10 Hz、ふだんは 1 秒ごと
        send_config(comp, st, vals)

    if now < st["estop_until"] and st["status"].get("state") != "ESTOP":
        send(comp, "/go2pf/estop", [], vals)

    _reap(st, now)
    update_status_pars(comp, st, vals, now)

    if now - st["last_render"] >= 1.0 / 15.0:
        st["last_render"] = now
        st["images"]["map"] = render_map(st, vals)
        st["images"]["overlay"] = render_overlay(st, vals)
        for name in ("map", "overlay"):
            top = comp.op(name)
            if top is not None:
                top.cook(force=True)


def on_pulse(comp, name):
    if name == "Launch":
        _set(comp, "Procstate", launch(comp))
    elif name == "Shutdown":
        shutdown(comp)
    elif name == "Arm":
        arm(comp)
    elif name == "Estop":
        estop(comp)
    elif name == "Routeload":
        load_route(comp)
    elif name == "Routesave":
        save_route(comp)
    elif name == "Recalibrate":
        st = _state()
        if st["tracker"] is not None:
            st["tracker"].reset_calibration()


def on_value_change(comp, name):
    if name not in STATUS_NAMES:
        _state()["config_dirty"] = True


def on_key(comp, key):
    if str(key).lower() in ("space", " "):
        estop(comp)


def on_start(comp):
    if read_vals(comp)["Autolaunch"]:
        launch(comp)


def on_exit(comp):
    """TD を閉じるとき: 止めてから go2pf を終了させる。"""
    estop(comp)
    shutdown(comp)


def get_image(name):
    return _state()["images"].get(name)


# ---------------------------------------------------------------- 状態表示

def _route_key(r):
    return None if r is None else (tuple((p["x"], p["y"], p.get("speed")) for p in r[0]), bool(r[1]))


def update_status_pars(comp, st, vals, now):
    p = st["proc"]
    if p is None:
        proc = "未起動" if not st["status"] else "外部で起動中"
    elif p.poll() is None:
        where = f", ssh {vals['Sshtarget']}" if str(vals.get("Sshtarget", "")).strip() else ""
        proc = f"起動中 pid {p.pid}（{st['proc_backend']}{where}）"
    elif p.returncode == 255 and str(vals.get("Sshtarget", "")).strip():
        proc = "ssh に失敗（IP・ユーザー名・鍵認証を確認）  logs/go2pf_td.log を参照"
    else:
        proc = f"終了（code {p.returncode}）  logs/go2pf_td.log を参照"
    _set(comp, "Procstate", proc)

    alive = now - st["last_status"] < 1.0
    status = st["status"]
    _set(comp, "State", status.get("state", "-") if alive else "応答なし")
    _set(comp, "Reason", status.get("reason", "") if alive else "go2pf からの状態が届いていません")
    ack = st["ack"]
    err = st["send_error"]
    if err is not None and now - err[0] < 3.0:
        msg = err[1]
    elif ack is None:
        msg = "-"
        if proc_alive(st) and now - st["launched_at"] > 2.0:
            msg = "go2pf から設定の応答がありません（届いていない可能性。Connect の IP / ポートを確認）"
    else:
        msg = "OK" if ack[0] else ack[1]
    _set(comp, "Configack", msg)
    route = st["route_cache"] or []
    src = "SOP" if vals.get("Routesource") == "sop" else "表"
    info = f"{src}: {len(route)} 点, {route_length(route, st['loop_cache']):.2f} m" + \
        ("、ループ" if st["loop_cache"] else "")
    if st["route_warn"]:
        info += f"  ⚠ {st['route_warn']}"
    elif alive and _route_key(st["active_route"]) != _route_key((route, st["loop_cache"])):
        info += "  （保留中: 停止すると反映）" if status.get("state") in ("RUNNING", "FINISHING", "HOLD") \
            else "  （送信中）"
    _set(comp, "Routeinfo", info)
    tr = st["tracker"]
    if not vals["Detect"]:
        _set(comp, "Calib", "検出 Off")
    elif tr is None:
        _set(comp, "Calib", "カメラ画像なし")
    else:
        refs_seen = [i for i in range(4) if i in tr.markers]
        _set(comp, "Calib", ("OK" if tr.H is not None else "未完了") + f"  基準 {refs_seen} が見えている")
    rate, pstd, ystd = tracking_stats(st["hist"], now)
    _set(comp, "Tracking", f"検出率 {rate * 100:.0f}%  揺れ 位置 {pstd * 100:.1f} cm / 向き {math.degrees(ystd):.1f}°"
         if math.isfinite(pstd) else f"検出率 {rate * 100:.0f}%")
    pose = st["pose"]
    _set(comp, "Posetxt", "-" if pose is None else
         f"x {pose[0]:+.2f}  y {pose[1]:+.2f}  yaw {math.degrees(pose[2]):+.0f}°  found {pose[3]}")
    cmd = st["cmd"]
    _set(comp, "Cmdtxt", "-" if cmd is None or not alive else
         f"vx {cmd[0]:+.2f}  vy {cmd[1]:+.2f}  vyaw {cmd[2]:+.2f}")


# ---------------------------------------------------------------- 描画（Script TOP 用。RGBA uint8、下の行が先頭）

STATE_COLORS = {  # BGR
    "DISARMED": (160, 160, 160), "RUNNING": (80, 200, 80), "HOLD": (0, 165, 255),
    "FINISHING": (230, 160, 60), "FINISHED": (230, 160, 60), "ESTOP": (60, 60, 255), "FAULT": (60, 60, 255),
}


def _to_td(bgr):
    rgba = np.dstack([bgr[..., 2], bgr[..., 1], bgr[..., 0], np.full(bgr.shape[:2], 255, np.uint8)])
    return np.ascontiguousarray(rgba[::-1])


def render_map(st, vals, size=720):
    import cv2
    fmin, fmax = vals["Fencemin"], vals["Fencemax"]
    refs = [vals[f"Ref{i}"] for i in range(4)]
    xs = [fmin[0], fmax[0]] + [r[0] for r in refs]
    ys = [fmin[1], fmax[1]] + [r[1] for r in refs]
    m = float(vals["Mapmargin"])
    x0, x1, y0, y1 = min(xs) - m, max(xs) + m, min(ys) - m, max(ys) + m
    scale = size / max(x1 - x0, y1 - y0, 0.5)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    def px(x, y):
        return int(round(size / 2 + (x - cx) * scale)), int(round(size / 2 - (y - cy) * scale))

    img = np.full((size, size, 3), 28, np.uint8)
    g = 0.5
    for gx in np.arange(math.floor(x0 / g) * g, x1 + g, g):
        cv2.line(img, px(gx, y0 - 9), px(gx, y1 + 9), (60, 60, 60) if abs(gx) > 1e-6 else (90, 90, 90), 1)
    for gy in np.arange(math.floor(y0 / g) * g, y1 + g, g):
        cv2.line(img, px(x0 - 9, gy), px(x1 + 9, gy), (60, 60, 60) if abs(gy) > 1e-6 else (90, 90, 90), 1)
    cv2.rectangle(img, px(fmin[0], fmax[1]), px(fmax[0], fmin[1]), (60, 60, 230), 2)
    for i, r in enumerate(refs):
        p = px(*r)
        cv2.rectangle(img, (p[0] - 6, p[1] - 6), (p[0] + 6, p[1] + 6), (200, 200, 200), 1)
        cv2.putText(img, str(i), (p[0] + 8, p[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (200, 200, 200), 1)
    tr = st["tracker"]
    if tr is not None and tr.nadir is not None:
        p = px(*tr.nadir)
        cv2.drawMarker(img, p, (200, 120, 200), cv2.MARKER_CROSS, 14, 1)

    alive = time.monotonic() - st["last_status"] < 1.0

    def draw_route(route, loop, color, width, labels):
        if len(route) < 2:
            return
        pts = [px(p["x"], p["y"]) for p in route]
        cv2.polylines(img, [np.array(pts, np.int32)], bool(loop), color, width, cv2.LINE_AA)
        if labels:
            dense = len(route) > 60
            for p, q in zip(route, pts):
                if not dense:
                    cv2.circle(img, q, 3, color, -1)
                if p.get("speed") is not None and not dense:
                    cv2.putText(img, f"{p['speed']:.2f}", (q[0] + 5, q[1] - 5), cv2.FONT_HERSHEY_SIMPLEX,
                                0.38, (150, 150, 150), 1)
            cv2.rectangle(img, (pts[0][0] - 6, pts[0][1] - 6), (pts[0][0] + 6, pts[0][1] + 6), (255, 255, 255), 2)
            a, b = np.array(pts[0], float), np.array(pts[1], float)   # 進む向き
            d = (b - a) / max(1e-6, np.linalg.norm(b - a))
            cv2.arrowedLine(img, tuple(a.astype(int)), tuple((a + d * 28).astype(int)), (255, 255, 255), 2,
                            cv2.LINE_AA, tipLength=0.4)

    # go2pf が使っているルート（灰）と、TD 側の新しいルート（保留中: 橙、不正: 赤）
    cur = (st.get("route_cache") or [], bool(st.get("loop_cache")))
    active = st.get("active_route") if alive else None
    pending = active is not None and _route_key(active) != _route_key(cur)
    if st.get("route_warn"):
        if active is not None:
            draw_route(active[0], active[1], (170, 170, 170), 2, True)
        draw_route(cur[0], cur[1], (60, 60, 255), 1, False)
    elif pending:
        draw_route(active[0], active[1], (170, 170, 170), 2, True)
        draw_route(cur[0], cur[1], (0, 165, 255), 1, False)
    else:
        draw_route(cur[0], cur[1], (170, 170, 170), 2, True)
    state = st["status"].get("state", "NO LINK") if alive else "NO LINK"
    col = STATE_COLORS.get(state, (200, 200, 200))
    if len(st["trail"]) >= 2:
        cv2.polylines(img, [np.array([px(*t) for t in st["trail"]], np.int32)], False, (70, 140, 70), 1)
    rp = st["remote_pose"] if alive else None
    pose = st["pose"]
    use_remote = vals["Backend"] == "mock" and rp is not None  # mock のときは go2pf のシミュレーション位置
    shown = rp if use_remote else pose
    if shown is not None:
        x, y, yaw, found = shown
        p = px(x, y)
        q = px(x - 0.35 * math.sin(yaw), y + 0.35 * math.cos(yaw))
        cv2.circle(img, p, 11, col if found else (110, 110, 110), -1, cv2.LINE_AA)
        cv2.line(img, p, q, col, 3, cv2.LINE_AA)
    if st["target"] is not None and alive and shown is not None:
        t = px(*st["target"])
        cv2.drawMarker(img, t, (220, 80, 220), cv2.MARKER_TILTED_CROSS, 14, 2)
        cv2.line(img, px(shown[0], shown[1]), t, (220, 80, 220), 1, cv2.LINE_AA)

    lines = [state]
    if shown is not None:
        lines.append(f"x {shown[0]:+.2f} y {shown[1]:+.2f} yaw {math.degrees(shown[2]):+.0f} found {int(shown[3])}"
                     + ("  (mock)" if use_remote else ""))
    if st["cmd"] is not None and alive:
        c = st["cmd"]
        lines.append(f"cmd vx {c[0]:+.2f} vy {c[1]:+.2f} vyaw {c[2]:+.2f}")
    s = st["status"]
    if alive and s.get("length"):
        lines.append(f"progress {s['s']:.2f} / {s['length']:.2f} m  laps {s['laps']}")
    rate, pstd, ystd = tracking_stats(st["hist"], time.monotonic())
    lines.append(f"detect {rate * 100:.0f}%" + (f"  jitter {pstd * 100:.1f}cm {math.degrees(ystd):.1f}deg"
                                                  if math.isfinite(pstd) else ""))
    lines.append("calib OK" if tr is not None and tr.H is not None else "calib: show 4 ref markers")
    src = "SOP" if vals.get("Routesource") == "sop" else "table"
    lines.append(f"route ({src}) {len(cur[0])} pts {route_length(cur[0], cur[1]):.2f} m"
                 + (" loop" if cur[1] else "")
                 + ("  INVALID (see Status)" if st.get("route_warn") else "  pending (stop to apply)" if pending else ""))
    cv2.rectangle(img, (0, 0), (430, 12 + 20 * len(lines)), (20, 20, 20), -1)
    for i, t in enumerate(lines):
        cv2.putText(img, t, (10, 22 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.55,
                    col if i == 0 else (230, 230, 230), 2 if i == 0 else 1, cv2.LINE_AA)
    cv2.putText(img, "space: E-STOP", (10, size - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (60, 60, 255), 1)
    return _to_td(img)


def render_overlay(st, vals, max_w=1280):
    """カメラ映像に、検出したマーカー・フェンス・ルート・推定位置（床面）を重ねる。

    赤い点（推定した床面上の位置）が機体の中心の真下に来ていれば、カメラとマーカーの高さの設定が合っている。
    """
    import cv2
    gray, tr = st["gray"], st["tracker"]
    if gray is None or tr is None:
        return None
    img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    k = min(1.0, max_w / img.shape[1])
    if k < 1.0:
        img = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    lw = 2

    for i, c in tr.markers.items():
        c = (c * k).astype(np.int32)
        color = (0, 220, 255) if i == tr.settings["robot_id"] else (0, 220, 0)
        cv2.polylines(img, [c], True, color, lw, cv2.LINE_AA)
        cv2.line(img, tuple(c[0]), tuple(c[1]), (0, 0, 255), lw, cv2.LINE_AA)  # 上辺（頭の向き）
        cv2.putText(img, str(i), tuple(c[0] + [4, -6]), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

    if tr.H is not None:
        def ip(pts):
            return (tr.room_to_image(np.asarray(pts, np.float64)) * k).astype(np.int32)

        fmin, fmax = vals["Fencemin"], vals["Fencemax"]
        cv2.polylines(img, [ip([(fmin[0], fmin[1]), (fmax[0], fmin[1]), (fmax[0], fmax[1]), (fmin[0], fmax[1])])],
                      True, (60, 60, 230), lw, cv2.LINE_AA)
        route = st.get("route_cache") or []
        if len(route) >= 2:
            cv2.polylines(img, [ip([(p["x"], p["y"]) for p in route])], bool(st.get("loop_cache")),
                          (220, 220, 220), lw, cv2.LINE_AA)
        # 床に 1 m の格子
        fx = np.arange(math.ceil(fmin[0]), math.floor(fmax[0]) + 1)
        fy = np.arange(math.ceil(fmin[1]), math.floor(fmax[1]) + 1)
        for x in fx:
            cv2.polylines(img, [ip([(x, fmin[1]), (x, fmax[1])])], False, (90, 90, 90), 1)
        for y in fy:
            cv2.polylines(img, [ip([(fmin[0], y), (fmax[0], y)])], False, (90, 90, 90), 1)
        if tr.nadir is not None:
            cv2.drawMarker(img, tuple(ip([tr.nadir])[0]), (200, 120, 200), cv2.MARKER_CROSS, 24, 2)
        pose = st["pose"]
        if pose is not None and pose[3]:
            x, y, yaw = pose[:3]
            p, q = ip([(x, y), (x - 0.3 * math.sin(yaw), y + 0.3 * math.cos(yaw))])
            cv2.circle(img, tuple(p), 8, (0, 0, 255), -1, cv2.LINE_AA)
            cv2.arrowedLine(img, tuple(p), tuple(q), (0, 0, 255), 3, cv2.LINE_AA, tipLength=0.3)
        cv2.putText(img, "red dot = estimated floor point (should be under the robot center)",
                    (10, img.shape[0] - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 255), 1, cv2.LINE_AA)
    else:
        cv2.putText(img, "NOT CALIBRATED: show reference markers 0-3", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2, cv2.LINE_AA)
    return _to_td(img)
