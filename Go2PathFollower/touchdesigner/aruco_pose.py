"""天井カメラの画像から、ArUco マーカーで Go2 の位置と向きを部屋座標で求める。

TouchDesigner では go2ctl.py から使う（設定は TD のカスタムパラメータから毎フレーム渡される）。
単体でも動く（Web カメラで直接 OSC を送る。末尾の main を参照）。

座標（go2pf と同じ）:
  原点は区画の中心、単位 m、x は右が正、y は前が正、
  yaw は +y を向いているとき 0、反時計回りが正、[-π, π)

キャリブレーション:
  床に基準マーカーを 4 枚貼り、その中心の部屋座標を settings["refs"] に入れる。
  4 枚が同時に見えたら、画像 → 床面のホモグラフィを計算して保存する。
  カメラを動かさなければ、保存後は基準マーカーを剥がしてもよい。
"""
import json
import math
import os
import time

import cv2
import numpy as np

DICTIONARIES = {
    "4X4_50": cv2.aruco.DICT_4X4_50,
    "5X5_100": cv2.aruco.DICT_5X5_100,
    "6X6_250": cv2.aruco.DICT_6X6_250,
}

DEFAULTS = {
    "dictionary": "4X4_50",
    # 床の基準マーカー: ID → 中心の部屋座標 (x, y) [m]（0 左手前、1 右手前、2 右奥、3 左奥）
    "refs": {0: (-2.0, -2.0), 1: (2.0, -2.0), 2: (2.0, 2.0), 3: (-2.0, 2.0)},
    "robot_id": 10,        # ロボットの背中のマーカー
    # マーカーの上辺（ArUco の 0 番・1 番の角の辺）を機体の頭の方へ向けて貼る。ずれの補正 [rad]
    "yaw_offset": 0.0,
    # 視差補正: マーカーが床より高いと、カメラの真下から離れるほど外側にずれて見える
    "camera_height": 3.0,  # レンズの床からの高さ [m]
    "marker_height": 0.0,  # ロボットのマーカーの床からの高さ [m]。0 なら補正しない
    "nadir": None,         # カメラ真下の部屋座標 (x, y)。None なら画像中心を床に写した点で近似
}

# 互換用（tools/make_markers.py などが参照する）
ARUCO_DICT = DICTIONARIES[DEFAULTS["dictionary"]]
REF_MARKERS = DEFAULTS["refs"]
ROBOT_ID = DEFAULTS["robot_id"]


def wrap_angle(a):
    a = math.fmod(a + math.pi, 2.0 * math.pi)
    if a < 0.0:
        a += 2.0 * math.pi
    return a - math.pi


def yaw_from_forward(fx, fy):
    """部屋座標の前方ベクトル → yaw（+y で 0、反時計回りが正）"""
    return math.atan2(-fx, fy)


def apply_h(H, pts):
    """(N, 2) の点をホモグラフィ H で写す。"""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(pts, H).reshape(-1, 2)


def parallax_correct(p, nadir, camera_height, marker_height):
    """高さ marker_height の点を床面のホモグラフィで写した位置 p を、真上から見た位置に直す。"""
    if marker_height <= 0.0 or camera_height <= marker_height:
        return p
    k = (camera_height - marker_height) / camera_height
    return nadir + (p - nadir) * k


def merged(settings):
    s = dict(DEFAULTS)
    if settings:
        s.update(settings)
    s["refs"] = {int(k): (float(v[0]), float(v[1])) for k, v in s["refs"].items()}
    return s


class Detector:
    """OpenCV 4.7 以降（ArucoDetector）と、それより前（detectMarkers）の両方に対応。"""

    def __init__(self, dict_name="4X4_50"):
        self.dict_name = dict_name
        dictionary = cv2.aruco.getPredefinedDictionary(DICTIONARIES[dict_name])
        if hasattr(cv2.aruco, "ArucoDetector"):
            params = cv2.aruco.DetectorParameters()
            det = cv2.aruco.ArucoDetector(dictionary, params)
            self._detect = det.detectMarkers
        else:
            params = cv2.aruco.DetectorParameters_create()
            self._detect = lambda g: cv2.aruco.detectMarkers(g, dictionary, parameters=params)

    def __call__(self, gray):
        """{id: 角 (4, 2)} を返す。角の順は 左上・右上・右下・左下（マーカーから見て）"""
        corners, ids, _ = self._detect(gray)
        if ids is None:
            return {}
        return {int(i): c.reshape(4, 2) for i, c in zip(ids.flatten(), corners)}


