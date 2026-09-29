# Bài thực hành số 2 — Thu thập, lưu trữ và tiền xử lý dữ liệu IoT

Pipeline gồm 5 khối:

1. ESP32 trên Wokwi đọc DHT22, LDR và HC-SR04 rồi publish JSON lên MQTT.
2. Gateway Python subscribe, kiểm tra hợp lệ, loại dữ liệu trùng và phát hiện mất gói, sau đó ghi vào **InfluxDB 2.x**.
3. Script tiền xử lý làm sạch dữ liệu, xử lý outlier, resample và chuẩn hóa. Kết quả ghi vào một bucket riêng.
4. Dashboard **Streamlit** hiển thị dữ liệu real-time, dữ liệu đã xử lý, độ trễ, chất lượng dữ liệu và dung lượng lưu trữ.
5. Script đánh giá đo độ trễ end-to-end và hiệu năng lưu trữ.

```
 ESP32 (Wokwi)            MQTT broker              Gateway (Python, máy tính)                 InfluxDB 2.7
 DHT22 / LDR / HC-SR04 ─► broker.emqx.io:1883 ─►   collector.py                          ─►  iot_raw        (30 ngày)
 JSON + ts (NTP) + seq     QoS0 (thiết bị)          · validate · dedup · phát hiện mất gói     iot_ops        (7 ngày)
 bộ đệm 10 phút khi       QoS1 (gateway,           · đo độ trễ · spool khi DB lỗi                   │
 mất kết nối               persistent session)                                                      ▼
 device_sim.py ────────►  (thiết bị mô phỏng)      preprocess.py  (làm sạch → outlier →   ─►  iot_processed  (365 ngày)
                                                    resample → đặc trưng → chuẩn hóa)
                                                    dashboard.py (Streamlit) ◄──── đọc cả 3 bucket
```

## 1. Cấu trúc thư mục

| Đường dẫn | Nội dung |
| --- | --- |
| `firmware/` | Firmware ESP32 (`sketch.ino`), `diagram.json` (mạch Wokwi), `platformio.ini`, `wokwi.toml` |
| `gateway/common.py` | Đọc cấu hình, kiểm tra hợp lệ payload, kết nối InfluxDB |
| `gateway/setup_influx.py` | Khởi tạo InfluxDB: tài khoản, 3 bucket + retention, token quyền tối thiểu |
| `gateway/collector.py` | Nhận MQTT → validate → dedup → ghi InfluxDB, đo độ trễ, spool khi DB lỗi |
| `gateway/device_sim.py` | Thiết bị mô phỏng bằng Python, có thể chèn lỗi chủ đích |
| `gateway/preprocess.py` | Tiền xử lý dữ liệu và ghi vào `iot_processed` |
| `gateway/dashboard.py` | Dashboard/app giám sát Streamlit |
| `gateway/evaluate.py` | Thống kê độ trễ, chất lượng dữ liệu, benchmark ghi/đọc, vẽ biểu đồ cho báo cáo |
| `tests/test_pipeline.py` | Unit test cho phần validate và tiền xử lý (không cần MQTT/DB) |
| `config.example.toml` | Mẫu cấu hình; `setup_influx.py` tạo `config.toml` (chứa token, không commit) |
| `scripts/*.ps1` | Tải/chạy InfluxDB trên Windows không cần Docker |
| `scripts/serial_log.py` | Ghi log Serial của ESP32 trong Wokwi qua cổng RFC2217 4003 |
| `scripts/capture_dashboard.py` | Chụp ảnh 4 tab dashboard bằng Edge headless (phục vụ báo cáo) |
| `docker-compose.yml` | Tùy chọn: InfluxDB + Mosquitto bằng Docker |
| `report/` | Báo cáo Word `Bao_cao_Bai_2_IoT.docx` và script sinh báo cáo. `results.json`/`experiment.json` chứa số liệu; hình nằm trong `figures/` |

## 2. Cài đặt (Windows, Python ≥ 3.11)

```powershell
cd "D:\Bài 1 TH IOT\Bai2"
python -m pip install -r requirements.txt
```

Nếu tải từ PyPI quá chậm, có thể dùng mirror: thêm `-i https://mirrors.aliyun.com/pypi/simple/`.

InfluxDB: nếu chưa có `tools\influxdb\influxd.exe`, chạy `scripts\download_influxdb.ps1` (tải bản chính thức 2.7.11, ~48 MB). Nếu máy có Docker, có thể dùng `docker compose up -d` thay thế.

## 3. Chạy toàn bộ pipeline

