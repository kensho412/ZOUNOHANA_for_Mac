"""ArUco マーカー（基準 0〜3 とロボット 10）を紙にまとめた、印刷用 PDF を作る。

    # A4 1 枚に全部（入る最大の大きさになる）
    python tools/make_marker_sheet.py --paper A4

    # 大きさを指定（入りきらなければ自動で複数ページ）
    python tools/make_marker_sheet.py --paper A3 --size-cm 12

印刷は必ず「実際のサイズ」「100%」で（「用紙に合わせる」にしない）。
各ページの 10 cm のものさしを定規で測って、大きさが合っているか確認すること。
マーカーの周りの白い余白は検出に必要なので、切り取り線（点線）より内側を切り落とさない。
"""
import argparse
import math
import os
import sys

import cv2
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "touchdesigner"))
import aruco_pose as ap  # noqa: E402

PAPERS_MM = {"A4": (210, 297), "A3": (297, 420), "B4": (257, 364), "Letter": (216, 279)}
QUIET = 1 / 6     # 白い余白（片側）。ArUco に必要な黒いマス 1 つ分 = マーカーの一辺の 1/6
LABEL_MM = 9       # ラベルの高さ
MARGIN_MM = 8      # 紙の端の余白（プリンタが印刷できない部分）
RULER_MM = 14      # ものさし用の帯


def layout(n, paper_mm, size_mm=None):
    """(1 ページあたりの列, 行, マーカーの一辺 mm)。size_mm が None なら 1 ページに全部入る最大の大きさ。"""
    w = paper_mm[0] - 2 * MARGIN_MM
    h = paper_mm[1] - 2 * MARGIN_MM - RULER_MM
    best = None
    for cols in range(1, n + 1):
        for rows in range(1, n + 1):
            cell_w, cell_h = w / cols, h / rows
            s = min(cell_w / (1 + 2 * QUIET), (cell_h - LABEL_MM) / (1 + 2 * QUIET))
            if size_mm is None:
                if cols * rows >= n and (best is None or s > best[2] + 1e-9):
                    best = (cols, rows, s)
            elif s >= size_mm and (best is None or cols * rows > best[0] * best[1]):
                best = (cols, rows, size_mm)
    if best is None:
        raise SystemExit(f"{size_mm / 10:g} cm のマーカーはこの用紙に入りません。用紙を大きくするか --size-cm を小さく")
    return best


