"""Thiet bi mo phong bang phan mem - cung dinh dang JSON/topic voi firmware ESP32.

Dung de kiem thu pipeline va tao bo du lieu co loi co chu dich (chen loi):
mat goi, trung lap, gia tri null, outlier dang spike, payload hong, goi den tre.
Thiet bi mo phong chay tren cung may voi gateway nen dong ho trung nhau => do tre
end-to-end do duoc khong bi sai lech do dong bo NTP.

Vi du:
  python -m gateway.device_sim --duration 600
  python -m gateway.device_sim --device sim-02 --interval 1 --p-spike 0.03 --p-drop 0.05
"""
from __future__ import annotations

import argparse
import json
import math
import random
import time

import paho.mqtt.client as mqtt

from .common import load_config, now_ms


class SignalModel:
    """Tin hieu gia lap co xu huong ngay/dem (chu ky rut gon) + nhieu."""

    def __init__(self, period_s: float, rng: random.Random):
        self.period_s = period_s
        self.rng = rng
        self.t0 = time.time()
        self.object_until = 0.0

    def sample(self) -> dict:
        t = time.time() - self.t0
        phase = 2 * math.pi * t / self.period_s
        temperature = 27.0 + 3.0 * math.sin(phase) + self.rng.gauss(0, 0.15)
        humidity = 62.0 - 8.0 * math.sin(phase) + self.rng.gauss(0, 0.6)
        light = max(0.0, 450.0 + 350.0 * math.sin(phase - 0.4) + self.rng.gauss(0, 15))
        # Thinh thoang co vat can tien lai gan cam bien sieu am trong ~1 phut.
        if t > self.object_until and self.rng.random() < 0.01:
            self.object_until = t + 60
        distance = (40.0 if t < self.object_until else 120.0) + self.rng.gauss(0, 0.8)
        return {"temperature": round(temperature, 2), "humidity": round(humidity, 2),
                "light_lux": round(light, 1), "distance_cm": round(distance, 2)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config")
    parser.add_argument("--device", default="sim-01")
    parser.add_argument("--site", default="lab")
    parser.add_argument("--interval", type=float, default=5.0, help="chu ky gui (giay)")
    parser.add_argument("--duration", type=float, default=0, help="0 = chay mai")
    parser.add_argument("--period", type=float, default=1800, help="chu ky tin hieu ngay/dem rut gon (giay)")
    parser.add_argument("--qos", type=int, default=1)
    parser.add_argument("--seed", type=int, default=2)
    parser.add_argument("--p-drop", type=float, default=0.02, help="xac suat mat goi (tang seq nhung khong gui)")
    parser.add_argument("--p-dup", type=float, default=0.02, help="xac suat gui trung lap")
    parser.add_argument("--p-null", type=float, default=0.02, help="xac suat mot truong = null")
    parser.add_argument("--p-spike", type=float, default=0.015, help="xac suat outlier dang spike")
    parser.add_argument("--p-invalid", type=float, default=0.01, help="xac suat payload hong/ngoai dai do")
    parser.add_argument("--p-late", type=float, default=0.01, help="xac suat goi den tre (sai thu tu)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    rng = random.Random(args.seed)
    model = SignalModel(args.period, rng)
    root = cfg["mqtt"]["topic_root"].strip("/")
    topic = f"{root}/{args.site}/{args.device}/telemetry"
    status_topic = f"{root}/{args.site}/{args.device}/status"

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{args.device}-{rng.getrandbits(32):08x}")
    if cfg["mqtt"].get("tls"):
        client.tls_set()
    client.will_set(status_topic, json.dumps({"status": "offline"}), qos=1, retain=True)
    client.connect(cfg["mqtt"]["host"], int(cfg["mqtt"]["port"]), keepalive=30)
    client.loop_start()
    client.publish(status_topic, json.dumps({"device_id": args.device, "status": "online", "fw": "sim"}),
                   qos=1, retain=True)
    print(f"Publishing to {topic} every {args.interval}s (Ctrl+C de dung)")

    seq, sent, held = 0, 0, None
    started = time.monotonic()
    next_tick = started
    try:
        while not args.duration or time.monotonic() - started < args.duration:
            next_tick += args.interval
            seq += 1
            values = model.sample()
            if rng.random() < args.p_spike:
                key = rng.choice(["temperature", "humidity", "light_lux"])
                values[key] = round(values[key] * rng.choice([1.6, 0.4]) + rng.choice([-1, 1]) * 5, 2)
            if rng.random() < args.p_null:
                values[rng.choice(list(values))] = None
            payload = {"device_id": args.device, "ts": now_ms(), "seq": seq, **values,
                       "rssi": int(-55 + rng.gauss(0, 4)), "uptime_s": int(time.monotonic() - started)}
            body = json.dumps(payload)
            if rng.random() < args.p_invalid:
                body = rng.choice([body[: len(body) // 2],                               # JSON hong
                                   json.dumps({**payload, "humidity": 180.0}),            # ngoai dai do
                                   json.dumps({**payload, "device_id": "khac"})])         # sai device_id

            if rng.random() < args.p_drop:
                pass  # mat goi tren duong truyen
            elif held is None and rng.random() < args.p_late:
                held = body  # giu lai, gui sau goi ke tiep => sai thu tu
            else:
                client.publish(topic, body, qos=args.qos)
                sent += 1
                if held is not None:
                    client.publish(topic, held, qos=args.qos)
                    sent += 1
                    held = None
                if rng.random() < args.p_dup:
                    client.publish(topic, body, qos=args.qos)
                    sent += 1
            if seq % 20 == 0:
                print(f"seq={seq} sent={sent} last={body[:110]}")
            time.sleep(max(0.0, next_tick - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(status_topic, json.dumps({"device_id": args.device, "status": "offline"}),
                       qos=1, retain=True).wait_for_publish(5)
        client.loop_stop()
        client.disconnect()
        print(f"Stopped: seq={seq}, messages sent={sent}")


if __name__ == "__main__":
    main()
