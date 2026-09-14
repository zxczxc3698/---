#!/usr/bin/env python3
"""여러 강의를 한 번에 편집한다.

목록 파일에 링크를 한 줄씩 적고 돌리면, 받아서 편집하고 결과를 모아 준다.
중간에 끊겨도 다시 돌리면 끝난 것은 건너뛰고 이어서 한다.

    python3 batch.py 목록.txt -o 결과 --mode speech --denoise light

목록 파일 한 줄의 형식 (제목은 없어도 된다):

    https://drive.google.com/file/d/<아이디>/view | 1강 체력검정 기준
    https://drive.google.com/file/d/<아이디>/view
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDITOR = HERE / "edit_lecture.py"


def hhmmss(sec):
    h, rem = divmod(int(max(0, sec)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def drive_id(url):
    """구글 드라이브 링크에서 파일 아이디만 뽑는다. 여러 형태를 받아 준다."""
    for pattern in (r"/file/d/([A-Za-z0-9_-]{20,})",
                    r"[?&]id=([A-Za-z0-9_-]{20,})",
                    r"/d/([A-Za-z0-9_-]{20,})"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def folder_files(url):
    """공개된 드라이브 폴더 페이지를 읽어 안에 든 영상들을 뽑는다.

    구글이 페이지 짜임새를 바꾸면 깨질 수 있다. 안 되면 링크를 한 줄씩 적어 주면 된다."""
    p = subprocess.run(["curl", "-sSL", "--max-time", "90",
                        "-H", "User-Agent: Mozilla/5.0", url],
                       capture_output=True, text=True)
    if p.returncode != 0 or not p.stdout:
        return []
    html = p.stdout
    # 폴더 페이지는 [ "<아이디>", ... ,"<파일이름>" ...] 꼴로 목록을 품고 있다
    found, seen = [], set()
    for fid, name in re.findall(
            r'"([A-Za-z0-9_-]{25,45})"\s*,\s*\[[^\]]*\]\s*,\s*"([^"]{1,120})"', html):
        if fid in seen or fid == drive_id(url):
            continue
        if not re.search(r"\.(mp4|mov|m4v|avi|mkv|webm)$", name, re.I):
            continue
        seen.add(fid)
        found.append({"url": f"https://drive.google.com/file/d/{fid}/view",
                      "title": Path(name).stem})
    found.sort(key=lambda j: j["title"])
    return found


def read_list(path):
    """목록 파일을 읽는다. 링크 | 제목."""
    jobs = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        url, _, title = line.partition("|")
        jobs.append({"url": url.strip(), "title": title.strip() or None})
    return jobs


def download(url, dst):
    """드라이브 링크든 직접 링크든 로컬 파일이든 가져온다."""
    local = Path(url).expanduser()
    if local.exists() and local.is_file():
        dst.unlink(missing_ok=True)
        try:                               # 같은 디스크면 링크로 족하다. 복사는 낭비다
            dst.symlink_to(local.resolve())
        except OSError:
            shutil.copy2(local, dst)
        return None
    if not re.match(r"https?://", url):
        return f"링크도 파일도 아닙니다: {url[:60]}"

    fid = drive_id(url)
    target = (f"https://drive.usercontent.google.com/download?id={fid}"
              f"&export=download&confirm=t") if fid else url
    p = subprocess.run(
        ["curl", "-sSL", "--fail", "--retry", "4", "--retry-delay", "2",
         "--max-time", "1800", "-o", str(dst), target],
        capture_output=True, text=True)
    if p.returncode != 0:
        return f"내려받기 실패 ({p.stderr.strip()[:120]})"
    if dst.stat().st_size < 100_000:
        if dst.is_symlink():
            return None
        head = dst.read_bytes()[:400].decode("utf-8", "ignore")
        if "<html" in head.lower():
            return "링크가 비공개입니다. 「링크가 있는 모든 사용자」로 바꿔 주세요"
        return f"파일이 너무 작습니다 ({dst.stat().st_size} bytes)"
    return None


def edit(src, out_dir, job, args):
    """편집기를 단계별로 부른다. 어느 단계에서 엎어졌는지 알 수 있게."""
    common = ["--out", str(out_dir)]
    steps = [
        ["cut", str(src), "--mode", args.mode, "--minlen", str(args.minlen),
         "--pad", str(args.pad)],
        ["sub", str(out_dir / "01_cut.mp4"), "--model", args.model,
         "--terms", str(args.terms), "--glossary", args.glossary],
    ]
    render = ["render", str(out_dir / "01_cut.mp4"), str(out_dir / "subs.srt"),
              "--denoise", args.denoise, "--crf", str(args.crf)]
    if job["title"]:
        render += ["--title", args.course, "--subtitle", job["title"]]
    if args.scale:
        render += ["--scale", str(args.scale)]
    if args.font:
        render += ["--font", args.font]
    chapters = args.chapter_dir and Path(args.chapter_dir) / f"{job['slug']}.txt"
    render += ["--chapters", str(chapters) if chapters and chapters.exists() else "/dev/null"]
    steps.append(render)

    for step in steps:
        p = subprocess.run([sys.executable, str(EDITOR)] + step + common,
                           capture_output=True, text=True)
        if p.returncode != 0:
            tail = (p.stderr or p.stdout).strip().splitlines()
            return f"{step[0]} 단계 실패: {tail[-1] if tail else '까닭 모름'}"
        for line in p.stdout.splitlines():
            if line.startswith("·"):
                print(f"    {line}")
    return None


def main():
    ap = argparse.ArgumentParser(description="강의 여러 개를 한 번에 편집")
    ap.add_argument("list", nargs="?", help="링크 목록 파일")
    ap.add_argument("--folder", default=None,
                    help="공개된 드라이브 폴더 링크. 안에 든 영상을 모두 처리한다")
    ap.add_argument("-o", "--out", default="결과", help="결과를 모을 폴더")
    ap.add_argument("--work", default=None, help="내려받은 원본을 둘 곳")
    ap.add_argument("--course", default="특수부대 합격 로드맵", help="제목 카드 첫 줄")
    ap.add_argument("--mode", choices=["sound", "speech"], default="sound")
    ap.add_argument("--denoise", choices=["off", "light", "strong"], default="off")
    ap.add_argument("--model", default="medium")
    ap.add_argument("--terms", default=str(HERE / "terms.txt"))
    ap.add_argument("--glossary", choices=["prompt", "hotwords", "both", "off"],
                    default="prompt")
    ap.add_argument("--chapter-dir", default=None,
                    help="강의별 챕터 파일이 든 폴더. <번호>.txt 를 찾는다")
    ap.add_argument("--minlen", type=float, default=0.6)
    ap.add_argument("--pad", type=float, default=0.12)
    ap.add_argument("--scale", type=int, default=None)
    ap.add_argument("--crf", type=int, default=20)
    ap.add_argument("--font", default=None)
    ap.add_argument("--keep-source", action="store_true",
                    help="편집이 끝나도 내려받은 원본을 지우지 않는다")
    args = ap.parse_args()

    if args.folder:
        jobs = folder_files(args.folder)
        if not jobs:
            raise SystemExit(
                "폴더에서 영상을 찾지 못했습니다.\n"
                "  폴더가 「링크가 있는 모든 사용자」로 열려 있는지 확인하고,\n"
                "  그래도 안 되면 링크를 한 줄씩 적은 목록 파일을 쓰세요.")
        listing = Path(args.out); listing.mkdir(parents=True, exist_ok=True)
        (listing / "_목록.txt").write_text(
            "\n".join(f"{j['url']} | {j['title']}" for j in jobs) + "\n", encoding="utf-8")
        print(f"폴더에서 영상 {len(jobs)}개를 찾았습니다 ({listing / '_목록.txt'})")
    elif args.list:
        jobs = read_list(args.list)
    else:
        raise SystemExit("목록 파일이나 --folder 중 하나는 있어야 합니다.")
    out_root = Path(args.out); out_root.mkdir(parents=True, exist_ok=True)
    work = Path(args.work) if args.work else out_root / "_원본"
    work.mkdir(parents=True, exist_ok=True)
    state_path = out_root / "_진행.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    print(f"강의 {len(jobs)}개\n")
    started = time.time()
    for i, job in enumerate(jobs, 1):
        job["slug"] = f"{i:02d}"
        name = job["title"] or f"{i}강"
        key = job["url"]
        if state.get(key, {}).get("done"):
            print(f"[{i}/{len(jobs)}] {name} — 이미 끝남, 건너뜀")
            continue

        print(f"[{i}/{len(jobs)}] {name}")
        t0 = time.time()
        out_dir = out_root / f"{i:02d}_{re.sub(r'[^가-힣A-Za-z0-9]+', '_', name)[:40]}"
        out_dir.mkdir(parents=True, exist_ok=True)
        src = work / f"{i:02d}_원본"

        if not src.exists() or src.stat().st_size < 100_000:
            print("    내려받는 중…")
            err = download(job["url"], src)
            if err:
                print(f"    ✗ {err}\n")
                state[key] = {"done": False, "error": err}
                state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
                continue

        err = edit(src, out_dir, job, args)
        if err:
            print(f"    ✗ {err}\n")
            state[key] = {"done": False, "error": err}
        else:
            final = out_dir / "final.mp4"
            size = final.stat().st_size / 1e6 if final.exists() else 0
            print(f"    ✓ {final}  ({size:.0f}MB, {hhmmss(time.time() - t0)} 걸림)\n")
            state[key] = {"done": True, "out": str(final)}
            if not args.keep_source:
                src.unlink(missing_ok=True)   # 디스크가 좁다. 끝난 원본은 비운다
                # 링크였다면 링크만 사라진다. 선생님 원본 파일은 그대로다
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                              encoding="utf-8")

    done = sum(1 for v in state.values() if v.get("done"))
    print(f"끝: {done}/{len(jobs)}개 완료, 총 {hhmmss(time.time() - started)} 걸림")
    failed = [(k, v["error"]) for k, v in state.items() if not v.get("done")]
    if failed:
        print("\n안 된 것:")
        for k, e in failed:
            print(f"  {k[:60]} — {e}")


if __name__ == "__main__":
    main()
