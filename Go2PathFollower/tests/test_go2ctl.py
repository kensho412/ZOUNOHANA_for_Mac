"""touchdesigner/go2ctl.py を TouchDesigner なしで確かめる。

TD の COMP・パラメータ・表 DAT・TOP を最小限まねた偽物を使い、コールバックと同じ順で呼ぶ。
E2E テストでは本物の go2pf を起動し、OSC だけでやりとりする。
"""
import json
import math
import os
import socket
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "touchdesigner"))

try:
    import cv2  # noqa: F401
    import numpy as np
    import go2ctl
except ImportError:  # pragma: no cover
    go2ctl = None


# ---------------------------------------------------------------- TD の偽物

class FakePar:
    def __init__(self, name, val):
        self.name, self.val = name, val
        self.readOnly = False

    def eval(self):
        return self.val


class FakeParNS:
    pass


class FakeCell:
    def __init__(self, val):
        self.val = val


class FakeTable:
    def __init__(self):
        self.rows = []

    @property
    def numRows(self):
        return len(self.rows)

    @property
    def numCols(self):
        return max((len(r) for r in self.rows), default=0)

    def __getitem__(self, rc):
        r, c = rc
        return FakeCell(self.rows[r][c] if c < len(self.rows[r]) else "")

    def clear(self):
        self.rows = []

    def appendRow(self, row):
        self.rows.append([str(v) for v in row])


class FakeTOP:
    def __init__(self, image_fn=None):
        self.image_fn = image_fn
        self.cooks = 0

    def numpyArray(self, delayed=False):
        gray = self.image_fn()
        rgba = np.dstack([gray, gray, gray, np.full_like(gray, 255)]).astype(np.float32) / 255.0
        return rgba[::-1]  # TD は下の行が先頭

    def cook(self, force=False):
        self.cooks += 1


class FakePoint:
    def __init__(self, index, x, y, z=0.0, speed=None):
        self.index, self.x, self.y, self.z = index, x, y, z
        if speed is not None:
            self.speed = (speed,)


class FakeVertex:
    def __init__(self, point):
        self.point = point


class FakePrim(list):
    def __init__(self, points, closed):
        super().__init__(FakeVertex(p) for p in points)
        self.closed = closed


class FakeSOP:
    """TD の SOP の代わり。points と、頂点順を持つ prims[0]。"""

    def __init__(self, xyz, closed=True, speeds=None, with_prim=True):
        self.set(xyz, closed, speeds, with_prim)

    def set(self, xyz, closed=True, speeds=None, with_prim=True):
        speeds = speeds or [None] * len(xyz)
        self.points = [FakePoint(i, *p, speed=sp) for i, (p, sp) in enumerate(zip(xyz, speeds))]
        self.prims = [FakePrim(self.points, closed)] if with_prim else []


def circle(r, n=40, cx=0.0, cy=0.0):
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n), 0.0)
            for i in range(n)]


class FakeComp:
    def __init__(self, image_fn, **overrides):
        self.par = FakeParNS()
        self.children = {"route": FakeTable(), "map": FakeTOP(), "overlay": FakeTOP(), "cam": FakeTOP(image_fn)}
        for pg, name, label, kind, default, lo, hi, key in go2ctl.PARAMS:
            if kind == "xy":
                setattr(self.par, name + "x", FakePar(name + "x", default[0]))
                setattr(self.par, name + "y", FakePar(name + "y", default[1]))
            elif kind == "top":
                setattr(self.par, name, FakePar(name, self.children["cam"]))
            else:
                setattr(self.par, name, FakePar(name, default))
        for k, v in overrides.items():
            getattr(self.par, k).val = v

    def op(self, name):
        return self.children.get(name)

    def pulse(self, name):
        go2ctl.on_pulse(self, name)

    def set(self, name, val):
        getattr(self.par, name).val = val
        go2ctl.on_value_change(self, name)

    def get(self, name):
        return getattr(self.par, name).eval()


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def reset_state():
    st = getattr(sys, "_go2ctl_state", None)
    if st:
        for k in ("send_sock", "recv_sock"):
            if st.get(k):
                st[k].close()
        p = st.get("proc")
        if p is not None and p.poll() is None:
            p.kill()
            p.wait()
        if st.get("log"):
            st["log"].close()
        del sys._go2ctl_state