Mỗi lệnh dưới đây chạy trong **một cửa sổ PowerShell riêng**, đứng tại thư mục `Bai2`.

**Bước 1 — InfluxDB** (giữ cửa sổ mở):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_influxdb.ps1
```

**Bước 2 — Khởi tạo database** (chỉ lần đầu; chạy lại vẫn an toàn):

```powershell
python -m gateway.setup_influx
```

Lệnh này tạo `config.toml` và 3 bucket có retention policy. Token ghi vào `config.toml` chỉ có quyền đọc/ghi 3 bucket. Tài khoản đăng nhập web UI http://127.0.0.1:8086 được lưu trong `data\influx_admin.json`.

**Bước 3 — Gateway thu thập:**

```powershell
python -m gateway.collector
```

Log in ra `MQTT connected ... subscribed`. Cứ 30 giây collector in một dòng thống kê gồm số bản tin nhận, hợp lệ, không hợp lệ, đã ghi, độ trễ p50/p95 và các sự cố chất lượng.

**Bước 4 — Thiết bị.** Chọn một hoặc cả hai:

- **ESP32 trên Wokwi (VS Code):** mở thư mục `Bai2\firmware` bằng **File → Open Folder**. Build bằng **PlatformIO: Build**. Sau đó chạy **Wokwi: Start Simulator** từ Command Palette. Serial Monitor hiển thị `connected`, rồi mỗi 5 giây in một dòng JSON kèm `publish=OK`.
  - Muốn dùng Wokwi online: tạo dự án ESP32 mới, dán `sketch.ino` và `diagram.json`, thêm thư viện **DHT sensor library for ESPx** và **PubSubClient**.
- **Thiết bị mô phỏng Python** (có chèn lỗi để kiểm thử):

  ```powershell
  python -m gateway.device_sim --device sim-01
  ```

**Bước 5 — Tiền xử lý** (chạy một lần, hoặc lặp mỗi 60 giây):

```powershell
python -m gateway.preprocess --start -1h
python -m gateway.preprocess --start -30m --loop 60
```

**Bước 6 — Dashboard:**

```powershell
python -m streamlit run gateway/dashboard.py
```

Mở http://localhost:8501. Dashboard có 4 tab:

- **Real-time:** giá trị mới nhất, trạng thái thiết bị, biểu đồ tự làm mới, outlier đánh dấu ✕.
- **Dữ liệu đã xử lý:** so sánh dữ liệu thô, dữ liệu sạch và rolling mean; biểu đồ Z-score, delta và độ đầy đủ.
- **Độ trễ & chất lượng:** p50/p95/p99, histogram, phân rã độ trễ theo chặng, thống kê sự cố.
- **Lưu trữ:** retention, số điểm dữ liệu, dung lượng; có nút chạy tiền xử lý.

**Bước 7 — Đánh giá** (sinh số liệu và hình cho báo cáo):

```powershell
python -m gateway.evaluate --start -2h --bench
```

InfluxDB giữ dữ liệu mới trong cache/WAL (chưa nén) và chỉ snapshot sang file TSM (đã nén) khi shard không nhận ghi trong 10 phút. Muốn đo dung lượng sau nén ngay, hãy dừng collector rồi chạy lại `influxd` với thêm tham số `--storage-cache-snapshot-write-cold-duration 10s`.

**Bước 8 — Báo cáo** (tùy chọn; cần dashboard đang chạy và Microsoft Edge):

```powershell
python scripts\capture_dashboard.py esp32-01
python scripts\capture_dashboard.py sim-01
python report\architecture.py
python report\build_report.py
```

**Kiểm thử đơn vị:**

```powershell
python -m unittest discover -s tests -v
```

## 4. Định dạng dữ liệu MQTT

- Topic dữ liệu: `<topic_root>/<site_id>/<device_id>/telemetry`, ví dụ `int14149/lab2/lab/esp32-01/telemetry`.
- Topic trạng thái: `.../status`. Bản tin gửi với cờ retained; LWT là `{"status":"offline"}`.
- Gateway subscribe wildcard `int14149/lab2/+/+/telemetry` nên nhận được dữ liệu từ nhiều thiết bị.

> Broker công cộng ai cũng đọc được. Hãy đổi `TOPIC_ROOT` trong `firmware/sketch.ino` **và** `topic_root` trong `config.toml` sang một mã riêng (ví dụ mã sinh viên). Không gửi dữ liệu nhạy cảm qua broker này.

```json
{"device_id":"esp32-01","ts":1790659200123,"seq":42,"temperature":25.00,"humidity":50.00,
 "light_lux":500.0,"distance_cm":99.47,"rssi":-60,"uptime_s":215}
