"""matplotlib による簡易可視化。メインスレッドで動かす。

日本語フォントがない環境でも文字化けしないよう、図中の表記は英語にしている。
"""
from __future__ import annotations

import math
import threading
from collections import deque
from typing import Callable

from .geometry import Polyline, forward_vector
from .supervisor import State, Supervisor

STATE_COLORS = {
    State.DISARMED: "gray", State.RUNNING: "tab:green", State.HOLD: "tab:orange",
    State.FINISHING: "tab:blue", State.FINISHED: "tab:blue",
    State.ESTOP: "tab:red", State.FAULT: "tab:red",
}


def run_viz(sup: Supervisor, stop_event: threading.Event,
            on_key: Callable[[str], None], fps: float, trail_len: int) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.patches import Rectangle

    fence = sup.fence
    fig, ax = plt.subplots(figsize=(7, 7))
    try:
        fig.canvas.manager.set_window_title("Go2 path follower")
    except Exception:
        pass
    ax.set_aspect("equal")
    m = 0.3
    ax.set_xlim(fence.x_min - m, fence.x_max + m)
    ax.set_ylim(fence.y_min - m, fence.y_max + m)
    ax.grid(True, alpha=0.3)
    ax.set_xlabel("x [m] (right +)")
    ax.set_ylabel("y [m] (front +)")
    ax.add_patch(Rectangle((fence.x_min, fence.y_min), fence.x_max - fence.x_min,
                           fence.y_max - fence.y_min, fill=False, ec="tab:red", ls="--", lw=1))
    route_artists = []
    drawn = [None]

    def draw_route(path: Polyline) -> None:
        # TouchDesigner からルートが変わることがあるので、変わったら描き直す
        for a in route_artists:
            a.remove()
        route_artists.clear()
        xs = [p.x for p in path.points]
        ys = [p.y for p in path.points]
        route_artists.extend(ax.plot(xs, ys, "-o", color="0.6", ms=4, lw=1.5, zorder=1))
        route_artists.extend(ax.plot(xs[0], ys[0], "s", color="0.3", ms=8, zorder=2))
        for p in path.points[:-1]:
            if p.speed is not None:
                route_artists.append(ax.annotate(f"{p.speed:.2f}", (p.x, p.y), textcoords="offset points",
                                                 xytext=(5, 5), fontsize=7, color="0.4"))

    trail = deque(maxlen=trail_len)
    trail_line, = ax.plot([], [], "-", color="tab:green", lw=1, alpha=0.6, zorder=3)
    body, = ax.plot([], [], "o", ms=12, zorder=5)
    heading, = ax.plot([], [], "-", lw=3, zorder=6)
    target, = ax.plot([], [], "x", color="tab:purple", ms=10, mew=2, zorder=4)
    look, = ax.plot([], [], ":", color="tab:purple", lw=1, zorder=4)
    text = ax.text(0.01, 0.99, "", transform=ax.transAxes, va="top", family="monospace", fontsize=8)
    ax.set_title("a: arm   space: E-STOP   q: quit", fontsize=9)

    def update(_):
        if stop_event.is_set():
            plt.close(fig)
            return ()
        if sup.path is not drawn[0] and sup.path is not None:
            draw_route(sup.path)
            drawn[0] = sup.path
        s = sup.last
        col = STATE_COLORS.get(s.state, "k")
        if s.pose is not None:
            p = s.pose
            trail.append((p.x, p.y))
            fx, fy = forward_vector(p.yaw)
            body.set_data([p.x], [p.y])
            body.set_color(col if p.found else "0.7")
            heading.set_data([p.x, p.x + 0.35 * fx], [p.y, p.y + 0.35 * fy])
            heading.set_color(col)
            trail_line.set_data(*zip(*trail))
            if s.target is not None:
                target.set_data([s.target[0]], [s.target[1]])
                look.set_data([p.x, s.target[0]], [p.y, s.target[1]])
        pose_txt = ("x=%+.2f y=%+.2f yaw=%+.0f° found=%d age=%.2fs"
                    % (s.pose.x, s.pose.y, math.degrees(s.pose.yaw), s.pose.found, s.pose_age)
                    if s.pose else "pose: (none)")
        text.set_text(f"{s.state.value}\n{pose_txt}\n"
                      f"cmd vx={s.cmd.vx:+.2f} vy={s.cmd.vy:+.2f} vyaw={s.cmd.vyaw:+.2f}")
        text.set_color(col)
        return ()

    def key(ev):
        if ev.key is None:
            return
        k = " " if ev.key == "space" else ev.key
        on_key(k)

    fig.canvas.mpl_connect("key_press_event", key)
    fig.canvas.mpl_connect("close_event", lambda ev: stop_event.set())
    anim = FuncAnimation(fig, update, interval=1000.0 / fps, cache_frame_data=False)
    plt.show()
    del anim  # plt.show() の間はアニメーションを保持しておく必要がある