def font(px):
    for path in ("/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf",
                 "C:/Windows/Fonts/arial.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, px)
    return ImageFont.load_default()


def label_for(i, size_cm):
    if i == ap.ROBOT_ID:
        return f"ID {i}  ROBOT  {size_cm:.1f} cm   (top edge = robot head)"
    if i in ap.REF_MARKERS:
        x, y = ap.REF_MARKERS[i]
        return f"ID {i}  REF  {size_cm:.1f} cm   default ({x:+g}, {y:+g}) m"
    return f"ID {i}  {size_cm:.1f} cm"


def render_pages(ids, paper, size_mm, dpi):
    paper_mm = PAPERS_MM[paper]
    cols, rows, s_mm = layout(len(ids), paper_mm, size_mm)
    mm = dpi / 25.4
    W, H = round(paper_mm[0] * mm), round(paper_mm[1] * mm)
    d = cv2.aruco.getPredefinedDictionary(ap.ARUCO_DICT)
    s_px = round(s_mm * mm)
    cell_w = (paper_mm[0] - 2 * MARGIN_MM) / cols * mm
    cell_h = (paper_mm[1] - 2 * MARGIN_MM - RULER_MM) / rows * mm
    f_label, f_small = font(round(3.2 * mm)), font(round(2.6 * mm))
    per_page = cols * rows
    pages = []
    for start in range(0, len(ids), per_page):
        img = Image.new("L", (W, H), 255)
        dr = ImageDraw.Draw(img)
        for k, i in enumerate(ids[start:start + per_page]):
            c, r = k % cols, k // cols
            x0 = MARGIN_MM * mm + c * cell_w
            y0 = MARGIN_MM * mm + r * cell_h
            # マーカー（セルの中央）。ラベルと矢印は白い余白のさらに外（上）に置く
            q = round(s_px * QUIET)
            content_h = LABEL_MM * mm + s_px + 2 * q
            top = round(y0 + (cell_h - content_h) / 2)
            mx = round(x0 + (cell_w - s_px) / 2)
            my = top + round(LABEL_MM * mm) + q
            img.paste(Image.fromarray(cv2.aruco.generateImageMarker(d, i, s_px)), (mx, my))
            dr.text((mx - q, top + round(1.5 * mm)), label_for(i, s_mm / 10), font=f_label, fill=0)
            if i == ap.ROBOT_ID:  # 頭の向きの矢印（ラベルの行の右端）
                ax, ay = mx + s_px + q - round(3 * mm), top + round(7 * mm)
                dr.polygon([(ax, ay - round(5 * mm)), (ax - round(2.5 * mm), ay), (ax + round(2.5 * mm), ay)], fill=0)
            # 切り取り線（白い余白とラベルの外側）
            _dashed_rect(dr, (mx - q, top, mx + s_px + q, my + s_px + q), round(0.25 * mm) or 1, round(2 * mm))
        # 10 cm のものさし
        y = H - round((MARGIN_MM + RULER_MM / 2) * mm)
        x = round(MARGIN_MM * mm)
        dr.line([(x, y), (x + round(100 * mm), y)], fill=0, width=max(1, round(0.4 * mm)))
        for t in range(11):
            tx = x + round(t * 10 * mm)
            dr.line([(tx, y - round((3 if t % 5 == 0 else 1.5) * mm)), (tx, y)], fill=0,
                    width=max(1, round(0.3 * mm)))
        dr.text((x + round(102 * mm), y - round(3 * mm)),
                f"10 cm  - print at 100% (actual size)   {paper}  page {len(pages) + 1}", font=f_small, fill=0)
        pages.append(img)
    return pages, s_mm, cols, rows


def _dashed_rect(dr, box, width, dash):
    x0, y0, x1, y1 = box
    for (a, b) in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        n = max(1, int(length // (2 * dash)))
        for k in range(n):
            t0, t1 = (2 * k * dash) / length, min(1.0, (2 * k + 1) * dash / length)
            dr.line([(a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0),
                     (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)], fill=150, width=width)


def main(argv=None):
    p = argparse.ArgumentParser(description="ArUco マーカーを紙にまとめた印刷用 PDF を作る")
    p.add_argument("--paper", choices=sorted(PAPERS_MM), default="A4")
    p.add_argument("--size-cm", type=float, help="黒い正方形の一辺 [cm]。省略すると 1 枚に全部入る最大の大きさ")
    p.add_argument("--ids", type=int, nargs="*", default=sorted(ap.REF_MARKERS) + [ap.ROBOT_ID])
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--out", default="markers/marker_sheet.pdf")
    a = p.parse_args(argv)

    pages, s_mm, cols, rows = render_pages(a.ids, a.paper, a.size_cm * 10 if a.size_cm else None, a.dpi)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    pages[0].save(a.out, save_all=True, append_images=pages[1:], resolution=a.dpi)
    print(f"{a.out}: {a.paper} {len(pages)} ページ、マーカー {s_mm / 10:.1f} cm（{cols}×{rows} 並び）")
    if s_mm < 120:
        print(f"注意: {s_mm / 10:.1f} cm は机の上での確認用の大きさです。天井から 5 m 四方を映す本番では 12〜18 cm を目安に"
              f"（例: --paper A3 --size-cm 15。ロボット用は背中に収まる大きさで）")
    return a.out, pages


if __name__ == "__main__":
    main()
