#!/usr/bin/env python3
"""인강 자동 편집기 — 무음 컷 → 자막 → 렌더.

ffmpeg/ffprobe만 있으면 컷과 렌더가 돌아가고,
자막 받아쓰기는 faster-whisper가 깔려 있을 때만 동작한다.

    python3 edit_lecture.py all  강의원본.mp4
    python3 edit_lecture.py cut  강의원본.mp4
    python3 edit_lecture.py sub  out/01_cut.mp4
    python3 edit_lecture.py render out/01_cut.mp4 out/subs.srt
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# ── 기본값 ────────────────────────────────────────────────────────────────
SILENCE_DB = -35.0      # 이보다 조용하면 무음으로 본다
SILENCE_MIN = 0.60      # 이 길이(초) 이상 이어져야 잘라낸다
PAD = 0.12              # 말 앞뒤로 남길 여유(초). 말꼬리가 잘리는 걸 막는다
MIN_KEEP = 0.30         # 이보다 짧게 남는 토막은 버린다
LOUDNESS = "loudnorm=I=-16:TP=-1.5:LRA=11"   # 인강 표준 라우드니스
# loudnorm은 출력 표본율을 제멋대로 올린다. 뒤에서 다시 48k 스테레오로 못박아야
# 제목 카드와 본편을 재인코딩 없이 이어 붙일 수 있다.
AFORMAT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
AUDIO_ARGS = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

SRT_MAX_CHARS = 20      # 자막 한 줄 최대 글자수(한글 기준)
SRT_MAX_LINES = 2
SRT_MAX_DUR = 6.0       # 자막 한 장이 머무는 최대 시간(초)

STYLE = {
    "Fontsize": "22",
    "PrimaryColour": "&H00FFFFFF",   # 흰 글자
    "OutlineColour": "&H00000000",   # 검은 테두리
    "BackColour": "&H80000000",      # 반투명 검정 그림자
    "Bold": "1",
    "BorderStyle": "1",
    "Outline": "2",
    "Shadow": "1",
    "MarginV": "40",
    "Alignment": "2",                # 하단 중앙
}


def run(cmd, **kw):
    """ffmpeg 호출. 실패하면 stderr를 그대로 보여주고 멈춘다."""
    p = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if p.returncode != 0:
        sys.stderr.write(p.stderr[-4000:])
        raise SystemExit(f"\n실패: {' '.join(str(c) for c in cmd[:3])} ... (코드 {p.returncode})")
    return p


def need(tool):
    if not shutil.which(tool):
        raise SystemExit(
            f"{tool} 이(가) 없습니다.\n"
            "  macOS:   brew install ffmpeg\n"
            "  Windows: winget install Gyan.FFmpeg\n"
            "  Ubuntu:  sudo apt install ffmpeg"
        )


def probe(path):
    p = run(["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)])
    info = json.loads(p.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if a is None:
        raise SystemExit("오디오 트랙이 없습니다. 무음 구간을 찾을 수 없어요.")
    fps = 30.0
    if v and v.get("avg_frame_rate", "0/0") not in ("0/0", "0"):
        num, _, den = v["avg_frame_rate"].partition("/")
        if float(den or 1):
            fps = float(num) / float(den or 1)
    return {
        "duration": float(info["format"]["duration"]),
        "fps": round(fps, 4),
        "width": int(v["width"]) if v else 0,
        "height": int(v["height"]) if v else 0,
        "has_video": v is not None,
    }


def hhmmss(sec):
    sec = max(0.0, sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── 1단계: 무음 구간 잘라내기 ─────────────────────────────────────────────
@dataclass
class Segment:
    start: float
    end: float

    @property
    def dur(self):
        return self.end - self.start


def find_silences(path, db, minlen):
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={db}dB:d={minlen}", "-f", "null", "-"],
        capture_output=True, text=True)
    log = p.stderr
    starts = [float(m) for m in re.findall(r"silence_start: (-?[\d.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end: (-?[\d.]+)", log)]
    return starts, ends


def keep_segments(duration, starts, ends, pad, min_keep):
    """무음 목록을 '남길 구간' 목록으로 뒤집는다."""
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        silences.append((max(0.0, s), min(duration, e)))
    silences.sort()

    keeps, cursor = [], 0.0
    for s, e in silences:
        if s > cursor:
            keeps.append(Segment(cursor, s))
        cursor = max(cursor, e)
    if cursor < duration:
        keeps.append(Segment(cursor, duration))

    # 앞뒤 여유를 주고, 겹치면 합치고, 너무 짧은 토막은 버린다
    padded = [Segment(max(0.0, k.start - pad), min(duration, k.end + pad)) for k in keeps]
    merged = []
    for k in padded:
        if merged and k.start <= merged[-1].end:
            merged[-1].end = max(merged[-1].end, k.end)
        else:
            merged.append(k)
    return [k for k in merged if k.dur >= min_keep]


def build_filter(segs, has_video):
    """select 표현식 한 방으로 잘라 붙인다. 토막이 몇백 개여도 한 번만 인코딩한다."""
    expr = "+".join(f"between(t,{s.start:.3f},{s.end:.3f})" for s in segs)
    chains = []
    if has_video:
        chains.append(f"[0:v]select='{expr}',setpts=N/FRAME_RATE/TB[v]")
    chains.append(f"[0:a]aselect='{expr}',asetpts=N/SR/TB,{LOUDNESS},{AFORMAT}[a]")
    return ";".join(chains)


def cut(args):
    need("ffmpeg"); need("ffprobe")
    src = Path(args.input)
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(src)

    print(f"· 원본 {hhmmss(info['duration'])} / {info['width']}x{info['height']}")
    print(f"· 무음 탐지 (임계 {args.db}dB, {args.minlen}초 이상)")
    starts, ends = find_silences(src, args.db, args.minlen)
    segs = keep_segments(info["duration"], starts, ends, args.pad, MIN_KEEP)
    if not segs:
        raise SystemExit("남길 구간이 없습니다. --db 를 더 낮춰(-45 등) 보세요.")

    kept = sum(s.dur for s in segs)
    cut_out = info["duration"] - kept
    ratio = cut_out / info["duration"] * 100
    print(f"· 말하는 구간 {len(segs)}개 = {hhmmss(kept)} "
          f"(무음 {hhmmss(cut_out)} 제거, {ratio:.0f}% 단축)")

    (out_dir / "cuts.json").write_text(json.dumps(
        {"source_duration": info["duration"],
         "segments": [[round(k.start, 3), round(k.end, 3)] for k in segs]},
        ensure_ascii=False, indent=1), encoding="utf-8")

    filter_path = out_dir / "cut_filter.txt"
    filter_path.write_text(build_filter(segs, info["has_video"]), encoding="utf-8")
    dst = out_dir / "01_cut.mp4"

    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "warning",
           "-i", str(src), "-filter_complex_script", str(filter_path)]
    if info["has_video"]:
        cmd += ["-map", "[v]"]
    cmd += ["-map", "[a]",
            "-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", *AUDIO_ARGS,
            "-movflags", "+faststart", str(dst)]
    print("· 컷 편집 중…")
    run(cmd)
    print(f"→ {dst}")
    return dst


# ── 2단계: 받아쓰기 → 자막 ────────────────────────────────────────────────
def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def wrap_lines(text, width):
    """한글은 띄어쓰기가 드물다. 단어로 채우되, 단어 하나가 폭보다 길면 글자로 끊는다."""
    lines, cur = [], ""
    for w in text.split():
        while len(w) > width:                 # 폭보다 긴 낱말은 강제로 자른다
            if cur:
                lines.append(cur); cur = ""
            lines.append(w[:width]); w = w[width:]
        cand = f"{cur} {w}".strip()
        if len(cand) <= width:
            cur = cand
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def wrap_korean(text, width, max_lines):
    return "\n".join(wrap_lines(text, width)[:max_lines])


def fits(text, width, max_lines):
    """이 문장이 자막 한 장(폭 x 줄수)에 들어가는가."""
    return len(wrap_lines(text, width)) <= max_lines


def cues_from_words(words, max_chars, max_lines, max_dur):
    """낱말 타임스탬프를 문장 부호와 쉼 자리에서 끊어 자막 한 장씩으로 묶는다."""
    cues, buf = [], []

    def text_of(ws):
        return "".join(w["word"] for w in ws).strip()

    def flush():
        if buf:
            t = text_of(buf)
            if t:
                cues.append({"start": buf[0]["start"], "end": buf[-1]["end"], "text": t})
            buf.clear()

    for i, w in enumerate(words):
        buf.append(w)
        # 한 장에 안 들어가면 이 낱말은 다음 장으로 넘긴다
        if len(buf) > 1 and not fits(text_of(buf), max_chars, max_lines):
            buf.pop()
            flush()
            buf.append(w)

        text = text_of(buf)
        dur = buf[-1]["end"] - buf[0]["start"]
        gap = words[i + 1]["start"] - w["end"] if i + 1 < len(words) else 99.0
        ends_sentence = text.endswith((".", "?", "!", "…", "다", "요", "죠", "까", "야"))
        if (dur >= max_dur                       # 너무 오래 머무른 자막
                or gap > 0.7                     # 확실히 쉬어 간 자리
                or (ends_sentence and gap > 0.25)):   # 문장이 끝나고 숨을 돌린 자리
            flush()
    flush()
    return stretch_short(cues)


def stretch_short(cues, minimum=1.1):
    """눈으로 읽기엔 너무 짧게 스치는 자막을, 다음 장을 침범하지 않는 선에서 늘린다."""
    for i, c in enumerate(cues):
        if c["end"] - c["start"] < minimum:
            ceiling = cues[i + 1]["start"] if i + 1 < len(cues) else c["end"] + minimum
            c["end"] = min(c["start"] + minimum, max(c["end"], ceiling))
    return cues


def write_srt(cues, path, max_chars, max_lines):
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n"
                   f"{wrap_korean(c['text'], max_chars, max_lines)}\n")
    path.write_text("\n".join(out), encoding="utf-8")


def sub(args):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise SystemExit(
            "받아쓰기 엔진이 없습니다.\n"
            "  pip install faster-whisper\n"
            "(GPU가 있으면 훨씬 빠릅니다. 없으면 --model medium 정도를 권합니다.)")

    src = Path(args.input)
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / "subs.srt"

    prompt = None
    if args.terms and Path(args.terms).exists():
        terms = [t.strip() for t in Path(args.terms).read_text(encoding="utf-8").splitlines()
                 if t.strip() and not t.startswith("#")]
        if terms:
            prompt = "다음 용어가 나옵니다: " + ", ".join(terms) + "."
            print(f"· 용어집 {len(terms)}개 적용")

    print(f"· 받아쓰기 ({args.model}) — 영상 길이의 0.3~1배쯤 걸립니다")
    model = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    segments, _ = model.transcribe(
        str(src), language="ko", word_timestamps=True, initial_prompt=prompt,
        vad_filter=True, beam_size=5)

    words = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})
        print(f"\r  {hhmmss(seg.end)}", end="", flush=True)
    print()
    if not words:
        raise SystemExit("받아쓸 말이 잡히지 않았습니다.")

    cues = cues_from_words(words, args.max_chars, SRT_MAX_LINES, SRT_MAX_DUR)
    write_srt(cues, dst, args.max_chars, SRT_MAX_LINES)
    print(f"→ {dst}  (자막 {len(cues)}장)")
    print("  ※ 오타·전문용어는 이 파일을 직접 고친 뒤 render 하세요.")
    return dst


# ── 챕터 / 소제목 ────────────────────────────────────────────────────────
# BorderStyle 3 = 글자 뒤에 불투명 상자. 4는 libass가 그리지 않는다.
# Outline 값이 그 상자의 여백 노릇을 한다.
CHAPTER_STYLE = {
    "Fontsize": "30", "PrimaryColour": "&H00FFFFFF", "BackColour": "&HA0141414",
    "Bold": "1", "BorderStyle": "3", "Outline": "7", "Shadow": "0",
    "Alignment": "7", "MarginL": "48", "MarginR": "48", "MarginV": "48",
}


def parse_timecode(text):
    """'12:34' 또는 '1:02:03' 또는 '75' 를 초로."""
    parts = [float(x) for x in text.strip().split(":")]
    seconds = 0.0
    for x in parts:
        seconds = seconds * 60 + x
    return seconds


def read_chapters(path):
    """한 줄에 '시각 제목'. 예) 00:00 체력검정 기준 잡기"""
    out = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, title = line.partition(" ")
        if not title.strip():
            raise SystemExit(f"챕터 줄에 제목이 없습니다: {raw!r}\n  예) 00:00 체력검정 기준 잡기")
        try:
            out.append({"time": parse_timecode(head), "title": title.strip()})
        except ValueError:
            raise SystemExit(f"시각을 읽을 수 없습니다: {raw!r}\n  예) 00:00 또는 1:02:03")
    return sorted(out, key=lambda c: c["time"])


def map_to_cut(seconds, cuts_path):
    """원본 시각을 컷 편집된 영상의 시각으로 옮긴다.

    잘려 나간 무음 한가운데를 가리키면, 바로 다음 말이 시작하는 자리로 당긴다."""
    data = json.loads(Path(cuts_path).read_text(encoding="utf-8"))
    elapsed = 0.0
    for start, end in data["segments"]:
        if seconds < start:          # 잘려 나간 구간 안 → 다음 구간 머리로
            return elapsed
        if seconds <= end:
            return elapsed + (seconds - start)
        elapsed += end - start
    return elapsed


def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    sec, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{sec:02d}.{cs:02d}"


def write_chapter_ass(chapters, path, width, height, font, hold, numbered):
    """소제목 카드를 ASS로. 화면 왼쪽 위에 반투명 띠로 잠깐 떴다 사라진다."""
    style = dict(CHAPTER_STYLE)
    style["FontName"] = font
    if height:
        style["Fontsize"] = str(int(height * 0.038))
        style["Outline"] = str(max(4, int(height * 0.009)))
        style["MarginL"] = style["MarginV"] = str(int(height * 0.05))
    order = ["FontName", "Fontsize", "PrimaryColour", "BackColour", "Bold",
             "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV"]
    head = [
        "[Script Info]", "ScriptType: v4.00+",
        f"PlayResX: {width or 1920}", f"PlayResY: {height or 1080}", "",
        "[V4+ Styles]",
        ("Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, "
         "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV"),
        "Style: Chapter," + ",".join(style[k] for k in order), "",
        "[Events]", "Format: Layer, Start, End, Style, Text",
    ]
    for i, c in enumerate(chapters, 1):
        title = c["title"].replace("{", "(").replace("}", ")")
        label = f"{i}. {title}" if numbered else title
        head.append(f"Dialogue: 0,{ass_time(c['at'])},{ass_time(c['at'] + hold)},Chapter,"
                    f"{{\\fad(350,350)}}{label}")
    Path(path).write_text("\n".join(head) + "\n", encoding="utf-8")


# ── 3단계: 자막 입혀 렌더 ─────────────────────────────────────────────────
def pick_font(explicit):
    if explicit:
        return explicit
    system = platform.system()
    if system == "Darwin":
        return "AppleSDGothicNeo-Bold"
    if system == "Windows":
        return "Malgun Gothic"
    for name in ("Noto Sans CJK KR", "NanumGothic", "Noto Sans KR"):
        p = subprocess.run(["fc-list", ":family"], capture_output=True, text=True)
        if p.returncode == 0 and name.lower() in p.stdout.lower():
            return name
    return "Noto Sans CJK KR"


def escape_for_filter(path):
    """subtitles 필터는 경로 속 \\ : ' 를 이스케이프해야 한다 (윈도우 C:\\ 대응)."""
    s = str(Path(path).resolve())
    return s.replace("\\", "\\\\\\\\").replace(":", "\\\\:").replace("'", "\\\\'")


