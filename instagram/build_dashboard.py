#!/usr/bin/env python3
"""수집한 지표를 계산해 dashboard.html 을 만든다.

    python3 build_dashboard.py            # data/ 를 읽어 대시보드 생성
    python3 build_dashboard.py --demo     # 예시 데이터로 화면만 미리 본다

없는 값은 None 으로 두고 화면에 N/A 로 나간다. 채워 넣지 않는다.
"""

import argparse
import json
import os
import random
import re
import shutil
from datetime import datetime, timedelta, timezone
from statistics import median

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
OUT = os.path.join(HERE, "dashboard.html")
TEMPLATE = os.path.join(HERE, "template.html")
REPORT = os.path.join(HERE, "report", "report.md")

# 게시 후 이 일수가 지나야 순위 비교에 넣는다. 그 전에는 아직 수치가 자라는 중이다.
MATURITY_DAYS = 7

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]


def read_json(path, fallback):
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError):
        return fallback


def ratio(numerator, denominator):
    """둘 중 하나라도 없거나 분모가 0이면 None. 0으로 채우지 않는다."""
    if numerator is None or not denominator:
        return None
    return numerator / denominator


def parse_ts(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------- 릴스 계산


def build_reels(store, now):
    rows = []
    for entry in (store.get("reels") or {}).values():
        posted = parse_ts(entry.get("timestamp"))
        metrics = entry.get("metrics") or {}
        views = metrics.get("views")
        caption = (entry.get("caption") or "").strip()
        hashtags = re.findall(r"#([^\s#]+)", caption)
        headline = caption.split("\n", 1)[0].strip()
        headline = re.sub(r"#\S+", "", headline).strip()

        age_days = (now - posted).total_seconds() / 86400 if posted else None
        rows.append(
            {
                "id": entry.get("id"),
                "permalink": entry.get("permalink"),
                "timestamp": entry.get("timestamp"),
                "posted_label": posted.astimezone().strftime("%Y.%m.%d") if posted else "N/A",
                "weekday": WEEKDAYS[posted.weekday()] if posted else None,
                "hour": posted.hour if posted else None,
                "age_days": round(age_days, 1) if age_days is not None else None,
                "mature": bool(age_days is not None and age_days >= MATURITY_DAYS),
                "headline": headline[:90] or "(캡션 없음)",
                "caption": caption,
                "hashtags": hashtags[:12],
                "thumb": "thumbs/{}.jpg".format(entry.get("id")),
                "video": "videos/{}.mp4".format(entry.get("id")),
                "views": views,
                "reach": metrics.get("reach"),
                "likes": metrics.get("likes"),
                "comments": metrics.get("comments"),
                "saved": metrics.get("saved"),
                "shares": metrics.get("shares"),
                "interactions": metrics.get("total_interactions"),
                "watch_time": metrics.get("ig_reels_avg_watch_time"),
                "save_rate": ratio(metrics.get("saved"), views),
                "share_rate": ratio(metrics.get("shares"), views),
                "like_rate": ratio(metrics.get("likes"), views),
                "comment_rate": ratio(metrics.get("comments"), views),
                "engagement_rate": ratio(metrics.get("total_interactions"), views),
                "replay": ratio(views, metrics.get("reach")),
                "history": entry.get("history") or [],
            }
        )
    rows.sort(key=lambda r: r["timestamp"] or "", reverse=True)
    return rows


def add_ranks(rows):
    """성숙한 릴스들 사이에서만 백분위를 매긴다."""
    pool = [r for r in rows if r["mature"]]
    for key in ("views", "save_rate", "share_rate", "engagement_rate"):
        values = sorted(r[key] for r in pool if r[key] is not None)
        for row in rows:
            value = row.get(key)
            if value is None or not values or not row["mature"]:
                row[key + "_pct"] = None
                continue
            below = sum(1 for v in values if v < value)
            row[key + "_pct"] = round(100 * below / len(values))
    for row in rows:
        badges = []
        if not row["mature"]:
            badges.append({"label": "집계중", "tone": "muted"})
        if (row.get("save_rate_pct") or 0) >= 75:
            badges.append({"label": "저장률 상위", "tone": "signal"})
        if (row.get("share_rate_pct") or 0) >= 75:
            badges.append({"label": "공유율 상위", "tone": "signal"})
        if (row.get("views_pct") or 0) >= 75:
            badges.append({"label": "조회 상위", "tone": "accent"})
        if row["mature"] and (row.get("views_pct") or 100) <= 25 and (row.get("save_rate_pct") or 0) >= 60:
            badges.append({"label": "저평가", "tone": "warn"})
        row["badges"] = badges
    return rows


def summarize(rows):
    """계정 전체의 중앙값. 비교의 기준선이 된다."""
    pool = [r for r in rows if r["mature"]]

    def mid(key):
        values = [r[key] for r in pool if r[key] is not None]
        return median(values) if values else None

    return {
        "count": len(rows),
        "mature_count": len(pool),
        "median_views": mid("views"),
        "median_save_rate": mid("save_rate"),
        "median_share_rate": mid("share_rate"),
        "median_engagement_rate": mid("engagement_rate"),
        "median_watch_time": mid("watch_time"),
        "total_views": sum(r["views"] for r in rows if r["views"] is not None) or None,
    }


def group_stats(rows, keyfn, label):
    """요일·시간대처럼 묶어서 볼 때 쓴다. 표본 2개 미만인 묶음은 버린다."""
    buckets = {}
    for row in rows:
        if not row["mature"]:
            continue
        key = keyfn(row)
        if key is None:
            continue
        buckets.setdefault(key, []).append(row)
    out = []
    for key, group in buckets.items():
        if len(group) < 2:
            continue
        views = [r["views"] for r in group if r["views"] is not None]
        saves = [r["save_rate"] for r in group if r["save_rate"] is not None]
        out.append(
            {
                "key": key,
                "label": label(key),
                "n": len(group),
                "median_views": median(views) if views else None,
                "median_save_rate": median(saves) if saves else None,
            }
        )
    out.sort(key=lambda r: r["median_views"] or 0, reverse=True)
    return out


def hashtag_stats(rows, minimum=2):
    buckets = {}
    for row in rows:
        if not row["mature"]:
            continue
        for tag in set(row["hashtags"]):
            buckets.setdefault(tag, []).append(row)
    out = []
    for tag, group in buckets.items():
        if len(group) < minimum:
            continue
        views = [r["views"] for r in group if r["views"] is not None]
        saves = [r["save_rate"] for r in group if r["save_rate"] is not None]
        out.append(
            {
                "tag": tag,
                "n": len(group),
                "median_views": median(views) if views else None,
                "median_save_rate": median(saves) if saves else None,
            }
        )
    out.sort(key=lambda r: r["median_save_rate"] or 0, reverse=True)
    return out[:14]


# ---------------------------------------------------------------- 추이 계산


def build_trend(daily):
    days = daily.get("days") or {}
    series = []
    for date in sorted(days):
        row = days[date]
        series.append(
            {
                "date": date,
                "followers": row.get("followers_count"),
                "views": row.get("views"),
                "reach": row.get("reach"),
            }
        )
    # 팔로워 증감은 전날 대비로 뽑는다. 첫날은 비교 대상이 없으니 None.
    previous = None
    for row in series:
        row["follower_delta"] = (
            row["followers"] - previous if row["followers"] is not None and previous is not None else None
        )
        if row["followers"] is not None:
            previous = row["followers"]
    return series


def bucket_series(series, mode):
    """일별 기록을 주/월/연 단위로 접는다. 조회수는 합, 팔로워는 그 구간 마지막 값."""
    if mode == "day":
        return [
            {"label": r["date"][5:], "key": r["date"], "views": r["views"], "followers": r["followers"]}
            for r in series
        ]
    buckets = {}
    for row in series:
        date = datetime.strptime(row["date"], "%Y-%m-%d").date()
        if mode == "week":
            start = date - timedelta(days=date.weekday())
            key, label = start.isoformat(), start.strftime("%m/%d")
        elif mode == "month":
            key, label = date.strftime("%Y-%m"), date.strftime("%Y.%m")
        else:
            key, label = date.strftime("%Y"), date.strftime("%Y")
        slot = buckets.setdefault(key, {"label": label, "key": key, "views": None, "followers": None})
        if row["views"] is not None:
            slot["views"] = (slot["views"] or 0) + row["views"]
        if row["followers"] is not None:
            slot["followers"] = row["followers"]
    return [buckets[k] for k in sorted(buckets)]


# ---------------------------------------------------------------- 포지셔닝


def positioning(rows, account):
    """밖에서 셀 수 있는 지표로만 견준다.

    남의 계정은 저장·공유가 공개되지 않는다. 조회·좋아요·댓글, 그리고 팔로워
    대비 도달 배수까지가 정직하게 비교 가능한 전부다.
    """
    pool = [r for r in rows if r["mature"]]

    def mid(key):
        values = [r[key] for r in pool if r[key] is not None]
        return median(values) if values else None

    mine = {
        "username": account.get("username") or "내 계정",
        "label": "내 계정",
        "mine": True,
        "followers": account.get("followers_count"),
        "sample_size": len(pool),
        "median_views": mid("views"),
        "median_likes": mid("likes"),
        "median_comments": mid("comments"),
        "note": None,
    }

    others = []
    raw = read_json(os.path.join(DATA, "competitors.json"), None)
    for item in (raw or {}).get("accounts", []):
        others.append(
            {
                "username": item.get("username"),
                "label": item.get("label"),
                "mine": False,
                "followers": item.get("followers"),
                "sample_size": item.get("sample_size"),
                "median_views": item.get("median_views"),
                "median_likes": item.get("median_likes"),
                "median_comments": item.get("median_comments"),
                "note": item.get("note"),
            }
        )

    everyone = [mine] + others
    for row in everyone:
        row["reach_multiple"] = ratio(row["median_views"], row["followers"])
        row["like_rate"] = ratio(row["median_likes"], row["median_views"])
        row["comment_rate"] = ratio(row["median_comments"], row["median_views"])
    return {"rows": everyone, "has_competitors": bool(others)}


# ---------------------------------------------------------------- 보고서


def markdown_to_html(text):
    """보고서용 최소 변환기. 제목·목록·강조·표만 다룬다."""
    lines = text.replace("\r\n", "\n").split("\n")
    out, in_list, in_table = [], False, False

    def inline(s):
        s = (
            s.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
        s = re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        s = re.sub(r"\[(.+?)\]\((https?://[^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', s)
        return s

    def close_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    def close_table():
        nonlocal in_table
        if in_table:
            out.append("</tbody></table></div>")
            in_table = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            close_list()
            close_table()
            continue
        if re.match(r"^\|.*\|$", line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.match(r"^:?-{2,}:?$", c) for c in cells):
                continue
            if not in_table:
                out.append('<div class="tablewrap"><table><tbody>')
                in_table = True
            out.append("<tr>" + "".join("<td>{}</td>".format(inline(c)) for c in cells) + "</tr>")
            continue
        close_table()
        heading = re.match(r"^(#{1,4})\s+(.*)$", line)
        if heading:
            close_list()
            level = min(len(heading.group(1)) + 1, 5)
            out.append("<h{0}>{1}</h{0}>".format(level, inline(heading.group(2))))
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>{}</li>".format(inline(bullet.group(1))))
            continue
        close_list()
        out.append("<p>{}</p>".format(inline(line)))
    close_list()
    close_table()
    return "\n".join(out)


def load_report():
    if not os.path.exists(REPORT):
        return None
    with open(REPORT, encoding="utf-8") as fh:
        text = fh.read().strip()
    if not text:
        return None
    body = re.sub(r"[#*`|>\-\s]", "", text)
    return {"html": markdown_to_html(text), "chars": len(body)}


# ---------------------------------------------------------------- 예시 데이터


def demo_payload(now):
    """화면 확인용. 실제 계정 수치가 아니며 대시보드에 그렇게 표시된다."""
    rng = random.Random(20260913)
    reels = {}
    for i in range(24):
        posted = now - timedelta(days=i * 2.4, hours=rng.randrange(0, 20))
        views = int(rng.lognormvariate(9.1, 0.85))
        save_rate = rng.uniform(0.004, 0.061)
        share_rate = rng.uniform(0.002, 0.038)
        mid = "demo{:03d}".format(i)
        reels[mid] = {
            "id": mid,
            "permalink": "https://www.instagram.com/reel/{}/".format(mid),
            "caption": rng.choice(
                [
                    "3분 만에 끝내는 정리법\n#정리 #자취 #살림팁",
                    "이거 모르면 손해봅니다\n#정보 #꿀팁",
                    "매일 아침 이것만 합니다\n#루틴 #아침",
                    "초보가 제일 많이 하는 실수\n#입문 #가이드 #정리",
                    "돈 안 쓰고 바꾸는 법\n#절약 #살림팁",
                ]
            ),
            "timestamp": posted.isoformat().replace("+00:00", "+0000"),
            "metrics": {
                "views": views,
                "reach": int(views / rng.uniform(1.05, 1.45)),
                "likes": int(views * rng.uniform(0.02, 0.075)),
                "comments": int(views * rng.uniform(0.0008, 0.006)),
                "saved": int(views * save_rate),
                "shares": int(views * share_rate),
                "total_interactions": None,
                "ig_reels_avg_watch_time": round(rng.uniform(3.2, 14.5), 1),
            },
            "history": [],
        }
        m = reels[mid]["metrics"]
        m["total_interactions"] = m["likes"] + m["comments"] + m["saved"] + m["shares"]

    days, followers = {}, 4120
    for back in range(89, -1, -1):
        date = (now - timedelta(days=back)).date().isoformat()
        followers += rng.randrange(-4, 26)
        days[date] = {
            "followers_count": followers,
            "views": int(rng.lognormvariate(8.4, 0.5)),
            "reach": int(rng.lognormvariate(8.1, 0.5)),
        }
    account = {"username": "example_account", "name": "예시 계정", "followers_count": followers, "media_count": 212}
    return {"reels": reels}, {"days": days}, account


# ---------------------------------------------------------------- 조립


def main():
    parser = argparse.ArgumentParser(description="릴스 대시보드 생성기")
    parser.add_argument("--demo", action="store_true", help="예시 데이터로 화면만 확인한다")
    parser.add_argument("--out", default=OUT, help="출력 경로")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)

    if args.demo:
        store, daily, account = demo_payload(now)
    else:
        store = read_json(os.path.join(DATA, "reels.json"), {"reels": {}})
        daily = read_json(os.path.join(DATA, "daily.json"), {"days": {}})
        account = read_json(os.path.join(DATA, "account.json"), {})

    rows = add_ranks(build_reels(store, now))
    series = build_trend(daily)
    collected = store.get("collected_at") or daily.get("collected_at")

    payload = {
        "demo": args.demo,
        "generated_at": now.isoformat(),
        "collected_at": collected,
        "account": account,
        "reels": rows,
        "summary": summarize(rows),
        "trend": {
            "day": bucket_series(series, "day"),
            "week": bucket_series(series, "week"),
            "month": bucket_series(series, "month"),
            "year": bucket_series(series, "year"),
        },
        "by_weekday": group_stats(rows, lambda r: r["weekday"], lambda k: k + "요일"),
        "by_hour": group_stats(
            rows,
            lambda r: None if r["hour"] is None else (r["hour"] // 3) * 3,
            lambda k: "{:02d}–{:02d}시".format(k, k + 3),
        ),
        "hashtags": hashtag_stats(rows),
        "positioning": positioning(rows, account),
        "unavailable_metrics": store.get("unavailable_metrics") or [],
        "maturity_days": MATURITY_DAYS,
        "report": load_report(),
    }

    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    marker = '<script id="ig-data" type="application/json">'
    start = html.index(marker) + len(marker)
    end = html.index("</script>", start)
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = html[:start] + blob + html[end:]

    # 두 벌을 쓴다. 로컬에서 바로 열 파일과, Artifact 로 발행할 조각.
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(
            '<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            "</head>\n<body>\n" + html + "\n</body>\n</html>\n"
        )
    fragment = os.path.join(os.path.dirname(os.path.abspath(args.out)), "dashboard.artifact.html")
    with open(fragment, "w", encoding="utf-8") as fh:
        fh.write(html)

    # 썸네일·영상은 대시보드 옆에 그대로 있어야 보인다
    out_dir = os.path.dirname(os.path.abspath(args.out))
    for name in ("thumbs", "videos"):
        src = os.path.join(DATA, name)
        dst = os.path.join(out_dir, name)
        if os.path.isdir(src) and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copytree(src, dst, dirs_exist_ok=True)

    if not args.quiet:
        summary = payload["summary"]
        print("만들었다: {}".format(args.out))
        print("         {} (Artifact 발행용)".format(fragment))
        print("  릴스 {}개 (비교 대상 {}개, 나머지는 게시 {}일 미만)".format(
            summary["count"], summary["mature_count"], MATURITY_DAYS))
        print("  일별 기록 {}일".format(len(series)))
        if payload["report"]:
            print("  보고서 {:,}자".format(payload["report"]["chars"]))
        else:
            print("  보고서 없음 — report/report.md 를 만들면 대시보드 하단에 붙는다")
        if args.demo:
            print("\n  ※ 예시 데이터다. 실제 계정 수치가 아니다.")


if __name__ == "__main__":
    main()
