#!/usr/bin/env python3
"""현대 Developers API로 누적 주행거리를 받아 car/odo.json 에 저장.

GitHub Actions에서 실행됩니다 (.github/workflows/hyundai-odometer.yml).
필요한 Secrets:
  HYUNDAI_CLIENT_ID, HYUNDAI_CLIENT_SECRET   - 현대 Developers 콘솔 > 설정
  HYUNDAI_REFRESH_TOKEN                      - 최초 연결 후 자동 저장됨
  GH_PAT                                     - 새 refresh token을 Secret에 다시 저장할 때 사용
선택 Variables:
  HYUNDAI_REDIRECT_URI  - 콘솔에 등록한 Redirect URI (최초 연결 때 필요)
  HYUNDAI_CAR_ID        - 비워두면 내 차량 리스트의 첫 번째 차
  HYUNDAI_API_HOST      - 데이터 API 호스트 (기본: dev, 상용화 프로젝트면 prd)
"""
import base64, json, os, subprocess, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta

AUTH_HOST = os.environ.get("HYUNDAI_AUTH_HOST") or "https://prd.kr-ccapi.hyundai.com"
API_HOST = os.environ.get("HYUNDAI_API_HOST") or "https://dev.kr-ccapi.hyundai.com"
OUT = os.environ.get("ODO_PATH") or "car/odo.json"
CONSENT = os.path.join(os.path.dirname(OUT) or ".", "consent.json")
KST = timezone(timedelta(hours=9))


def env(name, required=True):
    v = (os.environ.get(name) or "").strip()
    if required and not v:
        sys.exit(f"::error::{name} 가 설정되지 않았습니다 (Settings > Secrets and variables > Actions)")
    return v


def mask(v):
    if v:
        print(f"::add-mask::{v}")


def http(method, url, headers, data=None):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        sys.exit(f"::error::{method} {url.split('?')[0]} -> HTTP {e.code}: {txt[:500]}")
    try:
        return json.loads(txt)
    except json.JSONDecodeError:
        sys.exit(f"::error::JSON 아님: {txt[:300]}")


def get_tokens():
    cid, sec = env("HYUNDAI_CLIENT_ID"), env("HYUNDAI_CLIENT_SECRET")
    mask(sec)
    basic = base64.b64encode(f"{cid}:{sec}".encode()).decode()
    h = {"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"}
    raw = env("AUTH_CODE", required=False)
    code, _, pubkey = raw.partition("|")
    code = code.strip()
    if code:
        data = {"grant_type": "authorization_code", "code": code,
                "redirect_uri": env("HYUNDAI_REDIRECT_URI")}
        print("최초 연결: 인증 코드로 토큰 발급")
    else:
        rt = env("HYUNDAI_REFRESH_TOKEN")
        mask(rt)
        data = {"grant_type": "refresh_token", "refresh_token": rt}
    tok = http("POST", f"{AUTH_HOST}/api/v1/user/oauth2/token", h, data)
    at, new_rt = tok.get("access_token"), tok.get("refresh_token")
    mask(at); mask(new_rt)
    if not at:
        sys.exit(f"::error::토큰 응답에 access_token 없음 (키: {list(tok)})")
    old_rt = os.environ.get("HYUNDAI_REFRESH_TOKEN", "")
    if new_rt and new_rt != old_rt:
        save_secret("HYUNDAI_REFRESH_TOKEN", new_rt)
    return at, pubkey.strip()


def save_secret(name, value):
    pat = os.environ.get("GH_PAT")
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not pat or not repo:
        print(f"::warning::GH_PAT가 없어 {name} 을 저장하지 못했습니다. 다음 실행이 실패할 수 있습니다.")
        return
    r = subprocess.run(["gh", "secret", "set", name, "--repo", repo, "--body", value],
                       env={**os.environ, "GH_TOKEN": pat}, capture_output=True, text=True)
    if r.returncode:
        print(f"::warning::{name} 저장 실패: {r.stderr.strip()[:300]}")
    else:
        print(f"{name} 갱신 저장 완료")