def make_intro(title, subtitle, out_dir, width, height, fps, seconds, font, preset, crf):
    """제목 카드. 본편과 같은 코덱·해상도로 뽑아야 이어 붙일 때 다시 인코딩하지 않는다."""
    dst = out_dir / "00_intro.mp4"
    esc = lambda t: t.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019")
    draw = (f"drawtext=font='{font}':text='{esc(title)}':fontcolor=white:"
            f"fontsize={int(height * 0.075)}:x=(w-text_w)/2:y=(h-text_h)/2-{int(height * 0.04)}:"
            f"alpha='min(1,t)'")
    if subtitle:
        draw += (f",drawtext=font='{font}':text='{esc(subtitle)}':fontcolor=0xC8C8C8:"
                 f"fontsize={int(height * 0.038)}:x=(w-text_w)/2:y=(h-text_h)/2+{int(height * 0.05)}:"
                 f"alpha='min(1,max(0,t-0.4))'")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
         "-f", "lavfi", "-i", f"color=c=0x0F1419:s={width}x{height}:d={seconds}:r={fps}",
         "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={seconds}",
         "-vf", draw, "-shortest",
         "-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p",
         *AUDIO_ARGS, str(dst)])
    return dst


def render(args):
    need("ffmpeg"); need("ffprobe")
    src = Path(args.input)
    srt = Path(args.srt) if args.srt else Path(args.out) / "subs.srt"
    out_dir = Path(args.out); out_dir.mkdir(parents=True, exist_ok=True)
    info = probe(src)
    font = pick_font(args.font)

    # 화면을 줄여 뽑을 때는 먼저 줄이고, 글자 크기를 줄인 화면에 맞춘다
    height = info["height"]
    width = info["width"]
    prescale = []
    if args.scale and info["height"] and args.scale < info["height"]:
        width = int(info["width"] * args.scale / info["height"]) // 2 * 2
        height = args.scale
        prescale.append(f"scale=-2:{args.scale}")
        print(f"· {info['width']}x{info['height']} → {width}x{height} 로 줄여서 뽑습니다")

    style = dict(STYLE)
    style["FontName"] = font
    style["Fontsize"] = str(int(height * 0.042)) if height else STYLE["Fontsize"]
    style["MarginV"] = str(int(height * 0.05)) if height else STYLE["MarginV"]
    force = ",".join(f"{k}={v}" for k, v in style.items())

    body = out_dir / "02_subbed.mp4"
    vf = list(prescale)
    if srt.exists():
        print(f"· 자막 입히는 중 ({font})")
        vf.append(f"subtitles='{escape_for_filter(srt)}':force_style='{force}'")
    else:
        print(f"· 자막 파일이 없어 건너뜁니다 ({srt})")

    chapters = []
    chapters_file = Path(args.chapters) if args.chapters else None
    if chapters_file and chapters_file.exists():
        # 결과 폴더를 따로 잡았더라도, 넣은 영상 옆에 있는 구간표를 찾아 쓴다.
        # 못 찾고 조용히 컷 기준으로 넘어가면 소제목이 죄다 어긋난다.
        cuts = next((c for c in (out_dir / "cuts.json", Path(src).parent / "cuts.json")
                     if c.exists()), None)
        basis = args.chapter_basis
        if basis == "original" and cuts is None:
            raise SystemExit(
                "원본 시각 기준으로 맞추려면 cut 단계가 남긴 cuts.json 이 필요합니다.\n"
                "  컷 편집본과 같은 폴더에 두거나, --chapter-basis cut 으로 넘기세요.")
        if basis == "auto":
            basis = "original" if cuts else "cut"
        for c in read_chapters(chapters_file):
            at = map_to_cut(c["time"], cuts) if basis == "original" else c["time"]
            chapters.append({"at": at, "title": c["title"]})
        if basis == "original":
            print(f"· 챕터 {len(chapters)}개 — 원본 시각을 컷 편집본 기준으로 옮겼습니다")
        else:
            print(f"· 챕터 {len(chapters)}개 — 컷 편집본 시각 그대로 씁니다")
        ass = out_dir / "chapters.ass"
        write_chapter_ass(chapters, ass, width, height, font,
                          args.chapter_seconds, not args.no_chapter_numbers)
        vf.append(f"subtitles='{escape_for_filter(ass)}'")
    elif args.chapters and args.chapters != "chapters.txt":
        print(f"· 챕터 파일이 없습니다 ({args.chapters})")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "warning", "-i", str(src)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
            "-pix_fmt", "yuv420p", *AUDIO_ARGS,
            "-movflags", "+faststart", str(body)]
    run(cmd)

    final = out_dir / "final.mp4"
    if args.title:
        print("· 제목 카드 붙이는 중")
        intro = make_intro(args.title, args.subtitle, out_dir,
                           width or 1920, height or 1080, info["fps"],
                           args.intro_seconds, font, args.preset, args.crf)
        listing = out_dir / "concat.txt"
        listing.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in (intro, body)),
                           encoding="utf-8")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
             "-f", "concat", "-safe", "0", "-i", str(listing),
             "-c", "copy", "-movflags", "+faststart", str(final)])
    else:
        shutil.copy(body, final)

    if chapters:
        offset = args.intro_seconds if args.title else 0.0
        lines = ["00:00 " + (args.title or "시작")] if offset else []
        for i, c in enumerate(chapters, 1):
            at = c["at"] + offset
            stamp = hhmmss(at)[3:] if at < 3600 else hhmmss(at)
            prefix = "" if args.no_chapter_numbers else f"{i}. "
            line = f"{stamp} {prefix}{c['title']}"
            if not lines and at > 0:          # 유튜브는 첫 줄이 00:00이어야 받아 준다
                lines.append("00:00 시작")
            lines.append(line)
        yt = out_dir / "youtube_chapters.txt"
        yt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  유튜브 설명란에 붙일 챕터 목록: {yt}")

    print(f"→ {final}  ({hhmmss(probe(final)['duration'])})")
    if srt.exists():
        print(f"  유튜브용 자막 파일도 그대로 쓰세요: {srt}")
    return final


