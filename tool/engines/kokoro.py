"""Kokoro (kokoro-onnx, MIT; model weights Apache): more natural than Piper, about real time on the Intel Mac. Lives in envs/piper."""
import os, re
from paths import ROOT

FILES = os.path.join(ROOT, "envs", "piper", "kokoro")
DEFAULTS = {"pause": 0.25, "comma": 0.1}   # kokoro-onnx's own sentence_pause and clause_pause, in seconds
ENGLISH = {"af": "US woman", "am": "US man", "bf": "British woman", "bm": "British man"}

def voices():
    """The English voices in the voices file (a zip of .npy files) -> {menu key: label}. No numpy needed."""
    import zipfile
    out = {}
    for n in sorted(zipfile.ZipFile(os.path.join(FILES, "voices-v1.0.bin")).namelist()):
        v = n[:-4]
        if v[:2] in ENGLISH:
            key = "michael" if v == "am_michael" else v   # 'michael' was the first key; its clips are stored under it
            out[key] = f"{v[3:].capitalize()} ({ENGLISH[v[:2]]})"
    return out

def load(speaker="am_onyx"):
    import numpy as np
    from kokoro_onnx import Kokoro
    from kokoro_onnx.config import SAMPLE_RATE
    k = Kokoro(os.path.join(FILES, "kokoro-v1.0.onnx"), os.path.join(FILES, "voices-v1.0.bin"))

    name = lambda v: "am_michael" if v == "michael" else v

    def speak(t, speed=1.0, pause=None, comma=None, mix=None, mixamt=0.3, voice=None):
        """speed 1.2 = 20% faster; mix blends in a second voice (e.g. 'am_michael'), mixamt = its share 0..1.
        voice picks the voice for this line, so one loaded model serves them all."""
        voice = name(voice or speaker)
        style = k.get_voice_style(voice)
        if mix:
            style = (1 - mixamt) * style + mixamt * k.get_voice_style(name(mix))
        say = lambda part: k.create(part, voice=style, speed=speed, lang="en-gb" if voice[0] == "b" else "en-us")[0]
        # This model reports no timings, so kokoro-onnx only pauses between its long batches: in a short box it
        # hardly stops at a full stop. So each sentence (and with a comma pause, each clause) is always made on
        # its own and joined with silence; unset pause = 0.25 s.
        gap = lambda secs: np.zeros(int(secs * SAMPLE_RATE), dtype=np.float32)
        def trimmed(part):   # each piece comes with its own silence at both ends, of varying length; cut it so the gap is exact
            x = say(part)
            loud = np.flatnonzero(np.abs(x) > 0.01)
            keep = int(0.03 * SAMPLE_RATE)
            return x[max(loud[0] - keep, 0):loud[-1] + keep] if len(loud) else x
        out = []
        for sentence in re.split(r"(?<=[.!?…])\s+", t.strip()):
            parts = re.split(r"(?<=[,;:])\s+", sentence) if comma is not None else [sentence]
            for j, part in enumerate(parts):
                out += [trimmed(part), gap(comma if j < len(parts) - 1 else DEFAULTS["pause"] if pause is None else pause)]
        return np.concatenate(out[:-1])
    return speak, SAMPLE_RATE
