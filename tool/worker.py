"""Runs inside one model's environment. Loads the model once, then makes one clip per line read from stdin.
Replies are stdout lines starting with @@ so library chatter on stdout is ignored."""
import importlib, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np, soundfile as sf

def reply(**kw):
    print("@@" + json.dumps(kw), flush=True)

try:  # Intel Macs stop at torch 2.2.2, which has no torch.xpu; newer model code asks for it
    import torch, types
    if not hasattr(torch, "xpu"):
        torch.xpu = types.SimpleNamespace(is_available=lambda: False, device_count=lambda: 0,
                                          _is_in_bad_fork=lambda: True, manual_seed_all=lambda s: None)
except ImportError:
    pass

try:
    name, _, arg = sys.argv[1].partition("-")   # "piper-p17" -> engines.piper, speaker p17
    mod = importlib.import_module("engines." + name)
    speak, sr = mod.load(arg) if arg else mod.load()
except Exception as e:
    reply(ready=False, error=f"{type(e).__name__}: {e}"); sys.exit(1)
reply(ready=True)
for line in sys.stdin:
    job = json.loads(line)
    t = time.time()
    try:
        kw = {k: v for k, v in job.items() if k not in ("text", "out", "speed")}   # the engine's own settings
        if job.get("speed", 1) != 1:
            kw["speed"] = job["speed"]
        wav = np.asarray(speak(job["text"], **kw), dtype=np.float32).flatten()
        os.makedirs(os.path.dirname(job["out"]), exist_ok=True)
        tmp = job["out"] + ".part.wav"
        sf.write(tmp, wav, sr)
        os.replace(tmp, job["out"])
        reply(ok=True, secs=round(time.time() - t, 1))
    except Exception as e:
        reply(ok=False, error=f"{type(e).__name__}: {e}")
