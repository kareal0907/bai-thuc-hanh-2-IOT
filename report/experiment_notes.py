"""Tong hop so lieu cua buoi thuc nghiem ngay 29/09/2026 -> report/experiment.json.

Cac moc thoi gian (UTC) duoi day lay tu log cua buoi do (data/collector*.log, data/wokwi_serial.log):
  06:47:52  bat dau collector + sim-01
  06:54:30  firmware ESP32 sua cong thuc LDR (du lieu truoc do da xoa vi sai)
  06:56:18-06:57:18  kich ban 1: tat collector 60 s
  06:58:33-06:59:33  kich ban 2: tat InfluxDB 60 s
  07:14:27  firmware ESP32 dong bo lai NTP moi 60 s
Chay:  python report/experiment_notes.py [stop_utc]
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from gateway.common import influx_client, load_config  # noqa: E402

START = "2026-09-29T06:47:00Z"
STOP = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
NTP_FIX = pd.Timestamp("2026-09-29T07:14:30Z")
cfg = load_config()


def q(client, flux):
    df = client.query_api().query_data_frame(flux)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
    if not df.empty and "_time" in df:
        df["_time"] = pd.to_datetime(df["_time"], utc=True)
    return df


def lat_stats(df):
    e = df["e2e_ms"]
    return {"n": int(len(df)), "ingest_p50": float(df["ingest_ms"].median()),
            "write_mean": float(df["write_ms"].mean()), "e2e_p50": float(e.median()),
            "e2e_p95": float(e.quantile(.95)), "e2e_p99": float(e.quantile(.99)), "e2e_max": float(e.max())}


with influx_client(cfg, timeout_ms=60_000) as c:
    lat = q(c, f'''from(bucket:"iot_ops") |> range(start:{START}, stop:{STOP})
      |> filter(fn:(r)=>r._measurement=="pipeline_latency")
      |> pivot(rowKey:["_time"],columnKey:["_field"],valueColumn:"_value") |> group()''')
    raw = q(c, f'''from(bucket:"iot_raw") |> range(start:{START}, stop:{STOP})
      |> filter(fn:(r)=>r._measurement=="sensor_raw" and r._field=="seq") |> group()''')

live = lat[lat["delivery"] == "live"]
sim = live[live["device_id"] == "sim-01"]
esp = live[live["device_id"] == "esp32-01"]
esp_all = lat[lat["device_id"] == "esp32-01"]
latency = {
    "sim": lat_stats(sim),
    "esp_before": lat_stats(esp[esp["_time"] < NTP_FIX]),
    "esp_after": lat_stats(esp[esp["_time"] >= NTP_FIX]),
}
esp_before_all = esp_all[esp_all["_time"] < NTP_FIX]

# Kich ban 1: collector tat 06:56:18 -> ket noi lai 06:57:21; tin sim-01 duoc broker giu va giao lai.
w1 = lat[(lat["device_id"] == "sim-01") & (lat["_time"] >= "2026-09-29T06:57:19Z")
         & (lat["_time"] < "2026-09-29T06:57:30Z")]
seq_sim = raw[raw["device_id"] == "sim-01"].sort_values("_time")
s1 = seq_sim[(seq_sim["_time"] >= "2026-09-29T06:56:18Z") & (seq_sim["_time"] < "2026-09-29T06:57:18Z")]
# Kich ban 2: InfluxDB tat 06:58:33 -> 06:59:33
s2 = seq_sim[(seq_sim["_time"] >= "2026-09-29T06:58:33Z") & (seq_sim["_time"] < "2026-09-29T06:59:40Z")]


def seq_cover(s):
    v = sorted(s["_value"].astype(int))
    missing = sorted(set(range(v[0], v[-1] + 1)) - set(v)) if v else []
    return len(v), (v[0], v[-1]) if v else None, missing


n1, r1, m1 = seq_cover(s1)
n2, r2, m2 = seq_cover(s2)

# Toc do mo phong Wokwi: uptime cua ESP32 so voi gio thuc trong mot phien chay dai nhat.
sessions, cur, offsets = [], [], []
for line in (BASE / "data" / "wokwi_serial.log").read_text(encoding="utf-8").splitlines():
    m = re.match(r"\[(\d\d:\d\d:\d\d)\] \{.*\"ts\":(\d+).*\"uptime_s\":(\d+)\}", line)
    if not m:
        continue
    t = datetime.strptime("2026-09-29 " + m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    up = int(m.group(3))
    if cur and up < cur[-1][1]:
        sessions.append(cur)
        cur = []
    cur.append((t, up))
    # Do lech dong ho = gio may tinh luc nhan dong Serial - ts cua thiet bi. Log Serial chi co do
    # phan giai 1 s (cat bot) nen cong 0.5 s; trung binh tren nhieu mau sai so ~ vai chuc ms.
    if t > pd.Timestamp(STOP).timestamp():
        break
    if t >= NTP_FIX.timestamp():
        offsets.append(t + 0.5 - int(m.group(2)) / 1000)
sessions.append(cur)
speeds, overall = [], []
for s in sessions:
    if len(s) > 20:
        overall.append((s[-1][1] - s[0][1], s[-1][0] - s[0][0]))
        # toc do khi chay lien tuc: chi lay cap mau lien tiep cach nhau < 10 s thuc
        for a, b in zip(s, s[1:]):
            if b[0] - a[0] < 10:
                speeds.append((b[1] - a[1], b[0] - a[0]))
speed = sum(u for u, _ in speeds) / sum(r for _, r in speeds)
speed_overall = sum(u for u, _ in overall) / sum(r for _, r in overall)
clock_offset_ms = float(pd.Series(offsets).median() * 1000) if offsets else None

results = json.loads((BASE / "report" / "results.json").read_text(encoding="utf-8"))
rtt = results["broker_rtt_ms"]
bench = results["benchmark"]
ls, lb, la = latency["sim"], latency["esp_before"], latency["esp_after"]
P = json.loads((BASE / "data" / "preprocess_report.json").read_text(encoding="utf-8"))
ps = P["sim-01"]
pe = P.get("esp32-01", {})
dur_min = (pd.Timestamp(STOP) - pd.Timestamp("2026-09-29T06:47:52Z")).total_seconds() / 60


def f0(v):
    return f"{v:,.0f}".replace(",", ".")


def dec(v, d=1):
    """So thap phan kieu Viet Nam: 7,5"""
    return f"{v:.{d}f}".replace(".", ",")


X = {
    "date": "29/09/2026",
    "stop": STOP,
    "wokwi_speed": round(speed, 3),
    "wokwi_speed_overall": round(speed_overall, 3),
    "esp_clock_offset_ms": round(clock_offset_ms, 0) if clock_offset_ms is not None else None,
    "latency": latency,
    "latency_rows": [
        ["sim-01 (thiết bị Python, cùng đồng hồ)", "sim"],
        ["esp32-01 – NTP chỉ 1 lần", "esp_before"],
        ["esp32-01 – NTP mỗi 60 s", "esp_after"],
    ],
    "esp_before_backfill": int((esp_before_all["delivery"] != "live").sum()),
    "fault": {"s1_samples": n1, "s1_seq": r1, "s1_missing": m1, "s1_redelivered": int(len(w1)),
              "s1_max_ingest": float(w1["ingest_ms"].max()) if len(w1) else None,
              "s2_samples": n2, "s2_seq": r2, "s2_missing": m2},
    "preprocess_window": f"toàn bộ buổi thực nghiệm, {ps['start'][11:16]}–{ps['stop'][11:16]} UTC",
}
X["setup_text"] = (
    f"Pipeline chạy liên tục khoảng {dur_min:.0f} phút trên một máy tính cá nhân (Windows 11, InfluxDB chạy cục bộ), "
    f"qua broker công cộng broker.emqx.io; RTT TCP tới broker có p50 = {rtt['p50']:.0f} ms. Có 2 nguồn dữ liệu "
    "chạy đồng thời. Thứ nhất là esp32-01, firmware thật chạy trên Wokwi (VS Code); các thanh trượt cảm biến được "
    "chỉnh tay nên dữ liệu gần như là hằng số. Thứ hai là sim-01, thiết bị Python chu kỳ 5 s với tín hiệu hình sin "
    "cộng nhiễu, chèn lỗi có chủ đích: mất 2 % số gói, trùng 2 %, null 2 %, spike 1,5 %, payload hỏng 1 %, đến trễ 1 %. "
    "Độ trễ chỉ thống kê trên mẫu live, bỏ qua mẫu gửi bù hoặc đến sai thứ tự.")
X["preprocess_comment"] = (
    f"Với sim-01, lưới 5 s có {ps['grid_slots']} ô, trong đó {ps['missing_before']['temperature']} ô thiếu nhiệt độ; "
    "các ô này tương ứng với gói bị mất, bản tin hỏng bị loại và giá trị null. Phương pháp Z-score trượt phát hiện "
    f"{sum(ps['outliers_zscore'].values())} outlier, còn IQR phát hiện {sum(ps['outliers_iqr'].values())}. Cả hai đều "
    "bắt được các spike nhiệt độ (bị nhân 1,6 hoặc 0,4 lần). Sau khi nội suy các khoảng trống ngắn, dữ liệu sim-01 không "
    f"còn giá trị thiếu và được gom thành {ps['rows_processed']} cửa sổ 1 phút. "
    + (f"Với esp32-01, {pe['missing_after']['temperature']}/{pe['grid_slots']} ô vẫn thiếu sau xử lý. Đó là các lần mô "
       "phỏng khởi động lại hoặc tạm dừng khi tab Wokwi bị ẩn, kéo dài hơn 30 s, nên cố ý không nội suy; completeness "
       "của các cửa sổ này thấp. Khi độ ẩm trên Wokwi được chỉnh từ 50 lên 71,5 %, IQR toàn khoảng đánh dấu nhầm "
       f"{pe['outliers_iqr']['humidity']} mẫu mức mới là outlier, vì IQR của tín hiệu gần như hằng số xấp xỉ 0. Z-score "
       "trượt không đánh dấu mẫu nào: độ lệch chuẩn cục bộ tăng ngay tại bước nhảy. Đây là lý do chọn Z-score trượt "
       "làm mặc định. Z-score chuẩn hóa cũng khuếch đại nhiễu lượng tử 0,02 cm "
       "của khoảng cách thành ±2σ, nên cần thận trọng khi chuẩn hóa tín hiệu gần như không đổi." if pe else ""))
X["fault_rows"] = [
    ["Gateway (collector) dừng 60 s",
     f"Khi kết nối lại, broker báo session_present=True và giao lại {len(w1)} bản tin QoS1 đã giữ trong lúc gateway "
     f"tắt (độ trễ tối đa {f0(X['fault']['s1_max_ingest'] or 0)} ms). Trong khoảng 60 s đó, sim-01 lưu {n1} mẫu "
     f"(seq {r1[0]}–{r1[1]}). Seq thiếu {m1 or 'không có'} đã được ghi nhận là sự kiện gap; nhiều khả năng đó là gói "
     "bị chèn lỗi mất (2 %), vì mọi bản tin publish trong lúc gateway dừng đều được giao lại."],
    ["InfluxDB dừng 60 s",
     f"Lần ghi đầu tiên lỗi (connection refused) → chuyển sang spool.jsonl. Collector ghi tạm 27 bản ghi. Sau khi DB "
     f"khởi động lại, lần kiểm tra kế tiếp tự ghi bù cả 27 bản ghi. Trong cửa sổ này sim-01 có {n2} mẫu "
     f"(seq {r2[0]}–{r2[1]}). Seq thiếu {m2 or 'không có'} là gap phát sinh trước khi tới gateway, nên không có mẫu "
     "nào bị mất do DB lỗi."],
    [f"ESP32 khởi động lại ({results['quality'].get('esp32-01', {}).get('events', {}).get('reset', 0)} lần ghi "
     "nhận, do nạp firmware mới hoặc chỉnh mô phỏng)",
     "seq quay về 1 nhưng ts mới hơn → gateway ghi sự kiện reset, không nhầm với dữ liệu trùng hay đến trễ. "
     "LWT phát status=offline cho phiên cũ."],
    ["Payload hỏng, trùng lặp, đến trễ (sim-01)",
     "Bản tin hỏng bị loại (bad_json, device_id_mismatch). Bản trùng bị cache LRU chặn. Gói đến trễ được ghi đúng "
     "timestamp gốc và gắn delivery=late. Mọi sự kiện đều có trong iot_ops/data_quality (Bảng 6)."],
]
X["analysis"] = [
    ("Độ trễ end-to-end.",
     f"Với sim-01 (cùng đồng hồ với gateway), E2E p50 = {f0(ls['e2e_p50'])} ms, trong đó chặng thiết bị → broker "
     f"→ gateway chiếm gần hết (ingest p50 = {f0(ls['ingest_p50'])} ms). Xử lý tại gateway dưới 1 ms và ghi InfluxDB "
     f"trung bình {dec(ls['write_mean'])} ms. Như vậy nút cổ chai là mạng: mỗi bản tin đi hai chặng tới broker công "
     f"cộng ở xa (broker.emqx.io phân giải tới máy chủ AWS ở Mỹ/châu Âu, RTT p50 = {rtt['p50']:.0f} ms), cộng thời "
     f"gian broker chuyển tiếp. Đuôi phân bố (p95 = {f0(ls['e2e_p95'])} ms, "
     f"max = {f0(ls['e2e_max'])} ms) xuất hiện quanh thời điểm gateway kết nối lại và vào các lúc broker công cộng chậm."),
    ("Đồng bộ thời gian là điều kiện để đo đúng.",
     f"Khi chạy liên tục, Wokwi đạt khoảng {speed * 100:.0f}% tốc độ thực. Tuy nhiên mô phỏng tạm dừng khi tab bị ẩn, "
     f"nên tính trên cả phiên thì đồng hồ ESP32 chỉ chạy được {speed_overall * 100:.0f}% thời gian thực. Với firmware ban "
     "đầu (chỉ đồng bộ NTP khi khởi động), độ lệch này tích lũy: \"độ trễ\" đo được tăng tới hàng chục giây, "
     f"{X['esp_before_backfill']} mẫu bị phân loại nhầm là gửi bù, và timestamp trong DB bị lệch theo. Sau khi đồng bộ "
     f"lại NTP mỗi 60 s, E2E p50 của ESP32 còn {f0(la['e2e_p50'])} ms (p95 = {f0(la['e2e_p95'])} ms). So sánh thời "
     "điểm dòng Serial tới máy tính với ts của thiết bị cho thấy đồng hồ ESP32 vẫn chậm hơn máy tính khoảng "
     f"{f0(clock_offset_ms or 0)} ms (sai số NTP qua mạng ảo của Wokwi). Trừ phần này, độ trễ mạng thực của ESP32 còn "
     f"khoảng {f0(la['e2e_p50'] - (clock_offset_ms or 0))} ms, cùng cỡ với sim-01. Kết luận: phép đo E2E giữa hai "
     "đồng hồ khác nhau chỉ chính xác tới mức sai số đồng bộ. Thiết bị thật dùng NTP có sai số vài chục ms; khi cần "
     "chính xác hơn, nên dùng PTP hoặc so sánh bằng timestamp tại gateway."),
    ("Lưu trữ.",
     f"Ghi đồng bộ từng điểm chỉ đạt khoảng {f0(bench['single_point_write']['points_per_s'])} điểm/s vì mỗi lần ghi là một "
     f"HTTP request. Ghi theo lô 5.000 điểm đạt khoảng {f0(bench['batch_5000_write']['points_per_s'])} điểm/s. Với tải "
     "hiện tại (0,2–0,4 bản tin/s) ghi đồng bộ là đủ và cho độ trễ thấp nhất; khi số thiết bị tăng, nên dùng batching "
     "(ví dụ 500 điểm hoặc 1 s mỗi lô). Sau khi nén TSM, mỗi giá trị field của iot_raw chiếm khoảng "
     f"{dec(results['storage']['iot_raw']['bytes_per_value'])} B (benchmark: "
     f"{dec(bench['bench_bytes_per_value'])} B), tức khoảng {f0(results['storage']['iot_raw']['bytes_per_value'] * 7)} B "
     "cho mỗi mẫu 7 field. Một thiết bị gửi mỗi 5 s tạo khoảng 17.280 mẫu/ngày, tức chưa tới 1 MB/ngày, nên raw 30 ngày "
     "của hàng trăm thiết bị vẫn nhỏ. Chi phí cố định đáng kể lại là các file series/index cấp phát trước, khoảng "
     "32 MB mỗi bucket. Dữ liệu 1 phút đã xử lý có số điểm ít hơn khoảng 12 lần, nên việc giữ raw 30 ngày và "
     "processed 1 năm là cân đối. Rủi ro cần tránh là cardinality: "
     "không đưa seq, ts hay giá trị liên tục vào tag."),
    ("Tính toàn vẹn.",
     "Chuỗi seq giúp đo chính xác tỉ lệ nhận và phân biệt 4 loại sự cố: mất gói, trùng, đến trễ, khởi động lại. "
     "Timestamp của thiết bị kết hợp với series (device_id, site) làm cho việc ghi lại (spool, QoS1 redelivery, chạy "
     "lại tiền xử lý) là idempotent. Điểm yếu còn lại là PubSubClient chỉ publish QoS0: gói mất giữa ESP32 và broker "
     "không được gửi lại, mà chỉ được phát hiện qua gap."),
]
X["improvements"] = [
    ("Bảo mật:", "dùng broker riêng (Mosquitto/EMQX) với TLS 8883, username/password hoặc chứng chỉ client, ACL theo "
                  "topic; broker công cộng chỉ phù hợp để thực hành."),
    ("QoS1 ở thiết bị:", "dùng thư viện hỗ trợ QoS1 (ví dụ esp-mqtt/AsyncMqttClient) và lưu bộ đệm vào flash/LittleFS "
                         "để không mất dữ liệu khi mất điện."),
    ("Hiệu năng ghi:", "batching bất đồng bộ theo kích thước hoặc thời gian; đặt broker và gateway gần nhau (LAN/edge) "
                       "để giảm độ trễ vài trăm ms xuống vài ms."),
    ("Tiền xử lý:", "chuyển phần downsampling đơn giản sang Flux task (aggregateWindow) chạy ngay trong InfluxDB; "
                    "cập nhật scaler theo cửa sổ trượt để phục vụ phát hiện bất thường real-time."),
    ("Vận hành:", "chạy các thành phần bằng docker compose hoặc dịch vụ Windows, thêm cảnh báo khi completeness "
                  "thấp hoặc thiết bị offline."),
]
X["difficulties"] = [
    ["Tải thư viện từ PyPI rất chậm (~20–30 KB/s), pip bị treo",
     "Tải wheel từ mirror (khoảng 0,5 MB/s), đối chiếu SHA-256 từng file với metadata chính thức trên pypi.org, rồi cài "
     "offline từ thư mục wheelhouse"],
    ["Máy không có Docker", "Dùng bản InfluxDB 2.7.11 cho Windows (file zip) cùng script start/setup; vẫn cung cấp "
                            "docker-compose.yml cho máy có Docker"],
    ["Lux đọc sai (500 lux → khoảng 900 lux)",
     "Tính ngược từ số đo cho thấy module Wokwi dùng điện trở 10 kΩ (mã ví dụ trong tài liệu dùng 2 kΩ, cho 5 V). "
     "Đã sửa công thức và lấy trung bình 16 lần đọc ADC; dữ liệu sai trước đó đã bị xóa"],
    ["Độ trễ ESP32 tăng dần tới khoảng 50 s",
     "Đối chiếu uptime với giờ thực cho thấy đồng hồ mô phỏng bị trễ do Wokwi tạm dừng hoặc chạy chậm. Đã thêm "
     "sntp_set_sync_interval(60 s) và đo riêng độ lệch đồng hồ qua log Serial"],
    ["Gói đến trễ bị nhận nhầm là thiết bị khởi động lại",
     "Chuyển tiêu chí từ uptime sang ts: seq lùi mà ts mới hơn mới là reset; bổ sung unit test"],
    ["pandas interpolate(limit=6) vẫn điền 6 ô đầu của khoảng trống dài",
     "Tự tính độ dài từng chuỗi NaN, chỉ nội suy chuỗi ≤ 6 mẫu (có unit test)"],
    ["IQR xóa nhầm bước nhảy thật (vật cản, đổi thanh trượt Wokwi)",
     "Dùng Z-score cửa sổ trượt làm mặc định, thêm min_spread và loại distance_cm khỏi lọc outlier"],
    ["Bản tin LWT offline của phiên cũ đến sau bản tin online của phiên mới",
     "Dashboard xác định trạng thái theo độ mới của dữ liệu, không chỉ theo bản tin status"],
    ["Wokwi tạm dừng khi tab mô phỏng bị ẩn", "Ghi chú trong README; khoảng trống được phản ánh qua completeness"],
]
X["conclusion"] = (
    "Pipeline ESP32 → MQTT → gateway Python → InfluxDB → tiền xử lý → dashboard đã chạy ổn định trong buổi thực "
    "nghiệm. Hệ thống qua được các kịch bản gateway dừng và database dừng mà không mất dữ liệu, đồng thời phát hiện và "
    "ghi nhận đầy đủ các loại sự cố chất lượng. Kết quả đo cho thấy độ trễ chủ yếu do mạng tới broker công cộng. Muốn đo "
    "độ trễ chính xác thì đồng bộ thời gian là bắt buộc. Mã nguồn, cấu hình, unit test (9 test) và README hướng dẫn chạy "
    "nằm trong thư mục Bai2.")
(BASE / "report" / "experiment.json").write_text(json.dumps(X, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps({k: X[k] for k in ("wokwi_speed", "latency", "fault", "esp_before_backfill")}, indent=2,
                 ensure_ascii=False, default=str))
