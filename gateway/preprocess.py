"""Tien xu ly du lieu IoT: doc InfluxDB -> lam sach -> outlier -> resampling -> dac trung -> chuan hoa.

Cac buoc (moi thiet bi xu ly rieng):
  1. Doc sensor_raw theo khoang thoi gian, loai ban ghi trung timestamp.
  2. Dua ve luoi deu theo chu ky gui (5 s) de lo ro cac mau bi thieu.
  3. Phat hien outlier (IQR hoac Z-score truot) tren cac truong cau hinh => dat NaN.
  4. Xu ly thieu: noi suy theo thoi gian cho khoang trong ngan (<= max_interp_gap mau);
     khoang trong dai giu NaN (khong bia du lieu), the hien qua completeness.
  5. Resample theo cua so (mac dinh 1 phut): trung binh + so mau.
  6. Dac trung: rolling mean, delta; chuan hoa Z-score (StandardScaler) va Min-Max.
  7. Ghi vao bucket_processed / measurement sensor_processed (+ outlier_events), xuat CSV.

Chay:  python -m gateway.preprocess --start -2h
       python -m gateway.preprocess --start -30m --loop 60      (xu ly lien tuc moi 60 s)
"""
from __future__ import annotations

import argparse
import json
import logging
import time

import numpy as np
import pandas as pd
from influxdb_client import Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from sklearn.preprocessing import MinMaxScaler, StandardScaler

from .common import BASE_DIR, SENSOR_FIELDS, influx_client, load_config

log = logging.getLogger("preprocess")
DATA_DIR = BASE_DIR / "data"


# ---------------- Doc du lieu ----------------
def flux_time(value: str) -> str:
    """'-2h' / 'now()' giu nguyen, RFC3339 bao bang time(v: ...)."""
    if value.startswith("-") or value.endswith("()"):
        return value
    return f'time(v: "{value}")'


def query_raw(cfg: dict, start: str, stop: str, device: str | None) -> pd.DataFrame:
    device_filter = f'|> filter(fn: (r) => r.device_id == "{device}")' if device else ""
    fields = "|".join(SENSOR_FIELDS + ("seq",))
    flux = f'''
from(bucket: "{cfg['influx']['bucket_raw']}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "sensor_raw")
  {device_filter}
  |> filter(fn: (r) => r._field =~ /^({fields})$/)
  |> drop(columns: ["ts_source"])
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> group()
'''
    with influx_client(cfg, timeout_ms=60_000) as client:
        frames = client.query_api().query_data_frame(flux)
    if isinstance(frames, list):
        frames = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if frames.empty:
        return frames
    keep = ["_time", "device_id", "site"] + [f for f in SENSOR_FIELDS + ("seq",) if f in frames.columns]
    frames = frames[keep].copy()
    frames["_time"] = pd.to_datetime(frames["_time"], utc=True)
    return frames


# ---------------- Outlier ----------------
def iqr_mask(series: pd.Series, k: float, min_spread: float) -> pd.Series:
    q1, q3 = series.quantile(0.25), series.quantile(0.75)
    spread = max(q3 - q1, min_spread)  # du lieu gan nhu hang so (Wokwi) => IQR ~ 0
    return (series < q1 - k * spread) | (series > q3 + k * spread)


