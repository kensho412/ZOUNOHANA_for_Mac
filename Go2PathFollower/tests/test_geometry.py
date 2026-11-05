import math
import unittest

from go2pf.geometry import (Polyline, Waypoint, forward_vector, robot_to_world,
                            target_heading, world_to_robot, wrap_angle)

PI = math.pi


class WrapAngleTest(unittest.TestCase):
    def test_in_range_unchanged(self):
        for a in (0.0, 0.5, -0.5, 3.0, -3.0):
            self.assertAlmostEqual(wrap_angle(a), a)

    def test_wraps(self):
        self.assertAlmostEqual(wrap_angle(2 * PI), 0.0)
        self.assertAlmostEqual(wrap_angle(3 * PI / 2), -PI / 2)
        self.assertAlmostEqual(wrap_angle(-3 * PI / 2), PI / 2)
        self.assertAlmostEqual(wrap_angle(7 * PI + 0.1), -PI + 0.1)

    def test_range(self):
        for i in range(-2000, 2000):
            a = wrap_angle(i * 0.01 * PI)
            self.assertTrue(-PI <= a < PI, a)


class TargetHeadingTest(unittest.TestCase):
    """yaw の定義: +y 向きで 0、反時計回りが正"""

    def test_cardinal(self):
        self.assertAlmostEqual(target_heading(0, 0, 0, 1), 0.0)        # 前 (+y)
        self.assertAlmostEqual(target_heading(0, 0, -1, 0), PI / 2)    # 左 (-x) は +90°
        self.assertAlmostEqual(target_heading(0, 0, 1, 0), -PI / 2)    # 右 (+x) は -90°
        self.assertAlmostEqual(abs(target_heading(0, 0, 0, -1)), PI)   # 後ろ

    def test_offset_origin(self):
        self.assertAlmostEqual(target_heading(1, 1, 2, 2), -PI / 4)

    def test_consistent_with_forward_vector(self):
        for yaw in (-2.5, -1.0, 0.0, 0.7, 2.9):
            fx, fy = forward_vector(yaw)
            self.assertAlmostEqual(wrap_angle(target_heading(0, 0, fx, fy) - yaw), 0.0)


class FrameTest(unittest.TestCase):
    def test_forward_at_zero_is_plus_y(self):
        self.assertEqual(tuple(round(v, 9) for v in robot_to_world(1, 0, 0)), (0, 1))

    def test_left_at_zero_is_minus_x(self):
        self.assertEqual(tuple(round(v, 9) for v in robot_to_world(0, 1, 0)), (-1, 0))

    def test_ccw_turn(self):
        # 反時計回りに 90° 回ると前は -x
        wx, wy = robot_to_world(1, 0, PI / 2)
        self.assertAlmostEqual(wx, -1)
        self.assertAlmostEqual(wy, 0)

    def test_roundtrip(self):
        for yaw in (-3.0, -1.2, 0.0, 0.4, 2.2):
            wx, wy = robot_to_world(0.3, -0.1, yaw)
            vx, vy = world_to_robot(wx, wy, yaw)
            self.assertAlmostEqual(vx, 0.3)
            self.assertAlmostEqual(vy, -0.1)


def square(loop=False):
    return Polyline([Waypoint(0, 0), Waypoint(0, 2), Waypoint(2, 2), Waypoint(2, 0)], loop=loop)


class PolylineTest(unittest.TestCase):
    def test_length_and_point_at(self):
        p = square()
        self.assertAlmostEqual(p.length, 6)
        self.assertEqual(p.point_at(1), (0, 1))
        self.assertEqual(p.point_at(3), (1, 2))
        self.assertEqual(p.point_at(99), (2, 0))   # 端でクランプ
        self.assertEqual(p.point_at(-1), (0, 0))

    def test_loop_wraps(self):
        p = square(loop=True)
        self.assertAlmostEqual(p.length, 8)
        self.assertEqual(p.point_at(8.5), (0, 0.5))
        self.assertEqual(p.point_at(7), (1, 0))

    def test_duplicate_points_removed(self):
        p = Polyline([Waypoint(0, 0), Waypoint(0, 0), Waypoint(0, 1)])
        self.assertEqual(p.num_segments, 1)

    def test_needs_two_points(self):
        with self.assertRaises(ValueError):
            Polyline([Waypoint(1, 1), Waypoint(1, 1)])

    def test_project(self):
        pr = square().project(0.3, 1.0)
        self.assertAlmostEqual(pr.s, 1.0)
        self.assertAlmostEqual(pr.dist, 0.3)
        # 進行方向 (+y) の右側にいる → cross_track は負
        self.assertAlmostEqual(pr.cross_track, -0.3)

    def test_project_left_positive(self):
        self.assertAlmostEqual(square().project(-0.2, 1.0).cross_track, 0.2)

    def test_project_window_prevents_jump(self):
        # 点 (1.2, 0.1) は全体では最後の区間に近いが、窓を最初の区間に限れば最初の区間に投影される
        p = square()
        self.assertGreater(p.project(1.2, 0.1).s, 4)
        pr = p.project(1.2, 0.1, 0.0, 1.5)
        self.assertLessEqual(pr.s, 1.5)
        self.assertEqual(pr.seg, 0)

    def test_project_loop_window_across_seam(self):
        p = square(loop=True)
        pr = p.project(0.0, 0.2, 7.5, 8.6)   # 継ぎ目をまたぐ窓
        self.assertAlmostEqual(pr.s, 8.2)    # 展開された s
        self.assertAlmostEqual(p.normalize_s(pr.s), 0.2)

    def test_speed_per_segment(self):
        p = Polyline([Waypoint(0, 0, 0.1), Waypoint(0, 1), Waypoint(0, 2, 0.5)])
        self.assertEqual(p.speed_at(0.5, 0.3), 0.1)
        self.assertEqual(p.speed_at(1.5, 0.3), 0.3)


if __name__ == "__main__":
    unittest.main()
