"""AudioRite: script in, one voice clip per line out. Open http://localhost:8765

Scripts are the .md files in Temp/. Lines starting with # are headings and are not spoken.
Clips are named by a hash of (model, spoken text), so a changed line gets a new clip and
unchanged lines are never made again. Only one model is loaded at a time (old Mac, 32 GB).
"""
import glob, hashlib, json, os, queue, re, shutil, subprocess, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

TOOL = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TOOL)
TEMP = os.path.join(ROOT, "Temp")
CLIPS = os.path.join(TEMP, "clips")
WORDS = os.path.join(ROOT, "words.txt")
PROJECTS = os.environ.get("VIDEORITE_PROJECTS") or os.path.join(ROOT, "projects")  # a test copy can use its own
PORT = int(os.environ.get("VIDEORITE_PORT", 8765))  # a test copy can run on another port
for d in (PROJECTS, CLIPS, os.path.join(ROOT, "work")):   # a fresh download has none of these yet
    os.makedirs(d, exist_ok=True)

# model id -> (label, environment folder in envs/)
MODELS = {
    "piper-northern": ("Piper northern English male", "piper"),
    "piper-ryanhigh": ("Piper Ryan (high)", "piper"),
    # Ryan medium is left out: its file ignores the speed setting and always rushes (Ryan high is the same man)
    "piper-mike": ("Piper Mike (medium)", "piper"),
    "piper-bryce": ("Piper Bryce (medium)", "piper"),
    **{f"piper-{p}": (f"Piper {p}", "piper") for p in ["p17", "p55", "p90", "p122", "p157", "p204", "p210", "p216"]},
}
try:  # Kokoro: every English voice in its voices file; kokoro-onnx lives in envs/piper
    from engines.kokoro import voices as kokoro_voices
    MODELS.update({f"kokoro-{k}": (f"Kokoro {v}", "piper") for k, v in kokoro_voices().items()})
except FileNotFoundError:
    pass


def word_list():
    """words.txt lines like 'CanGrow = Can Grow' -> [(word, said_as)]."""
    pairs = []
    if os.path.exists(WORDS):
        for raw in open(WORDS, encoding="utf-8"):
            if "=" in raw and not raw.strip().startswith("#"):
                a, b = raw.split("=", 1)
                pairs.append((a.strip(), b.strip()))
    return pairs


def spoken(text):
    for a, b in word_list():
        text = re.sub(r"(?<!\w)" + re.escape(a) + r"(?!\w)", b, text)
    return text


# The engine's own settings a project or box can set, beyond speed. Unset = the engine's default.
SETTINGS = {"piper": ("rhythm", "tone"),            # Piper noise_w, noise_scale
            "kokoro": ("pause", "comma", "mix", "mixamt")}  # pauses in seconds; mix = second voice, mixamt = its share
TAGS = {"rhythm": "r", "tone": "t"}   # the hash tags clips were first made with


def clip_id(model, text, speed=1.0, extra=None):
    speed = round(float(speed), 2)
    key = f"{model}|{spoken(text)}" + (f"|{speed}" if speed != 1 else "")
    for k, v in sorted((extra or {}).items()):   # only set ones, so clips made before a setting existed stay valid
        key += f"|{TAGS.get(k, k)}{round(float(v), 2) if isinstance(v, (int, float)) else v}"
    if {"pause", "comma"} & set(extra or {}):
        key += "|trim"   # set pauses are exact since 2026-11-01; clips made before had the model's edge silence on top
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def clip_path(model, text, speed=1.0, extra=None):
    return os.path.join(CLIPS, model, clip_id(model, text, speed, extra) + ".wav")


def voice_defaults(model):
    """The values the setting sliders start from: Piper's rhythm and tone, Kokoro's pauses."""
    engine, _, arg = model.partition("-")
    if engine == "piper":
        from engines.piper import defaults
        return defaults(arg)
    if engine == "kokoro":
        from engines.kokoro import DEFAULTS
        return DEFAULTS
    return None