def all_steps(args):
    args.input = str(cut(args))
    try:
        sub(args)
    except SystemExit as e:
        print(f"\n[자막 건너뜀] {e}\n")
    render(args)


# ── CLI ───────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="인강 자동 편집기")
    ap.add_argument("step", choices=["all", "cut", "sub", "render"])
    ap.add_argument("input")
    ap.add_argument("srt", nargs="?", help="render 단계에서 쓸 자막 파일")
    ap.add_argument("-o", "--out", default="out", help="결과 폴더 (기본 out)")
    ap.add_argument("--db", type=float, default=SILENCE_DB, help="무음 임계값 dB")
    ap.add_argument("--minlen", type=float, default=SILENCE_MIN, help="잘라낼 최소 무음 길이(초)")
    ap.add_argument("--pad", type=float, default=PAD, help="말 앞뒤 여유(초)")
    ap.add_argument("--model", default="medium", help="whisper 모델 (medium/large-v3)")
    ap.add_argument("--device", default="auto")
    ap.add_argument("--compute-type", default="default")
    ap.add_argument("--terms", default="terms.txt", help="전문용어 목록 파일")
    ap.add_argument("--max-chars", type=int, default=SRT_MAX_CHARS)
    ap.add_argument("--font", default=None, help="자막 글꼴 이름")
    ap.add_argument("--title", default=None, help="제목 카드 문구 (없으면 생략)")
    ap.add_argument("--subtitle", default=None, help="제목 카드 둘째 줄")
    ap.add_argument("--intro-seconds", type=float, default=3.0)
    ap.add_argument("--chapters", default="chapters.txt", help="챕터 목록 파일")
    ap.add_argument("--chapter-seconds", type=float, default=4.0, help="소제목이 떠 있는 시간")
    ap.add_argument("--chapter-basis", choices=["auto", "original", "cut"], default="auto",
                    help="챕터 시각이 원본 기준인지 컷 편집본 기준인지")
    ap.add_argument("--no-chapter-numbers", action="store_true", help="소제목 앞 번호를 빼기")
    ap.add_argument("--crf", type=int, default=20, help="화질 (낮을수록 고화질, 18~23)")
    ap.add_argument("--preset", default="medium")
    ap.add_argument("--scale", type=int, default=None,
                    help="세로 해상도를 이 값으로 줄여서 뽑는다 (예: 720). 전달용 용량 줄이기")
    args = ap.parse_args()

    {"all": all_steps, "cut": cut, "sub": sub, "render": render}[args.step](args)


if __name__ == "__main__":
    main()
