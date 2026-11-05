"""印刷用の ArUco マーカー画像を作る（基準 0〜3 とロボット 10）。

    python tools/make_markers.py --size-cm 18 --out markers

300 dpi の PNG を出力する。印刷時は「実際のサイズ」で印刷し、黒い正方形の一辺を定規で確認すること。
周りの白い余白（quiet zone）は切り落とさない。
"""
import argparse
import os
import sys

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "touchdesigner"))
import aruco_pose as ap  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--size-cm", type=float, default=18.0, help="黒い正方形の一辺 [cm]")
parser.add_argument("--dpi", type=int, default=300)
parser.add_argument("--out", default="markers")
parser.add_argument("--ids", type=int, nargs="*", default=sorted(ap.REF_MARKERS) + [ap.ROBOT_ID])
args = parser.parse_args()

os.makedirs(args.out, exist_ok=True)
d = cv2.aruco.getPredefinedDictionary(ap.ARUCO_DICT)
px = int(round(args.size_cm / 2.54 * args.dpi))
pad = px // 4
for i in args.ids:
    img = cv2.aruco.generateImageMarker(d, i, px)
    img = cv2.copyMakeBorder(img, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
    label = f"id {i}  {args.size_cm:g} cm  " + ("robot (top edge = head)" if i == ap.ROBOT_ID else
                                               f"ref {ap.REF_MARKERS.get(i, '')}")
    cv2.putText(img, label, (pad, pad // 2), cv2.FONT_HERSHEY_SIMPLEX, px / 900, 0, max(1, px // 300))
    path = os.path.join(args.out, f"aruco_{i}.png")
    cv2.imwrite(path, img, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    print(path)