def script_lines(name):
    lines = []
    for raw in open(os.path.join(TEMP, name), encoding="utf-8"):
        line = raw.strip()
        if line:
            lines.append({"text": line, "heading": line.startswith("#")})
    return lines


def project_file(name):
    return os.path.join(PROJECTS, os.path.basename(name), "project.json")


ORDER = os.path.join(PROJECTS, "order.json")   # the order he dragged the projects into


def project_names():
    """Projects in his order; new ones (not in order.json yet) at the end, A–Z."""
    names = {os.path.basename(os.path.dirname(p)) for p in glob.glob(os.path.join(PROJECTS, "*", "project.json"))}
    order = json.load(open(ORDER, encoding="utf-8")) if os.path.exists(ORDER) else []
    return [n for n in order if n in names] + sorted(names - set(order))


def save_order(names):
    with open(ORDER, "w", encoding="utf-8") as f:
        json.dump(names, f, indent=1, ensure_ascii=False)


def load_project(name):
    return json.load(open(project_file(name), encoding="utf-8"))


def voices(project):
    """For every item in project["boxes"], the voice settings it uses: the speaker of the last speaker row above it,
    else the project's own (the default voice). Divider rows are {"kind": "speaker" | "scene" | "end", ...};
    a scene runs from its row to its end row (older projects have none: to the next scene)."""
    speakers = {sp["name"]: sp for sp in project.get("speakers", [])}
    cur, out = project, []
    for box in project["boxes"]:
        if box.get("kind") == "speaker":
            cur = speakers.get(box.get("speaker"), project)
        out.append(cur)
    return out


def box_settings(vs, box):
    """(speed, {engine setting: value}) for a box: its own, else its voice's, only the ones that are set."""
    speed = box.get("speed") or vs["speed"]
    extra = {}
    for k in SETTINGS.get(vs["voice"].partition("-")[0], ()):
        v = box[k] if box.get(k) is not None else vs.get(k)
        if v is not None:
            extra[k] = v
    if "mix" not in extra:
        extra.pop("mixamt", None)
    return speed, extra


def box_clip(vs, box):
    """(clip id, done) for a box, using its own settings or its voice's."""
    s = box_settings(vs, box)
    return clip_id(vs["voice"], box["text"].strip(), *s), \
        os.path.exists(clip_path(vs["voice"], box["text"].strip(), *s))


def box_file_name(n, text, scene=0, speaker=""):
    words = re.sub(r"[^\w' ,.-]", "", text.replace("\u2019", "'")).split()
    who = re.sub(r"[^\w' .-]", "", speaker) + " - " if speaker else ""
    return f"{f'S{scene}-' if scene else ''}{n:02d} {who}{' '.join(words[:8]).rstrip(',.')}.wav"


def export_project(name):
    """Every converted box as a numbered clip in 'projects/<name>/Final Cut/', then open it in Finder."""
    project = load_project(name)
    out = os.path.join(PROJECTS, os.path.basename(name), "Final Cut")
    os.makedirs(out, exist_ok=True)
    wanted, missing, n, scene, inside = set(), 0, 0, 0, False
    for box, vs in zip(project["boxes"], voices(project)):
        if box.get("kind") == "scene":
            scene, inside = scene + 1, True
        if box.get("kind") == "end":
            inside = False
        if box.get("kind") or not box["text"].strip():
            continue
        n += 1
        src = clip_path(vs["voice"], box["text"].strip(), *box_settings(vs, box))
        if not os.path.exists(src):
            missing += 1
            continue
        fname = box_file_name(n, box["text"].strip(), scene if inside else 0, vs.get("name", ""))
        wanted.add(fname)
        dest = os.path.join(out, fname)
        if os.path.exists(dest):
            os.remove(dest)
        shutil.copy(src, dest)
    for f in os.listdir(out):
        if f.endswith(".wav") and f not in wanted:
            os.remove(os.path.join(out, f))
    subprocess.Popen(["open", out])
    return {"clips": len(wanted), "missing": missing}


