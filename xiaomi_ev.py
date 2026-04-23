#!/usr/bin/env python3
"""Xiaomi EV (ICCC) trip data fetcher with auto auth management."""

import base64
import json
import random
import time
import uuid
from pathlib import Path

import requests

AUTH_FILE = Path("xiaomi_auth.json")
ICCC_BASE = "https://mobile.iccc.xiaomiev.com/mobile"
ACCOUNT_BASE = "https://account.xiaomi.com/pass"
SID = "iccc_app_api"
CALLBACK = f"{ICCC_BASE}/sts"

UA_HTTP = "okhttp/3.14.9"

_DEVICES = [
    ("2210132C",   "Xiaomi 13 Pro",  "13", "TQ2A.230505.002"),
    ("23127PN0CC",  "Xiaomi 14",      "14", "UQ1A.240105.004"),
    ("2312DRA49G",  "Xiaomi 14 Pro",  "14", "UQ1A.240105.004"),
    ("25020PN94G",  "Xiaomi 15",      "15", "AQ3A.240827.001"),
]


def _build_ua() -> str:
    code, name, android, build = random.choice(_DEVICES)
    mk = base64.b64encode(name.encode()).decode()
    return (
        f"Dalvik/2.1.0 (Linux; U; Android {android}; {code} Build/{build}) "
        f"APP/car.mobile APPV/26040914 MK/{mk} "
        f"SDKV/5.3.0 PassportSDK/5.3.0 XiaomiAccountSSO/5.3.0 "
        f"CPN/com.mi.car.mobile passport-ui/5.3.0 "
        f"DEVT/UGhvbmU= BRA/WGlhb21p DEVS/QW5kcm9pZA=="
    )


def _parse(text: str) -> dict:
    """Strip Xiaomi's '&&&START&&&' prefix and parse JSON."""
    return json.loads(text.replace("&&&START&&&", ""))


def _login_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": _build_ua(),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept-Encoding": "gzip",
        "Connection": "Keep-Alive",
    })
    return s


def _collect_cookies(session: requests.Session) -> dict:
    result = {}
    for cookie in session.cookies:
        result[cookie.name] = cookie.value
    return result


def _display_qr(login_url: str, image_url: str) -> None:
    """Render QR code in terminal if qrcode is installed, otherwise print URLs."""
    try:
        import qrcode  # pip install qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(login_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print(f"  二维码图片: {image_url}")
    if login_url and login_url != image_url:
        print(f"  登录链接:   {login_url}")


def qr_login() -> dict:
    """
    QR code login — no password required.

    Flow:
      1. GET longPolling/loginUrl  →  get QR image URL + long-poll URL
      2. Display QR, long-poll until user scans (HTTP 200)
      3. GET location  →  get serviceToken cookie
    """
    s = _login_session()
    device_id = "an_" + uuid.uuid4().hex
    s.cookies.set("deviceId", device_id, domain="account.xiaomi.com")

    # Step 1: obtain QR code
    r = s.get("https://account.xiaomi.com/longPolling/loginUrl", params={
        "_qrsize": "480",
        "qs": f"%3Fsid%3D{SID}%26_json%3Dtrue",
        "callback": CALLBACK,
        "_hasLogo": "false",
        "sid": SID,
        "serviceParam": "",
        "_locale": "zh_CN",
        "_dc": str(int(time.time() * 1000)),
    })
    r.raise_for_status()
    qr_data = _parse(r.text)

    qr_image_url = qr_data.get("qr", "")
    login_url = qr_data.get("loginUrl", "")
    lp_url = qr_data.get("lp", "")
    qr_timeout = float(qr_data.get("timeout", 300))

    if not lp_url:
        raise RuntimeError(f"Failed to get QR code: {qr_data}")

    print("[QR] 请使用小米汽车 APP 扫描以下二维码登录:")
    _display_qr(login_url or qr_image_url, qr_image_url)
    print(f"[QR] 等待扫码... (有效期 {qr_timeout:.0f} 秒)")

    # Step 2: long-poll until scanned
    start = time.time()
    while True:
        if time.time() - start > qr_timeout:
            raise RuntimeError(f"二维码扫码超时（{qr_timeout:.0f}s），请重试")
        try:
            poll_r = s.get(lp_url, timeout=65)  # server holds ~60 s; go slightly over
        except requests.Timeout:
            continue  # server closed the hold; loop again immediately
        except requests.RequestException as e:
            print(f"[QR] 轮询失败: {e}，继续等待...")
            time.sleep(2)
            continue

        if poll_r.status_code != 200:
            time.sleep(2)
            continue
        break

    # Step 3: extract credentials
    data = _parse(poll_r.text)
    print(f"[QR] 扫码成功，userId={data.get('userId')}")

    location = data.get("location", "")
    if not location:
        raise RuntimeError("扫码登录未返回 location URL")

    r = s.get(location)
    r.raise_for_status()

    cookies = _collect_cookies(s)
    service_token = cookies.get("iccc_app_api_serviceToken")
    if not service_token:
        raise RuntimeError("Failed to obtain serviceToken from QR login")

    return {
        "passToken": data.get("passToken", ""),
        "userId": str(data["userId"]),
        "ssecurity": data["ssecurity"],
        "serviceToken": service_token,
        "cUserId": data.get("cUserId") or cookies.get("cUserId", ""),
        "iccc_app_api_ph": cookies.get("iccc_app_api_ph", ""),
        "iccc_app_api_slh": cookies.get("iccc_app_api_slh", ""),
        "deviceId": device_id,
        "mobileId": str(uuid.uuid4()),
        "xmuuid": "XMGUEST-" + str(uuid.uuid4()),
    }