```

| Khóa | Kiểu | Ý nghĩa / quy tắc kiểm tra |
| --- | --- | --- |
| `device_id` | string | Bắt buộc, phải trùng với `device_id` trong topic |
| `ts` | int (ms epoch) | Thời điểm đo, đồng bộ NTP. Nếu `0`/thiếu hoặc lệch giờ quá ngưỡng, gateway dùng giờ của mình và gắn `ts_source=gateway` |
| `seq` | int ≥ 1 | Số thứ tự tăng dần, dùng để phát hiện mất gói, trùng lặp, gói đến trễ và thiết bị khởi động lại |
| `temperature` | float/null | °C, hợp lệ trong [-40, 80] |
| `humidity` | float/null | %RH, hợp lệ trong [0, 100] |
| `light_lux` | float/null | lux, hợp lệ trong [0, 100000] |
| `distance_cm` | float/null | cm, hợp lệ trong [2, 400]; `null` khi HC-SR04 timeout |
| `rssi`, `uptime_s` | int | Thông tin chẩn đoán |

Lỗi ở mức trường (null, sai kiểu, ngoài dải đo) chỉ bỏ trường đó, mẫu vẫn được giữ. Lỗi ở mức cấu trúc làm cả mẫu bị loại: JSON hỏng, sai `device_id`, thiếu/sai `seq`, hoặc không còn giá trị cảm biến nào hợp lệ. Mỗi sự cố được ghi vào `iot_ops/data_quality`.

## 5. Schema InfluxDB

| Bucket (retention) | Measurement | Tags | Fields |
| --- | --- | --- | --- |
| `iot_raw` (30 ngày) | `sensor_raw` | `device_id`, `site`, `ts_source` | `temperature`, `humidity`, `light_lux`, `distance_cm` (float); `seq`, `rssi`, `uptime_s` (int) |
| `iot_processed` (365 ngày) | `sensor_processed` | `device_id`, `site`, `window`, `method` | `<đại lượng>` (TB sạch), `<đại lượng>_rm`, `_delta`, `_z`, `_mm`; `n_raw`, `n_imputed`, `n_outliers`, `completeness` |
| `iot_processed` | `outlier_events` | `device_id`, `field`, `method` | `value` |
| `iot_ops` (7 ngày) | `pipeline_latency` | `device_id`, `delivery` (live/backfill/late) | `ingest_ms`, `process_ms`, `write_ms`, `e2e_ms`, `seq` |
| `iot_ops` | `data_quality` | `device_id`, `kind` | `count`, `seq` |
| `iot_ops` | `device_status` | `device_id` | `status`, `online` |

Nguyên tắc thiết kế:

- **Tag** là các thuộc tính ít giá trị, dùng để lọc/nhóm (thiết bị, site). **Field** là giá trị đo.
- `seq` là field chứ không phải tag. Đặt `seq` làm tag sẽ làm số series tăng không giới hạn (cardinality).
- Timestamp là `ts` của thiết bị, độ chính xác ms.
- InfluxDB coi hai điểm có cùng measurement + tag set + timestamp là một điểm (ghi sau đè ghi trước). Vì vậy việc ghi lại cùng một mẫu, hoặc chạy lại tiền xử lý, là **idempotent** (lặp lại không tạo bản ghi mới).

## 6. Xử lý trùng lặp và mất mát

| Tình huống | Cơ chế |
| --- | --- |
| Mất Wi-Fi/MQTT ở thiết bị | Firmware giữ tối đa 120 mẫu (10 phút) trong ring buffer, gửi bù theo đúng thứ tự khi có kết nối lại. Gateway nhận ra mẫu gửi bù (`delivery=backfill`) và không tính vào thống kê độ trễ |
| Gateway tạm dừng | Collector dùng client id cố định với `clean_session=False` và subscribe QoS1, nên broker giữ tin QoS1 trong thời gian gateway offline |
| InfluxDB lỗi | Mẫu được ghi vào `data/spool.jsonl`. Cứ 10 giây collector kiểm tra lại DB và tự ghi bù khi DB hoạt động |
| Trùng lặp (gửi lại, QoS1 redelivery) | Cache LRU theo khóa (`device_id`, `seq`, `ts`) loại bản trùng trước khi ghi. Nếu vẫn lọt, ghi đè cùng timestamp trong InfluxDB cũng không tạo bản sao |
| Mất gói / đến trễ | `seq` nhảy bước → sự kiện `gap` (số gói mất). `seq` lùi nhưng `ts` mới hơn → thiết bị khởi động lại (`reset`). `seq` lùi và `ts` cũ hơn → gói đến trễ (`out_of_order`, `delivery=late`) |

## 7. Tiền xử lý (`preprocess.py`)

1. **Đọc dữ liệu** theo khoảng thời gian (`--start/--stop`) và loại các timestamp trùng.
2. **Lưới đều 5 giây**: mỗi ô không có mẫu là một giá trị thiếu (missing value).
3. **Outlier**, cấu hình `outlier_method`:
   - **Z-score cửa sổ trượt** (mặc định): |x − mean₆₀| / std₆₀ > 3.
   - **IQR**: ngoài [Q1 − 1,5·IQR, Q3 + 1,5·IQR].

   Cả hai phương pháp đều đặt ngưỡng tối thiểu cho độ phân tán (`min_spread`), vì dữ liệu Wokwi gần như không đổi. `distance_cm` không bị lọc outlier: vật cản xuất hiện là sự kiện thật. Báo cáo ghi lại số outlier của **cả hai** phương pháp để so sánh.
4. **Giá trị thiếu**: nội suy theo thời gian cho khoảng trống ≤ 6 mẫu (30 giây). Khoảng trống dài hơn giữ nguyên NaN để không tạo ra dữ liệu giả; điều này thể hiện qua `completeness`.
5. **Resampling** theo cửa sổ 1 phút (giá trị trung bình), kèm `n_raw`, `n_imputed`, `n_outliers`, `completeness`.
6. **Đặc trưng**: rolling mean trên 5 cửa sổ và delta giữa hai cửa sổ liên tiếp. **Chuẩn hóa**: Z-score (`StandardScaler`) và Min-Max (`MinMaxScaler`) của scikit-learn. Tham số scaler được lưu trong `data/preprocess_report.json`.
7. **Ghi kết quả** vào `iot_processed`, đồng thời xuất `data/processed_<device>.csv`.

## 8. Đo độ trễ end-to-end

- `ingest_ms` = thời điểm gateway nhận − `ts` thiết bị (mạng + broker).
- `process_ms`: thời gian chờ trong hàng đợi và kiểm tra hợp lệ.
- `write_ms`: thời gian ghi đồng bộ vào InfluxDB.
- `e2e_ms` = thời điểm ghi xong − `ts`.

`device_sim.py` dùng chung đồng hồ với gateway nên cho số đo chính xác. Với ESP32 trên Wokwi, số đo còn gồm sai số đồng hồ:

- Wokwi mô phỏng chậm hơn thời gian thực (khoảng 89% trong thực nghiệm).
- Nếu chỉ đồng bộ NTP một lần, đồng hồ ESP32 trôi khoảng 10%: sau vài phút, "độ trễ" đo được đã lên tới hàng chục giây.

Firmware vì vậy đồng bộ lại NTP mỗi 60 giây (`NTP_RESYNC_MS`). Wokwi cũng **tạm dừng mô phỏng khi tab bị ẩn**, nên hãy giữ tab mô phỏng hiển thị khi thu số liệu.

## 9. Xử lý sự cố

- **`Khong tim thay config.toml`**: chạy `python -m gateway.setup_influx`.
- **`InfluxDB khong phan hoi`**: bật `scripts\start_influxdb.ps1`, đợi `http://127.0.0.1:8086/health` trả về `pass`.
- **Collector không nhận được gì**: kiểm tra `topic_root` trong `config.toml` trùng với `TOPIC_ROOT` trong firmware. Dùng MQTTX để subscribe `int14149/lab2/#`.
- **Wokwi báo `Waiting for NTP time`**: đợi vài chu kỳ; firmware chỉ gửi dữ liệu khi đã có giờ chuẩn.
- **Wokwi `rc=-2`**: broker không truy cập được. Kiểm tra mạng, hoặc đổi sang `broker.hivemq.com` ở cả firmware và config.

## 10. Đẩy lên GitHub

`.gitignore` đã loại `config.toml` (có token), `data/` và `tools/`. Máy hiện chưa cài git; sau khi cài [Git for Windows](https://git-scm.com/download/win):

```powershell
cd "D:\Bài 1 TH IOT\Bai2"
git init
git add .
git commit -m "Bai thuc hanh 2: MQTT -> InfluxDB -> tien xu ly -> dashboard"
git remote add origin https://github.com/<tai-khoan>/<repo>.git
git push -u origin main
```
