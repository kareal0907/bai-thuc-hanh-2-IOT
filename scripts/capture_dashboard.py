"""Chup anh dashboard Streamlit (tung tab) bang Edge/Chrome headless qua DevTools Protocol.

Chay khi dashboard dang chay:  python scripts/capture_dashboard.py [device_id]
Anh luu vao report/figures/dashboard_<tab>.png
"""
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

URL = "http://localhost:8501"
OUT = Path(__file__).resolve().parent.parent / "report" / "figures"
BROWSERS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
PORT = 9333
WIDTH, HEIGHT = 1500, 1900


class CDP:
    def __init__(self, ws):
        self.ws, self.next_id = ws, 0

    async def send(self, method, **params):
        self.next_id += 1
        my_id = self.next_id
        await self.ws.send(json.dumps({"id": my_id, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == my_id:
                return msg.get("result", {})

    async def js(self, expr):
        res = await self.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in res:
            print("JS error:", res["exceptionDetails"].get("exception", {}).get("description", res)[:300])
        return res.get("result", {}).get("value")


async def click(cdp, selector_js):
    """Click chuot that (Streamlit/BaseWeb khong phan ung voi element.click())."""
    for _ in range(60):  # cho element xuat hien (toi da 30 s)
        rect = await cdp.js(f"(() => {{ const e = {selector_js}; if (!e) return null;"
                            " const r = e.getBoundingClientRect();"
                            " return [r.x + r.width / 2, r.y + r.height / 2]; })()")
        if rect:
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"Khong tim thay element: {selector_js}")
    for kind in ("mousePressed", "mouseReleased"):
        await cdp.send("Input.dispatchMouseEvent", type=kind, x=rect[0], y=rect[1], button="left", clickCount=1)


async def shoot(cdp, name):
    from io import BytesIO

    from PIL import Image, ImageChops

    data = await cdp.send("Page.captureScreenshot", format="png")
    img = Image.open(BytesIO(base64.b64decode(data["data"]))).convert("RGB")
    # Cat bo khoang trang phia duoi phan noi dung chinh (ben phai sidebar 300 px).
    main = img.crop((310, 0, img.width, img.height))
    bbox = ImageChops.difference(main, Image.new("RGB", main.size, main.getpixel((main.width - 5, main.height - 5)))).getbbox()
    bottom = min(img.height, (bbox[3] if bbox else img.height) + 30)
    path = OUT / f"dashboard_{name}.png"
    img.crop((0, 0, img.width, bottom)).save(path)
    print("saved", path)


async def main(device):
    browser = next(p for p in BROWSERS if os.path.exists(p))
    profile = tempfile.mkdtemp(prefix="cdp-")
    proc = subprocess.Popen([browser, "--headless=new", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={profile}", f"--window-size={WIDTH},{HEIGHT}",
                             "--force-device-scale-factor=1", "--hide-scrollbars", "about:blank"])
    try:
        for _ in range(50):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
                page = next(t for t in targets if t["type"] == "page")
                break
            except Exception:
                time.sleep(0.3)
        async with websockets.connect(page["webSocketDebuggerUrl"], max_size=50_000_000) as ws:
            cdp = CDP(ws)
            await cdp.send("Page.enable")
            await cdp.send("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT,
                           deviceScaleFactor=1, mobile=False)
            await cdp.send("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme",
                                                                     "value": "light"}])
            await cdp.send("Page.navigate", url=URL)
            await asyncio.sleep(9)
            if device:
                # chon thiet bi trong selectbox cua sidebar
                await click(cdp, "document.querySelector('[data-testid=stSidebar] [data-testid=stSelectbox] input')")
                await asyncio.sleep(1)
                await click(cdp, f"""[...document.querySelectorAll('[role=option]')]
                    .find(li => li.innerText.trim() === {json.dumps(device)})""")
                await asyncio.sleep(6)
            names = ["realtime", "processed", "latency", "storage"]
            for i, name in enumerate(names):
                await click(cdp, f"document.querySelectorAll('[data-testid=stTab]')[{i}]")
                await asyncio.sleep(7)
                await shoot(cdp, f"{name}_{device}" if device else name)
    finally:
        proc.terminate()
        time.sleep(1)
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