@unittest.skipIf(go2ctl is None, "OpenCV / numpy がない")
class PureFunctionTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        self.comp = FakeComp(lambda: np.zeros((10, 10), np.uint8), Projectdir=ROOT)

    def test_config_is_accepted_by_go2pf(self):
        from go2pf.config import Config, merge_remote
        vals = go2ctl.read_vals(self.comp)
        cfg = go2ctl.go2pf_config(vals, [{"x": 0, "y": 0}, {"x": 0, "y": 1}])
        route = cfg.pop("route")
        merged = merge_remote(Config(), cfg)
        self.assertEqual(merged.control.lookahead, 0.5)
        self.assertEqual(merged.safety.geofence.x_max, 2.3)
        self.assertEqual(len(route), 2)

    def test_loop_only_sent_with_route(self):
        vals = go2ctl.read_vals(self.comp)
        self.assertNotIn("loop", go2ctl.go2pf_config(vals, None, True)["control"])
        self.assertTrue(go2ctl.go2pf_config(vals, [{"x": 0, "y": 0}], True)["control"]["loop"])

    def test_every_go2pf_key_exists(self):
        from go2pf.config import Config
        import dataclasses
        c = Config()
        for pg, name, label, kind, default, lo, hi, key in go2ctl.PARAMS:
            if key:
                section, field = key.split(".")
                self.assertIn(field, {f.name for f in dataclasses.fields(getattr(c, section))}, key)

    def test_detect_settings(self):
        self.comp.par.Yawoffset.val = 90.0
        self.comp.par.Nadirauto.val = False
        s = go2ctl.detect_settings(go2ctl.read_vals(self.comp))
        self.assertAlmostEqual(s["yaw_offset"], math.pi / 2)
        self.assertEqual(s["nadir"], (0.0, 0.0))
        self.assertEqual(s["refs"][2], (2.0, 2.0))

    def test_route_table_roundtrip(self):
        t = self.comp.op("route")
        pts = [{"x": -1.5, "y": 0.0}, {"x": 1.0, "y": 0.5, "speed": 0.2}]
        go2ctl.write_route_table(t, pts)
        self.assertEqual(go2ctl.read_route_table(t), pts)
        t.appendRow(["", "", ""])           # 空行は飛ばす
        t.appendRow(["abc", "1", ""])        # 不正な行も飛ばす
        self.assertEqual(go2ctl.read_route_table(t), pts)

    def test_route_load_save(self):
        with tempfile.TemporaryDirectory() as d:
            self.comp.par.Routefile.val = os.path.join(ROOT, "routes", "example_route.json")
            self.comp.pulse("Routeload")
            self.assertEqual(len(go2ctl.read_route_table(self.comp.op("route"))), 6)
            out = os.path.join(d, "r.json")
            self.comp.par.Routefile.val = out
            self.comp.pulse("Routesave")
            with open(out) as f:
                self.assertEqual(len(json.load(f)["points"]), 6)

    def test_osc_roundtrip_with_python_osc(self):
        from pythonosc.osc_message import OscMessage
        from pythonosc.osc_message_builder import OscMessageBuilder
        raw = go2ctl.osc_message("/go2pf/config", [3, "日本語 {\"a\": 1}", 0.5, np.float64(1.25), True])
        m = OscMessage(raw)
        self.assertEqual(m.address, "/go2pf/config")
        self.assertEqual(m.params[:2], [3, "日本語 {\"a\": 1}"])
        b = OscMessageBuilder("/go2pf/status")
        for v in ["RUNNING", "理由", 1.5, 9.0, 2, 0.03]:
            b.add_arg(v)
        (addr, args), = go2ctl.parse_osc(b.build().dgram)
        self.assertEqual(addr, "/go2pf/status")
        self.assertEqual(args[:2], ["RUNNING", "理由"])
        self.assertAlmostEqual(args[2], 1.5)
        self.assertEqual(args[4], 2)

    def test_sop_closed_circle_is_loop(self):
        route, loop = go2ctl.sop_route(FakeSOP(circle(1.0, 24)))
        self.assertTrue(loop)
        self.assertEqual(len(route), 24)
        self.assertAlmostEqual(route[0]["x"], 1.0)

    def test_sop_open_line_and_loop_fallback(self):
        line = [(0, -1, 0), (0, 0, 0), (0, 1, 0)]
        route, loop = go2ctl.sop_route(FakeSOP(line, closed=False))
        self.assertFalse(loop)
        sop = FakeSOP(line, with_prim=False)              # 閉じているか分からない → Loop パラメータに従う
        self.assertTrue(go2ctl.sop_route(sop, loop=True)[1])
        self.assertFalse(go2ctl.sop_route(sop, loop=False)[1])

    def test_sop_repeated_first_point_means_closed(self):
        pts = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 0, 0)]
        route, loop = go2ctl.sop_route(FakeSOP(pts, with_prim=False))
        self.assertTrue(loop)
        self.assertEqual(len(route), 3)

    def test_sop_planes(self):
        sop = FakeSOP([(1, 5, 2), (3, 5, 4)], closed=False)
        self.assertEqual(go2ctl.sop_route(sop, "xz")[0], [{"x": 1, "y": 2}, {"x": 3, "y": 4}])
        self.assertEqual(go2ctl.sop_route(sop, "xzneg")[0], [{"x": 1, "y": -2}, {"x": 3, "y": -4}])

    def test_sop_speed_attribute_and_reverse(self):
        sop = FakeSOP([(0, 0, 0), (0, 1, 0), (0, 2, 0)], closed=False, speeds=[0.1, 0.2, 0.3])
        route, _ = go2ctl.sop_route(sop)
        self.assertEqual([p["speed"] for p in route], [0.1, 0.2, 0.3])
        rev, _ = go2ctl.sop_route(sop, reverse=True)
        # 逆向き: (0,2)→(0,1) は元の区間 (0,1)→(0,2) の速度 0.2
        self.assertEqual([(p["y"], p["speed"]) for p in rev[:2]], [(2, 0.2), (1, 0.1)])

    def test_sop_shift_and_spacing(self):
        sop = FakeSOP(circle(1.0, 8))
        r0, _ = go2ctl.sop_route(sop)
        r1, _ = go2ctl.sop_route(sop, shift=0.25)
        self.assertEqual(r1[0], r0[2])
        dense, _ = go2ctl.sop_route(FakeSOP([(0, 0, 0), (0, 1, 0)], closed=False), spacing=0.25)
        self.assertEqual([p["y"] for p in dense], [0, 0.25, 0.5, 0.75, 1.0])

    def test_sop_too_many_points_are_thinned(self):
        route, loop = go2ctl.sop_route(FakeSOP(circle(1.0, 2000)))
        self.assertLessEqual(len(route), go2ctl.MAX_ROUTE_POINTS)
        self.assertAlmostEqual(go2ctl.route_length(route, loop), 2 * math.pi, delta=0.02)

    def test_config_fits_in_one_udp_datagram(self):
        # macOS は UDP 1 通 9216 バイトまで。SOP の点が多くても収まるように間引かれる
        vals = go2ctl.read_vals(self.comp)
        for n in (48, 400, 5000):
            route, loop = go2ctl.sop_route(FakeSOP(circle(1.5, n)))
            raw = go2ctl.osc_message("/go2pf/config", [1, json.dumps(
                go2ctl.go2pf_config(vals, route, loop), ensure_ascii=False, separators=(",", ":"))])
            self.assertLess(len(raw), 8192, n)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.sendto(raw, ("127.0.0.1", free_port()))
            s.close()

    def test_too_long_table_route_is_refused(self):
        vals = go2ctl.read_vals(self.comp)
        go2ctl.write_route_table(self.comp.op("route"),
                                 [{"x": round(-2 + 4 * i / 999, 3), "y": 0.123} for i in range(1000)])
        vals["Routesource"] = "table"
        self.assertIn("多すぎ", go2ctl.current_route(self.comp, vals)[2])

    def test_send_error_is_shown(self):
        go2ctl.send(self.comp, "/x", ["a" * 20000])
        go2ctl.update_status_pars(self.comp, go2ctl._state(), go2ctl.read_vals(self.comp), time.monotonic())
        self.assertIn("送れません", self.comp.get("Configack"))

    def test_current_route_warnings(self):
        vals = go2ctl.read_vals(self.comp)
        vals["Routesource"], vals["Routesop"] = "sop", FakeSOP(circle(3.0))
        self.assertIn("ジオフェンス", go2ctl.current_route(self.comp, vals)[2])
        vals["Routesop"] = FakeSOP([(0, 0, 0), (0, 1, 0)], closed=False, speeds=[2.0, None])
        self.assertIn("speed", go2ctl.current_route(self.comp, vals)[2])
        vals["Routesop"] = None
        self.assertIn("見つかりません", go2ctl.current_route(self.comp, vals)[2])

    def test_old_state_is_upgraded(self):
        # 前のバージョンの go2ctl が作った状態（新しい項目がない）が残っていても動く
        st = go2ctl._initial_state()
        for k in ("sent_routes", "active_route", "route_cache", "loop_cache", "route_warn", "last_route"):
            del st[k]
        sys._go2ctl_state = st
        self.comp.par.Routesource.val = "table"
        go2ctl.on_frame(self.comp)
        self.assertIn("sent_routes", go2ctl._state())
        self.assertIs(go2ctl._state(), st)   # ソケットなどは同じものを引き継ぐ

    def test_launch_command(self):
        vals = go2ctl.read_vals(self.comp)
        vals["Backend"] = "go2"
        cmd, shell = go2ctl.launch_command(vals)
        self.assertFalse(shell)
        self.assertEqual(cmd[1:3], ["-m", "go2pf"])
        self.assertIn("--interface", cmd)
        vals["Launchcmd"] = "ssh robot 'cd ~/g && python -m go2pf --backend {backend} --port {port}'"
        cmd, shell = go2ctl.launch_command(vals)
        self.assertTrue(shell)
        self.assertIn("--backend go2 --port 9000", cmd)

    def test_ssh_launch_command(self):
        vals = go2ctl.read_vals(self.comp)
        vals.update(Backend="go2", Sshtarget="unitree@192.168.11.50", Remotedir="~/my dir")
        cmd, shell = go2ctl.launch_command(vals)
        self.assertFalse(shell)
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("BatchMode=yes", cmd)
        self.assertEqual(cmd[-2], "unitree@192.168.11.50")
        self.assertTrue(cmd[-1].startswith("cd ~/'my dir' && exec .venv/bin/python -m go2pf --backend go2"))
        self.assertIn("--interface eth0", cmd[-1])
        self.assertEqual(go2ctl.go2pf_host(vals), "192.168.11.50")
        vals["Sshtarget"] = ""
        self.assertEqual(go2ctl.go2pf_host(vals), "127.0.0.1")

    def test_tracking_stats(self):
        hist = [(i * 0.05, 1.0 + 0.01 * (i % 2), 0.0, 0.1, 1) for i in range(20)]
        rate, pstd, ystd = go2ctl.tracking_stats(hist, 1.0)
        self.assertEqual(rate, 1.0)
        self.assertAlmostEqual(pstd, 0.005, places=3)
        self.assertAlmostEqual(ystd, 0.0, places=3)


