import math
import unittest

from go2pf.backends import MockBackend, MockWorld, RobotBackend
from go2pf.config import Config
from go2pf.geometry import Polyline, Waypoint
from go2pf.pose import Pose, PoseBuffer, parse_pose_args
from go2pf.supervisor import State, Supervisor


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class RecordingBackend(RobotBackend):
    def __init__(self):
        self.calls = []

    def move(self, vx, vy, vyaw):
        self.calls.append(("move", vx, vy, vyaw))

    def stop(self):
        self.calls.append(("stop",))

    def moves(self):
        return [c for c in self.calls if c[0] == "move"]


def make(cfg=None, route=None):
    cfg = cfg or Config()
    clock = FakeClock()
    buf = PoseBuffer(clock=clock)
    path = Polyline(route or [Waypoint(0, -1.5), Waypoint(0, 1.5)])
    be = RecordingBackend()
    sup = Supervisor(cfg, path, be, buf, clock=clock)
    return sup, be, buf, clock


def run(sup, clock, buf, n, pose=None, dt=1 / 30):
    for _ in range(n):
        clock.t += dt
        if pose is not None:
            buf.update(pose)
        sup.tick()


class SupervisorTest(unittest.TestCase):
    def test_starts_disarmed_and_sends_nothing(self):
        sup, be, buf, clock = make()
        run(sup, clock, buf, 30, Pose(0, -1.4, 0, True))
        self.assertEqual(sup.state, State.DISARMED)
        self.assertEqual(be.calls, [])

    def test_arm_requires_pose(self):
        sup, be, buf, clock = make()
        self.assertIn("できません", sup.arm())
        buf.update(Pose(0, -1.4, 0, False))
        self.assertIn("できません", sup.arm())
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        self.assertEqual(sup.state, State.RUNNING)

    def test_arm_refused_outside_fence(self):
        sup, be, buf, clock = make()
        buf.update(Pose(2.5, 0, 0, True))
        self.assertIn("ジオフェンス", sup.arm())
        self.assertEqual(sup.state, State.DISARMED)

    def test_running_sends_rate_limited_capped_commands(self):
        sup, be, buf, clock = make()
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        run(sup, clock, buf, 60, Pose(0, -1.4, 0, True))
        moves = be.moves()
        self.assertEqual(len(moves), 60)
        cfg = sup.cfg.control
        prev = 0.0
        for _, vx, vy, vyaw in moves:
            self.assertLessEqual(vx, cfg.speed_max + 1e-9)
            self.assertLessEqual(vx - prev, cfg.accel_vx * 2 / cfg.rate_hz + 1e-9)
            prev = vx
        self.assertAlmostEqual(moves[-1][1], cfg.speed)

    def test_estop_immediate_and_latched(self):
        sup, be, buf, clock = make()
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        run(sup, clock, buf, 5, Pose(0, -1.4, 0, True))
        sup.estop()
        self.assertEqual(be.calls[-1], ("stop",))
        n = len(be.calls)
        run(sup, clock, buf, 30, Pose(0, -1.4, 0, True))
        self.assertEqual(len(be.calls), n)
        self.assertEqual(sup.state, State.ESTOP)
        sup.arm()
        self.assertEqual(sup.state, State.RUNNING)

    def test_found0_stops_then_resumes(self):
        sup, be, buf, clock = make()
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        run(sup, clock, buf, 3, Pose(0, -1.4, 0, True))
        run(sup, clock, buf, 1, Pose(0, -1.4, 0, False))
        self.assertEqual(sup.state, State.HOLD)
        self.assertEqual(be.calls[-1], ("stop",))
        n = len(be.calls)
        run(sup, clock, buf, 10, Pose(0, -1.4, 0, False))
        self.assertEqual(len(be.calls), n)
        run(sup, clock, buf, 1, Pose(0, -1.4, 0, True))
        self.assertEqual(sup.state, State.RUNNING)
        # 再開時は 0 から加速し直す
        self.assertLess(be.moves()[-1][1], 0.05)

    def test_no_resume_when_disabled(self):
        cfg = Config()
        cfg.safety.resume_after_tracking_loss = False
        sup, be, buf, clock = make(cfg)
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        run(sup, clock, buf, 1, Pose(0, -1.4, 0, False))
        run(sup, clock, buf, 10, Pose(0, -1.4, 0, True))
        self.assertEqual(sup.state, State.HOLD)

    def test_pose_timeout(self):
        sup, be, buf, clock = make()
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        run(sup, clock, buf, 3, Pose(0, -1.4, 0, True))
        run(sup, clock, buf, 14, None)          # 約 0.47 s: まだ動く
        self.assertEqual(sup.state, State.RUNNING)
        run(sup, clock, buf, 2, None)           # 0.5 s 超
        self.assertEqual(sup.state, State.HOLD)
        self.assertIn("途絶", sup.reason)

    def test_outside_fence_faults(self):
        sup, be, buf, clock = make()
        buf.update(Pose(0, -1.4, 0, True))
        sup.arm()
        run(sup, clock, buf, 3, Pose(0, -1.4, 0, True))
        run(sup, clock, buf, 1, Pose(2.4, 0, 0, True))
        self.assertEqual(sup.state, State.FAULT)
        self.assertEqual(be.calls[-1], ("stop",))
        run(sup, clock, buf, 5, Pose(0, -1.4, 0, True))
        self.assertEqual(sup.state, State.FAULT)   # 自動では復帰しない

    def test_heading_out_of_fence_faults(self):
        # ルート終点がフェンス際。速度が乗った状態でフェンスへ向かうと予測で止まる
        sup, be, buf, clock = make(route=[Waypoint(0, 0), Waypoint(0, 2.29)])
        buf.update(Pose(0, 1.9, 0, True))
        sup.arm()
        sup.limiter.current.vx = 0.6
        run(sup, clock, buf, 1, Pose(0, 2.05, 0, True))
        self.assertEqual(sup.state, State.FAULT)
        self.assertIn("向かう", sup.reason)

    def test_finishes_and_stops(self):
        sup, be, buf, clock = make()
        buf.update(Pose(0, 1.2, 0, True))
        sup.arm()
        run(sup, clock, buf, 30, Pose(0, 1.2, 0, True))
        run(sup, clock, buf, 60, Pose(0, 1.4, 0, True))
        self.assertEqual(sup.state, State.FINISHED)
        self.assertEqual(be.calls[-1], ("stop",))
        vxs = [m[1] for m in be.moves()]
        # 減速も加速度制限に従う
        for a, b in zip(vxs, vxs[1:]):
            self.assertLessEqual(abs(b - a), sup.cfg.control.accel_vx * 2 / 30 + 1e-9)

    def test_shutdown_always_sends_stop(self):
        sup, be, buf, clock = make()
        sup.shutdown()
        self.assertEqual(be.calls, [("stop",)])