def zscore_mask(series: pd.Series, threshold: float, window: int, min_spread: float) -> pd.Series:
    roll = series.rolling(window, center=True, min_periods=max(5, window // 4))
    mean, std = roll.mean(), roll.std().clip(lower=min_spread)
    return ((series - mean) / std).abs() > threshold


def detect_outliers(grid: pd.DataFrame, pcfg: dict, method: str) -> pd.DataFrame:
    mask = pd.DataFrame(False, index=grid.index, columns=grid.columns)
    for name in pcfg["outlier_fields"]:
        if name not in grid or grid[name].notna().sum() < 10:
            continue
        s = grid[name]
        spread = float(pcfg["min_spread"][name])
        if method == "iqr":
            m = iqr_mask(s.dropna(), pcfg["iqr_k"], spread)
        else:
            m = zscore_mask(s.dropna(), pcfg["z_threshold"], pcfg["z_window"], spread)
        mask.loc[m.index, name] = m
    return mask


# ---------------- Gia tri thieu ----------------
def fill_short_gaps(df: pd.DataFrame, max_gap: int) -> pd.DataFrame:
    """Noi suy tuyen tinh theo thoi gian chi cho cac chuoi NaN dai <= max_gap mau.

    (interpolate(limit=...) cua pandas van dien max_gap mau dau cua khoang trong dai,
    nen phai tu tinh do dai tung chuoi NaN.)
    """
    filled = df.interpolate(method="time", limit_area="inside")
    for col in df:
        na = df[col].isna()
        run_len = na.groupby((na != na.shift()).cumsum()).transform("sum")
        filled.loc[na & (run_len > max_gap), col] = np.nan
    return filled


# ---------------- Pipeline ----------------
def preprocess_device(raw: pd.DataFrame, cfg: dict, method: str) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    pcfg = cfg["preprocess"]
    interval = f"{pcfg['expected_interval_s']}s"
    fields = [f for f in SENSOR_FIELDS if f in raw.columns]
    df = raw.set_index("_time").sort_index()
    report: dict = {"rows_raw": int(len(df))}

    dup = df.index.duplicated(keep="last")
    df = df[~dup]
    report["duplicate_timestamps"] = int(dup.sum())
    if "seq" in df:
        seq = df["seq"].dropna().astype(int)
        report["seq_first"], report["seq_last"] = int(seq.iloc[0]), int(seq.iloc[-1])

    # Kiem tra lai dai vat ly (phong khi du lieu duoc ghi truc tiep, khong qua collector).
    for name in fields:
        lo, hi = cfg["validation"][name]
        df.loc[(df[name] < lo) | (df[name] > hi), name] = np.nan

    # (2) Luoi deu 5 s: moi o khong co mau => NaN = gia tri thieu.
    grid = df[fields].astype(float).resample(interval).mean()
    missing_before = grid.isna().sum()
    report["grid_slots"] = int(len(grid))
    report["missing_before"] = {k: int(v) for k, v in missing_before.items()}

    # (3) Outlier: ghi nhan ca hai phuong phap de so sanh, ap dung phuong phap duoc chon.
    masks = {m: detect_outliers(grid, pcfg, m) for m in ("iqr", "zscore")}
    report["outliers_iqr"] = {k: int(v) for k, v in masks["iqr"].sum().items()}
    report["outliers_zscore"] = {k: int(v) for k, v in masks["zscore"].sum().items()}
    mask = masks[method]
    outlier_rows = [(ts, name, float(grid.at[ts, name]))
                    for name in fields for ts in grid.index[mask[name]]]
    outliers = pd.DataFrame(outlier_rows, columns=["_time", "field", "value"])
    clean = grid.mask(mask)

    # (4) Noi suy khoang trong ngan; khoang trong dai giu NaN.
    filled = fill_short_gaps(clean, int(pcfg["max_interp_gap"]))
    imputed = filled.notna() & clean.isna()
    report["imputed"] = {k: int(v) for k, v in imputed.sum().items()}
    report["missing_after"] = {k: int(v) for k, v in filled.isna().sum().items()}

    # (5) Resample theo cua so thoi gian.
    window = pcfg["resample"]
    out = filled.resample(window, label="left").mean()
    expected = pd.Timedelta(window) / pd.Timedelta(interval)
    out["n_raw"] = grid.notna().any(axis=1).resample(window, label="left").sum()
    out["n_outliers"] = mask.any(axis=1).resample(window, label="left").sum()
    out["n_imputed"] = imputed.any(axis=1).resample(window, label="left").sum()
    out["completeness"] = (out["n_raw"] / expected).clip(upper=1.0).round(3)

    # (6) Dac trung + chuan hoa (scaler bo qua NaN khi fit va giu NaN khi transform).
    rw = int(pcfg["rolling_windows"])
    std_scaler, mm_scaler = StandardScaler(), MinMaxScaler()
    z = std_scaler.fit_transform(out[fields])
    mm = mm_scaler.fit_transform(out[fields])
    for i, name in enumerate(fields):
        out[f"{name}_rm"] = out[name].rolling(rw, min_periods=1).mean()
        out[f"{name}_delta"] = out[name].diff()
        out[f"{name}_z"] = z[:, i]
        out[f"{name}_mm"] = mm[:, i]
    report["scaler"] = {
        name: {"mean": _num(std_scaler.mean_[i]), "std": _num(std_scaler.scale_[i]),
               "min": _num(mm_scaler.data_min_[i]), "max": _num(mm_scaler.data_max_[i])}
        for i, name in enumerate(fields)
    }
    out = out[out["n_raw"] > 0]
    report["rows_processed"] = int(len(out))
    return out, outliers, report


def _num(value) -> float | None:
    return None if value is None or not np.isfinite(value) else round(float(value), 4)


# ---------------- Ghi ket qua ----------------
def to_points(device: str, site: str, out: pd.DataFrame, outliers: pd.DataFrame, cfg: dict, method: str):
    window = cfg["preprocess"]["resample"]
    for ts, row in out.iterrows():
        point = (Point("sensor_processed").tag("device_id", device).tag("site", site)
                 .tag("window", window).tag("method", method).time(ts.to_pydatetime(), WritePrecision.S))
        for col, value in row.items():
            if pd.isna(value):
                continue
            point.field(col, int(value) if col.startswith("n_") else float(value))
        yield point
    for _, row in outliers.iterrows():
        yield (Point("outlier_events").tag("device_id", device).tag("field", row["field"])
               .tag("method", method).field("value", float(row["value"]))
               .time(row["_time"].to_pydatetime(), WritePrecision.S))


def run_once(cfg: dict, start: str, stop: str, device: str | None, method: str, write: bool) -> dict:
    raw = query_raw(cfg, start, stop, device)
    if raw.empty:
        log.warning("Khong co du lieu sensor_raw trong khoang %s -> %s", start, stop)
        return {}
    DATA_DIR.mkdir(exist_ok=True)
    summary = {}
    with influx_client(cfg, timeout_ms=60_000) as client:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        for dev, part in raw.groupby("device_id"):
            site = str(part["site"].iloc[0])
            out, outliers, report = preprocess_device(part, cfg, method)
            report.update({"device_id": dev, "method": method, "start": str(part["_time"].min()),
                           "stop": str(part["_time"].max()), "outlier_events": int(len(outliers))})
            out.to_csv(DATA_DIR / f"processed_{dev}.csv", float_format="%.4f")
            if write:
                points = list(to_points(dev, site, out, outliers, cfg, method))
                write_api.write(cfg["influx"]["bucket_processed"], record=points)
                report["points_written"] = len(points)
            summary[dev] = report
            log.info("%s: raw=%d -> %d windows | outliers(%s)=%d | imputed=%s | missing_after=%s",
                     dev, report["rows_raw"], report["rows_processed"], method, len(outliers),
                     report["imputed"], report["missing_after"])
    (DATA_DIR / "preprocess_report.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config")
    parser.add_argument("--start", default="-1h", help="vd. -2h, -30m hoac 2026-09-29T08:00:00Z")
    parser.add_argument("--stop", default="now()")
    parser.add_argument("--device", help="chi xu ly mot device_id")
    parser.add_argument("--method", choices=["iqr", "zscore"], help="ghi de outlier_method trong config")
    parser.add_argument("--no-write", action="store_true", help="chi xuat CSV/bao cao, khong ghi InfluxDB")
    parser.add_argument("--loop", type=float, default=0, help="chay lap lai moi N giay")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    cfg = load_config(args.config)
    method = args.method or cfg["preprocess"]["outlier_method"]
    while True:
        run_once(cfg, args.start, args.stop, args.device, method, not args.no_write)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
