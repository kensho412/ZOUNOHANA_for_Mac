import json
import os
import socket
import subprocess
import sys
import time
import unittest

from go2pf.config import Config
from go2pf.pose import Pose, PoseBuffer
from go2pf.remote import RemoteControl
from go2pf.supervisor import State, Supervisor

from .test_supervisor import FakeClock, RecordingBackend

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TD = ("10.0.0.5", 50000)
OTHER = ("10.0.0.9", 50000)
ROUTE = [{"x": 0, "y": -1.5}, {"x": 0, "y": 1.5, "speed": 0.2}]


def make():
    clock = FakeClock()
    buf = PoseBuffer(clock=clock)
    be = RecordingBackend()
    sup = Supervisor(Config(), None, be, buf, clock=clock)
    sent = []
    rc = RemoteControl(sup, sup.cfg, on_quit=lambda: sent.append("QUIT"), clock=clock,
                       send=lambda ip, port, addr, args: sent.append((ip, port, addr, args)))
    return rc, sup, be, buf, clock, sent


def cfg_msg(rc, data, src=TD, seq=1):
    rc.handlers()["/go2pf/config"](src, "/go2pf/config", seq, json.dumps(data))


def acks(sent):
    return [m[3] for m in sent if m != "QUIT" and m[2] == "/go2pf/ack"]


