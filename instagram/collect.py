#!/usr/bin/env python3
"""인스타그램 릴스 지표 수집기.

Instagram Graph API에서 릴스 목록과 인사이트, 계정 스냅샷을 받아 data/ 아래에 쌓는다.
표준 라이브러리만 쓴다.

    python3 collect.py --check          # 연결 확인
    python3 collect.py --limit 30       # 릴스 30개 수집
    python3 collect.py --refresh-token  # 장기 토큰 갱신

값이 없으면 비워 둔다. 어떤 경우에도 지어내지 않는다.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
THUMBS = os.path.join(DATA, "thumbs")
VIDEOS = os.path.join(DATA, "videos")

API_VERSION = "v25.0"

# 릴스에서 받을 수 있는 지표. 계정·게시물에 따라 일부가 거부되면 하나씩 빼고 다시 부른다.
REEL_METRICS = [
    "views",
    "reach",
    "likes",
    "comments",
    "saved",
    "shares",
    "total_interactions",
    "ig_reels_avg_watch_time",
]

MEDIA_FIELDS = [
    "id",
    "caption",
    "media_type",
    "media_product_type",
    "media_url",
    "thumbnail_url",
    "permalink",
    "timestamp",
    "like_count",
    "comments_count",
]

ACCOUNT_FIELDS = ["id", "username", "name", "followers_count", "follows_count", "media_count"]


class ApiError(RuntimeError):
    def __init__(self, payload, status=None):
        self.payload = payload
        self.status = status
        err = (payload or {}).get("error", {})
        self.code = err.get("code")
        self.subcode = err.get("error_subcode")
        self.message = err.get("message") or str(payload)
        super().__init__(self.message)


# ---------------------------------------------------------------- 환경 설정


def load_env():
    """.env 를 읽어 os.environ 에 채운다. 이미 들어 있는 값은 건드리지 않는다."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def config():
    load_env()
    token = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if not token:
        die(
            "IG_ACCESS_TOKEN 이 없다.\n"
            "  instagram/.env 를 만들고 토큰을 넣어라. 방법은 SETUP.md 3~4번."
        )
    login = os.environ.get("IG_LOGIN", "instagram").strip().lower()
    host = "graph.facebook.com" if login == "facebook" else "graph.instagram.com"
    return {
        "token": token,
        "host": os.environ.get("IG_HOST", host).strip(),
        "version": os.environ.get("IG_API_VERSION", API_VERSION).strip(),
        "user_id": os.environ.get("IG_USER_ID", "").strip() or "me",
        "app_id": os.environ.get("IG_APP_ID", "").strip(),
        "app_secret": os.environ.get("IG_APP_SECRET", "").strip(),
        "login": login,
    }


def die(msg, code=1):
    print("\n[멈춤] " + msg + "\n", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- API 호출


def call(cfg, path, params=None, tries=3):
    params = dict(params or {})
    params["access_token"] = cfg["token"]
    url = "https://{host}/{ver}/{path}?{qs}".format(
        host=cfg["host"],
        ver=cfg["version"],
        path=path.lstrip("/"),
        qs=urllib.parse.urlencode(params),
    )
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=45) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(body)
            except ValueError:
                payload = {"error": {"message": body[:400]}}
            err = ApiError(payload, exc.code)
            # 쓰로틀(4, 17, 32, 613)이면 잠깐 쉬었다 다시
            if err.code in (4, 17, 32, 613) and attempt < tries - 1:
                wait = 2 ** (attempt + 3)
                print("  · 호출 한도에 걸렸다. {}초 쉬었다 다시 시도.".format(wait))
                time.sleep(wait)
                last = err
                continue
            raise err
        except urllib.error.URLError as exc:
            last = RuntimeError("네트워크 오류: {}".format(exc.reason))
            if attempt < tries - 1:
                time.sleep(2 ** (attempt + 1))
                continue
            raise last
    raise last


def call_paged(cfg, path, params, max_items):
    """커서를 따라가며 최대 max_items 개를 모은다."""
    out = []
    params = dict(params)
    params.setdefault("limit", 50)
    while len(out) < max_items:
        page = call(cfg, path, params)
        rows = page.get("data") or []
        out.extend(rows)
        after = (page.get("paging") or {}).get("cursors", {}).get("after")
        if not rows or not after or not (page.get("paging") or {}).get("next"):
            break
        params["after"] = after
    return out[:max_items]


# ---------------------------------------------------------------- 인사이트


