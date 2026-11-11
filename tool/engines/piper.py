"""Piper: northern_english_male, Ryan, Mike, Bryce, or a libritts speaker picked by ear. Fast on an Intel Mac."""
import json, os
from paths import ROOT

VOICES = os.path.join(ROOT, "envs", "piper", "voices")
SINGLE = {"northern": "en_GB-northern_english_male-medium",
          "ryanhigh": "en_US-ryan-high", "ryanmedium": "en_US-ryan-medium",
          "mike": "en_US-mike-medium", "bryce": "en_US-bryce-medium"}

def voice_file(speaker):
    return os.path.join(VOICES, SINGLE.get(speaker, "en_US-libritts-high") + ".onnx")

def defaults(speaker):
    """The voice's own rhythm (noise_w) and tone variation (noise_scale), from its .onnx.json."""
    inf = json.load(open(voice_file(speaker) + ".json", encoding="utf-8")).get("inference", {})
    return {"rhythm": inf.get("noise_w", 0.8), "tone": inf.get("noise_scale", 0.667)}

def load(speaker):
    import numpy as np
    from piper import PiperVoice, SynthesisConfig
    voice = PiperVoice.load(voice_file(speaker))
    cfg = {} if speaker in SINGLE else {"speaker_id": voice.config.speaker_id_map[speaker]}

    def speak(t, speed=1.0, rhythm=None, tone=None):  # speed 1.2 = 20% faster; None = the voice's own value
        syn = SynthesisConfig(length_scale=1 / speed, noise_w_scale=rhythm, noise_scale=tone, **cfg)
        return np.concatenate([c.audio_int16_array for c in voice.synthesize(t, syn_config=syn)]) / 32768.0
    return speak, voice.config.sample_rate