class RemoteControlTest(unittest.TestCase):
    def test_config_sets_route_and_params_and_locks_controller(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"control": {"lookahead": 0.7}, "route": ROUTE})
        self.assertEqual(acks(sent)[-1][1:], [1, "OK"])
        self.assertEqual(sup.cfg.control.lookahead, 0.7)
        self.assertEqual(len(sup.path.points), 2)
        self.assertEqual(rc.controller, TD[0])
        self.assertEqual(sent[-1][0:2], (TD[0], 9001))

    def test_other_host_cannot_configure_or_arm(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"route": ROUTE})
        cfg_msg(rc, {"control": {"lookahead": 1.5}}, src=OTHER)
        self.assertEqual(sup.cfg.control.lookahead, 0.5)
        buf.update(Pose(0, -1.4, 0, True))
        rc.handlers()["/go2pf/arm"](OTHER, "/go2pf/arm")
        self.assertEqual(sup.state, State.DISARMED)

    def test_estop_from_any_host(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"route": ROUTE})
        buf.update(Pose(0, -1.4, 0, True))
        rc.handlers()["/go2pf/arm"](TD, "/go2pf/arm")
        self.assertEqual(sup.state, State.RUNNING)
        rc.handlers()["/go2pf/estop"](OTHER, "/go2pf/estop")
        self.assertEqual(sup.state, State.ESTOP)
        self.assertEqual(be.calls[-1], ("stop",))

    def test_invalid_config_rejected_whole(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"control": {"lookahead": 0.8, "speed_max": 5.0}, "route": ROUTE})
        self.assertEqual(acks(sent)[-1][1], 0)
        self.assertEqual(sup.cfg.control.lookahead, 0.5)
        self.assertIsNone(sup.path)

    def test_unknown_or_forbidden_keys(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"control": {"lookahed": 0.8}})
        self.assertEqual(acks(sent)[-1][1], 0)
        cfg_msg(rc, {"go2": {"interface": "en0"}}, seq=2)
        self.assertEqual(acks(sent)[-1][1], 0)

    def test_route_change_refused_while_running_but_params_apply(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"route": ROUTE})
        buf.update(Pose(0, -1.4, 0, True))
        rc.handlers()["/go2pf/arm"](TD, "/go2pf/arm")
        new_route = [{"x": 1, "y": -1}, {"x": 1, "y": 1}]
        cfg_msg(rc, {"control": {"k_yaw": 2.0}, "route": new_route}, seq=2)
        self.assertEqual(acks(sent)[-1][1], 0)
        self.assertEqual(sup.cfg.control.k_yaw, 2.0)
        self.assertEqual(sup.path.points[0].x, 0)
        # 停止後、同じ JSON の再送で反映される
        sup.estop()
        cfg_msg(rc, {"control": {"k_yaw": 2.0}, "route": new_route}, seq=3)
        self.assertEqual(acks(sent)[-1][1:], [1, "OK"])
        self.assertEqual(sup.path.points[0].x, 1)

    def test_fence_must_contain_route(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"route": ROUTE})
        cfg_msg(rc, {"safety": {"geofence": {"y_max": 1.0}}}, seq=2)
        self.assertEqual(acks(sent)[-1][1], 0)
        self.assertEqual(sup.cfg.safety.geofence.y_max, 2.3)
        cfg_msg(rc, {"safety": {"geofence": {"y_max": 2.0}}}, seq=3)
        self.assertEqual(sup.cfg.safety.geofence.y_max, 2.0)

    def test_loop_toggle_rebuilds_path(self):
        rc, sup, be, buf, clock, sent = make()
        route = ROUTE + [{"x": 1, "y": 1.5}]
        cfg_msg(rc, {"route": route})
        cfg_msg(rc, {"control": {"loop": True}, "route": route}, seq=2)
        self.assertTrue(sup.path.loop)
        self.assertEqual(len(sup.path.points), 4)

    def test_controller_timeout(self):
        rc, sup, be, buf, clock, sent = make()
        rc.rcfg.controller_timeout = 3.0
        cfg_msg(rc, {"route": ROUTE})
        clock.t += 2.0
        self.assertFalse(rc.controller_lost())
        clock.t += 1.5
        self.assertTrue(rc.controller_lost())

    def test_quit(self):
        rc, sup, be, buf, clock, sent = make()
        cfg_msg(rc, {"route": ROUTE})
        rc.handlers()["/go2pf/quit"](TD, "/go2pf/quit")
        self.assertIn("QUIT", sent)


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EndToEndTest(unittest.TestCase):
    """TouchDesigner の代わりに、go2pf を起動して OSC だけで操作する。"""

    def test_launch_configure_arm_estop_quit(self):
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
            from pythonosc.udp_client import SimpleUDPClient
        except ImportError:
            self.skipTest("python-osc がない")
        import threading
        port, status_port = free_port(), free_port()
        got = {}
        disp = Dispatcher()
        disp.set_default_handler(lambda addr, *a: got.__setitem__(addr, a))
        server = ThreadingOSCUDPServer(("127.0.0.1", status_port), disp)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        proc = subprocess.Popen(
            [sys.executable, "-m", "go2pf", "--backend", "mock", "--no-viz", "--no-log",
             "--port", str(port), "--status-port", str(status_port), "--controller-timeout", "5"],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        client = SimpleUDPClient("127.0.0.1", port)

        def wait_for(pred, timeout=8.0):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if pred():
                    return True
                time.sleep(0.05)
            return False

        try:
            config = json.dumps({"control": {"speed": 0.4}, "route": [
                {"x": -1.5, "y": -1.5}, {"x": -1.5, "y": 1.5}, {"x": 1.5, "y": 1.5}]})
            ok = wait_for(lambda: (client.send_message("/go2pf/config", [1, config]) or True)
                          and got.get("/go2pf/ack", (None, 0))[1] == 1)
            self.assertTrue(ok, got)
            client.send_message("/go2pf/arm", [])
            self.assertTrue(wait_for(lambda: got.get("/go2pf/status", ("",))[0] == "RUNNING"), got)
            self.assertTrue(wait_for(lambda: got.get("/go2pf/cmd", (0,))[0] > 0.1))
            self.assertIn("/go2pf/target", got)
            client.send_message("/go2pf/estop", [])
            self.assertTrue(wait_for(lambda: got.get("/go2pf/status", ("",))[0] == "ESTOP"), got)
            client.send_message("/go2pf/quit", [])
            self.assertEqual(proc.wait(timeout=8), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
            out = proc.stdout.read().decode()
            proc.stdout.close()
            client._sock.close()
            server.shutdown()
            server.server_close()
        self.assertIn("リモート非常停止", out)


if __name__ == "__main__":
    unittest.main()