class Tracker:
    def __init__(self, save_path=None):
        self.detector = Detector(DEFAULTS["dictionary"])
        self.save_path = save_path
        self.H = None
        self.H_refs = None        # H を計算したときの基準マーカー座標
        self.image_size = None
        self.last = (0.0, 0.0, 0.0)
        self.markers = {}
        self.settings = merged(None)
        self.nadir = None          # 実際に使ったカメラ真下の点（表示用）
        self._saved_at = -math.inf
        if save_path and os.path.exists(save_path):
            try:
                self.load()
            except (OSError, ValueError, KeyError):
                pass

    # ---- キャリブレーション

    def calibrate(self, markers):
        """基準マーカーが全部見えていればホモグラフィを更新する。更新したら True。"""
        refs = self.settings["refs"]
        if len(refs) < 4 or not all(i in markers for i in refs):
            return False
        img = np.array([markers[i].mean(axis=0) for i in refs], dtype=np.float64)
        room = np.array([refs[i] for i in refs], dtype=np.float64)
        H, _ = cv2.findHomography(img, room)
        if H is None:
            return False
        self.H, self.H_refs = H, dict(refs)
        if self.save_path and time.monotonic() - self._saved_at > 5.0:  # 書き込みは 5 秒に 1 回まで
            self.save()
            self._saved_at = time.monotonic()
        return True

    def reset_calibration(self):
        self.H = None
        self.H_refs = None
        if self.save_path and os.path.exists(self.save_path):
            os.remove(self.save_path)

    def save(self):
        with open(self.save_path, "w") as f:
            json.dump({"H": self.H.tolist(), "image_size": self.image_size,
                       "refs": {str(k): list(v) for k, v in self.H_refs.items()}}, f, indent=1)

    def load(self):
        with open(self.save_path) as f:
            data = json.load(f)
        self.H = np.array(data["H"], dtype=np.float64)
        self.image_size = data.get("image_size")
        self.H_refs = {int(k): (float(v[0]), float(v[1])) for k, v in data.get("refs", {}).items()}

    def room_to_image(self, pts):
        return apply_h(np.linalg.inv(self.H), pts)

    # ---- 毎フレーム

    def process(self, gray, settings=None, auto_calibrate=True):
        """グレースケール画像（上が上の通常の向き）から (x, y, yaw, found) を返す。

        settings は DEFAULTS と同じキーの辞書（一部だけでもよい）。毎回渡してよい。
        見つからないときは最後の位置と found=0 を返す。
        """
        s = self.settings = merged(settings)
        if self.detector.dict_name != s["dictionary"]:
            self.detector = Detector(s["dictionary"])
        if self.H is not None and self.H_refs != s["refs"]:
            self.H = None  # 基準マーカーの座標が変わったら計算し直す
        h, w = gray.shape[:2]
        if self.image_size is not None and list(self.image_size) != [w, h]:
            self.H = None  # 解像度が変わったら計算し直す
        self.image_size = [w, h]

        self.markers = self.detector(gray)
        if auto_calibrate:
            self.calibrate(self.markers)
        if self.H is None:
            self.nadir = None
            return self.last + (0,)
        self.nadir = (np.array(s["nadir"], dtype=np.float64) if s["nadir"] is not None
                      else apply_h(self.H, [[w / 2.0, h / 2.0]])[0])
        if s["robot_id"] not in self.markers:
            return self.last + (0,)
        return self.pose_of(self.markers[s["robot_id"]]) + (1,)

    def pose_of(self, corners):
        s = self.settings
        room = apply_h(self.H, corners)                     # 4 つの角を床面へ
        room = parallax_correct(room, self.nadir, s["camera_height"], s["marker_height"])
        cx, cy = room.mean(axis=0)
        top = (room[0] + room[1]) / 2.0                     # マーカーの上辺の中点
        bottom = (room[3] + room[2]) / 2.0
        fx, fy = top - bottom
        yaw = wrap_angle(yaw_from_forward(fx, fy) + s["yaw_offset"])
        self.last = (float(cx), float(cy), float(yaw))
        return self.last


# ---------------------------------------------------------------- 単体実行（TD なし）

def main():
    import argparse

    from pythonosc.udp_client import SimpleUDPClient

    ap = argparse.ArgumentParser(description="カメラから直接 /dog/pose を送る（TouchDesigner を使わない場合）")
    ap.add_argument("--camera", type=int, default=0)
    ap.add_argument("--host", default="127.0.0.1", help="go2pf を動かす PC の IP")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--calib", default="homography.json")
    ap.add_argument("--camera-height", type=float, default=DEFAULTS["camera_height"])
    ap.add_argument("--marker-height", type=float, default=DEFAULTS["marker_height"])
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    settings = {"camera_height": args.camera_height, "marker_height": args.marker_height}
    cap = cv2.VideoCapture(args.camera)
    client = SimpleUDPClient(args.host, args.port)
    tr = Tracker(args.calib)
    while True:
        ok, frame = cap.read()
        if not ok:
            time.sleep(0.01)
            continue
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        x, y, yaw, found = tr.process(gray, settings)
        client.send_message("/dog/pose", [x, y, yaw, found])
        if args.show:
            for i, c in tr.markers.items():
                cv2.polylines(frame, [c.astype(np.int32)], True, (0, 255, 0), 2)
                cv2.putText(frame, str(i), tuple(c[0].astype(int)), 0, 0.6, (0, 0, 255), 2)
            cv2.putText(frame, f"x={x:+.2f} y={y:+.2f} yaw={math.degrees(yaw):+.0f} found={found} "
                        f"calib={'ok' if tr.H is not None else 'NO'}", (10, 25), 0, 0.6, (0, 0, 255), 2)
            cv2.imshow("aruco", frame)
            if cv2.waitKey(1) == 27:
                break


if __name__ == "__main__":
    main()