def save_clips(name, picks):
    """Copy the chosen boxes' clips to Downloads/<project>/, named by the box's own name (else the automatic one),
    then open the folder in Finder. Returns which boxes were saved (index -> clip id) and how many weren't converted."""
    project = load_project(name)
    out = os.path.join(os.path.expanduser("~/Downloads"), os.path.basename(name))
    os.makedirs(out, exist_ok=True)
    saved, missing, used, n, scene, inside = {}, 0, set(), 0, 0, False
    for i, (box, vs) in enumerate(zip(project["boxes"], voices(project))):
        if box.get("kind") == "scene":
            scene, inside = scene + 1, True
        if box.get("kind") == "end":
            inside = False
        if box.get("kind") or not box["text"].strip():
            continue
        n += 1
        if i not in picks:
            continue
        cid, done = box_clip(vs, box)
        if not done:
            missing += 1
            continue
        label = re.sub(r"[/:\\]", "-", box.get("label", "")).strip()
        fname = label + ".wav" if label else box_file_name(n, box["text"].strip(), scene if inside else 0, vs.get("name", ""))
        base, k = fname[:-4], 2
        while fname in used:   # two boxes with the same name: "Intro (2).wav"
            fname, k = f"{base} ({k}).wav", k + 1
        used.add(fname)
        shutil.copy(clip_path(vs["voice"], box["text"].strip(), *box_settings(vs, box)), os.path.join(out, fname))
        saved[i] = cid
    if saved:
        subprocess.Popen(["open", out])
    return {"saved": saved, "missing": missing, "folder": out}


def export_text(name, picks):
    """The chosen boxes' text in one file in Downloads/<project>/, each under its box name (else its number),
    then open it. A new file each time ("<project> text (2).txt"), so an earlier export is never overwritten."""
    project = load_project(name)
    parts, n = [], 0
    for i, box in enumerate(project["boxes"]):
        if box.get("kind"):
            continue
        n += 1   # the number the page shows
        text = box.get("text", "").strip()
        if i in picks and text:
            parts.append(f"{box.get('label', '').strip() or n}\n{text}")
    if not parts:
        return {"count": 0}
    out = os.path.join(os.path.expanduser("~/Downloads"), os.path.basename(name))
    os.makedirs(out, exist_ok=True)
    path, k = os.path.join(out, f"{os.path.basename(name)} text.txt"), 2
    while os.path.exists(path):
        path, k = os.path.join(out, f"{os.path.basename(name)} text ({k}).txt"), k + 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n\n".join(parts) + "\n")
    subprocess.Popen(["open", path])
    return {"count": len(parts), "file": path}


def clip_name(section, n, text):
    words = re.sub(r"[^\w' ,.-]", "", text.replace("\u2019", "'")).split()
    return f"{section:02d}-{n:02d} {' '.join(words[:8]).rstrip(',.')}.wav"


def final_cut_folder(script, model):
    """Named copies of the made clips in 'Temp/<script> clips/', in script order, then open it in Finder."""
    out = os.path.join(TEMP, os.path.splitext(os.path.basename(script))[0] + " clips")
    os.makedirs(out, exist_ok=True)
    wanted, missing, section, n = set(), 0, 0, 0
    for ln in script_lines(script):
        if ln["heading"]:
            section, n = section + 1, 0
            continue
        n += 1
        src = clip_path(model, ln["text"])
        if not os.path.exists(src):
            missing += 1
            continue
        name = clip_name(section, n, ln["text"])
        wanted.add(name)
        dest = os.path.join(out, name)
        if os.path.exists(dest):
            os.remove(dest)
        os.link(src, dest)
    for f in os.listdir(out):
        if f.endswith(".wav") and f not in wanted:
            os.remove(os.path.join(out, f))
    subprocess.Popen(["open", out])
    return {"clips": len(wanted), "missing": missing}


SAVE_LOCK = threading.Lock()


def worker_key(model):
    """The worker a model runs in: all Kokoro voices share one (one model file, the voice is per line)."""
    return "kokoro" if model.startswith("kokoro-") else model


