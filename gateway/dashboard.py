"""Dashboard giam sat (Streamlit): du lieu real-time, du lieu da xu ly, do tre, chat luong, luu tru.

Chay:  streamlit run gateway/dashboard.py
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gateway.common import BASE_DIR, SENSOR_FIELDS, influx_client, load_config  # noqa: E402

LABELS = {"temperature": "Nhiệt độ (°C)", "humidity": "Độ ẩm (%)",
          "light_lux": "Ánh sáng (lux)", "distance_cm": "Khoảng cách (cm)"}
RANGES = {"15 phút": "15m", "1 giờ": "1h", "6 giờ": "6h", "24 giờ": "24h", "7 ngày": "7d"}

st.set_page_config(page_title="IoT Lab 2 - Giám sát", page_icon="📡", layout="wide")
CFG = load_config()


@st.cache_resource
def get_client():
    return influx_client(CFG, timeout_ms=30_000)


def query_df(flux: str) -> pd.DataFrame:
    frames = get_client().query_api().query_data_frame(flux)
    if isinstance(frames, list):
        frames = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if not frames.empty and "_time" in frames:
        frames["_time"] = pd.to_datetime(frames["_time"], utc=True).dt.tz_convert("Asia/Ho_Chi_Minh")
    return frames


@st.cache_data(ttl=3)
def devices() -> list[str]:
    df = query_df(f'''import "influxdata/influxdb/schema"
schema.tagValues(bucket: "{CFG['influx']['bucket_raw']}", tag: "device_id", start: -30d)''')
    return sorted(df["_value"].tolist()) if not df.empty else []


@st.cache_data(ttl=3)
def pivot(bucket: str, measurement: str, rng: str, device: str, drop: str = "") -> pd.DataFrame:
    drop_stmt = f'|> drop(columns: [{drop}])' if drop else ""
    return query_df(f'''from(bucket: "{bucket}") |> range(start: -{rng})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.device_id == "{device}")
  {drop_stmt}
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value") |> group()
  |> sort(columns: ["_time"])''')


@st.cache_data(ttl=3)
def outlier_events(rng: str, device: str) -> pd.DataFrame:
    return query_df(f'''from(bucket: "{CFG['influx']['bucket_processed']}") |> range(start: -{rng})
  |> filter(fn: (r) => r._measurement == "outlier_events" and r.device_id == "{device}")
  |> group()''')


@st.cache_data(ttl=3)
def quality_counts(rng: str, device: str) -> pd.DataFrame:
    return query_df(f'''from(bucket: "{CFG['influx']['bucket_ops']}") |> range(start: -{rng})
  |> filter(fn: (r) => r._measurement == "data_quality" and r.device_id == "{device}" and r._field == "count")
  |> group(columns: ["kind"]) |> sum() |> group()''')


@st.cache_data(ttl=3)
def last_status(device: str) -> str:
    df = query_df(f'''from(bucket: "{CFG['influx']['bucket_ops']}") |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "device_status" and r.device_id == "{device}" and r._field == "status")
  |> last()''')
    return str(df["_value"].iloc[0]) if not df.empty else "không rõ"


def pct(series: pd.Series, q: float) -> float:
    return float(series.quantile(q / 100)) if len(series) else float("nan")


# ---------------- Sidebar ----------------
st.sidebar.title("📡 IoT Lab 2")
device_list = devices()
if not device_list:
    st.warning("Chưa có dữ liệu trong InfluxDB. Hãy chạy collector và thiết bị (Wokwi hoặc device_sim).")
    st.stop()
device = st.sidebar.selectbox("Thiết bị", device_list)
rng_label = st.sidebar.selectbox("Khoảng thời gian", list(RANGES), index=1)
rng = RANGES[rng_label]
refresh = st.sidebar.slider("Tự làm mới (giây)", 2, 60, 5)
st.sidebar.caption(f"Broker: `{CFG['mqtt']['host']}` · topic `{CFG['mqtt']['topic_root']}/+/+/telemetry`")
st.sidebar.caption(f"InfluxDB: `{CFG['influx']['url']}` · org `{CFG['influx']['org']}`")

tab_rt, tab_proc, tab_lat, tab_store = st.tabs(
    ["Real-time", "Dữ liệu đã xử lý", "Độ trễ & chất lượng", "Lưu trữ"])


# ---------------- Tab 1: real-time ----------------
@st.fragment(run_every=refresh)
def realtime():
    raw = pivot(CFG["influx"]["bucket_raw"], "sensor_raw", rng, device, '"ts_source"')
    if raw.empty:
        st.info("Không có mẫu nào trong khoảng thời gian đã chọn.")
        return
    last = raw.iloc[-1]
    prev = raw.iloc[-2] if len(raw) > 1 else last
    age = (pd.Timestamp.now(tz="Asia/Ho_Chi_Minh") - last["_time"]).total_seconds()
    # Trang thai tinh theo do moi cua du lieu: ban tin LWT "offline" cua phien cu co the den
    # sau ban tin "online" cua phien moi (vd. broker giu tin khi gateway offline).
    live = age < 3 * CFG["preprocess"]["expected_interval_s"]
    cols = st.columns(6)
    cols[0].metric("Trạng thái", "🟢 online" if live else "🔴 offline", f"{age:.0f}s trước",
                   delta_color="off", help=f"Bản tin status gần nhất: {last_status(device)}")
    for col, name in zip(cols[1:5], SENSOR_FIELDS):
        if name in raw and pd.notna(last.get(name)):
            delta = last[name] - prev[name] if pd.notna(prev.get(name)) else None
            col.metric(LABELS[name], f"{last[name]:.2f}", None if delta is None else f"{delta:+.2f}")
        else:
            col.metric(LABELS[name], "—")
    minutes = max((raw["_time"].iloc[-1] - raw["_time"].iloc[0]).total_seconds() / 60, 1 / 60)
    cols[5].metric("Mẫu/phút", f"{len(raw) / minutes:.1f}", f"seq {int(last['seq'])}",
                   delta_color="off")

    fields = [f for f in SENSOR_FIELDS if f in raw]
    fig = make_subplots(rows=len(fields), cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        subplot_titles=[LABELS[f] for f in fields])
    outs = outlier_events(rng, device)
    for i, name in enumerate(fields, start=1):
        fig.add_trace(go.Scatter(x=raw["_time"], y=raw[name], mode="lines+markers", marker_size=3,
                                 name=LABELS[name], line_width=1.3), row=i, col=1)
        if not outs.empty:
            o = outs[outs["field"] == name]
            if not o.empty:
                fig.add_trace(go.Scatter(x=o["_time"], y=o["_value"], mode="markers", name="Outlier",
                                         marker=dict(color="#d62728", size=9, symbol="x"),
                                         showlegend=i == 1), row=i, col=1)
    fig.update_layout(height=190 * len(fields), margin=dict(l=10, r=10, t=30, b=10), showlegend=False)
    st.plotly_chart(fig, use_container_width=True)
    st.caption(f"Cập nhật lúc {time.strftime('%H:%M:%S')} · {len(raw)} mẫu thô · "
               "dấu ✕ đỏ = outlier do bước tiền xử lý phát hiện")


with tab_rt:
    realtime()


# ---------------- Tab 2: da xu ly ----------------
@st.fragment(run_every=max(refresh, 10))
def processed():
    proc = pivot(CFG["influx"]["bucket_processed"], "sensor_processed", rng, device,
                 '"window", "method", "site"')
    raw = pivot(CFG["influx"]["bucket_raw"], "sensor_raw", rng, device, '"ts_source"')
    if proc.empty:
        st.info("Chưa có dữ liệu đã xử lý. Chạy `python -m gateway.preprocess --start -1h` "
                "hoặc bấm nút ở tab Lưu trữ.")
        return
    name = st.radio("Đại lượng", [f for f in SENSOR_FIELDS if f in proc], horizontal=True,
                    format_func=LABELS.get)
    fig = go.Figure()
    if not raw.empty and name in raw:
        fig.add_trace(go.Scatter(x=raw["_time"], y=raw[name], name="Thô (5 s)", mode="markers",
                                 marker=dict(size=3, color="#9aa5b1")))
    fig.add_trace(go.Scatter(x=proc["_time"], y=proc[name], name=f"Sạch, TB {CFG['preprocess']['resample']}",
                             mode="lines+markers", line=dict(width=2)))
    if f"{name}_rm" in proc:
        fig.add_trace(go.Scatter(x=proc["_time"], y=proc[f"{name}_rm"], name="Rolling mean",
                                 line=dict(dash="dash", width=2)))
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=10, b=10), yaxis_title=LABELS[name],
                      legend=dict(orientation="h"))
    st.plotly_chart(fig, use_container_width=True)

    c1, c2 = st.columns(2)
    zcols = [f"{f}_z" for f in SENSOR_FIELDS if f"{f}_z" in proc]
    zdf = proc.melt(id_vars="_time", value_vars=zcols, var_name="field", value_name="z")
    c1.plotly_chart(px.line(zdf, x="_time", y="z", color="field", height=300,
                            title="Chuẩn hóa Z-score (so sánh các đại lượng khác đơn vị)"),
                    use_container_width=True)
    if f"{name}_delta" in proc:
        c2.plotly_chart(px.bar(proc, x="_time", y=f"{name}_delta", height=300,
                               title=f"Delta {LABELS[name]} giữa hai cửa sổ"), use_container_width=True)
    comp = proc[["_time", "completeness", "n_raw", "n_imputed", "n_outliers"]]
    st.plotly_chart(px.bar(comp, x="_time", y="completeness", height=220, range_y=[0, 1.05],
                           title="Độ đầy đủ dữ liệu mỗi cửa sổ (mẫu nhận được / mẫu kỳ vọng)"),
                    use_container_width=True)
    with st.expander("Bảng dữ liệu đã xử lý"):
        st.dataframe(proc.drop(columns=[c for c in ("result", "table", "_start", "_stop", "_measurement",
                                                      "device_id") if c in proc]),
                     use_container_width=True)


with tab_proc:
    processed()


# ---------------- Tab 3: do tre & chat luong ----------------
@st.fragment(run_every=max(refresh, 10))
def latency():
    lat = pivot(CFG["influx"]["bucket_ops"], "pipeline_latency", rng, device)
    if lat.empty:
        st.info("Chưa có số liệu độ trễ.")
    else:
        live = lat[lat["delivery"] == "live"] if "delivery" in lat else lat
        e2e = live["e2e_ms"]
        cols = st.columns(5)
        for col, (label, value) in zip(cols, [("p50", pct(e2e, 50)), ("p95", pct(e2e, 95)),
                                              ("p99", pct(e2e, 99)), ("Trung bình", e2e.mean()),
                                              ("Max", e2e.max())]):
            col.metric(f"E2E {label}", f"{value:.0f} ms")
        st.caption(f"{len(live)} mẫu live, {len(lat) - len(live)} mẫu gửi bù từ bộ đệm hoặc đến sai thứ tự "
                   "(không tính vào thống kê). E2E = thời điểm ghi xong InfluxDB − ts do thiết bị gắn.")
        c1, c2 = st.columns(2)
        c1.plotly_chart(px.histogram(live, x="e2e_ms", nbins=40, height=300,
                                     title="Phân bố độ trễ end-to-end (ms)"), use_container_width=True)
        stages = pd.DataFrame({"Giai đoạn": ["Thiết bị → broker → gateway", "Xử lý tại gateway",
                                             "Ghi InfluxDB"],
                               "ms": [live["ingest_ms"].mean(), live["process_ms"].mean(),
                                      live["write_ms"].mean()]})
        c2.plotly_chart(px.bar(stages, x="ms", y="Giai đoạn", orientation="h", height=300,
                               title="Độ trễ trung bình từng chặng"), use_container_width=True)
        st.plotly_chart(px.line(live, x="_time", y=["e2e_ms", "ingest_ms", "write_ms"], height=280,
                                title="Độ trễ theo thời gian"), use_container_width=True)

    q = quality_counts(rng, device)
    st.subheader("Chất lượng dữ liệu tại gateway")
    if q.empty:
        st.success("Không ghi nhận sự cố chất lượng dữ liệu trong khoảng thời gian này.")
    else:
        st.plotly_chart(px.bar(q, x="kind", y="_value", height=280, labels={"_value": "Số lần", "kind": ""},
                               title="gap = mất gói (seq nhảy) · duplicate = trùng · null/out_of_range = "
                                     "giá trị lỗi · bad_json/device_id_mismatch = payload bị loại"),
                        use_container_width=True)


with tab_lat:
    latency()


# ---------------- Tab 4: luu tru ----------------
def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


with tab_store:
    icfg = CFG["influx"]
    rows = []
    for key, days in (("bucket_raw", "retention_raw_days"), ("bucket_processed", "retention_processed_days"),
                      ("bucket_ops", "retention_ops_days")):
        cnt = query_df(f'''from(bucket: "{icfg[key]}") |> range(start: -{rng})
  |> group() |> count()''')
        rows.append({"Bucket": icfg[key], "Retention (ngày)": icfg[days],
                     f"Số giá trị (field) trong {rng_label}": int(cnt["_value"].iloc[0]) if not cnt.empty else 0})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    engine = BASE_DIR / "data" / "influxdb" / "engine"
    st.metric("Dung lượng thư mục dữ liệu InfluxDB", f"{dir_size(engine) / 1024 / 1024:.2f} MB")
    spool = BASE_DIR / CFG["collector"]["spool_file"]
    if spool.exists():
        st.warning(f"Có {sum(1 for _ in spool.open(encoding='utf-8'))} bản ghi đang chờ trong spool "
                   "(InfluxDB từng lỗi) - collector sẽ tự ghi bù.")
    if st.button("▶ Chạy tiền xử lý cho khoảng thời gian đang chọn"):
        with st.spinner("Đang tiền xử lý..."):
            res = subprocess.run([sys.executable, "-m", "gateway.preprocess", "--start", f"-{rng}"],
                                 cwd=BASE_DIR, capture_output=True, text=True)
        st.code((res.stdout + res.stderr)[-3000:] or "OK")
        st.cache_data.clear()
