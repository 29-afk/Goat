#!/usr/bin/env python3
"""Download a video, extract key frames, and transcribe its audio.

Produces a work directory that the /video-to-skill command turns into a skill:

    <out>/
      video.<ext>          downloaded video
      audio.wav            16 kHz mono audio track
      frames/frame_0001.jpg ...  key frames
      frames.json          [{file, timestamp, seconds}]
      transcript.txt       transcript with [mm:ss] timestamps
      transcript.json      [{start, end, text}]
      metadata.json        title, uploader, duration, url, description, ...

Requires: ffmpeg on PATH, `pip install yt-dlp faster-whisper`.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def fmt_ts(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def check_deps() -> None:
    missing = []
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        missing.append("ffmpeg (system package)")
    try:
        import yt_dlp  # noqa: F401
    except ImportError:
        missing.append("yt-dlp (pip install yt-dlp)")
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        missing.append("faster-whisper (pip install faster-whisper)")
    if missing:
        die("missing dependencies: " + ", ".join(missing))


def download(url: str, out: Path) -> tuple[Path, dict]:
    import yt_dlp

    local = Path(url).expanduser()
    if local.is_file():  # allow local files too
        dest = out / f"video{local.suffix}"
        shutil.copy(local, dest)
        return dest, {"title": local.stem, "source": str(local)}

    opts = {
        "outtmpl": str(out / "video.%(ext)s"),
        "format": "bv*[height<=720]+ba/b[height<=720]/bv*+ba/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        # grab captions too, used as a fallback if local transcription fails
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en.*", "en"],
        "subtitlesformat": "vtt",
        "ignoreerrors": "only_download",
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        die(f"download failed: {e}")
    if not info:
        die("download failed")
    files = [p for p in out.glob("video.*")
             if p.suffix not in (".part", ".ytdl", ".vtt")]
    if not files:
        die("download produced no video file")
    keep = ("title", "uploader", "channel", "duration", "upload_date",
            "description", "webpage_url", "tags", "chapters")
    meta = {k: info.get(k) for k in keep if info.get(k) is not None}
    meta["source"] = url
    return files[0], meta


def probe_duration(video: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(video)],
        capture_output=True, text=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def extract_frames(video: Path, out: Path, max_frames: int, scene: float) -> list[dict]:
    frames_dir = out / "frames"
    frames_dir.mkdir(exist_ok=True)
    duration = probe_duration(video)

    def run(vf: str) -> list[float]:
        for f in frames_dir.glob("*.jpg"):
            f.unlink()
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", str(video),
             "-vf", f"{vf},showinfo,scale='min(1280,iw)':-2", "-vsync", "vfr",
             "-q:v", "3", str(frames_dir / "frame_%04d.jpg")],
            capture_output=True, text=True,
        )
        times = []
        for line in r.stderr.splitlines():
            if "pts_time:" in line:
                try:
                    times.append(float(line.split("pts_time:")[1].split()[0]))
                except (IndexError, ValueError):
                    pass
        return times

    # Scene changes (slide changes / cuts) plus at least one frame every
    # `interval` seconds, so static talking-head stretches are still covered.
    interval = max(duration / max_frames, 2.0) if duration else 10.0
    times = run(
        f"select='eq(n\\,0)+gt(scene\\,{scene})"
        f"+gte(t-prev_selected_t\\,{interval:.3f})'"
    )
    files = sorted(frames_dir.glob("frame_*.jpg"))
    # Too many (very cut-heavy video): keep an evenly spaced subset.
    if len(files) > max_frames:
        step = len(files) / max_frames
        keep_idx = {int(i * step) for i in range(max_frames)}
        kept_files, kept_times = [], []
        for i, (f, t) in enumerate(zip(files, times)):
            if i in keep_idx:
                kept_files.append(f)
                kept_times.append(t)
            else:
                f.unlink()
        times = kept_times
        for n, f in enumerate(kept_files, 1):  # renumber contiguously
            f.rename(frames_dir / f"frame_{n:04d}.jpg")

    files = sorted(frames_dir.glob("frame_*.jpg"))
    return [
        {"file": f"frames/{f.name}", "seconds": round(t, 2), "timestamp": fmt_ts(t)}
        for f, t in zip(files, times)
    ]


def extract_audio(video: Path, out: Path) -> Path | None:
    audio = out / "audio.wav"
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
         "-vn", "-ac", "1", "-ar", "16000", str(audio)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not audio.exists() or audio.stat().st_size < 1024:
        return None
    return audio


def parse_vtt(path: Path) -> list[dict]:
    """Parse a WebVTT caption file into de-duplicated segments."""
    import re

    def secs(ts: str) -> float:
        parts = ts.replace(",", ".").split(":")
        return sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts)))

    segments, seen = [], set()
    blocks = path.read_text(encoding="utf-8", errors="replace").split("\n\n")
    for block in blocks:
        lines = block.strip().splitlines()
        for i, line in enumerate(lines):
            if "-->" in line:
                start, end = (x.strip().split()[0] for x in line.split("-->"))
                text = " ".join(lines[i + 1:])
                text = re.sub(r"<[^>]+>", "", text).strip()
                if text and text not in seen:  # auto-captions repeat lines
                    seen.add(text)
                    segments.append({"start": round(secs(start), 2),
                                     "end": round(secs(end), 2), "text": text})
                break
    return segments


def transcribe(audio: Path, model_name: str, language: str | None) -> list[dict]:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="auto", compute_type="auto")
    segments, info = model.transcribe(str(audio), language=language, vad_filter=True)
    print(f"  detected language: {info.language}", file=sys.stderr)
    return [
        {"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()}
        for s in segments
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="video URL (anything yt-dlp supports) or local file path")
    ap.add_argument("-o", "--out", required=True, help="work directory to create")
    ap.add_argument("--max-frames", type=int, default=30)
    ap.add_argument("--scene", type=float, default=0.3,
                    help="scene-change threshold 0-1 (lower = more frames)")
    ap.add_argument("--model", default="base",
                    help="whisper model: tiny, base, small, medium, large-v3")
    ap.add_argument("--language", default=None, help="force language code, e.g. en")
    args = ap.parse_args()

    check_deps()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)

    print("[1/4] downloading...", file=sys.stderr)
    video, meta = download(args.url, out)
    meta["duration"] = meta.get("duration") or round(probe_duration(video), 2)
    (out / "metadata.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    print("[2/4] extracting frames...", file=sys.stderr)
    frames = extract_frames(video, out, args.max_frames, args.scene)
    (out / "frames.json").write_text(json.dumps(frames, indent=2))

    print("[3/4] extracting audio...", file=sys.stderr)
    audio = extract_audio(video, out)

    segments: list[dict] = []
    source = "none"
    if audio:
        print(f"[4/4] transcribing with whisper '{args.model}'...", file=sys.stderr)
        try:
            segments = transcribe(audio, args.model, args.language)
            source = f"whisper:{args.model}"
        except Exception as e:  # model download / runtime failure
            print(f"  whisper failed: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            subs = sorted(out.glob("video*.vtt"))
            if subs:
                print(f"  falling back to captions: {subs[0].name}", file=sys.stderr)
                segments = parse_vtt(subs[0])
                source = f"captions:{subs[0].name}"
    else:
        print("[4/4] no audio track found, skipping transcription", file=sys.stderr)
    (out / "transcript.json").write_text(json.dumps(segments, indent=2, ensure_ascii=False))
    (out / "transcript.txt").write_text(
        "\n".join(f"[{fmt_ts(s['start'])}] {s['text']}" for s in segments) + "\n"
    )

    summary = {
        "out_dir": str(out),
        "video": str(video),
        "title": meta.get("title"),
        "duration": meta.get("duration"),
        "frames": len(frames),
        "transcript_source": source,
        "transcript_segments": len(segments),
        "transcript_words": sum(len(s["text"].split()) for s in segments),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
