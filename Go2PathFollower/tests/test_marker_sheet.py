"""tools/make_marker_sheet.py: 並べたマーカーが検出でき、寸法が正しいか。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tools"), os.path.join(ROOT, "touchdesigner")]
try:
    import cv2
    import numpy as np
    import aruco_pose as ap
    import make_marker_sheet as ms
except ImportError:  # pragma: no cover
    ms = None


@unittest.skipIf(ms is None, "OpenCV / Pillow がない")
class MarkerSheetTest(unittest.TestCase):
    IDS = [0, 1, 2, 3, 10]

    def test_all_on_one_page_and_detectable(self):
        for paper in ("A4", "A3"):
            pages, s_mm, cols, rows = ms.render_pages(self.IDS, paper, None, 150)
            self.assertEqual(len(pages), 1)
            img = np.array(pages[0])
            self.assertEqual(sorted(ap.Detector()(img)), self.IDS)
            # 1 px ≈ 2 mm まで縮めても（天井カメラ相当）検出できる
            k = 25.4 / 150 / 2   # 150 dpi → 1 px = 2 mm
            small = cv2.resize(img, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
            self.assertEqual(sorted(ap.Detector()(small)), self.IDS, paper)

    def test_marker_size_is_exact(self):
        dpi = 300
        pages, s_mm, _, _ = ms.render_pages([10], "A4", 100, dpi)
        corners = ap.Detector()(np.array(pages[0]))[10]
        side_px = np.linalg.norm(corners[1] - corners[0])
        self.assertAlmostEqual(side_px / dpi * 25.4, 100, delta=1.0)

    def test_fixed_size_paginates(self):
        pages, s_mm, cols, rows = ms.render_pages(self.IDS, "A3", 120, 100)
        self.assertEqual(s_mm, 120)
        self.assertEqual(len(pages), 3)

    def test_too_big_is_refused(self):
        with self.assertRaises(SystemExit):
            ms.render_pages(self.IDS, "A4", 180, 100)

    def test_pdf_page_size(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "s.pdf")
            ms.main(["--paper", "A4", "--out", out, "--dpi", "100"])
            data = open(out, "rb").read()
            self.assertIn(b"MediaBox", data)
            import re
            w, h = map(float, re.search(rb"MediaBox\s*\[\s*0\s+0\s+([\d.]+)\s+([\d.]+)", data).groups())
            self.assertAlmostEqual(w / 72 * 25.4, 210, delta=0.5)
            self.assertAlmostEqual(h / 72 * 25.4, 297, delta=0.5)


if __name__ == "__main__":
    unittest.main()