class ParsePoseTest(unittest.TestCase):
    def test_ok(self):
        p = parse_pose_args([1.0, -2.0, 4.0, 1])
        self.assertEqual((p.x, p.y, p.found), (1.0, -2.0, True))
        self.assertAlmostEqual(p.yaw, 4.0 - 2 * math.pi)

    def test_found_as_float(self):
        self.assertFalse(parse_pose_args([0, 0, 0, 0.0]).found)

    def test_bad(self):
        self.assertIsNone(parse_pose_args([1, 2, 3]))
        self.assertIsNone(parse_pose_args([1, "a", 3, 1]))
        self.assertIsNone(parse_pose_args([float("nan"), 0, 0, 1]))


class ClosedLoopTest(unittest.TestCase):
    """MockWorld を手動で時間発展させて、ルートを最後まで走れるか確認する。"""

    def drive(self, route, loop=False, start=None, seconds=60.0):
        cfg = Config()
        cfg.control.loop = loop
        path = Polyline(route, loop=loop)
        clock = FakeClock()
        buf = PoseBuffer(clock=clock)
        x, y, yaw = start or (route[0].x, route[0].y, 0.0)
        world = MockWorld(x, y, yaw, cfg.mock.velocity_tau)
        sup = Supervisor(cfg, path, MockBackend(world), buf, clock=clock)
        buf.update(world.pose())
        self.assertEqual(sup.arm(), "アームしました")
        max_dev = 0.0
        dt = 1 / 30
        for _ in range(int(seconds / dt)):
            for _ in range(6):
                world.step(dt / 6)
            clock.t += dt
            buf.update(world.pose())
            sup.tick()
            max_dev = max(max_dev, path.project(world.x, world.y).dist)
            if sup.state in (State.FINISHED, State.FAULT):
                break
        return sup, world, max_dev

    def test_u_route(self):
        route = [Waypoint(-1.5, -1.5), Waypoint(-1.5, 1.5), Waypoint(1.5, 1.5), Waypoint(1.5, -1.5)]
        sup, world, dev = self.drive(route)
        self.assertEqual(sup.state, State.FINISHED, sup.reason)
        self.assertLess(math.hypot(world.x - 1.5, world.y + 1.5), 0.4)
        self.assertLess(dev, 0.3)

    def test_starts_facing_wrong_way(self):
        route = [Waypoint(0, -1.5), Waypoint(0, 1.5)]
        sup, world, dev = self.drive(route, start=(0.3, -1.5, math.pi))
        self.assertEqual(sup.state, State.FINISHED, sup.reason)

    def test_loop_keeps_going(self):
        route = [Waypoint(-1, -1), Waypoint(-1, 1), Waypoint(1, 1), Waypoint(1, -1)]
        sup, world, dev = self.drive(route, loop=True, seconds=40)
        self.assertEqual(sup.state, State.RUNNING)
        self.assertGreaterEqual(sup.tracker.laps, 1)
        self.assertLess(dev, 0.3)


if __name__ == "__main__":
    unittest.main()
