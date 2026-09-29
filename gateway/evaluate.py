"""Danh gia he thong: do tre end-to-end, chat luong du lieu, hieu nang ghi/doc InfluxDB.

Chay:  python -m gateway.evaluate --start -2h            (thong ke + bieu do)
       python -m gateway.evaluate --start -2h --bench    (them benchmark ghi/doc)
Ket qua: report/results.json va report/figures/*.png
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
from influxdb_client import BucketRetentionRules, InfluxDBClient, Point, WritePrecision  # noqa: E402
from influxdb_client.client.write_api import SYNCHRONOUS  # noqa: E402

from .common import BASE_DIR, SENSOR_FIELDS, influx_client, load_config  # noqa: E402
from .preprocess import flux_time, query_raw  # noqa: E402

REPORT_DIR = BASE_DIR / "report"
FIG_DIR = REPORT_DIR / "figures"


def frame(client, flux: str) -> pd.DataFrame:
    df = client.query_api().query_data_frame(flux)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
    return df


def stats(series: pd.Series) -> dict:
    s = series.dropna()
    if s.empty:
        return {"n": 0}
    return {"n": int(len(s)), "mean": round(float(s.mean()), 1), "std": round(float(s.std()), 1),
            "min": int(s.min()), "p50": round(float(s.quantile(.5)), 1),
            "p95": round(float(s.quantile(.95)), 1), "p99": round(float(s.quantile(.99)), 1),
            "max": int(s.max())}


def latency_section(cfg, client, start, stop) -> dict:
    lat = frame(client, f'''from(bucket: "{cfg['influx']['bucket_ops']}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "pipeline_latency")
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") |> group()''')
    if lat.empty:
        return {}
    result = {}
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.8))
    for dev, part in lat.groupby("device_id"):
        live = part[part["delivery"] == "live"]
        result[dev] = {"live": len(live), "backfill": int((part["delivery"] != "live").sum()),
                       "e2e_ms": stats(live["e2e_ms"]), "ingest_ms": stats(live["ingest_ms"]),
                       "process_ms": stats(live["process_ms"]), "write_ms": stats(live["write_ms"])}
        axes[0].hist(live["e2e_ms"], bins=40, alpha=0.6, label=dev)
        t = pd.to_datetime(live["_time"])
        axes[1].plot(t, live["e2e_ms"], ".", ms=3, label=dev)
    axes[0].set(title="Phân bố độ trễ end-to-end", xlabel="ms", ylabel="Số mẫu")
    axes[1].set(title="Độ trễ end-to-end theo thời gian", ylabel="ms")
    axes[1].tick_params(axis="x", rotation=30)
    for ax in axes:
        ax.legend()
        ax.grid(alpha=.3)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "latency.png", dpi=150)
    plt.close(fig)

    stages = {dev: [r["ingest_ms"].get("mean", 0), r["process_ms"].get("mean", 0), r["write_ms"].get("mean", 0)]
              for dev, r in result.items()}
    fig, ax = plt.subplots(figsize=(8, 2.8))
    labels = ["Thiết bị→broker→gateway", "Xử lý gateway", "Ghi InfluxDB"]
    for i, (dev, vals) in enumerate(stages.items()):
        left = 0
        for j, v in enumerate(vals):
            ax.barh(dev, v, left=left, color=f"C{j}", label=labels[j] if i == 0 else None)
            left += v
    ax.set(xlabel="ms (trung bình)", title="Phân rã độ trễ theo chặng")
    ax.legend(loc="lower right", fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG_DIR / "latency_stages.png", dpi=150)
    plt.close(fig)
    return result


def quality_section(cfg, client, start, stop) -> dict:
    q = frame(client, f'''from(bucket: "{cfg['influx']['bucket_ops']}")
  |> range(start: {flux_time(start)}, stop: {flux_time(stop)})
  |> filter(fn: (r) => r._measurement == "data_quality" and r._field == "count")
  |> group(columns: ["device_id", "kind"]) |> sum() |> group()''')
    raw = query_raw(cfg, start, stop, None)
    result = {}
    for dev, part in raw.groupby("device_id"):
        part = part.sort_values("_time")
        seq = part["seq"].dropna().astype(int)
        # seq co the reset khi thiet bi khoi dong lai => tinh tren doan seq tang dan cuoi cung
        expected = int(seq.max() - seq.min() + 1) if (seq.diff().dropna() > 0).all() else None
        kinds = {}
        if not q.empty:
            kinds = {r["kind"]: int(r["_value"]) for _, r in q[q["device_id"] == dev].iterrows()}
        result[dev] = {"stored_samples": int(len(part)), "expected_by_seq": expected,
                       "delivery_ratio": round(len(part) / expected, 4) if expected else None,
                       "null_values": {f: int(part[f].isna().sum()) for f in SENSOR_FIELDS if f in part},
                       "events": kinds}
    return result


def raw_figure(cfg, start, stop) -> None:
    raw = query_raw(cfg, start, stop, None)
    procs = {}
    for dev in raw["device_id"].unique():
        p = BASE_DIR / "data" / f"processed_{dev}.csv"
        if p.exists():
            procs[dev] = pd.read_csv(p, parse_dates=["_time"])
    for dev, part in raw.groupby("device_id"):
        fields = [f for f in SENSOR_FIELDS if f in part]
        fig, axes = plt.subplots(len(fields), 1, figsize=(11, 2.1 * len(fields)), sharex=True)
        for ax, name in zip(axes, fields):
            ax.plot(part["_time"], part[name], ".", ms=2.5, color="#9aa5b1", label="Thô")
            if dev in procs and name in procs[dev]:
                pr = procs[dev]
                ax.plot(pr["_time"], pr[name], "-", lw=1.8, label="Đã làm sạch (TB 1 phút)")
                ax.plot(pr["_time"], pr[f"{name}_rm"], "--", lw=1.4, label="Rolling mean")
            ax.set_ylabel(name, fontsize=8)
            ax.grid(alpha=.3)
        axes[0].legend(fontsize=7, loc="upper right", ncol=3)
        axes[0].set_title(f"Dữ liệu thô và đã tiền xử lý - {dev}")
        fig.tight_layout()
        fig.savefig(FIG_DIR / f"raw_vs_processed_{dev}.png", dpi=150)
        plt.close(fig)


def benchmark(cfg) -> dict:
    """Ghi/doc tren bucket tam (can operator token trong data/influx_admin.json)."""
    admin = json.loads((BASE_DIR / "data" / "influx_admin.json").read_text(encoding="utf-8"))
    url, org = cfg["influx"]["url"], cfg["influx"]["org"]
    result = {}
    with InfluxDBClient(url=url, token=admin["operator_token"], org=org, timeout=60_000) as client:
        buckets = client.buckets_api()
        old = buckets.find_bucket_by_name("bench_tmp")
        if old:
            buckets.delete_bucket(old)
        bucket = buckets.create_bucket(bucket_name="bench_tmp", org=org,
                                       retention_rules=BucketRetentionRules(type="expire", every_seconds=3600))
        write = client.write_api(write_options=SYNCHRONOUS)
        base = time.time_ns() // 1_000_000 - 3_000_000

        def pt(i: int, dev: str) -> Point:
            return (Point("sensor_raw").tag("device_id", dev).tag("site", "bench")
                    .field("temperature", 25 + i % 10 * .1).field("humidity", 50 + i % 7 * .3)
                    .field("light_lux", 400.0 + i % 50).field("distance_cm", 100.0 + i % 3)
                    .field("seq", i).time(base + i * 10, WritePrecision.MS))

        n = 300
        t0 = time.perf_counter()
        for i in range(n):
            write.write("bench_tmp", record=pt(i, "single"))
        dt = time.perf_counter() - t0
        result["single_point_write"] = {"points": n, "seconds": round(dt, 3), "points_per_s": round(n / dt, 1),
                                        "ms_per_write": round(dt / n * 1000, 2)}
        for batch in (100, 1000, 5000):
            n = 50_000
            pts = [pt(i, f"batch{batch}") for i in range(n)]
            t0 = time.perf_counter()
            for i in range(0, n, batch):
                write.write("bench_tmp", record=pts[i:i + batch])
            dt = time.perf_counter() - t0
            result[f"batch_{batch}_write"] = {"points": n, "seconds": round(dt, 3),
                                              "points_per_s": round(n / dt, 1)}
        q = client.query_api()
        for label, flux in {
            "query_raw_1_device_50k": 'from(bucket:"bench_tmp") |> range(start:-2h) '
                                      '|> filter(fn:(r)=>r.device_id=="batch1000" and r._field=="temperature")',
            "query_mean_1m_window": 'from(bucket:"bench_tmp") |> range(start:-2h) '
                                    '|> filter(fn:(r)=>r.device_id=="batch1000") '
                                    '|> aggregateWindow(every:1m, fn:mean, createEmpty:false)',
            "query_pivot_5_fields": 'from(bucket:"bench_tmp") |> range(start:-2h) '
                                    '|> filter(fn:(r)=>r.device_id=="batch1000") '
                                    '|> pivot(rowKey:["_time"], columnKey:["_field"], valueColumn:"_value")',
        }.items():
            times = []
            for _ in range(3):
                t0 = time.perf_counter()
                tables = q.query(flux)
                times.append(time.perf_counter() - t0)
            rows = sum(len(t.records) for t in tables)
            result[label] = {"rows": rows, "best_ms": round(min(times) * 1000, 1)}
        # Dung luong sau nen: cho cache duoc snapshot thanh file TSM (can influxd chay voi
        # --storage-cache-snapshot-write-cold-duration ngan, xem README), toi da 90 s.
        cnt = frame(client, 'from(bucket:"bench_tmp") |> range(start:-2h) |> group() |> count()')
        values = int(cnt["_value"].iloc[0]) if not cnt.empty else 0
        shard_dir = BASE_DIR / "data" / "influxdb" / "engine" / "data" / bucket.id
        tsm = 0
        for _ in range(90):
            tsm = sum(p.stat().st_size for p in shard_dir.rglob("*.tsm")) if shard_dir.exists() else 0
            if tsm:
                time.sleep(3)  # cho snapshot ghi xong
                tsm = sum(p.stat().st_size for p in shard_dir.rglob("*.tsm"))
                break
            time.sleep(1)
        result["bench_values"] = values
        result["bench_tsm_bytes"] = tsm
        result["bench_bytes_per_value"] = round(tsm / values, 3) if tsm and values else None
        buckets.delete_bucket(bucket)
    return result


def broker_rtt(cfg, samples: int = 20) -> dict:
    """RTT mang toi broker, uoc luong bang thoi gian bat tay TCP (SYN -> SYN/ACK)."""
    import socket

    host, port = cfg["mqtt"]["host"], int(cfg["mqtt"]["port"])
    addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
    times = []
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            with socket.create_connection(addr, timeout=5):
                times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            pass
        time.sleep(0.2)
    return {"host": host, "ip": addr[0], **stats(pd.Series(times))}


def storage_section(cfg) -> dict:
    engine = BASE_DIR / "data" / "influxdb" / "engine"

    def size(path: Path, suffixes: tuple = ()) -> int:
        if not path.exists():
            return 0
        return sum(p.stat().st_size for p in path.rglob("*")
                   if p.is_file() and (not suffixes or p.suffix in suffixes))

    admin_file = BASE_DIR / "data" / "influx_admin.json"
    result = {"engine_total_bytes": size(engine)}
    if admin_file.exists():
        admin = json.loads(admin_file.read_text(encoding="utf-8"))
        with InfluxDBClient(url=cfg["influx"]["url"], token=admin["operator_token"], org=cfg["influx"]["org"]) as c:
            for key in ("bucket_raw", "bucket_processed", "bucket_ops"):
                b = c.buckets_api().find_bucket_by_name(cfg["influx"][key])
                if b is None:
                    continue
                cnt = frame(c, f'from(bucket:"{b.name}") |> range(start: -365d) |> group() |> count()')
                values = int(cnt["_value"].iloc[0]) if not cnt.empty else 0
                # Du lieu that = file TSM (da nen) + WAL (chua nen). Thu muc _series/index duoc cap phat
                # truoc (~32 MB/bucket) nen khong dung de tinh byte/gia tri.
                tsm = size(engine / "data" / b.id, (".tsm",))
                wal = size(engine / "wal" / b.id, (".wal",))
                total = size(engine / "data" / b.id) + size(engine / "wal" / b.id)
                result[b.name] = {"retention_days": b.retention_rules[0].every_seconds // 86400
                                  if b.retention_rules else None,
                                  "field_values": values, "tsm_bytes": tsm, "wal_bytes": wal,
                                  "disk_bytes": total, "data_bytes": tsm + wal,
                                  "bytes_per_value": round((tsm + wal) / values, 2) if values else None}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config")
    parser.add_argument("--start", default="-2h")
    parser.add_argument("--stop", default="now()")
    parser.add_argument("--bench", action="store_true")
    args = parser.parse_args()
    cfg = load_config(args.config)
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    out_file = REPORT_DIR / "results.json"
    results = json.loads(out_file.read_text(encoding="utf-8")) if out_file.exists() else {}
    with influx_client(cfg, timeout_ms=60_000) as client:
        results["latency"] = latency_section(cfg, client, args.start, args.stop)
        results["quality"] = quality_section(cfg, client, args.start, args.stop)
    raw_figure(cfg, args.start, args.stop)
    results["storage"] = storage_section(cfg)
    results["broker_rtt_ms"] = broker_rtt(cfg)
    if args.bench:
        results["benchmark"] = benchmark(cfg)
    results["window"] = {"start": args.start, "stop": args.stop,
                         "generated": time.strftime("%Y-%m-%d %H:%M:%S")}
    out_file.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
