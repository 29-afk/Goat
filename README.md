# Goat

## `/video-to-skill` — turn a video into a Claude Code skill

```
/video-to-skill https://www.youtube.com/watch?v=... [skill-name] [--global]
```

This command:
1. **Downloads** the video with `yt-dlp` (YouTube, Vimeo, Loom, X, TikTok, direct links, local files).
2. **Grabs key frames** with `ffmpeg`: scene changes, plus at least one frame every few seconds (up to 30 by default).
3. **Transcribes** the audio locally with `faster-whisper`. If that fails, it falls back to the video's captions.
4. Has Claude review the transcript and frames, then **write a skill** to `.claude/skills/<name>/`:
   `SKILL.md`, `references/transcript.md`, and the frames that carry useful information.

### Setup

```bash
pip install yt-dlp faster-whisper
# plus ffmpeg: apt install ffmpeg  /  brew install ffmpeg
```

The first run downloads the Whisper model (`base`, about 150 MB).

### Run the processing step on its own

```bash
python3 .claude/scripts/video_to_skill.py <url-or-file> -o ./work \
  [--max-frames 30] [--scene 0.3] [--model base|small|medium|large-v3] [--language en]
```
