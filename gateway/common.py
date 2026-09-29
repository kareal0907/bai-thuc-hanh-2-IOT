"""Tien ich dung chung: doc cau hinh, kiem tra hop le payload, ket noi InfluxDB."""
from __future__ import annotations

import json
import math
import os
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SENSOR_FIELDS = ("temperature", "humidity", "light_lux", "distance_cm")
INT_FIELDS = ("rssi", "uptime_s")
MAX_PAYLOAD_BYTES = 1024


def load_config(path: str | os.PathLike | None = None) -> dict:
    """Doc config.toml (hoac duong dan trong bien moi truong LAB2_CONFIG)."""
    cfg_path = Path(path or os.environ.get("LAB2_CONFIG", BASE_DIR / "config.toml"))
    if not cfg_path.exists():
        raise SystemExit(
            f"Khong tim thay {cfg_path}. Chay 'python -m gateway.setup_influx' "
            "hoac sao chep config.example.toml thanh config.toml."
        )
    with cfg_path.open("rb") as fh:
        cfg = tomllib.load(fh)
    cfg["_path"] = str(cfg_path)
    return cfg


def resolve_path(relative: str) -> Path:
    path = Path(relative)
    return path if path.is_absolute() else BASE_DIR / path


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class Record:
    """Mot mau cam bien da qua kiem tra hop le."""

    device_id: str
    site: str
    ts: int                 # epoch ms dung lam timestamp cua point
    ts_source: str          # "device" (NTP) hoac "gateway" (ghi de khi lech gio)
    seq: int
    received_ms: int
    values: dict[str, float | None]
    rssi: int | None = None
    uptime_s: int | None = None
    device_ts: int | None = None   # ts goc do thiet bi gui (de tinh do tre)

    @property
    def dedup_key(self) -> tuple:
        return (self.device_id, self.seq, self.device_ts)


@dataclass
class ValidationResult:
    record: Record | None
    issues: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.record is not None


def parse_topic(topic: str, topic_root: str) -> tuple[str, str, str] | None:
    """<root>/<site>/<device>/<kind> -> (site, device, kind)."""
    root = topic_root.strip("/")
    if not topic.startswith(root + "/"):
        return None
    parts = topic[len(root) + 1:].split("/")
    if len(parts) != 3 or not all(parts):
        return None
    return parts[0], parts[1], parts[2]


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_payload(topic: str, payload: bytes, cfg: dict, received_ms: int) -> ValidationResult:
    """Kiem tra topic, JSON, kieu du lieu, dai do va dong ho thiet bi.

    - Loi cau truc (topic sai, JSON hong, thieu device_id/seq, khong co gia tri
      cam bien nao hop le) => loai ca mau.
    - Gia tri ngoai dai do vat ly => bo rieng truong do (luu null), giu mau.
    - ts = 0 hoac lech gio qua nguong => dung gio gateway, danh dau ts_source.
    """
    issues: list[str] = []
    parsed = parse_topic(topic, cfg["mqtt"]["topic_root"])
    if parsed is None or parsed[2] != "telemetry":
        return ValidationResult(None, ["bad_topic"])
    site, topic_device, _ = parsed

    if len(payload) > MAX_PAYLOAD_BYTES:
        return ValidationResult(None, ["payload_too_large"])
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ValidationResult(None, ["bad_json"])
    if not isinstance(data, dict):
        return ValidationResult(None, ["bad_json"])

    device_id = data.get("device_id")
    if device_id != topic_device:
        return ValidationResult(None, ["device_id_mismatch"])
    seq = data.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
        return ValidationResult(None, ["bad_seq"])

    limits = cfg["validation"]
    values: dict[str, float | None] = {}
    for name in SENSOR_FIELDS:
        value = data.get(name)
        if value is None:
            values[name] = None
            if name in data:
                issues.append(f"null:{name}")
            continue
        if not _is_number(value) or not math.isfinite(value):
            values[name] = None
            issues.append(f"bad_type:{name}")
            continue
        lo, hi = limits[name]
        if not lo <= value <= hi:
            values[name] = None
            issues.append(f"out_of_range:{name}")
            continue
        values[name] = float(value)
    if all(v is None for v in values.values()):
        return ValidationResult(None, issues + ["no_valid_value"])

    rssi = data.get("rssi")
    if rssi is not None:
        lo, hi = limits["rssi"]
        if not _is_number(rssi) or not lo <= rssi <= hi:
            rssi = None  # rssi = 0 khi mat Wi-Fi cung roi vao day
        else:
            rssi = int(rssi)
    uptime = data.get("uptime_s")
    uptime = int(uptime) if _is_number(uptime) and uptime >= 0 else None

    device_ts = data.get("ts")
    device_ts = int(device_ts) if _is_number(device_ts) and device_ts > 0 else None
    ts, ts_source = device_ts, "device"
    if device_ts is None:
        ts, ts_source = received_ms, "gateway"
        issues.append("missing_ts")
    elif (device_ts - received_ms > limits["max_future_ms"]
          or received_ms - device_ts > limits["max_past_ms"]):
        # Mau gui bu tu bo dem cua thiet bi hop le du tre vai phut (max_past_ms > 10 phut
        # bo dem); ts o tuong lai nghia la dong ho thiet bi sai.
        ts, ts_source = received_ms, "gateway"
        issues.append("clock_skew")

    record = Record(
        device_id=device_id, site=site, ts=ts, ts_source=ts_source, seq=seq,
        received_ms=received_ms, values=values, rssi=rssi, uptime_s=uptime,
        device_ts=device_ts,
    )
    return ValidationResult(record, issues)


def influx_client(cfg: dict, timeout_ms: int = 10_000):
    import warnings

    from influxdb_client import InfluxDBClient
    from influxdb_client.client.warnings import MissingPivotFunction

    # Truy van count()/sum() co y khong pivot - bo canh bao goi y cua thu vien.
    warnings.simplefilter("ignore", MissingPivotFunction)

    icfg = cfg["influx"]
    return InfluxDBClient(url=icfg["url"], token=icfg["token"], org=icfg["org"], timeout=timeout_ms)
