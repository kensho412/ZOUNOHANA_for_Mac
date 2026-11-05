import math
import unittest

from go2pf.config import ControlConfig
from go2pf.controller import Command, PurePursuit, RateLimiter
from go2pf.geometry import Polyline, Waypoint


def straight(loop=False):
    return Polyline([Waypoint(0, 0), Waypoint(0, 4)], loop=loop)


class PurePursuitTargetTest(unittest.TestCase):
    def test_target_is_lookahead_ahead_of_closest_point(self):
        pp = PurePursuit(straight(), ControlConfig(lookahead=0.5))
        out = pp.update(0.3, 1.0, 0.0)   # 最近点は (0, 1)
        self.assertAlmostEqual(out.tx, 0.0)
        self.assertAlmostEqual(out.ty, 1.5)

    def test_target_clamped_at_end(self):
        pp = PurePursuit(straight(), ControlConfig(lookahead=0.5))
        out = pp.update(0.0, 3.8, 0.0)
        self.assertEqual((out.tx, out.ty), (0.0, 4.0))

    def test_target_around_corner(self):
        path = Polyline([Waypoint(0, 0), Waypoint(0, 1), Waypoint(1, 1)])
        out = PurePursuit(path, ControlConfig(lookahead=0.5)).update(0, 0.8, 0)
        self.assertAlmostEqual(out.tx, 0.3)
        self.assertAlmostEqual(out.ty, 1.0)

    def test_loop_target_wraps(self):
        path = Polyline([Waypoint(0, 0), Waypoint(0, 1), Waypoint(1, 1), Waypoint(1, 0)], loop=True)
        out = PurePursuit(path, ControlConfig(lookahead=0.5)).update(0.2, 0.0, 0)
        self.assertAlmostEqual(out.tx, 0.0)
        self.assertAlmostEqual(out.ty, 0.3)


class PurePursuitCommandTest(unittest.TestCase):
    def test_on_path_straight(self):
        out = PurePursuit(straight(), ControlConfig(speed=0.3)).update(0, 1, 0.0)
        self.assertAlmostEqual(out.heading_error, 0.0)
        self.assertAlmostEqual(out.cmd.vx, 0.3)
        self.assertAlmostEqual(out.cmd.vyaw, 0.0)
        self.assertEqual(out.cmd.vy, 0.0)

    def test_robot_right_of_path_turns_left(self):
        # 経路の右 (+x) にいる → 目標は左前 → 反時計回り (vyaw > 0)
        cfg = ControlConfig(lookahead=0.5, k_yaw=1.5)
        out = PurePursuit(straight(), cfg).update(0.5, 1.0, 0.0)
        self.assertAlmostEqual(out.heading_error, math.pi / 4)
        self.assertAlmostEqual(out.cmd.vyaw, 1.5 * math.pi / 4 if 1.5 * math.pi / 4 < cfg.vyaw_max else cfg.vyaw_max)
        self.assertAlmostEqual(out.cmd.vx, 0.3 * math.cos(math.pi / 4))

    def test_vyaw_clamped(self):
        cfg = ControlConfig(k_yaw=10.0, vyaw_max=1.0)
        out = PurePursuit(straight(), cfg).update(0.0, 1.0, 1.0)
        self.assertAlmostEqual(out.cmd.vyaw, -1.0)

    def test_facing_backwards_no_forward(self):
        out = PurePursuit(straight(), ControlConfig()).update(0, 1, math.pi)
        self.assertEqual(out.cmd.vx, 0.0)
        self.assertGreater(abs(out.cmd.vyaw), 0.0)

    def test_segment_speed_and_cap(self):
        path = Polyline([Waypoint(0, 0, 0.9), Waypoint(0, 4)])
        out = PurePursuit(path, ControlConfig(speed_max=0.6)).update(0, 1, 0)
        self.assertAlmostEqual(out.cmd.vx, 0.6)

    def test_lateral_correction_sign(self):
        # 右にずれていて経路と平行 → 左 (vy > 0) に寄せる
        cfg = ControlConfig(k_lateral=1.0, vy_max=0.2)
        out = PurePursuit(straight(), cfg).update(0.1, 1.0, 0.0)
        self.assertAlmostEqual(out.cmd.vy, 0.1)

    def test_finish(self):
        pp = PurePursuit(straight(), ControlConfig(goal_radius=0.25))
        self.assertFalse(pp.update(0, 3.0, 0).finished)
        self.assertTrue(pp.update(0, 3.9, 0).finished)

    def test_no_early_finish_when_route_returns_to_start(self):
        path = Polyline([Waypoint(0, 0), Waypoint(0, 2), Waypoint(1, 2), Waypoint(1, 0), Waypoint(0.1, 0)])
        pp = PurePursuit(path, ControlConfig(goal_radius=0.25))
        self.assertFalse(pp.update(0, 0.05, 0).finished)

    def test_loop_never_finishes(self):
        path = Polyline([Waypoint(0, 0), Waypoint(0, 1), Waypoint(1, 1)], loop=True)
        pp = PurePursuit(path, ControlConfig())
        self.assertFalse(pp.update(0, 0, 0).finished)

    def test_progress_does_not_jump_at_crossing(self):
        # X 字に交差するループ。交差点付近を通過しても、もう一方の枝に飛ばない
        path = Polyline([Waypoint(-1, -1), Waypoint(1, 1), Waypoint(1, -1), Waypoint(-1, 1)], loop=True)
        pp = PurePursuit(path, ControlConfig(search_back=0.2, search_ahead=1.0))
        pp.update(-0.6, -0.6, 0)
        for k in range(1, 13):
            t = -0.6 + 0.1 * k
            out = pp.update(t + 0.02, t - 0.02, 0)
            self.assertEqual(path.segment_at(out.s), 0, (t, out.s))


class RateLimiterTest(unittest.TestCase):
    def test_limits_change_per_step(self):
        rl = RateLimiter(0.5, 0.5, 2.0)
        c = rl.step(Command(1.0, -1.0, 5.0), 0.1)
        self.assertAlmostEqual(c.vx, 0.05)
        self.assertAlmostEqual(c.vy, -0.05)
        self.assertAlmostEqual(c.vyaw, 0.2)

    def test_reaches_target(self):
        rl = RateLimiter(0.5, 0.5, 2.0)
        for _ in range(100):
            c = rl.step(Command(0.3, 0.0, -0.5), 0.05)
        self.assertEqual(c.as_tuple(), (0.3, 0.0, -0.5))


if __name__ == "__main__":
    unittest.main()
