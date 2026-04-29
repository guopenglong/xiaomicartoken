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
APP_VERSION = "2.3.25"
APP_VERSION_CODE = "26040914"
ANDROID_SDK = "36"

_DEVICES = [
    ("2210132C",   "Xiaomi 13 Pro",  "13", "TQ2A.230505.002"),
    ("23127PN0CC",  "Xiaomi 14",      "14", "UQ1A.240105.004"),
    ("2312DRA49G",  "Xiaomi 14 Pro",  "14", "UQ1A.240105.004"),
    ("25128PNA1C",  "Xiaomi 15 Ultra","15", "BP2A.250605.031.A3"),
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


def get_device_list(auth: dict) -> list[dict]:
    """Query Xiaomi account device list to find phone's real deviceId."""
    s = _login_session()
    s.cookies.set("passToken", auth["passToken"], domain="account.xiaomi.com")
    s.cookies.set("userId", auth["userId"], domain="account.xiaomi.com")
    s.cookies.set("deviceId", auth["deviceId"], domain="account.xiaomi.com")

    r = s.get("https://account.xiaomi.com/v3/device/user/list", params={
        "userId": auth["userId"],
        "page": 1,
        "pageSize": 20,
        "_locale": "zh_CN",
    })
    r.raise_for_status()
    data = _parse(r.text)
    return data.get("data", {}).get("records", [])


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
    code, _, _, build = _DEVICES[-1]
    r = requests.post(
        ICCC_BASE + path,
        json=body,
        cookies={
            "serviceToken": auth["serviceToken"],
            "cUserId": auth["cUserId"],
            "ph": auth["iccc_app_api_ph"],
            "slh": auth["iccc_app_api_slh"],
            "ssecurity": auth["ssecurity"],
        },
        headers={
            "User-Agent": UA_HTTP,
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN",
            "content-type": "application/json; charset=UTF-8",
            "request-source": "app",
            "mobileid": auth["mobileId"],
            "deviceostype": "android",
            "devicevendor": "Xiaomi",
            "devicemodel": code,
            "deviceosversion": build,
            "androidsdkversion": ANDROID_SDK,
            "deviceappversion": APP_VERSION,
            "deviceappversionname": APP_VERSION,
            "deviceappversioncode": APP_VERSION_CODE,
            "devicereleasechannel": "1",
            "devicepackagetype": "1",
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


def get_car_list(auth: dict) -> list[dict]:
    """Return owned cars with vid, vin, carModel, carPlate."""
    code, _, _, build = _DEVICES[-1]
    resp = _api("/clientbusiness/IcccUserAuthService/getUserCarListV2", {
        "viewList": ["SIDE_VIEW_DARK", "TOP_VIEW_DARK", "OBLIQUE_VIEW_HALF"],
        "deviceAppVersion": APP_VERSION,
        "deviceModel": code,
        "deviceOsType": "android",
        "deviceOsVersion": build,
        "deviceVendor": "Xiaomi",
    }, auth)
    data = resp.get("data", {})
    return data.get("ownCarList", []) + data.get("authorizedCarList", [])


def login_device(auth: dict) -> None:
    """Register this device session with ICCC — must be called before car/trip APIs."""
    code, _, _, build = _DEVICES[-1]
    _api("/clientbusiness/IcccUserDeviceService/loginDevice", {
        "infoBoxGroups": ["interact", "notice", "car", "chosen"],
        "pushType": "",
        "regId": "",
        "supportDuration": 1,
        "deviceAppVersion": APP_VERSION,
        "deviceModel": code,
        "deviceOsType": "android",
        "deviceOsVersion": build,
        "deviceVendor": "Xiaomi",
    }, auth)


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import os
    import sys

    vid = os.environ.get("XIAOMI_VID", "")

    if "--devices" in sys.argv:
        stored = load_auth()
        if not stored:
            print("No saved session found. Run without --devices first to login.")
            sys.exit(1)
        try:
            devices = get_device_list(stored)
        except Exception as e:
            print(f"[devices] Failed: {e}")
            sys.exit(1)
        print(f"[devices] Found {len(devices)} device(s):\n")
        for i, d in enumerate(devices, 1):
            print(f"  [{i}] {json.dumps(d, ensure_ascii=False, indent=4)}")
        sys.exit(0)

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

    try:
        login_device(auth)
        print("[auth] Device session registered with ICCC")
    except Exception as e:
        print(f"Error registering device: {e}")
        sys.exit(1)

    # If phone deviceId not yet selected, prompt user to pick from device list
    if not auth.get("phoneDeviceId"):
        try:
            devices = get_device_list(auth)
        except Exception as e:
            print(f"Error fetching device list: {e}")
            sys.exit(1)

        phone_devices = [d for d in devices if d.get("deviceId", "").startswith("an_")]
        if not phone_devices:
            phone_devices = devices  # fallback: show all

        print("[devices] 请选择手机设备 (用于鉴权):")
        for i, d in enumerate(phone_devices, 1):
            print(f"  [{i}] {d.get('modelName', '')}  deviceId={d.get('deviceId', '')}")

        while True:
            try:
                choice = int(input("请输入编号: ").strip())
                if 1 <= choice <= len(phone_devices):
                    selected = phone_devices[choice - 1]
                    break
            except ValueError:
                pass
            print(f"  请输入 1-{len(phone_devices)} 之间的数字")

        auth["deviceId"] = selected["deviceId"]
        auth["phoneDeviceId"] = selected["deviceId"]
        save_auth(auth)
        print(f"[devices] 已选择 {selected.get('modelName', '')}  deviceId={auth['deviceId']}")

    if not vid:
        try:
            cars = get_car_list(auth)
        except Exception as e:
            print(f"Error fetching car list: {e}")
            sys.exit(1)

        if not cars:
            print("Error: No cars found on this account")
            sys.exit(1)

        if len(cars) == 1:
            vid = cars[0]["vid"]
            c = cars[0]
            print(f"[cars] {c.get('carModel', '')} {c.get('carPlate', '')}  vid={vid}")
        else:
            print("[cars] 检测到多辆车，请选择:")
            for i, c in enumerate(cars, 1):
                print(f"  [{i}] {c.get('carModel', '')}  {c.get('carPlate', '')}  vid={c['vid']}")
            while True:
                try:
                    choice = int(input("请输入编号: ").strip())
                    if 1 <= choice <= len(cars):
                        vid = cars[choice - 1]["vid"]
                        break
                except ValueError:
                    pass
                print(f"  请输入 1-{len(cars)} 之间的数字")