@unittest.skipIf(go2ctl is None, "OpenCV / numpy がない")
class FakeTouchDesignerE2E(unittest.TestCase):
    """TD の代わりに on_frame を回し、本物の go2pf を起動・操作する。"""

    def setUp(self):
        reset_state()
        from tests.test_aruco_pose import marker_corners_room, render, room_to_image
        import aruco_pose as ap
        G = room_to_image()
        refs = [(i, marker_corners_room(x, y, 0.0, 0.25)) for i, (x, y) in ap.REF_MARKERS.items()]
        self.robot = [-1.5, -1.3, 0.0]
        self.calib_tmp = tempfile.TemporaryDirectory()

        def image():
            x, y, yaw = self.robot
            return render(refs + [(10, marker_corners_room(x, y, yaw, 0.2))], G)

        self.port, self.sport = free_port(), free_port()
        self.comp = FakeComp(image, Projectdir=ROOT, Port=self.port, Statusport=self.sport,
                             Python=sys.executable, Configfile="config/default.yaml", Routesource="table")
        self.comp.par.Routefile.val = "routes/example_route.json"
        self.comp.pulse("Routeload")
        # キャリブレーションの保存先をテスト用にする
        go2ctl._state()["tracker"] = None
        self._orig_tracker = go2ctl._tracker

        def tracker(st, vals):
            if st["tracker"] is None:
                st["tracker"] = ap.Tracker(os.path.join(self.calib_tmp.name, "h.json"))
                st["tracker_dir"] = "test"
            return st["tracker"]
        go2ctl._tracker = tracker

    def tearDown(self):
        go2ctl._tracker = self._orig_tracker
        reset_state()
        self.calib_tmp.cleanup()

    def run_frames(self, seconds, until=None):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            go2ctl.on_frame(self.comp)
            if until and until():
                return True
            time.sleep(1 / 60)
        return False

    def test_dry_full_cycle(self):
        c = self.comp
        c.par.Backend.val = "dry"
        self.run_frames(0.5)
        self.assertTrue(c.get("Calib").startswith("OK"), c.get("Calib"))
        self.assertIn("found 1", c.get("Posetxt"))

        c.pulse("Launch")
        self.assertTrue(self.run_frames(10, lambda: c.get("State") == "DISARMED"), c.get("State"))
        self.assertTrue(self.run_frames(3, lambda: c.get("Configack") == "OK"), c.get("Configack"))
        self.assertIn("起動中", c.get("Procstate"))

        c.pulse("Arm")
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "RUNNING"), c.get("Reason"))
        self.assertTrue(self.run_frames(3, lambda: "vx +0.0" not in c.get("Cmdtxt")), c.get("Cmdtxt"))

        # 走行中に TD からゲインを変える → 反映される（ack OK のまま）
        c.set("Lookahead", 0.8)
        self.assertTrue(self.run_frames(2, lambda: c.get("Configack") == "OK"))

        # ルートを外れるような フェンス変更は拒否される
        c.par.Fencemaxy.val = 1.0
        go2ctl.on_value_change(c, "Fencemaxy")
        self.assertTrue(self.run_frames(2, lambda: "ジオフェンス" in c.get("Configack")), c.get("Configack"))
        c.par.Fencemaxy.val = 2.3
        go2ctl.on_value_change(c, "Fencemaxy")

        # マーカーを隠す → HOLD
        saved = self.robot[:]
        self.robot[:] = [9, 9, 0]  # 画面外
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "HOLD"), c.get("State"))
        self.robot[:] = saved
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "RUNNING"), c.get("State"))

        go2ctl.on_key(c, "space")
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "ESTOP"), c.get("State"))

        c.pulse("Shutdown")
        st = go2ctl._state()
        self.assertTrue(self.run_frames(8, lambda: st["proc"].poll() is not None))
        self.assertEqual(st["proc"].returncode, 0)
        self.assertIn("終了", c.get("Procstate"))

        # 描画できている（TD 向け RGBA uint8）
        for name in ("map", "overlay"):
            img = go2ctl.get_image(name)
            self.assertEqual(img.dtype, np.uint8)
            self.assertEqual(img.shape[2], 4)
        self._save_images("dry")

    def test_mock_robot_moves(self):
        c = self.comp
        c.par.Backend.val = "mock"
        c.pulse("Launch")
        self.assertTrue(self.run_frames(10, lambda: c.get("Configack") == "OK"), c.get("Configack"))
        c.pulse("Arm")
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "RUNNING"), c.get("Reason"))
        self.run_frames(4)
        st = go2ctl._state()
        # 始点 (-1.5, -1.5) から最初の区間（+y 方向）を進んでいること
        self.assertGreater(st["status"]["s"], 0.5)
        self.assertLess(st["status"]["s"], 2.5)
        x, y = st["remote_pose"][:2]
        self.assertLess(abs(x + 1.5), 0.15)
        self.assertGreater(y, -1.0)
        self.assertEqual(st["target"][0], -1.5)
        self._save_images("mock")
        c.pulse("Estop")
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "ESTOP"))
        c.pulse("Shutdown")
        self.assertTrue(self.run_frames(8, lambda: st["proc"].poll() is not None))

    def test_go2pf_exits_when_td_disappears(self):
        c = self.comp
        c.par.Backend.val = "mock"
        c.pulse("Launch")
        self.assertTrue(self.run_frames(10, lambda: c.get("Configack") == "OK"))
        p = go2ctl._state()["proc"]
        p.wait(timeout=8)   # on_frame を止める = TD が固まった・落ちた
        self.assertEqual(p.returncode, 0)

    def test_sop_route_realtime(self):
        c = self.comp
        sop = FakeSOP(circle(1.2))
        c.par.Routesop.val = sop
        c.par.Routesource.val = "sop"
        c.par.Backend.val = "mock"
        c.pulse("Launch")
        st = go2ctl._state()
        self.assertTrue(self.run_frames(10, lambda: c.get("Configack") == "OK"), c.get("Configack"))
        self.assertTrue(self.run_frames(2, lambda: st["status"].get("length")))
        self.assertAlmostEqual(st["status"]["length"], 2 * math.pi * 1.2, delta=0.05)
        self.assertTrue(st["active_route"][1])                       # 閉じた形 → ループ
        self.assertEqual(len(go2ctl.read_route_table(c.op("route"))), 40)   # 表にも書き出される
        self.assertIn("SOP: 40 点", c.get("Routeinfo"))

        # 停止中: SOP を変えると、すぐ go2pf に反映される
        sop.set(circle(1.5))
        self.assertTrue(self.run_frames(2, lambda: abs(st["status"].get("length", 0) - 2 * math.pi * 1.5) < 0.05))

        # 走行中: 変えても保留（go2pf は今のルートのまま）、地図には保留中として出る
        c.pulse("Arm")
        self.assertTrue(self.run_frames(3, lambda: c.get("State") == "RUNNING"), c.get("Reason"))
        sop.set(circle(1.0))
        self.assertTrue(self.run_frames(2, lambda: "保留中" in c.get("Routeinfo")), c.get("Routeinfo"))
        self.assertAlmostEqual(st["status"]["length"], 2 * math.pi * 1.5, delta=0.05)
        self._save_images("sop_pending")

        # 止めると反映される
        c.pulse("Estop")
        self.assertTrue(self.run_frames(3, lambda: abs(st["status"].get("length", 0) - 2 * math.pi * 1.0) < 0.05))
        self.assertNotIn("保留中", c.get("Routeinfo"))

        # フェンスの外に出る形は送らない（go2pf は前のルートのまま）
        sop.set(circle(3.0))
        self.assertTrue(self.run_frames(2, lambda: "ジオフェンスの外" in c.get("Routeinfo")))
        self.run_frames(1.2)
        self.assertAlmostEqual(st["status"]["length"], 2 * math.pi * 1.0, delta=0.05)
        self.assertEqual(c.get("Configack"), "OK")   # 他の設定は引き続き反映される
        self._save_images("sop_invalid")
        c.pulse("Shutdown")
        self.assertTrue(self.run_frames(8, lambda: st["proc"].poll() is not None))

    def test_launch_over_ssh(self):
        """ssh 経由の起動（偽の ssh が、リモートのコマンドをこの PC で実行する）。"""
        fake_bin = tempfile.mkdtemp()
        with open(os.path.join(fake_bin, "ssh"), "w") as f:
            f.write('#!/bin/bash\n# 最後の引数（リモートで実行するコマンド）をここで実行する\n'
                    'cd "$HOME"; exec bash -c "${@: -1}"\n')
        os.chmod(os.path.join(fake_bin, "ssh"), 0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = fake_bin + os.pathsep + old_path
        try:
            c = self.comp
            c.par.Backend.val = "mock"
            c.par.Sshtarget.val = "robot@127.0.0.1"
            c.par.Remotedir.val = ROOT
            c.pulse("Launch")
            st = go2ctl._state()
            self.assertTrue(self.run_frames(10, lambda: c.get("Configack") == "OK"), c.get("Configack"))
            self.assertIn("ssh robot@127.0.0.1", c.get("Procstate"))
            c.pulse("Arm")
            self.assertTrue(self.run_frames(3, lambda: c.get("State") == "RUNNING"), c.get("Reason"))
            c.pulse("Shutdown")
            self.assertTrue(self.run_frames(8, lambda: st["proc"].poll() is not None))
            self.assertEqual(st["proc"].returncode, 0)
        finally:
            os.environ["PATH"] = old_path

    def test_ssh_failure_is_reported(self):
        fake_bin = tempfile.mkdtemp()
        with open(os.path.join(fake_bin, "ssh"), "w") as f:
            f.write("#!/bin/bash\necho 'Permission denied (publickey).' >&2\nexit 255\n")
        os.chmod(os.path.join(fake_bin, "ssh"), 0o755)
        old_path = os.environ["PATH"]
        os.environ["PATH"] = fake_bin + os.pathsep + old_path
        try:
            c = self.comp
            c.par.Sshtarget.val = "robot@127.0.0.1"
            c.pulse("Launch")
            self.assertTrue(self.run_frames(3, lambda: "ssh に失敗" in c.get("Procstate")), c.get("Procstate"))
        finally:
            os.environ["PATH"] = old_path

    def _save_images(self, tag):
        out = os.environ.get("GO2CTL_IMG_DIR")
        if not out:
            return
        import cv2
        for name in ("map", "overlay"):
            img = go2ctl.get_image(name)[::-1]
            cv2.imwrite(os.path.join(out, f"{tag}_{name}.png"), cv2.cvtColor(img, cv2.COLOR_RGBA2BGR))


if __name__ == "__main__":
    unittest.main()
