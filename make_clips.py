"""Script file → one voice clip per line, ready to drag into Final Cut.

    envs/piper/bin/python make_clips.py "Temp/getting-started-script.md" --voice p17

Clips go in a folder next to the script ("<script> clips/"), named by section and line:
"01-03 Click the button, and you'll be taken.wav". Headings (# ...) start a new section;
blank lines are skipped. Re-running only makes the lines that changed (text, voice or word list).
"""
import argparse, hashlib, os, re, shutil, wave
from piper import PiperVoice, SynthesisConfig

ROOT = os.path.dirname(os.path.abspath(__file__))
MODEL = os.path.join(ROOT, "envs", "piper", "voices", "en_US-libritts-high.onnx")
WORDS = os.path.join(ROOT, "words.txt")
CACHE = os.path.join(ROOT, "work", "clips-cache")
VOICES = ["p17", "p55", "p90", "p122", "p157", "p204", "p210", "p216"]


def read_words():
    # "Word = how to say it", one per line; # starts a comment
    words = {}
    for line in open(WORDS, encoding="utf-8"):
        line = line.split("#", 1)[0]
        if "=" in line:
            k, v = (s.strip() for s in line.split("=", 1))
            if k and v:
                words[k] = v
    return words


def apply_words(text, words):
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    for k in sorted(words, key=len, reverse=True):
        text = re.sub(rf"(?<!\w){re.escape(k)}(?!\w)", words[k], text, flags=re.IGNORECASE)
    return text


def read_script(path):
    # yields (section, line number in section, text)
    section, n = 0, 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            section, n = section + 1, 0
            continue
        n += 1
        yield section, n, line


def file_name(section, n, text):
    words = re.sub(r"[^\w' ,.-]", "", text.replace("’", "'")).split()
    short = " ".join(words[:8]).rstrip(",.")
    return f"{section:02d}-{n:02d} {short}.wav"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--voice", default="p17", choices=VOICES)
    ap.add_argument("--speed", type=float, default=1.0, help="above 1 = slower, below 1 = faster")
    args = ap.parse_args()

    out_dir = os.path.splitext(args.script)[0] + " clips"
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(CACHE, exist_ok=True)
    words = read_words()
    voice = cfg = None
    made = kept = 0
    wanted = set()

    for section, n, text in read_script(args.script):
        spoken = apply_words(text, words)
        key = hashlib.sha1(f"{args.voice}|{args.speed}|{spoken}".encode()).hexdigest()[:16]
        cached = os.path.join(CACHE, key + ".wav")
        if not os.path.exists(cached):
            if voice is None:
                voice = PiperVoice.load(MODEL)
                cfg = SynthesisConfig(speaker_id=voice.config.speaker_id_map[args.voice],
                                      length_scale=args.speed)
            with wave.open(cached + ".tmp", "wb") as w:
                voice.synthesize_wav(spoken, w, syn_config=cfg)
            os.replace(cached + ".tmp", cached)
            made += 1
            print("made", file_name(section, n, text))
        else:
            kept += 1
        name = file_name(section, n, text)
        wanted.add(name)
        dest = os.path.join(out_dir, name)
        if not os.path.exists(dest) or not os.path.samefile(cached, dest):
            if os.path.exists(dest):
                os.remove(dest)
            try:
                os.link(cached, dest)
            except OSError:
                shutil.copy(cached, dest)

    # clips for lines that are no longer in the script
    for f in os.listdir(out_dir):
        if f.endswith(".wav") and f not in wanted:
            os.remove(os.path.join(out_dir, f))
    print(f"{made} made, {kept} unchanged → {out_dir}")


if __name__ == "__main__":
    main()
