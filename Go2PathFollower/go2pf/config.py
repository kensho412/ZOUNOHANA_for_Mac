"""YAML 設定の読み込み。未知のキーはエラーにする（タイプミスで既定値が使われるのを防ぐ）。"""
from __future__ import annotations

import copy
import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Union

from .geometry import Geofence, Waypoint

# 設定ファイルで上書きできない絶対上限（設定ミス対策）
HARD_VX_MAX = 1.0      # m/s
HARD_VY_MAX = 0.5      # m/s
HARD_VYAW_MAX = 2.0    # rad/s


@dataclass
class OscConfig:
    host: str = "0.0.0.0"
    port: int = 9000
    address: str = "/dog/pose"


@dataclass
class ControlConfig:
    rate_hz: float = 30.0
    lookahead: float = 0.5
    speed: float = 0.3
    speed_max: float = 0.6
    k_yaw: float = 1.5
    vyaw_max: float = 1.0
    k_lateral: float = 0.0
    vy_max: float = 0.2
    accel_vx: float = 0.5
    accel_vy: float = 0.5
    accel_vyaw: float = 2.0
    goal_radius: float = 0.25
    loop: bool = False
    search_back: float = 0.2
    search_ahead: float = 1.0


@dataclass
class GeofenceConfig:
    x_min: float = -2.3
    x_max: float = 2.3
    y_min: float = -2.3
    y_max: float = 2.3

    def to_geofence(self) -> Geofence:
        return Geofence(self.x_min, self.x_max, self.y_min, self.y_max)


@dataclass
class SafetyConfig:
    pose_timeout: float = 0.5
    resume_after_tracking_loss: bool = True
    geofence: GeofenceConfig = field(default_factory=GeofenceConfig)
    geofence_horizon: float = 0.5


@dataclass
class Go2Config:
    interface: str = "eth0"
    domain_id: int = 0
    rpc_timeout: float = 1.0
    check_motion_mode: bool = True


@dataclass
class MockConfig:
    pose_rate_hz: float = 30.0
    physics_rate_hz: float = 200.0
    velocity_tau: float = 0.15
    noise_pos: float = 0.01
    noise_yaw: float = 0.02
    latency: float = 0.05
    dropout_per_s: float = 0.0
    dropout_duration: float = 0.3
    dropout_mode: str = "found0"   # found0: found=0 を送る / silent: 何も送らない
    start: Union[Dict[str, float], None] = None  # {"x", "y", "yaw"}。省略時はルートの始点
    seed: Union[int, None] = None


@dataclass
class RemoteConfig:
    """TouchDesigner などからの操作（/go2pf/*）と、状態の送り返し。"""
    enabled: bool = True
    status_port: int = 9001        # 状態を送り返す先のポート（送り先の IP は操作元）
    status_rate_hz: float = 15.0
    allow_hosts: List[str] = field(default_factory=list)  # 空なら、最初に /go2pf/config を送ってきたホストだけ
    controller_timeout: float = 0.0  # 操作元から この秒数なにも届かなければ終了（0 なら無効）


@dataclass
class LogConfig:
    enabled: bool = True
    dir: str = "logs"


@dataclass
class VizConfig:
    enabled: bool = True
    fps: float = 15.0
    trail: int = 600


