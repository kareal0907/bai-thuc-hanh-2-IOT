"""Gateway thu thap: MQTT subscribe -> kiem tra hop le -> loai trung -> InfluxDB.

Chay:  python -m gateway.collector [--duration 600] [--clean-session]

- Du lieu hop le  -> bucket_raw / measurement sensor_raw
- Do tre tung mau -> bucket_ops / measurement pipeline_latency
- Su co chat luong (JSON hong, ngoai dai do, trung, mat goi...) -> bucket_ops / data_quality
- Trang thai online/offline cua thiet bi -> bucket_ops / device_status
- InfluxDB loi => ghi tam vao spool.jsonl, tu ghi bu khi InfluxDB hoat dong lai.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import queue
import signal
import socket
import statistics
import threading
import time
from collections import Counter, OrderedDict, deque

import paho.mqtt.client as mqtt
from influxdb_client import Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

from .common import (Record, influx_client, load_config, now_ms, parse_topic,
                     resolve_path, validate_payload)

log = logging.getLogger("collector")
LIVE_THRESHOLD_MS = 10_000      # ingest > 10 s => mau gui bu tu bo dem (backfill)
OPS_FLUSH_S = 2.0
HEALTH_CHECK_S = 10.0


def record_to_point(rec: Record) -> Point:
    point = (Point("sensor_raw")
             .tag("device_id", rec.device_id)
             .tag("site", rec.site)
             .tag("ts_source", rec.ts_source)
             .field("seq", int(rec.seq))
             .time(rec.ts, WritePrecision.MS))
    for name, value in rec.values.items():
        if value is not None:
            point.field(name, float(value))
    if rec.rssi is not None:
        point.field("rssi", int(rec.rssi))
    if rec.uptime_s is not None:
        point.field("uptime_s", int(rec.uptime_s))
    return point


def percentile(values, q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    idx = min(len(ordered) - 1, max(0, round(q / 100 * (len(ordered) - 1))))
    return ordered[idx]


class Collector:
    def __init__(self, cfg: dict, clean_session: bool = False):
        self.cfg = cfg
        self.qos = int(cfg["mqtt"].get("qos", 1))
        self.root = cfg["mqtt"]["topic_root"].strip("/")
        self.bucket_raw = cfg["influx"]["bucket_raw"]
        self.bucket_ops = cfg["influx"]["bucket_ops"]
        self.spool_path = resolve_path(cfg["collector"]["spool_file"])
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self.dedup_size = int(cfg["collector"]["dedup_cache"])
        self.stats_interval = float(cfg["collector"]["stats_interval_s"])

        self.inbox: queue.Queue = queue.Queue(maxsize=20_000)
        self.stop_event = threading.Event()
        self.dedup: OrderedDict = OrderedDict()
        self.last_seen: dict[str, tuple[int, int]] = {}   # device -> (seq, ts) moi nhat
        self.stats: Counter = Counter()
        self.e2e_window: deque = deque(maxlen=5000)
        self.ops_points: list[Point] = []
        self.influx_down = False
        self.last_health_check = 0.0

        self.influx = influx_client(cfg, timeout_ms=5_000)
        self.write_api = self.influx.write_api(write_options=SYNCHRONOUS)

        client_id = f"{cfg['mqtt']['client_id_prefix']}-{socket.gethostname()}"[:64]
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                                clean_session=clean_session, protocol=mqtt.MQTTv311)
        if cfg["mqtt"].get("tls"):
            self.mqtt.tls_set()
        if cfg["mqtt"].get("username"):
            self.mqtt.username_pw_set(cfg["mqtt"]["username"], cfg["mqtt"].get("password") or None)
        self.mqtt.reconnect_delay_set(1, 30)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_disconnect = self._on_disconnect
        self.mqtt.on_message = self._on_message

    # ---------------- MQTT callbacks (thread cua paho) ----------------
    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connect failed: %s", reason_code)
            return
        topics = [(f"{self.root}/+/+/telemetry", self.qos), (f"{self.root}/+/+/status", self.qos)]
        client.subscribe(topics)
        log.info("MQTT connected (session_present=%s), subscribed %s",
                 flags.session_present, [t for t, _ in topics])

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected: %s - paho se tu ket noi lai", reason_code)

    def _on_message(self, client, userdata, msg):
        # Chi gan moc thoi gian va dua vao hang doi; xu ly/ghi DB o worker thread
        # de callback MQTT khong bi chan khi InfluxDB cham.
        try:
            self.inbox.put_nowait((msg.topic, msg.payload, now_ms()))
        except queue.Full:
            self.stats["dropped_queue_full"] += 1

    # ---------------- Xu ly ----------------
    def quality_event(self, device_id: str, kind: str, count: int = 1, seq: int | None = None) -> None:
        self.stats[f"q_{kind}"] += count
        point = (Point("data_quality").tag("device_id", device_id).tag("kind", kind)
                 .field("count", int(count)).time(now_ms(), WritePrecision.MS))
        if seq is not None:
            point.field("seq", int(seq))
        self.ops_points.append(point)

    def handle_status(self, device_id: str, payload: bytes) -> None:
        if not payload:  # ban tin rong = xoa retained status cu
            return
        try:
            data = json.loads(payload.decode("utf-8"))
            status = str(data.get("status", "unknown"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            status = "unknown"
        log.info("Device %s status=%s", device_id, status)
        self.ops_points.append(Point("device_status").tag("device_id", device_id)
                               .field("status", status).field("online", int(status == "online"))
                               .time(now_ms(), WritePrecision.MS))

    def track_sequence(self, rec: Record) -> bool:
        """Phat hien mat goi (nhay seq), khoi dong lai thiet bi va mau den tre.

        Tra ve False neu mau den tre (sai thu tu). seq lui + ts moi hon => thiet bi khoi
        dong lai; seq lui + ts cu hon => goi den tre (khong lui moc seq).
        """
        prev = self.last_seen.get(rec.device_id)
        state = (rec.seq, rec.ts)
        if prev is None:
            self.last_seen[rec.device_id] = state
            return True
        prev_seq, prev_ts = prev
        if rec.seq == prev_seq + 1:
            pass
        elif rec.seq > prev_seq + 1:
            self.quality_event(rec.device_id, "gap", rec.seq - prev_seq - 1, rec.seq)
        elif rec.ts > prev_ts:
            self.quality_event(rec.device_id, "reset", 1, rec.seq)
        else:
            self.quality_event(rec.device_id, "out_of_order", 1, rec.seq)
            return False
        self.last_seen[rec.device_id] = state
        return True

    def handle(self, topic: str, payload: bytes, received: int) -> None:
        self.stats["received"] += 1
        parsed = parse_topic(topic, self.root)
        if parsed and parsed[2] == "status":
            self.handle_status(parsed[1], payload)
            return

        result = validate_payload(topic, payload, self.cfg, received)
        device = parsed[1] if parsed else "unknown"
        if not result.ok:
            self.stats["invalid"] += 1
            self.quality_event(device, result.issues[-1])
            log.warning("Rejected message on %s: %s | %.120r", topic, result.issues, payload)
            return
        rec = result.record
        if rec.dedup_key in self.dedup:
            self.quality_event(device, "duplicate", 1, rec.seq)
            return
        self.dedup[rec.dedup_key] = None
        if len(self.dedup) > self.dedup_size:
            self.dedup.popitem(last=False)
        for issue in result.issues:  # sau dedup de ban trung khong bi dem lap
            self.quality_event(device, issue.split(":")[0], 1, rec.seq)
        in_order = self.track_sequence(rec)
        self.stats["valid"] += 1

        if self.influx_down:
            self.spool(rec)
            return
        started = now_ms()
        try:
            self.write_api.write(self.bucket_raw, record=record_to_point(rec))
        except Exception as exc:  # mat ket noi, timeout, 5xx...
            log.error("InfluxDB write failed (%s) - chuyen sang spool", exc)
            self.influx_down = True
            self.last_health_check = time.monotonic()
            self.spool(rec)
            return
        written = now_ms()
        self.stats["written"] += 1
        if rec.device_ts is not None and rec.ts_source == "device":
            ingest = received - rec.device_ts
            e2e = written - rec.device_ts
            if not in_order:
                delivery = "late"          # goi den sai thu tu
            elif ingest >= LIVE_THRESHOLD_MS:
                delivery = "backfill"      # gui bu tu bo dem cua thiet bi
            else:
                delivery = "live"
            if delivery == "live":
                self.e2e_window.append(e2e)
            self.ops_points.append(
                Point("pipeline_latency").tag("device_id", rec.device_id).tag("delivery", delivery)
                .field("ingest_ms", int(ingest)).field("process_ms", int(started - received))
                .field("write_ms", int(written - started)).field("e2e_ms", int(e2e))
                .field("seq", int(rec.seq)).time(written, WritePrecision.MS))

    # ---------------- Spool khi InfluxDB loi ----------------
    def spool(self, rec: Record) -> None:
        with self.spool_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(dataclasses.asdict(rec)) + "\n")
        self.stats["spooled"] += 1

    def replay_spool(self) -> bool:
        if not self.spool_path.exists() or self.spool_path.stat().st_size == 0:
            return True
        lines = self.spool_path.read_text(encoding="utf-8").splitlines()
        points = [record_to_point(Record(**json.loads(line))) for line in lines if line.strip()]
        try:
            for i in range(0, len(points), 5000):
                self.write_api.write(self.bucket_raw, record=points[i:i + 5000])
        except Exception as exc:
            log.warning("Replay spool failed: %s", exc)
            return False
        self.spool_path.unlink()
        self.stats["replayed"] += len(points)
        self.stats["written"] += len(points)
        log.info("Replayed %d spooled record(s) into InfluxDB", len(points))
        return True

    def maintenance(self) -> None:
        if self.influx_down and time.monotonic() - self.last_health_check >= HEALTH_CHECK_S:
            self.last_health_check = time.monotonic()
            if self.influx.ping() and self.replay_spool():
                self.influx_down = False
                log.info("InfluxDB is back online")
        if self.ops_points and not self.influx_down:
            batch, self.ops_points = self.ops_points, []
            try:
                self.write_api.write(self.bucket_ops, record=batch)
            except Exception as exc:
                log.warning("Write ops metrics failed: %s", exc)
                self.ops_points = batch[-5000:] + self.ops_points

    def print_stats(self) -> None:
        s = self.stats
        e2e = list(self.e2e_window)
        lat = (f"e2e p50={percentile(e2e, 50):.0f} p95={percentile(e2e, 95):.0f} "
               f"mean={statistics.fmean(e2e):.0f} ms (n={len(e2e)})") if e2e else "e2e n=0"
        quality = {k[2:]: v for k, v in s.items() if k.startswith("q_")}
        log.info("recv=%d valid=%d invalid=%d written=%d spooled=%d replayed=%d queue=%d | %s | %s",
                 s["received"], s["valid"], s["invalid"], s["written"], s["spooled"],
                 s["replayed"], self.inbox.qsize(), lat, quality or "{}")

    # ---------------- Vong doi ----------------
    def run(self, duration: float | None = None) -> None:
        if not self.influx.ping():
            log.warning("InfluxDB chua san sang tai %s - du lieu se vao spool", self.cfg["influx"]["url"])
            self.influx_down = True
        elif not self.replay_spool():
            self.influx_down = True
        host, port = self.cfg["mqtt"]["host"], int(self.cfg["mqtt"]["port"])
        log.info("Connecting MQTT %s:%d ...", host, port)
        self.mqtt.connect_async(host, port, keepalive=30)
        self.mqtt.loop_start()

        deadline = time.monotonic() + duration if duration else None
        last_maint = last_stats = time.monotonic()
        try:
            while not self.stop_event.is_set():
                if deadline and time.monotonic() >= deadline:
                    break
                try:
                    self.handle(*self.inbox.get(timeout=0.5))
                except queue.Empty:
                    pass
                now = time.monotonic()
                if now - last_maint >= OPS_FLUSH_S:
                    self.maintenance()
                    last_maint = now
                if now - last_stats >= self.stats_interval:
                    self.print_stats()
                    last_stats = now
        finally:
            self.mqtt.loop_stop()
            self.mqtt.disconnect()
            while not self.inbox.empty():  # xu ly not tin nhan con trong hang doi
                self.handle(*self.inbox.get_nowait())
            self.maintenance()
            self.print_stats()
            self.influx.close()
            log.info("Collector stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="duong dan config.toml")
    parser.add_argument("--duration", type=float, help="tu dung sau N giay (phuc vu do dac)")
    parser.add_argument("--clean-session", action="store_true",
                        help="khong giu session MQTT (mac dinh giu de broker luu tin QoS1 khi gateway offline)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    collector = Collector(load_config(args.config), clean_session=args.clean_session)
    signal.signal(signal.SIGINT, lambda *_: collector.stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: collector.stop_event.set())
    collector.run(args.duration)


if __name__ == "__main__":
    main()
