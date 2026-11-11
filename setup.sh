#!/bin/bash
# Sets AudioRite up in this folder: Python packages (about 300 MB) and the voices (about 800 MB of downloads).
# Run it again any time: what is already there is skipped.
#
#   ./setup.sh        set everything up
#   ./setup.sh app    also put a copy of AudioRite.app in /Applications
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"

# Python 3.10 to 3.13 (the one that comes with macOS is too old)
PY=""
for p in python3.12 python3.13 python3.11 python3.10 python3; do
  if command -v "$p" >/dev/null && "$p" -c 'import sys; sys.exit(not (3, 10) <= sys.version_info[:2] <= (3, 13))' 2>/dev/null; then
    PY="$p"; break
  fi
done
if [ -z "$PY" ]; then
  echo "AudioRite needs Python 3.10 to 3.13. Get 3.12 from https://www.python.org/downloads/macos/ and run this again."
  exit 1
fi
echo "Using $($PY --version)"

mkdir -p envs work projects Temp
[ -x envs/tools/bin/python ] || "$PY" -m venv envs/tools          # runs the page's server (no packages needed)
[ -x envs/piper/bin/python ] || "$PY" -m venv envs/piper          # runs the voices
echo "Installing the Python packages (about 300 MB)…"
envs/piper/bin/python -m pip install --quiet --upgrade pip
envs/piper/bin/python -m pip install --quiet -r requirements.txt

get() {  # get <url> <file>: download unless it is already there
  if [ -s "$2" ]; then return; fi
  echo "Downloading $(basename "$2")…"
  curl -L --fail --progress-bar -o "$2.part" "$1"
  mv "$2.part" "$2"
}

# Kokoro: the model and its voices (354 MB)
K=https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
mkdir -p envs/piper/kokoro envs/piper/voices
get $K/kokoro-v1.0.onnx envs/piper/kokoro/kokoro-v1.0.onnx
get $K/voices-v1.0.bin  envs/piper/kokoro/voices-v1.0.bin

# Piper: the voices in the menu (448 MB)
P=https://huggingface.co/rhasspy/piper-voices/resolve/main/en
for v in en_US/mike/medium/en_US-mike-medium \
         en_US/bryce/medium/en_US-bryce-medium \
         en_GB/northern_english_male/medium/en_GB-northern_english_male-medium \
         en_US/ryan/high/en_US-ryan-high \
         en_US/libritts/high/en_US-libritts-high; do
  get $P/$v.onnx.json envs/piper/voices/$(basename $v).onnx.json
  get $P/$v.onnx      envs/piper/voices/$(basename $v).onnx
done

# A first project to try, if there are none yet
if ! ls projects/*/project.json >/dev/null 2>&1; then cp -R sample/* projects/; fi

if [ "$1" = "app" ]; then
  rm -rf /Applications/AudioRite.app
  cp -R AudioRite.app /Applications/
  echo "$ROOT" > /Applications/AudioRite.app/Contents/Resources/root   # where this folder is
  echo "AudioRite.app is in /Applications."
fi

echo
echo "Done. Open AudioRite.app (in this folder) to start."