@dataclass
class Config:
    osc: OscConfig = field(default_factory=OscConfig)
    control: ControlConfig = field(default_factory=ControlConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    go2: Go2Config = field(default_factory=Go2Config)
    mock: MockConfig = field(default_factory=MockConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    log: LogConfig = field(default_factory=LogConfig)
    viz: VizConfig = field(default_factory=VizConfig)

    def validate(self) -> None:
        c, s = self.control, self.safety
        positive = {
            "control.rate_hz": c.rate_hz, "control.lookahead": c.lookahead,
            "control.speed_max": c.speed_max, "control.vyaw_max": c.vyaw_max,
            "control.accel_vx": c.accel_vx, "control.accel_vy": c.accel_vy,
            "control.accel_vyaw": c.accel_vyaw, "control.goal_radius": c.goal_radius,
            "safety.pose_timeout": s.pose_timeout,
        }
        for k, v in positive.items():
            if not v > 0:
                raise ValueError(f"{k} は正の値にしてください（{v}）")
        if not 10.0 <= c.rate_hz <= 100.0:
            raise ValueError("control.rate_hz は 10〜100 Hz の範囲にしてください")
        if c.speed_max > HARD_VX_MAX:
            raise ValueError(f"control.speed_max は {HARD_VX_MAX} m/s 以下にしてください")
        if c.vy_max > HARD_VY_MAX or c.vy_max < 0:
            raise ValueError(f"control.vy_max は 0〜{HARD_VY_MAX} m/s にしてください")
        if c.vyaw_max > HARD_VYAW_MAX:
            raise ValueError(f"control.vyaw_max は {HARD_VYAW_MAX} rad/s 以下にしてください")
        if not 0 <= c.speed <= c.speed_max:
            raise ValueError("control.speed は 0〜speed_max にしてください")
        g = s.geofence
        if not (g.x_min < g.x_max and g.y_min < g.y_max):
            raise ValueError("safety.geofence の min/max が不正です")
        for k, v in {"safety.geofence_horizon": s.geofence_horizon, "control.search_back": c.search_back,
                     "control.search_ahead": c.search_ahead, "control.k_lateral": c.k_lateral}.items():
            if v < 0:
                raise ValueError(f"{k} は 0 以上にしてください（{v}）")
        if self.mock.dropout_mode not in ("found0", "silent"):
            raise ValueError("mock.dropout_mode は found0 か silent にしてください")


def _from_dict(cls, data: Dict[str, Any], prefix: str = ""):
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"{prefix or '設定'} は辞書である必要があります")
    fields = {f.name: f for f in dataclasses.fields(cls)}
    kwargs = {}
    for key, value in data.items():
        if key not in fields:
            raise ValueError(f"未知の設定キー: {prefix}{key}")
        default = fields[key].default_factory() if fields[key].default_factory is not dataclasses.MISSING else None
        if dataclasses.is_dataclass(default):
            kwargs[key] = _from_dict(type(default), value, f"{prefix}{key}.")
        else:
            kwargs[key] = value
    return cls(**kwargs)


# 外部（TouchDesigner）から実行中に変えてよいセクション
REMOTE_SECTIONS = ("control", "safety")


def merge_remote(cfg: Config, data: Dict[str, Any]) -> Config:
    """cfg のコピーに data（{"control": {...}, "safety": {...}}）を重ねて検証したものを返す。

    不正なら ValueError。元の cfg は変えない。
    """
    new = copy.deepcopy(cfg)
    for section, values in data.items():
        if section not in REMOTE_SECTIONS:
            raise ValueError(f"外部から変更できないセクション: {section}")
        if not isinstance(values, dict):
            raise ValueError(f"{section} は辞書である必要があります")
        current = dataclasses.asdict(getattr(new, section))
        _deep_update(current, values, section + ".")
        setattr(new, section, _from_dict(type(getattr(new, section)), current, section + "."))
    new.validate()
    return new


def _deep_update(dst: Dict[str, Any], src: Dict[str, Any], prefix: str) -> None:
    for k, v in src.items():
        if k not in dst:
            raise ValueError(f"未知の設定キー: {prefix}{k}")
        if isinstance(dst[k], dict) and isinstance(v, dict):
            _deep_update(dst[k], v, f"{prefix}{k}.")
        else:
            dst[k] = v


def parse_route_points(points: Any, speed_max: float) -> List[Waypoint]:
    """[{x, y, speed?}, ...] を Waypoint のリストにする。"""
    if not isinstance(points, list):
        raise ValueError("ルートの points はリストである必要があります")
    out = []
    for i, p in enumerate(points):
        try:
            x, y = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            raise ValueError(f"ルートの {i} 番目の点に x, y がありません: {p!r}")
        speed = p.get("speed")
        if speed is not None and speed != "":
            speed = float(speed)
            if not 0 <= speed <= speed_max:
                raise ValueError(f"ルートの {i} 番目の speed={speed} が 0〜speed_max({speed_max}) の範囲外です")
        else:
            speed = None
        out.append(Waypoint(x, y, speed))
    return out


def load_config(path: Union[str, Path, None]) -> Config:
    if path is None:
        cfg = Config()
    else:
        import yaml
        with open(path, encoding="utf-8") as f:
            cfg = _from_dict(Config, yaml.safe_load(f) or {})
    cfg.validate()
    return cfg


def load_route(path: Union[str, Path], speed_max: float) -> List[Waypoint]:
    """ルート JSON を読む。[{x, y, speed?}, ...] または {"points": [...]} を受け付ける。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return parse_route_points(data["points"] if isinstance(data, dict) else data, speed_max)