def refresh_service_token(auth: dict) -> dict:
    """
    Refresh serviceToken using stored passToken.
    No password needed. passToken is valid ~30 days.
    Raises RuntimeError if passToken is expired (triggers full re-login).
    """
    s = _login_session()
    s.cookies.set("passToken", auth["passToken"], domain="account.xiaomi.com")
    s.cookies.set("userId", auth["userId"], domain="account.xiaomi.com")
    s.cookies.set("deviceId", auth["deviceId"], domain="account.xiaomi.com")

    r = s.get(f"{ACCOUNT_BASE}/serviceLogin", params={
        "_json": "true",
        "appName": "com.mi.car.mobile",
        "sid": SID,
        "_locale": "zh_CN",
    })
    r.raise_for_status()
    data = _parse(r.text)

    if data.get("code") != 0:
        raise RuntimeError(f"passToken expired (code={data.get('code')}), need full re-login")

    r = s.get(data["location"])
    r.raise_for_status()

    cookies = _collect_cookies(s)
    service_token = cookies.get("iccc_app_api_serviceToken")
    if not service_token:
        raise RuntimeError("Failed to refresh serviceToken")

    auth.update({
        "serviceToken": service_token,
        "ssecurity": data.get("ssecurity", auth.get("ssecurity")),
        "cUserId": cookies.get("cUserId") or data.get("cUserId") or auth.get("cUserId"),
        "iccc_app_api_ph": cookies.get("iccc_app_api_ph") or auth.get("iccc_app_api_ph"),
        "iccc_app_api_slh": cookies.get("iccc_app_api_slh") or auth.get("iccc_app_api_slh"),
    })
    return auth


def load_auth() -> dict | None:
    if AUTH_FILE.exists():
        return json.loads(AUTH_FILE.read_text(encoding="utf-8"))
    return None