class Engine:
    """One worker process at a time, fed from a queue by one thread."""

    def __init__(self):
        self.jobs = queue.Queue()
        self.proc = None
        self.model = None
        self.state = "idle"
        self.current = None      # clip id being made
        self.errors = {}         # clip id -> message
        self.secs = {}           # clip id -> seconds it took
        self.queued = set()
        threading.Thread(target=self.loop, daemon=True).start()

    def stop_worker(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
        self.proc, self.model = None, None

    def read_reply(self):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                return {"ok": False, "ready": False, "error": "model process ended (see work/worker.log)"}
            if line.startswith("@@"):
                return json.loads(line[2:])

    def start_worker(self, model):
        self.stop_worker()
        self.state = f"loading {MODELS[model][0]} (first time downloads it)"
        py = os.path.join(ROOT, "envs", MODELS[model][1], "bin", "python")
        log = open(os.path.join(ROOT, "work", "worker.log"), "a")
        # low priority (nice), so the Mac stays usable while a long box is made
        self.proc = subprocess.Popen(["nice", "-n", "10", py, os.path.join(TOOL, "worker.py"), worker_key(model)],
                                     cwd=TOOL, text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log)
        r = self.read_reply()
        if not r.get("ready"):
            self.stop_worker()
            raise RuntimeError(r.get("error", "could not load model"))
        self.model = model

    def loop(self):
        while True:
            model, text, s, cid = self.jobs.get()
            if cid not in self.queued:   # cleared by Stop
                continue
            try:
                if worker_key(self.model or "") != worker_key(model):
                    self.start_worker(model)
                self.model = model
                self.current = cid
                self.state = f"making a line with {MODELS[model][0]}"
                voice = {"voice": model.partition("-")[2]} if worker_key(model) == "kokoro" else {}
                self.proc.stdin.write(json.dumps({"text": spoken(text), "out": clip_path(model, text, *s),
                                                     "speed": round(float(s[0]), 2), **s[1], **voice}) + "\n")
                self.proc.stdin.flush()
                r = self.read_reply()
                if r.get("ok"):
                    self.secs[cid] = r["secs"]
                    self.errors.pop(cid, None)
                else:
                    self.errors[cid] = r.get("error", "failed")
            except Exception as e:
                self.errors[cid] = str(e)
                self.stop_worker()  # start fresh on the next line
            self.queued.discard(cid)
            self.current = None
            if not self.queued:
                self.state = f"ready ({MODELS[self.model][0]} loaded)" if self.model else "idle"

    def add(self, model, text, speed=1.0, extra=None):
        s = (speed, extra or {})
        cid = clip_id(model, text, *s)
        if cid in self.queued or os.path.exists(clip_path(model, text, *s)):
            return
        self.queued.add(cid)
        self.errors.pop(cid, None)
        self.jobs.put((model, text, s, cid))

    def clear(self):
        self.queued.clear()


engine = Engine()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "")) if ctype == "audio/wav" else None
        full = len(body)
        if m and full:  # the player asks for a part when you move it forward; without this it can't seek
            a, b = m.groups()
            start, end = (int(a), int(b or full - 1)) if a else (max(0, full - int(b)), full - 1)
            body, code = body[start:min(end, full - 1) + 1], 206
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if ctype == "audio/wav":
            self.send_header("Accept-Ranges", "bytes")
            if code == 206:
                self.send_header("Content-Range", f"bytes {start}-{start + len(body) - 1}/{full}")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/":
            return self.send(open(os.path.join(TOOL, "projects.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if url.path == "/quick":
            return self.send(open(os.path.join(TOOL, "say.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if url.path == "/api/projects":
            return self.send({"projects": project_names()})
        if url.path == "/api/project":
            project = load_project(q["name"])
            for box, vs in zip(project["boxes"], voices(project)):
                if not box.get("kind"):
                    box["clip"], box["done"] = box_clip(vs, box)
                    box["voice"] = vs["voice"]
            return self.send(project)
        if url.path == "/script":
            return self.send(open(os.path.join(TOOL, "index.html"), encoding="utf-8").read(), "text/html; charset=utf-8")
        if url.path == "/api/setup":
            scripts = sorted(os.path.basename(p) for p in glob.glob(os.path.join(TEMP, "*.md")))
            ready = {k: v[0] for k, v in MODELS.items() if os.path.isdir(os.path.join(ROOT, "envs", v[1]))}
            return self.send({"scripts": scripts, "models": ready,
                              "defaults": {k: voice_defaults(k) for k in ready if voice_defaults(k)}})
        if url.path == "/api/lines":
            model, out = q["model"], []
            for ln in script_lines(q["script"]):
                if not ln["heading"]:
                    cid = clip_id(model, ln["text"])
                    ln.update(id=cid, done=os.path.exists(clip_path(model, ln["text"])), queued=cid in engine.queued,
                              working=cid == engine.current, error=engine.errors.get(cid), secs=engine.secs.get(cid))
                out.append(ln)
            return self.send({"lines": out, "state": engine.state, "left": len(engine.queued)})
        if url.path == "/api/text":
            return self.send(open(os.path.join(TEMP, q["script"]), encoding="utf-8").read(), "text/plain; charset=utf-8")
        if url.path == "/api/words":
            return self.send(open(WORDS, encoding="utf-8").read() if os.path.exists(WORDS) else "", "text/plain; charset=utf-8")
        if url.path.startswith("/clip/"):
            model, cid = unquote(url.path)[6:].split("/")
            p = os.path.join(CLIPS, os.path.basename(model), os.path.basename(cid) + ".wav")
            if os.path.exists(p):
                return self.send(open(p, "rb").read(), "audio/wav")
        self.send({"error": "not found"}, code=404)

    def do_POST(self):
        b = self.body()
        if self.path == "/api/make":
            for text in b["lines"]:
                engine.add(b["model"], text)
            return self.send({"ok": True})
        if self.path == "/api/stop":
            engine.clear()
            return self.send({"ok": True})
        if self.path == "/api/text":
            open(os.path.join(TEMP, os.path.basename(b["script"])), "w", encoding="utf-8").write(b["text"])
            return self.send({"ok": True})
        if self.path == "/api/words":
            open(WORDS, "w", encoding="utf-8").write(b["text"])
            return self.send({"ok": True})
        if self.path == "/api/project/new":
            name = re.sub(r"[^\w .-]", "", b["name"]).strip() or "Untitled"
            if not os.path.exists(project_file(name)):
                os.makedirs(os.path.dirname(project_file(name)), exist_ok=True)
                json.dump({"voice": "piper-mike", "speed": 1.0, "boxes": [{"text": ""}]},
                          open(project_file(name), "w", encoding="utf-8"), indent=1)
            return self.send({"name": name})
        if self.path == "/api/project/rename":
            old, new = os.path.basename(b["name"]), re.sub(r"[^\w .-]", "", b["new"]).strip()
            if not new or new == old:
                return self.send({"error": "Give it a new name."})
            if os.path.exists(os.path.join(PROJECTS, new)):
                return self.send({"error": f"There is already a project called {new}."})
            order = project_names()
            with SAVE_LOCK:
                os.rename(os.path.join(PROJECTS, old), os.path.join(PROJECTS, new))
            save_order([new if n == old else n for n in order])
            return self.send({"name": new})
        if self.path == "/api/project/delete":
            name = os.path.basename(b["name"])
            folder = os.path.join(PROJECTS, name)
            if not name or not os.path.isdir(folder):
                return self.send({"error": f"No project called {name}."})
            trash, stamp = os.path.expanduser("~/.Trash"), time.strftime("%H.%M.%S")
            dest = os.path.join(trash, name if not os.path.exists(os.path.join(trash, name)) else f"{name} {stamp}")
            with SAVE_LOCK:
                try:
                    shutil.move(folder, dest)
                except OSError:  # no access to ~/.Trash: ask Finder to do it
                    subprocess.run(["osascript", "-e", f'tell application "Finder" to delete POSIX file "{folder}"'],
                                   capture_output=True)
            if os.path.exists(folder):
                return self.send({"error": "Couldn't move it to the Trash."})
            save_order([n for n in project_names() if n != name])
            return self.send({"ok": True})
        if self.path == "/api/projects/order":
            save_order([n for n in b["order"] if n in project_names()])
            return self.send({"ok": True})
        if self.path == "/api/project/save":
            names = [k for ks in SETTINGS.values() for k in ks]
            val = lambda v: v if isinstance(v, str) else float(v)
            own = lambda x: {k: val(x[k]) for k in ["speed", *names] if x.get(k) not in (None, "")}
            def item(x):
                if x.get("kind") == "speaker":
                    return {"kind": "speaker", "speaker": x.get("speaker", "")}
                if x.get("kind") == "end":
                    return {"kind": "end"}
                if x.get("kind") == "scene":
                    return {"kind": "scene", "title": x.get("title", ""), **({"folded": True} if x.get("folded") else {})}
                return {"text": x["text"], **own(x),
                        **{k: x[k] for k in ("label", "saved") if isinstance(x.get(k), str) and x[k].strip()}}
            data = {"voice": b["voice"], "speed": float(b["speed"]),
                    **{k: val(b[k]) for k in names if b.get(k) not in (None, "")},
                    **({"speakers": [{"name": sp["name"], "voice": sp["voice"], "speed": float(sp["speed"]),
                                      **{k: val(sp[k]) for k in names if sp.get(k) not in (None, "")}}
                                     for sp in b["speakers"]]} if b.get("speakers") else {}),
                    "boxes": [item(x) for x in b["boxes"]]}
            if not os.path.isdir(os.path.dirname(project_file(b["name"]))):
                return self.send({"ok": False})  # deleted (or renamed) meanwhile: don't bring it back
            with SAVE_LOCK:  # two saves at once (e.g. fold, then + scene) would write the same temp file and garble it
                tmp = project_file(b["name"]) + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=1, ensure_ascii=False)
                os.replace(tmp, project_file(b["name"]))
            return self.send({"ok": True})
        if self.path == "/api/project/convert":
            project = load_project(b["name"])
            box, vs = project["boxes"][b["box"]], voices(project)[b["box"]]
            text, s = box.get("text", "").strip(), box_settings(vs, box)
            if not text:
                return self.send({"error": "empty box"})
            cid = clip_id(vs["voice"], text, *s)
            engine.add(vs["voice"], text, *s)
            while cid in engine.queued:
                time.sleep(0.2)
            return self.send({"clip": cid, "voice": vs["voice"], "error": engine.errors.get(cid)})
        if self.path == "/api/project/saveclips":
            return self.send(save_clips(b["name"], set(b["boxes"])))
        if self.path == "/api/project/exporttext":
            return self.send(export_text(b["name"], set(b["boxes"])))
        if self.path == "/api/project/export":
            return self.send(export_project(b["name"]))
        if self.path == "/api/say":
            text, model = b["text"].strip(), b["model"]
            cid = clip_id(model, text)
            engine.add(model, text)
            while cid in engine.queued:
                time.sleep(0.2)
            return self.send({"id": cid, "error": engine.errors.get(cid)})
        if self.path == "/api/new":
            name = re.sub(r"[^\w .-]", "", b["name"]).strip() or "script"
            name = name if name.endswith(".md") else name + ".md"
            p = os.path.join(TEMP, name)
            if not os.path.exists(p):
                open(p, "w", encoding="utf-8").write("")
            return self.send({"script": name})
        if self.path == "/api/finalcut":
            return self.send(final_cut_folder(b["script"], b["model"]))
        if self.path == "/api/redo":
            p = clip_path(b["model"], b["text"])
            if os.path.exists(p):
                os.remove(p)
            engine.add(b["model"], b["text"])
            return self.send({"ok": True})
        self.send({"error": "not found"}, code=404)


if __name__ == "__main__":
    print(f"AudioRite on http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
