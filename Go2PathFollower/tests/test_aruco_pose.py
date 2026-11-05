"""touchdesigner/aruco_pose.py を合成カメラ画像で確かめる（OpenCV がなければスキップ）。"""
import math
import os
import sys
import unittest

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover
    cv2 = None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "touchdesigner"))
if cv2 is not None:
    import aruco_pose as ap

W, H = 1280, 960


def room_to_image():
    """少し斜めから見た天井カメラ。部屋の +y は画像の上、+x は画像の右。"""
    room = np.float32([[-2.5, -2.5], [2.5, -2.5], [2.5, 2.5], [-2.5, 2.5]])
    img = np.float32([[180, 900], [1110, 930], [1060, 40], [230, 70]])
    return cv2.getPerspectiveTransform(room, img)


def marker_corners_room(cx, cy, yaw, size):
    """部屋座標で、向き yaw の機体の背中に「上辺が頭側」で貼ったマーカーの角（左上・右上・右下・左下）"""
    fx, fy = -math.sin(yaw), math.cos(yaw)       # 前
    lx, ly = -math.cos(yaw), -math.sin(yaw)      # 左
    h = size / 2.0
    return np.float32([
        [cx + h * fx + h * lx, cy + h * fy + h * ly],
        [cx + h * fx - h * lx, cy + h * fy - h * ly],
        [cx - h * fx - h * lx, cy - h * fy - h * ly],
        [cx - h * fx + h * lx, cy - h * fy + h * ly],
    ])


def render(markers, G):
    canvas = np.full((H, W), 255, np.uint8)
    d = cv2.aruco.getPredefinedDictionary(ap.ARUCO_DICT)
    for mid, room_corners in markers:
        bmp = cv2.aruco.generateImageMarker(d, mid, 240)
        pad = 40
        bmp = cv2.copyMakeBorder(bmp, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        src = np.float32([[pad, pad], [pad + 240, pad], [pad + 240, pad + 240], [pad, pad + 240]])
        dst = cv2.perspectiveTransform(room_corners.reshape(-1, 1, 2), G).reshape(4, 2)
        M = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
        warped = cv2.warpPerspective(bmp, M, (W, H), borderValue=255)
        canvas = np.minimum(canvas, warped)
    return canvas


@unittest.skipIf(cv2 is None, "OpenCV がない")
class ArucoPoseTest(unittest.TestCase):
    def setUp(self):
        self.G = room_to_image()
        self.refs = [(i, marker_corners_room(x, y, 0.0, 0.25)) for i, (x, y) in ap.REF_MARKERS.items()]

    def track(self, robot_corners, settings=None, tracker=None):
        tr = tracker or ap.Tracker()
        img = render(self.refs + [(ap.ROBOT_ID, robot_corners)], self.G)
        return tr.process(img, settings), tr

    def test_position_and_yaw(self):
        for x, y, yaw in [(0, 0, 0), (1.2, -0.7, 0.6), (-1.5, 1.0, math.pi / 2),
                          (0.5, 1.5, -2.5), (-0.8, -1.4, 3.0)]:
            (px, py, pyaw, found), _ = self.track(marker_corners_room(x, y, yaw, 0.2))
            self.assertEqual(found, 1)
            self.assertAlmostEqual(px, x, delta=0.02)
            self.assertAlmostEqual(py, y, delta=0.02)
            self.assertLess(abs(ap.wrap_angle(pyaw - yaw)), math.radians(2), (x, y, yaw, pyaw))

    def test_yaw_convention(self):
        # 頭が -x（左）を向いていれば +90°
        (_, _, yaw, _), _ = self.track(marker_corners_room(0, 0, math.pi / 2, 0.2))
        self.assertAlmostEqual(yaw, math.pi / 2, delta=math.radians(2))

    def test_not_found_keeps_last_pose(self):
        (x, y, yaw, found), tr = self.track(marker_corners_room(0.5, 0.5, 0.0, 0.2))
        img = render(self.refs, self.G)
        self.assertEqual(tr.process(img), (x, y, yaw, 0))

    def test_no_calibration_means_not_found(self):
        tr = ap.Tracker()
        img = render([(ap.ROBOT_ID, marker_corners_room(0, 0, 0, 0.2))], self.G)
        self.assertEqual(tr.process(img)[3], 0)

    def test_calibration_persists_without_refs(self):
        _, tr = self.track(marker_corners_room(0, 0, 0, 0.2))
        img = render([(ap.ROBOT_ID, marker_corners_room(1.0, -1.0, 0.3, 0.2))], self.G)
        x, y, yaw, found = tr.process(img)
        self.assertEqual(found, 1)
        self.assertAlmostEqual(x, 1.0, delta=0.02)

    def test_parallax_correction(self):
        cam_h, mk_h, nadir = 3.0, 0.45, np.array([0.0, 0.0])
        x, y = 1.5, 0.8
        true = marker_corners_room(x, y, 0.4, 0.2)
        # 高さ mk_h の点は、床面上ではカメラ真下から外側に Hc/(Hc-h) 倍離れて写る
        seen = nadir + (true - nadir) * cam_h / (cam_h - mk_h)
        st = {"camera_height": cam_h, "marker_height": 0.0, "nadir": (0.0, 0.0)}
        (ux, uy, _, _), _ = self.track(seen.astype(np.float32), st)
        self.assertGreater(math.hypot(ux - x, uy - y), 0.25)   # 補正なしでは大きくずれる
        st["marker_height"] = mk_h
        (cx, cy, cyaw, _), _ = self.track(seen.astype(np.float32), st)
        self.assertAlmostEqual(cx, x, delta=0.02)
        self.assertAlmostEqual(cy, y, delta=0.02)
        self.assertLess(abs(ap.wrap_angle(cyaw - 0.4)), math.radians(2))

    def test_yaw_offset(self):
        (_, _, yaw, _), _ = self.track(marker_corners_room(0, 0, 0.0, 0.2), {"yaw_offset": 0.5})
        self.assertAlmostEqual(yaw, 0.5, delta=math.radians(2))

    def test_changing_refs_invalidates_calibration(self):
        _, tr = self.track(marker_corners_room(0, 0, 0, 0.2))
        img = render([(ap.ROBOT_ID, marker_corners_room(0.3, 0.3, 0, 0.2))], self.G)
        moved = {0: (-1.9, -2.0), 1: (2.0, -2.0), 2: (2.0, 2.0), 3: (-2.0, 2.0)}
        self.assertEqual(tr.process(img, {"refs": moved})[3], 0)
        self.assertIsNone(tr.H)

    def test_save_and_load(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "h.json")
            _, tr = self.track(marker_corners_room(0, 0, 0, 0.2), tracker=ap.Tracker(path))
            tr._saved_at = -math.inf
            tr.calibrate(tr.markers)
            tr2 = ap.Tracker(path)
            img = render([(ap.ROBOT_ID, marker_corners_room(1.0, 0.5, 0, 0.2))], self.G)
            x, y, _, found = tr2.process(img)
            self.assertEqual(found, 1)
            self.assertAlmostEqual(x, 1.0, delta=0.02)


if __name__ == "__main__":
    unittest.main()