def save_auth(auth: dict) -> None:
    AUTH_FILE.write_text(
        json.dumps(auth, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get_auth() -> dict:
    """Return a valid auth dict, refreshing or QR-logging in as needed."""
    auth = load_auth()

    if auth:
        try:
            auth = refresh_service_token(auth)
            save_auth(auth)
            print(f"[auth] Refreshed serviceToken for userId={auth['userId']}")
            return auth
        except RuntimeError as e:
            print(f"[auth] Refresh failed: {e}. Falling back to QR login…")

    auth = qr_login()
    save_auth(auth)
    print(f"[auth] Logged in as userId={auth['userId']}")
    return auth


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _api(path: str, body: dict, auth: dict) -> dict:
    r = requests.post(
        ICCC_BASE + path,
        json=body,
        cookies={
            "xmuuid": auth["xmuuid"],
            "mobileId": auth["mobileId"],
            "serviceToken": auth["serviceToken"],
            "cUserId": auth["cUserId"],
            "iccc_app_api_ph": auth["iccc_app_api_ph"],
            "iccc_app_api_slh": auth["iccc_app_api_slh"],
            "ssecurity": auth["ssecurity"],
        },
        headers={
            "User-Agent": UA_HTTP,
            "accept": "application/json, text/plain, */*",
        },
    )
    r.raise_for_status()
    return r.json()


def get_trip_detail(auth: dict, trip_id: str, vid: str) -> dict:
    """Get detailed info for a single trip."""
    return _api("/trip/detail", {"tripId": trip_id, "vid": vid}, auth)


def get_trip_track(auth: dict, trip_id: str, vid: str) -> dict:
    """Get GPS track for a single trip."""
    return _api("/trip/track", {"tripId": trip_id, "vid": vid}, auth)


def get_trip_list(auth: dict, vid: str, begin_date: str | None = None, direction: int = 1) -> dict:
    """List trips. begin_date is YYYYMMDD (e.g. '20260423'), direction 1 = before that date."""
    body: dict = {"vid": vid, "direction": direction}
    if begin_date:
        body["beginDate"] = begin_date
    return _api("/trip/list", body, auth)


def get_trip_months(auth: dict, vid: str) -> dict:
    """List months that have trip data."""
    return _api("/trip/queryTripMonths", {"vid": vid}, auth)


def get_month_stats(auth: dict, vid: str, year_month: str) -> dict:
    """Get driving statistics for a given month (year_month e.g. '202603')."""
    return _api("/trip/queryMonthStats", {"vid": vid, "month": year_month}, auth)


def get_data_page(auth: dict, vid: str, page: int = 1, page_size: int = 20) -> dict:
    """Paginated trip data."""
    return _api("/trip/dataPage", {"vid": vid, "page": page, "pageSize": page_size}, auth)


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys
    from datetime import date

    vid = os.environ.get("XIAOMI_VID", "")
    if not vid:
        print("Error: set XIAOMI_VID to your vehicle VIN")
        sys.exit(1)

    if "--refresh" in sys.argv:
        stored = load_auth()
        if not stored:
            print("No saved session found.")
            sys.exit(1)
        old_token = stored["serviceToken"][:20]
        print(f"[refresh] Old serviceToken prefix: {old_token}…")
        try:
            auth = refresh_service_token(stored)
            save_auth(auth)
            new_token = auth["serviceToken"][:20]
            changed = "CHANGED" if new_token != old_token else "unchanged"
            print(f"[refresh] New serviceToken prefix: {new_token}… ({changed})")
        except RuntimeError as e:
            print(f"[refresh] Failed: {e}")
            sys.exit(1)
        sys.exit(0)

    try:
        auth = get_auth()
    except RuntimeError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # List available months
    months_resp = get_trip_months(auth, vid)
    print("\n=== Trip months ===")
    print(json.dumps(months_resp, ensure_ascii=False, indent=2))

    # Flatten {year, months[]} list → ["YYYYMM", ...] sorted descending
    year_entries = (months_resp.get("data") or {}).get("list") or []
    months = [
        f"{entry['year']}{m}"
        for entry in reversed(year_entries)
        for m in reversed(entry.get("months", []))
    ]
    if not months:
        print("No trip data found.")
        sys.exit(0)

    latest_month = months[0]

    # Show stats for the most recent month
    stats = get_month_stats(auth, vid, latest_month)
    if stats.get("code") != 200:
        print(f"[warn] queryMonthStats failed: {stats}")
    else:
        print(f"\n=== Stats for {latest_month} ===")
        print(json.dumps(stats, ensure_ascii=False, indent=2))

    # List trips up to today
    today = date.today().strftime("%Y%m%d")
    trips_resp = get_trip_list(auth, vid, begin_date=today)
    if trips_resp.get("code") != 200:
        print(f"[warn] trip/list failed: {trips_resp}")
        sys.exit(1)
    print(f"\n=== Recent trips (before {today}) ===")
    print(json.dumps(trips_resp, ensure_ascii=False, indent=2))

    # Show detail for the most recent trip
    trips = trips_resp.get("data") or []
    if isinstance(trips, dict):
        trips = trips.get("list") or []
    trip_id = None
    for day in trips:
        car_trips = day.get("carTripList") or []
        if car_trips:
            trip_id = car_trips[0].get("tripId")
            break
    if trip_id:
        detail = get_trip_detail(auth, trip_id, vid)
        print(f"\n=== Detail: {trip_id} ===")
        print(json.dumps(detail, ensure_ascii=False, indent=2))
