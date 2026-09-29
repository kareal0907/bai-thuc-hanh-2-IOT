"""Ghi log Serial cua ESP32 trong Wokwi (VS Code) qua cong RFC2217 khai bao trong wokwi.toml.

Chay:  python scripts/serial_log.py [port] [file]     (mac dinh 4003, data/wokwi_serial.log)
Moi dong duoc gan thoi diem nhan tren may tinh.
"""
import socket
import sys
import time
from pathlib import Path

port = int(sys.argv[1]) if len(sys.argv) > 1 else 4003
out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).resolve().parent.parent / "data" / "wokwi_serial.log"
out.parent.mkdir(parents=True, exist_ok=True)


def strip_telnet(data: bytes) -> bytes:
    """Bo cac chuoi dieu khien telnet (IAC ...) cua RFC2217."""
    result, i = bytearray(), 0
    while i < len(data):
        b = data[i]
        if b == 255 and i + 1 < len(data):
            cmd = data[i + 1]
            if cmd == 255:
                result.append(255)
                i += 2
            elif cmd == 250:  # SB ... IAC SE
                end = data.find(bytes([255, 240]), i + 2)
                i = len(data) if end < 0 else end + 2
            elif cmd in (251, 252, 253, 254):
                i += 3
            else:
                i += 2
        else:
            result.append(b)
            i += 1
    return bytes(result)


with socket.create_connection(("127.0.0.1", port)) as sock, out.open("a", encoding="utf-8") as fh:
    buf = b""
    print(f"Logging serial from port {port} to {out}")
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += strip_telnet(chunk)
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            text = line.decode("utf-8", "replace").rstrip("\r")
            stamp = time.strftime("%H:%M:%S")
            fh.write(f"[{stamp}] {text}\n")
            fh.flush()
