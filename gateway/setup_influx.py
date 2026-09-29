"""Khoi tao InfluxDB 2.x cho bai thuc hanh (chay mot lan, chay lai an toan).

1. Onboarding: tao user admin, org (mat khau ngau nhien luu o data/influx_admin.json).
2. Tao/cap nhat 3 bucket va retention policy tuong ung.
3. Tao token chi co quyen doc/ghi 3 bucket do (least privilege) va ghi vao config.toml.

Chay:  python -m gateway.setup_influx
"""
from __future__ import annotations

import json
import re
import secrets
import shutil
import sys
import time
import urllib.error
import urllib.request

from influxdb_client import BucketRetentionRules, InfluxDBClient
from influxdb_client.domain.authorization import Authorization
from influxdb_client.domain.permission import Permission
from influxdb_client.domain.permission_resource import PermissionResource

from .common import BASE_DIR, load_config

ADMIN_FILE = BASE_DIR / "data" / "influx_admin.json"
DAY = 86_400


def http_json(method: str, url: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def wait_ready(url: str, timeout_s: float = 60) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if http_json("GET", f"{url}/health").get("status") == "pass":
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    sys.exit(f"InfluxDB khong phan hoi tai {url}. Hay chay scripts/start_influxdb.ps1 truoc.")


def ensure_config():
    target = BASE_DIR / "config.toml"
    if not target.exists():
        shutil.copy(BASE_DIR / "config.example.toml", target)
        print(f"Da tao {target} tu config.example.toml")
    return load_config(target)


def main() -> None:
    cfg = ensure_config()
    icfg = cfg["influx"]
    url, org = icfg["url"].rstrip("/"), icfg["org"]
    wait_ready(url)

    if http_json("GET", f"{url}/api/v2/setup").get("allowed"):
        password = secrets.token_urlsafe(12)
        resp = http_json("POST", f"{url}/api/v2/setup", {
            "username": "admin", "password": password, "org": org,
            "bucket": icfg["bucket_raw"], "retentionPeriodSeconds": icfg["retention_raw_days"] * DAY,
        })
        ADMIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        ADMIN_FILE.write_text(json.dumps({
            "url": url, "username": "admin", "password": password,
            "org": org, "operator_token": resp["auth"]["token"],
        }, indent=2), encoding="utf-8")
        print(f"Onboarding xong. Tai khoan web UI luu tai {ADMIN_FILE}")
    if not ADMIN_FILE.exists():
        sys.exit(f"InfluxDB da duoc khoi tao truoc do nhung thieu {ADMIN_FILE} (operator token).")
    admin = json.loads(ADMIN_FILE.read_text(encoding="utf-8"))

    with InfluxDBClient(url=url, token=admin["operator_token"], org=org) as client:
        org_id = client.organizations_api().find_organizations(org=org)[0].id
        buckets_api = client.buckets_api()
        bucket_ids = []
        for key, days in (("bucket_raw", "retention_raw_days"),
                          ("bucket_processed", "retention_processed_days"),
                          ("bucket_ops", "retention_ops_days")):
            name, seconds = icfg[key], icfg[days] * DAY
            rule = BucketRetentionRules(type="expire", every_seconds=seconds)
            bucket = buckets_api.find_bucket_by_name(name)
            if bucket is None:
                bucket = buckets_api.create_bucket(bucket_name=name, retention_rules=rule, org_id=org_id)
                print(f"Tao bucket {name} (retention {icfg[days]} ngay)")
            else:
                bucket.retention_rules = [rule]
                buckets_api.update_bucket(bucket)
                print(f"Cap nhat bucket {name} (retention {icfg[days]} ngay)")
            bucket_ids.append(bucket.id)

        token_ok = False
        if not icfg["token"].startswith("DIEN_TOKEN"):
            try:
                with InfluxDBClient(url=url, token=icfg["token"], org=org) as test:
                    test.query_api().query(f'buckets() |> limit(n: 1)')
                token_ok = True
            except Exception:
                token_ok = False
        if token_ok:
            print("Token trong config.toml van hop le - giu nguyen")
            return

        permissions = [
            Permission(action=action, resource=PermissionResource(type="buckets", id=bid, org_id=org_id))
            for bid in bucket_ids for action in ("read", "write")
        ]
        auth = client.authorizations_api().create_authorization(
            authorization=Authorization(org_id=org_id, permissions=permissions,
                                        description="lab2-gateway (read/write iot buckets)"))
        cfg_path = BASE_DIR / "config.toml"
        text = cfg_path.read_text(encoding="utf-8")
        text = re.sub(r'(?m)^token\s*=\s*".*"$', f'token = "{auth.token}"', text, count=1)
        cfg_path.write_text(text, encoding="utf-8")
        print("Da tao token doc/ghi 3 bucket va ghi vao config.toml")


if __name__ == "__main__":
    main()