def find_car_id(at):
    cid = env("HYUNDAI_CAR_ID", required=False)
    if cid:
        return cid
    res = http("GET", f"{API_HOST}/api/v1/car/profile/carlist",
               {"Authorization": f"Bearer {at}", "Content-Type": "application/json"})
    cars = res.get("cars") or res.get("carList") or []
    if not cars:
        sys.exit(f"::error::내 차량 리스트가 비어 있습니다 (응답 키: {list(res)}). 콘솔에서 차량 '활성' 상태와 서비스 이용 동의를 확인하세요.")
    car = cars[0]
    names = [c.get("carName") or c.get("carSellname") or "?" for c in cars]
    print(f"차량 {len(cars)}대 중 첫 번째 사용: {names}")
    return car.get("carId")


def pick_odometer(res):
    """응답 형식이 바뀌어도 최대한 찾아냄. 반환: (km, 측정시각 문자열|None)"""
    item = None
    for key in ("odometers", "odometer", "data"):
        v = res.get(key)
        if isinstance(v, list) and v:
            item = max(v, key=lambda x: str(x.get("timestamp") or x.get("date") or ""))
            break
        if isinstance(v, dict):
            item = v
            break
    if item is None:
        item = res
    val = item.get("value", item.get("odometer"))
    if val is None:
        sys.exit(f"::error::주행거리 값을 찾지 못했습니다. 응답 키: {list(res)} / {list(item)}")
    km = float(val)
    unit = item.get("unit")
    # 현대 API 단위 코드: 1=km, 3=mile (그 외/없음은 km로 간주)
    if unit in (3, "3", "mile", "mi"):
        km *= 1.609344
    ts = str(item.get("timestamp") or item.get("date") or "")
    digits = "".join(ch for ch in ts if ch.isdigit())
    if len(digits) >= 8 and "-" not in ts:
        # 20261019090000 → 2026-10-19T09:00:00
        d = digits.ljust(14, "0")
        ts = f"{d[:4]}-{d[4:6]}-{d[6:8]}T{d[8:10]}:{d[10:12]}:{d[12:14]}"
    return round(km, 1), (ts or None)


def write_consent(at, pubkey_b64):
    """데이터 제공 동의용 access token을 hyundai.html 이 만든 공개키로 암호화해서 저장.
    저장소가 공개라도 복호화 키는 그 브라우저에만 있음."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    pub = serialization.load_der_public_key(base64.b64decode(pubkey_b64))
    key = AESGCM.generate_key(bit_length=256)
    iv = os.urandom(12)
    ct = AESGCM(key).encrypt(iv, at.encode(), None)
    ek = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    b = lambda x: base64.b64encode(x).decode()
    os.makedirs(os.path.dirname(CONSENT) or ".", exist_ok=True)
    with open(CONSENT, "w", encoding="utf-8") as f:
        json.dump({"ek": b(ek), "iv": b(iv), "ct": b(ct),
                   "createdAt": datetime.now(KST).isoformat(timespec="seconds")}, f)
    print(f"{CONSENT} 저장 — 같은 브라우저에서 hyundai.html 을 열어 동의를 진행하세요")


def main():
    at, pubkey = get_tokens()
    if pubkey:
        write_consent(at, pubkey)
        return
    if os.path.exists(CONSENT):
        os.remove(CONSENT)
        print("동의용 임시 파일 삭제")
    car_id = find_car_id(at)
    mask(car_id)
    res = http("GET", f"{API_HOST}/api/v1/car/status/{car_id}/odometer",
               {"Authorization": f"Bearer {at}", "Content-Type": "application/json"})
    km, ts = pick_odometer(res)
    print(f"누적 주행거리: {km} km (측정 {ts})")

    now = datetime.now(KST).isoformat(timespec="seconds")
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            prev = {}
    if prev.get("km") == km:
        print("변경 없음 — 저장 안 함")
        return
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"km": km, "measuredAt": ts, "fetchedAt": now, "source": "hyundai"}, f, ensure_ascii=False, indent=1)
    print(f"{OUT} 저장")


if __name__ == "__main__":
    main()
