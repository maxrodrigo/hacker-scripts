#!/usr/bin/env bash
# vidgrab.sh URL [ROOT]: download a public video or reel, transcribe it, and pull a frame at every scene change.
# Leaves ROOT/<date>-<id>/ with video.*, video.info.json, video.srt and frames/MM-SS-n.jpg.
# ROOT defaults to data/media/ at the repo root (gitignored).
# Env: WHISPER_MODEL (default small, the one cached locally), SCENE threshold 0-1 (default 0.3, lower = more frames).
# Needs yt-dlp, ffmpeg, uv, and the Whisper model already in the Hugging Face cache (runs offline, never downloads it).
# Re-running skips the download and transcription if they are already done.
set -euo pipefail

url=${1:?usage: vidgrab.sh URL [ROOT]}
root=${2:-$(cd "$(dirname "$0")/.." && pwd)/data/media}

video=$(yt-dlp --no-simulate --no-playlist --write-info-json --print after_move:filepath \
  -S 'res:1080' -o "$root/%(upload_date)s-%(id)s/video.%(ext)s" "$url")
dir=$(dirname "$video")
echo "-> $dir" >&2

# Whisper on faster-whisper/CTranslate2, pinned and run through uv so nothing is installed globally.
# Uses the cached model only, int8 on all CPU cores, and VAD to skip music and silence.
# The .srt is written only once transcription finishes, so its presence means it is complete.
[[ -f "$dir/video.srt" ]] || uvx whisper-ctranslate2@0.5.8 "$video" --model "${WHISPER_MODEL:-small}" --local_files_only True \
  --device cpu --compute_type int8 --threads "$(nproc)" --vad_filter True \
  --output_format srt --output_dir "$dir" --verbose False

# First frame plus every scene change; showinfo reports each kept frame's timestamp.
mkdir -p "$dir/frames"
n=0
ffmpeg -hide_banner -nostdin -y -i "$video" \
  -vf "select='eq(n\,0)+gt(scene\,${SCENE:-0.3})',showinfo" -fps_mode vfr -q:v 3 \
  "$dir/frames/%04d.jpg" 2>&1 | grep -o 'pts_time:[0-9.]*' | cut -d: -f2 |
  while read -r t; do
    n=$((n + 1)); s=${t%.*}
    mv "$dir/frames/$(printf %04d "$n").jpg" "$dir/frames/$(printf '%02d-%02d' $((s / 60)) $((s % 60)))-$n.jpg"
  done

echo "$dir"