def fetch_insights(cfg, media_id, metrics=None):
    """지표를 받되, 거부당한 지표는 빼고 다시 부른다.

    반환: (값 사전, 못 받은 지표 목록)
    """
    wanted = list(metrics or REEL_METRICS)
    dropped = []
    while wanted:
        try:
            payload = call(cfg, "{}/insights".format(media_id), {"metric": ",".join(wanted)})
        except ApiError as err:
            bad = [m for m in wanted if m in (err.message or "")]
            if bad:
                for m in bad:
                    wanted.remove(m)
                    dropped.append(m)
                continue
            if err.code == 100 and len(wanted) > 1:
                # 어떤 지표가 문제인지 말해주지 않는 경우: 반씩 줄여 본다
                half = wanted[: len(wanted) // 2]
                dropped.extend(wanted[len(wanted) // 2 :])
                wanted = half
                continue
            return {}, wanted + dropped
        values = {}
        for row in payload.get("data") or []:
            name = row.get("name")
            vals = row.get("values") or []
            if vals and vals[0].get("value") is not None:
                values[name] = vals[0]["value"]
            elif row.get("total_value", {}).get("value") is not None:
                values[name] = row["total_value"]["value"]
        return values, dropped
    return {}, dropped


def fetch_account_insights(cfg, ig_id, day):
    """하루치 계정 지표. 못 받으면 빈 사전."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    params = {
        "metric": "reach,views,accounts_engaged,total_interactions",
        "period": "day",
        "metric_type": "total_value",
        "since": int(start.timestamp()),
        "until": int((start + timedelta(days=1)).timestamp()),
    }
    try:
        payload = call(cfg, "{}/insights".format(ig_id), params)
    except ApiError as err:
        print("  · 계정 인사이트를 못 받았다 ({}): {}".format(day, err.message[:120]))
        return {}
    out = {}
    for row in payload.get("data") or []:
        value = (row.get("total_value") or {}).get("value")
        if value is None:
            vals = row.get("values") or []
            value = vals[0].get("value") if vals else None
        if value is not None:
            out[row.get("name")] = value
    return out


# ---------------------------------------------------------------- 파일 입출력


def read_json(path, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return fallback


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
    os.replace(tmp, path)


def download(url, dest, quiet=False):
    if not url or os.path.exists(dest):
        return os.path.exists(dest)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp, open(dest + ".tmp", "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                fh.write(chunk)
        os.replace(dest + ".tmp", dest)
        return True
    except Exception as exc:  # 썸네일 하나 못 받았다고 전체를 멈출 이유는 없다
        if not quiet:
            print("  · 내려받기 실패 {}: {}".format(os.path.basename(dest), exc))
        if os.path.exists(dest + ".tmp"):
            os.remove(dest + ".tmp")
        return False


# ---------------------------------------------------------------- 명령들


def cmd_check(cfg):
    me = call(cfg, cfg["user_id"], {"fields": ",".join(ACCOUNT_FIELDS)})
    print("\n연결됨")
    print("  계정      @{}".format(me.get("username", "?")))
    print("  이름      {}".format(me.get("name") or "—"))
    print("  팔로워    {:,}".format(me.get("followers_count", 0)))
    print("  게시물    {:,}".format(me.get("media_count", 0)))
    print("  로그인    {} ({})".format(cfg["login"], cfg["host"]))

    media = call(cfg, "{}/media".format(me["id"]), {"fields": "id,media_product_type", "limit": 25})
    rows = media.get("data") or []
    reels = [m for m in rows if m.get("media_product_type") == "REELS"]
    print("  최근 25개 중 릴스 {}개".format(len(reels)))

    if reels:
        values, dropped = fetch_insights(cfg, reels[0]["id"])
        if values:
            print("  인사이트  받아짐 ({})".format(", ".join(sorted(values))))
        else:
            print("  인사이트  받지 못했다. 프로페셔널 계정인지, 스코프가 맞는지 확인하라.")
        if dropped:
            print("            이 계정에서 지원하지 않는 지표: {}".format(", ".join(dropped)))
    print()


def cmd_collect(cfg, args):
    quiet = args.quiet
    now = datetime.now(timezone.utc)

    me = call(cfg, cfg["user_id"], {"fields": ",".join(ACCOUNT_FIELDS)})
    ig_id = me["id"]
    if not quiet:
        print("@{} · 팔로워 {:,}".format(me.get("username"), me.get("followers_count", 0)))

    account = read_json(os.path.join(DATA, "account.json"), {})
    account.update(me)
    account["collected_at"] = now.isoformat()
    write_json(os.path.join(DATA, "account.json"), account)

    # --- 릴스 목록 -------------------------------------------------------
    want_types = {"REELS"} | ({"VIDEO"} if args.include_video else set())
    raw = call_paged(
        cfg,
        "{}/media".format(ig_id),
        {"fields": ",".join(MEDIA_FIELDS)},
        max_items=max(args.limit * 4, 100),
    )
    reels = [m for m in raw if m.get("media_product_type") in want_types][: args.limit]
    if not quiet:
        print("게시물 {}개 중 릴스 {}개를 고른다.".format(len(raw), len(reels)))
    if not reels:
        print("릴스가 잡히지 않았다. SETUP.md 의 '안 될 때'를 보라.")

    store = read_json(os.path.join(DATA, "reels.json"), {"reels": {}})
    store.setdefault("reels", {})
    dropped_all = set()

    for idx, media in enumerate(reels, 1):
        mid = media["id"]
        values, dropped = fetch_insights(cfg, mid)
        dropped_all.update(dropped)

        entry = store["reels"].get(mid, {})
        entry.update(
            {
                "id": mid,
                "permalink": media.get("permalink"),
                "caption": media.get("caption"),
                "media_type": media.get("media_type"),
                "media_product_type": media.get("media_product_type"),
                "timestamp": media.get("timestamp"),
                "thumbnail_url": media.get("thumbnail_url") or media.get("media_url"),
            }
        )
        # 지표: 인사이트를 우선 쓰고, 없으면 게시물 필드로 채운다. 그래도 없으면 None.
        metrics = {name: values.get(name) for name in REEL_METRICS}
        if metrics.get("likes") is None:
            metrics["likes"] = media.get("like_count")
        if metrics.get("comments") is None:
            metrics["comments"] = media.get("comments_count")
        entry["metrics"] = metrics
        entry["unavailable"] = sorted(dropped)
        entry["updated_at"] = now.isoformat()

        # 지표 변화를 볼 수 있도록 스냅샷을 남긴다 (하루 한 번만)
        hist = entry.setdefault("history", [])
        today = now.date().isoformat()
        if not hist or hist[-1].get("date") != today:
            hist.append({"date": today, **{k: v for k, v in metrics.items() if v is not None}})
            del hist[:-120]

        store["reels"][mid] = entry

        if media.get("thumbnail_url") or media.get("media_url"):
            download(
                media.get("thumbnail_url") or media.get("media_url"),
                os.path.join(THUMBS, "{}.jpg".format(mid)),
                quiet,
            )
        if args.videos and idx <= args.videos and media.get("media_url"):
            download(media["media_url"], os.path.join(VIDEOS, "{}.mp4".format(mid)), quiet)

        if not quiet:
            views = metrics.get("views")
            print(
                "  [{:>2}/{}] {} · 조회 {}".format(
                    idx,
                    len(reels),
                    (media.get("timestamp") or "")[:10],
                    "{:,}".format(views) if isinstance(views, int) else "N/A",
                )
            )

    store["collected_at"] = now.isoformat()
    store["unavailable_metrics"] = sorted(dropped_all)
    write_json(os.path.join(DATA, "reels.json"), store)

    # --- 계정 일별 추이 ---------------------------------------------------
    daily = read_json(os.path.join(DATA, "daily.json"), {"days": {}})
    daily.setdefault("days", {})

    today_key = now.date().isoformat()
    today_row = daily["days"].get(today_key, {})
    today_row["followers_count"] = me.get("followers_count")
    today_row["follows_count"] = me.get("follows_count")
    today_row["media_count"] = me.get("media_count")
    daily["days"][today_key] = today_row

    # 완결된 날만 계정 인사이트를 채운다. 이미 채워진 날은 건너뛴다.
    filled = 0
    for back in range(1, args.backfill_days + 1):
        day = (now - timedelta(days=back)).date()
        key = day.isoformat()
        row = daily["days"].get(key, {})
        if row.get("views") is not None or row.get("_insights_tried"):
            continue
        stats = fetch_account_insights(cfg, ig_id, day)
        row.update(stats)
        row["_insights_tried"] = True
        daily["days"][key] = row
        filled += 1
        if filled >= args.backfill_days:
            break

    daily["collected_at"] = now.isoformat()
    write_json(os.path.join(DATA, "daily.json"), daily)

    if not quiet:
        print("\n저장 완료 · 릴스 {}개 · 일별 기록 {}일치".format(len(store["reels"]), len(daily["days"])))
        if dropped_all:
            print("이 계정에서 받을 수 없는 지표: {} (대시보드에 N/A로 표기된다)".format(", ".join(sorted(dropped_all))))
        print("이어서: python3 build_dashboard.py")


def cmd_exchange(cfg, short_token):
    """단기 토큰 → 장기 토큰."""
    if cfg["login"] == "facebook":
        if not (cfg["app_id"] and cfg["app_secret"]):
            die("페이스북 로그인 방식은 .env 에 IG_APP_ID 와 IG_APP_SECRET 이 있어야 한다.")
        params = {
            "grant_type": "fb_exchange_token",
            "client_id": cfg["app_id"],
            "client_secret": cfg["app_secret"],
            "fb_exchange_token": short_token,
        }
        path = "oauth/access_token"
    else:
        if not cfg["app_secret"]:
            die("인스타그램 로그인 방식은 .env 에 IG_APP_SECRET 이 있어야 한다. (앱 대시보드 → 앱 설정 → 기본)")
        params = {"grant_type": "ig_exchange_token", "client_secret": cfg["app_secret"], "access_token": short_token}
        path = "access_token"

    url = "https://{}/{}?{}".format(cfg["host"], path, urllib.parse.urlencode(params))
    with urllib.request.urlopen(url, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    print("\n장기 토큰 (유효기간 {}일)\n".format(int(payload.get("expires_in", 0)) // 86400))
    print(payload["access_token"])
    print("\n이 값을 instagram/.env 의 IG_ACCESS_TOKEN 에 넣어라.\n")


def cmd_refresh(cfg):
    """장기 토큰 갱신. 만료 전에 돌려야 한다."""
    if cfg["login"] == "facebook":
        die("페이스북 로그인 방식의 페이지 토큰은 만료되지 않는다. 갱신할 것이 없다.")
    url = "https://{}/refresh_access_token?{}".format(
        cfg["host"],
        urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": cfg["token"]}),
    )
    with urllib.request.urlopen(url, timeout=45) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    new_token = payload["access_token"]
    days = int(payload.get("expires_in", 0)) // 86400

    env_path = os.path.join(HERE, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            lines = fh.readlines()
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith("IG_ACCESS_TOKEN="):
                lines[i] = "IG_ACCESS_TOKEN={}\n".format(new_token)
                found = True
        if not found:
            lines.append("IG_ACCESS_TOKEN={}\n".format(new_token))
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.writelines(lines)
        print("\n토큰을 갱신해 .env 에 새로 적었다. 앞으로 {}일 유효.\n".format(days))
    else:
        print("\n새 토큰 (유효기간 {}일)\n\n{}\n".format(days, new_token))


def main():
    parser = argparse.ArgumentParser(description="인스타그램 릴스 지표 수집기")
    parser.add_argument("--check", action="store_true", help="연결과 권한만 확인한다")
    parser.add_argument("--limit", type=int, default=30, help="모을 릴스 개수 (기본 30)")
    parser.add_argument("--include-video", action="store_true", help="릴스가 아닌 일반 동영상도 포함")
    parser.add_argument("--videos", type=int, default=0, help="최근 N개의 영상 파일도 내려받는다")
    parser.add_argument("--backfill-days", type=int, default=7, help="계정 인사이트를 며칠 전까지 채울지")
    parser.add_argument("--exchange-token", metavar="TOKEN", help="단기 토큰을 장기 토큰으로 바꾼다")
    parser.add_argument("--refresh-token", action="store_true", help="장기 토큰을 갱신한다")
    parser.add_argument("--quiet", action="store_true", help="진행 상황을 찍지 않는다")
    args = parser.parse_args()

    cfg = config()
    try:
        if args.exchange_token:
            cmd_exchange(cfg, args.exchange_token)
        elif args.refresh_token:
            cmd_refresh(cfg)
        elif args.check:
            cmd_check(cfg)
        else:
            cmd_collect(cfg, args)
    except ApiError as err:
        hint = ""
        if err.code == 190:
            hint = "\n  토큰이 만료됐거나 잘못됐다. SETUP.md 3번부터 다시."
        elif err.code == 10:
            hint = "\n  권한(스코프)이 모자란다. instagram_business_manage_insights 를 확인하라."
        elif err.code == 100:
            hint = "\n  요청한 필드나 지표를 이 계정/게시물에서 지원하지 않는다."
        die("API 오류 {}: {}{}".format(err.code, err.message, hint))


if __name__ == "__main__":
    main()
