#!/usr/bin/env python3
"""윈도우에서 명령어 없이 쓰는 창구.

2_편집하기.bat 을 두 번 눌러 쓰거나, 영상이 든 폴더를 그 위에 끌어다 놓는다.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EDITOR_DIR = HERE.parent
BATCH = EDITOR_DIR / "batch.py"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".mts"}


def say(*args):
    print(*args, flush=True)


def ask(question, options, default=1):
    """번호를 골라 답하게 한다. 그냥 엔터면 기본값."""
    say(f"\n{question}")
    for i, (label, _) in enumerate(options, 1):
        mark = " ←기본" if i == default else ""
        say(f"  {i}. {label}{mark}")
    while True:
        got = input("번호 입력 (엔터=기본): ").strip()
        if not got:
            return options[default - 1][1]
        if got.isdigit() and 1 <= int(got) <= len(options):
            return options[int(got) - 1][1]
        say("  다시 입력해 주세요.")


def check():
    """깔려야 할 것이 다 있는지, 이 컴퓨터가 받아쓰기를 감당하는지."""
    ok = True
    say("설치 상태를 확인합니다.\n")

    for name, cmd in (("ffmpeg", ["ffmpeg", "-version"]),):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            ver = p.stdout.splitlines()[0].split()[2] if p.returncode == 0 else "?"
            say(f"  [O] {name} {ver}")
        except (OSError, subprocess.SubprocessError):
            say(f"  [X] {name} 없음 — 1_설치.bat 을 먼저 실행하세요")
            ok = False

    say(f"  [O] 파이썬 {sys.version.split()[0]}")

    try:
        import faster_whisper          # noqa: F401
        say("  [O] 받아쓰기 엔진")
    except ImportError:
        say("  [X] 받아쓰기 엔진 없음 — 1_설치.bat 을 먼저 실행하세요")
        ok = False
        return ok

    say("\n이 컴퓨터가 받아쓰기를 감당하는지 봅니다 (1~2분).")
    try:
        from faster_whisper import WhisperModel
        import time
        t0 = time.time()
        WhisperModel("tiny", device="cpu", compute_type="int8")
        say(f"  [O] 문제없습니다 (모델 준비 {time.time() - t0:.0f}초)")
    except Exception as e:                                   # noqa: BLE001
        say(f"  [X] 받아쓰기를 돌릴 수 없습니다: {str(e)[:120]}")
        say("      오래된 CPU 라면 여기서 막힙니다. 자막 없이 컷 편집만은 됩니다.")
        ok = False

    say("\n" + ("준비 끝났습니다. 2_편집하기.bat 을 실행하세요."
                if ok else "위 항목을 해결한 뒤 다시 확인하세요."))
    return ok


def find_videos(folder):
    found = sorted(p for p in Path(folder).iterdir()
                   if p.is_file() and p.suffix.lower() in VIDEO_EXT)
    return found


def main():
    args = sys.argv[1:]
    if args and args[0] == "--check":
        check()
        return

    say("=" * 52)
    say("  강의 영상 자동 편집")
    say("=" * 52)

    # 끌어다 놓은 것이 있으면 그것부터
    dropped = [Path(a) for a in args if Path(a).exists()]
    folder, videos = None, None
    if len(dropped) == 1 and dropped[0].is_dir():
        folder = dropped[0]
    elif dropped:
        # 파일을 끌어다 놓았으면 그 파일들만 다룬다. 옆에 있는 남의 영상까지
        # 건드리면 안 된다.
        videos = [p for p in dropped if p.suffix.lower() in VIDEO_EXT]
        folder = dropped[0].parent
        if not videos:
            say("\n영상 파일이 아닙니다.")
            return
    if folder is None:
        say("\n영상이 든 폴더 경로를 넣으세요.")
        say("(탐색기에서 폴더를 끌어다 이 창에 놓아도 됩니다)")
        got = input("폴더: ").strip().strip('"')
        if not got:
            say("취소했습니다.")
            return
        folder = Path(got)

    if not folder.is_dir():
        say(f"\n폴더를 찾을 수 없습니다: {folder}")
        return

    if videos is None:
        videos = find_videos(folder)
    if not videos:
        say(f"\n{folder} 안에 영상이 없습니다.")
        return

    say(f"\n영상 {len(videos)}개를 찾았습니다.")
    for v in videos[:10]:
        say(f"  · {v.name}")
    if len(videos) > 10:
        say(f"  … 그 밖에 {len(videos) - 10}개")

    flip = ask("칠판 글씨가 거울처럼 뒤집혀 있나요? (앞 카메라로 찍으면 그렇습니다)",
               [("아니오 — 그대로 둔다", False), ("예 — 바로잡는다", True)], default=1)
    mode = ask("어떻게 잘라낼까요?",
               [("조용한 구간을 잘라낸다 — 앉아서 말하는 강의", "sound"),
                ("사람 목소리만 남긴다 — 움직이며 찍은 영상", "speech")], default=1)
    quality = ask("화질은 어떻게 할까요?",
                  [("높게 — 유튜브에 올릴 것 (파일이 큽니다)", "high"),
                   ("보통 — 확인용", "mid")], default=1)

    out = folder / "편집결과"
    out.mkdir(parents=True, exist_ok=True)
    listing = out / "_목록.txt"      # 선생님 영상 폴더에 남의 파일을 만들지 않는다
    listing.write_text(
        "\n".join(f"{v} | {v.stem}" for v in videos) + "\n", encoding="utf-8")

    cmd = [sys.executable, str(BATCH), str(listing), "-o", str(out),
           "--mode", mode, "--denoise", "light"]
    if flip:
        cmd.append("--flip")
    cmd += (["--preset", "slow", "--crf", "20"] if quality == "high"
            else ["--preset", "veryfast", "--crf", "26", "--scale", "720"])

    say(f"\n시작합니다. 결과는 {out} 에 쌓입니다.")
    say("오래 걸립니다. 영상 길이의 1.5배쯤 잡으세요. 중간에 창을 닫아도")
    say("다시 실행하면 끝난 것은 건너뛰고 이어서 합니다.\n")
    say("-" * 52)
    subprocess.run(cmd)
    say("-" * 52)
    say(f"\n결과 폴더: {out}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\n중단했습니다. 다시 실행하면 이어서 합니다.")
