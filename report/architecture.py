"""Ve so do kien truc he thong -> report/figures/architecture.png"""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

OUT = Path(__file__).resolve().parent / "figures" / "architecture.png"

BOXES = {
    # key: (x, y, w, h, title, lines, color)
    "esp": (0.2, 5.3, 3.0, 2.2, "ESP32 (Wokwi)", ["DHT22 · LDR · HC-SR04", "NTP ts + seq, JSON",
                                                  "Bộ đệm 120 mẫu (10 phút)"], "#dbeafe"),
    "sim": (0.2, 2.4, 3.0, 2.0, "device_sim.py", ["Thiết bị mô phỏng", "chèn lỗi có chủ đích"], "#e0e7ff"),
    "broker": (4.3, 3.9, 2.6, 2.2, "MQTT broker", ["broker.emqx.io:1883", ".../<site>/<device>/telemetry",
                                                   "status (retained, LWT)"], "#fef3c7"),
    "col": (8.0, 3.6, 3.3, 2.8, "collector.py", ["validate · dedup", "phát hiện mất gói/reset",
                                                "đo độ trễ · spool khi DB lỗi"], "#dcfce7"),
    "db": (12.4, 1.0, 3.4, 6.8, "InfluxDB 2.7", ["iot_raw (30 ngày)", "  sensor_raw", "iot_ops (7 ngày)",
                                                "  pipeline_latency", "  data_quality", "iot_processed (365 ngày)",
                                                "  sensor_processed", "  outlier_events"], "#fce7f3"),
    "pre": (8.0, 0.2, 3.3, 2.4, "preprocess.py", ["outlier (Z/IQR) · nội suy", "resample 1 phút · rolling",
                                                  "delta · chuẩn hóa"], "#ede9fe"),
    "dash": (12.4, -1.9, 3.4, 2.1, "dashboard.py", ["Streamlit (app)", "real-time · độ trễ · lưu trữ"], "#f1f5f9"),
}


def center(key, side):
    x, y, w, h = BOXES[key][:4]
    return {"l": (x, y + h / 2), "r": (x + w, y + h / 2), "t": (x + w / 2, y + h),
            "b": (x + w / 2, y)}[side]


def arrow(ax, a, b, label="", rad=0.0, dy=0.15):
    ax.add_patch(FancyArrowPatch(a, b, arrowstyle="-|>", mutation_scale=16, lw=1.6, color="#334155",
                                 connectionstyle=f"arc3,rad={rad}"))
    if label:
        ax.text((a[0] + b[0]) / 2, (a[1] + b[1]) / 2 + dy, label, ha="center", va="bottom", fontsize=8.5,
                color="#334155")


def main():
    fig, ax = plt.subplots(figsize=(13, 7))
    for key, (x, y, w, h, title, lines, color) in BOXES.items():
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.05,rounding_size=0.15",
                                    fc=color, ec="#475569", lw=1.2))
        ax.text(x + w / 2, y + h - 0.35, title, ha="center", va="top", fontsize=11.5, weight="bold")
        for i, line in enumerate(lines):
            ax.text(x + 0.2 if line.startswith("  ") or key == "db" else x + w / 2, y + h - 0.95 - i * 0.5,
                    line, ha="left" if key == "db" else "center", va="top", fontsize=9)
    arrow(ax, center("esp", "r"), (4.3, 5.4), "publish QoS0", rad=-0.1)
    arrow(ax, center("sim", "r"), (4.3, 4.5), "publish QoS1", rad=0.1)
    arrow(ax, center("broker", "r"), center("col", "l"), "subscribe QoS1\n(persistent session)", dy=0.1)
    arrow(ax, center("col", "r"), (12.4, 5.0), "ghi đồng bộ", dy=0.1)
    arrow(ax, (12.4, 2.0), (11.3, 2.0), "đọc raw", dy=0.08)
    arrow(ax, (11.3, 1.1), (12.4, 1.1), "ghi processed", dy=-0.45)
    arrow(ax, center("db", "b"), center("dash", "t"), "Flux query", dy=0)
    ax.text(0.2, 8.0, "Thiết bị (edge)", fontsize=10, color="#64748b", weight="bold")
    ax.text(4.3, 8.0, "Truyền tải", fontsize=10, color="#64748b", weight="bold")
    ax.text(8.0, 8.0, "Gateway (Python, máy tính)", fontsize=10, color="#64748b", weight="bold")
    ax.text(12.4, 8.0, "Lưu trữ & hiển thị", fontsize=10, color="#64748b", weight="bold")
    ax.set_xlim(0, 16)
    ax.set_ylim(-2.1, 8.4)
    ax.axis("off")
    fig.tight_layout()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=170)
    print("saved", OUT)


if __name__ == "__main__":
    main()
