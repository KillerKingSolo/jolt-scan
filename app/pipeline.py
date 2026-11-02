"""Jolt Scan pipeline: download, probe, find the HUD, crop, scan jolts, analyze."""
import csv, json, math, os, re, shutil, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor

APP = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(os.path.dirname(APP), "projects")
TSCLI = os.path.join(APP, "tscli", "target", "release", "tscli.exe")
WINGET = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Links")
os.environ["PATH"] = WINGET + os.pathsep + os.environ["PATH"]  # tscli calls ffmpeg by name
MAX_TICK = 20 * 3600 * 24
WORKERS = 4
ID_RE = re.compile(r"^[\w-]{1,64}$")

jobs = {}  # project id -> progress dict
lock = threading.Lock()


def tool(name):
    return shutil.which(name) or os.path.join(WINGET, name + ".exe")


def pdir(pid):
    if not ID_RE.match(pid):
        raise ValueError("bad project id")
    return os.path.join(PROJECTS, pid)


def load(pid, name, default=None):
    try:
        with open(os.path.join(pdir(pid), name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(pid, name, data):
    path = os.path.join(pdir(pid), name)
    with lock:
        with open(path + ".tmp", "w", encoding="utf-8") as f:
            json.dump(data, f)
        # Windows refuses the swap while a reader has the file open; readers are quick, so retry
        for i in range(50):
            try:
                return os.replace(path + ".tmp", path)
            except PermissionError:
                time.sleep(0.02 * (i + 1))
        os.replace(path + ".tmp", path)


def set_job(pid, **kw):
    with lock:
        jobs.setdefault(pid, {}).update(kw, t=time.time())


def run_bg(pid, fn, *args):
    def wrap():
        try:
            fn(pid, *args)
        except Exception as e:  # shown in the app
            set_job(pid, running=False, error=str(e))
            meta = load(pid, "meta.json", {})
            back = meta.pop("stage_before_error", None)
            meta["stage"] = back or ("setup" if meta.get("hud") and meta.get("duration") else "failed")
            save(pid, "meta.json", meta)
    set_job(pid, running=True, error=None, step="starting", pct=0)
    threading.Thread(target=wrap, daemon=True).start()


def video_path(pid):
    d = pdir(pid)
    for n in os.listdir(d):
        if n.startswith("video."):
            return os.path.join(d, n)
    return None


def list_projects():
    os.makedirs(PROJECTS, exist_ok=True)
    out = []
    for pid in sorted(os.listdir(PROJECTS)):
        meta = load(pid, "meta.json") if ID_RE.match(pid) else None
        if meta:
            res = load(pid, "result.json")
            meta = dict(meta, id=pid, job=jobs.get(pid),
                        summary={"jolts": len(res["points"]), "cuts": sum(c["removed"] > 0 for c in res["cuts"])} if res else None)
            out.append(meta)
    return sorted(out, key=lambda m: -m.get("created", 0))


# ---- create ----

def youtube_id(url):
    m = re.search(r"(?:youtu\.be/|v=|shorts/|live/)([\w-]{11})", url)
    return m and m.group(1)


def create_from_url(url, height):
    vid = youtube_id(url)
    if not vid:
        raise ValueError("That is not a YouTube video link.")
    # a second download of the same video (say at another quality) gets its own project
    pid, n = vid, 1
    while os.path.isdir(pdir(pid)) and (video_path(pid) or load(pid, "result.json")):
        n += 1
        pid = f"{vid}-{height}p" if n == 2 else f"{vid}-{height}p-{n - 1}"
    os.makedirs(pdir(pid), exist_ok=True)
    meta = load(pid, "meta.json") or {"created": time.time()}
    meta.update(url=f"https://youtu.be/{vid}", title=meta.get("title", vid), stage="downloading", height=height)
    save(pid, "meta.json", meta)
    run_bg(pid, _download, height)
    return pid


def _download(pid, height):
    _fetch(pid, height)
    _prepare(pid)


def _fetch(pid, height):
    d = pdir(pid)
    meta = load(pid, "meta.json")
    set_job(pid, step="Reading video info", pct=0)
    info = subprocess.run([tool("yt-dlp"), "--skip-download", "--no-playlist", "--print", "%(title)s\t%(duration)s\t%(upload_date)s", meta["url"]],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if info.returncode:
        lines = info.stderr.strip().splitlines()
        raise RuntimeError("yt-dlp could not read the video: " + (lines[-1] if lines else "unknown error"))
    title, dur, date = (info.stdout.strip().split("\t") + ["", "", ""])[:3]
    meta.update(title=title, uploaded=date)
    save(pid, "meta.json", meta)
    fmt = f"bv*[height<={height}]+ba/b[height<={height}]/bv*+ba/b"
    p = subprocess.Popen([tool("yt-dlp"), "--no-playlist", "--newline", "-f", fmt, "--merge-output-format", "mp4",
                          "-o", os.path.join(d, "video.%(ext)s"), "--ffmpeg-location", os.path.dirname(tool("ffmpeg")), meta["url"]],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    part, last = 0, ""
    for line in p.stdout:
        last = line.strip() or last
        if "Destination:" in line:
            part += 1
        m = re.search(r"\[download\]\s+([\d.]+)%", line)
        if m:
            set_job(pid, step=f"Downloading {'video' if part <= 1 else 'audio'}", pct=float(m.group(1)))
        elif "Merger" in line:
            set_job(pid, step="Merging video and audio", pct=100)
    if p.wait():
        raise RuntimeError("Download failed: " + last)


def create_from_upload(name, stream, length):
    base = re.sub(r"[^\w-]+", "-", os.path.splitext(name)[0]).strip("-")[:40] or "video"
    pid = f"{base}-{int(time.time()) % 100000}"
    ext = os.path.splitext(name)[1].lower() if re.match(r"^\.\w{2,4}$", os.path.splitext(name)[1]) else ".mp4"
    os.makedirs(pdir(pid))
    save(pid, "meta.json", {"created": time.time(), "title": os.path.splitext(name)[0], "url": "", "stage": "uploading"})
    set_job(pid, running=True, step="Copying video", pct=0, error=None)
    done = 0
    with open(os.path.join(pdir(pid), "video" + ext), "wb") as f:
        while done < length:
            chunk = stream.read(min(8 << 20, length - done))
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            set_job(pid, pct=done / length * 100)
    if done < length:
        set_job(pid, running=False, error="The upload stopped before the whole file arrived.")
        return pid
    run_bg(pid, _prepare)
    return pid


def _prepare(pid):
    set_job(pid, step="Reading the video file", pct=100)
    v = video_path(pid)
    pr = json.loads(subprocess.run([tool("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
                                    "stream=width,height,r_frame_rate,codec_name:format=duration", "-of", "json", v],
                                   capture_output=True, text=True).stdout)
    st = pr["streams"][0]
    num, den = map(int, st["r_frame_rate"].split("/"))
    meta = load(pid, "meta.json")
    meta.update(width=st["width"], height_px=st["height"], fps=num / den, codec=st.get("codec_name"),
                duration=float(pr["format"]["duration"]))
    meta.setdefault("trim", [0, meta["duration"]])
    save(pid, "meta.json", meta)
    find_hud(pid)
    check_hud(pid)
    meta = load(pid, "meta.json")
    meta["stage"] = "setup"
    save(pid, "meta.json", meta)
    set_job(pid, running=False, step="Ready to set up", pct=100)


HUD_OK = 30  # hudcheck scores: his own HUD about 10, other POVs or another GUI scale 70+


def check_hud(pid):
    """Score the HUD position on every keyframe. Long stretches without it are other POVs, another GUI
    scale or a hidden HUD; a stretch like that at the start or end becomes the suggested trim."""
    set_job(pid, step="Checking where the HUD is visible", pct=100)
    meta = load(pid, "meta.json")
    h = meta["hud"]
    out = subprocess.run([TSCLI, "hudcheck", video_path(pid), str(meta["width"]), str(meta["height_px"]), str(h["s"]), str(h["cx"]), str(h["by"])],
                         capture_output=True, text=True)
    track = [[float(t), float(s)] for t, s in (l.split(",") for l in out.stdout.split() if "," in l) if t != "nan"]
    save(pid, "hudtrack.json", track)
    if len(track) < 5:
        return
    # majority over 5 keyframes, so a menu or a dark frame does not count as a missing HUD
    good = [sum(s < HUD_OK for _, s in track[max(0, i - 2):i + 3]) >= 3 for i in range(len(track))]
    runs, i = [], 0
    while i < len(good):
        j = i
        while j + 1 < len(good) and good[j + 1] == good[i]:
            j += 1
        runs.append([good[i], track[i][0], track[j + 1][0] if j + 1 < len(track) else meta["duration"]])
        i = j + 1
    a = runs[0][2] if not runs[0][0] and len(runs) > 1 and runs[0][2] - runs[0][1] >= 5 else 0.0
    b = runs[-1][1] if not runs[-1][0] and len(runs) > 1 and runs[-1][2] - runs[-1][1] >= 15 else meta["duration"]
    meta = load(pid, "meta.json")
    meta["trim_suggest"] = [round(a, 2), round(b, 2)]
    meta["hud_missing"] = [[round(r[1], 2), round(r[2], 2)] for r in runs if not r[0] and r[2] - r[1] >= 10]
    if not meta.get("trim_by_user"):
        meta["trim"] = meta["trim_suggest"]
    save(pid, "meta.json", meta)


def find_hud(pid):
    set_job(pid, step="Finding the HUD", pct=100)
    meta = load(pid, "meta.json")
    out = subprocess.run([TSCLI, "hudfind", video_path(pid), str(meta["width"]), str(meta["height_px"]), str(meta["duration"]), "16"],
                         capture_output=True, text=True)
    hud = json.loads(out.stdout)
    # good fits score near 10 against a typical 90+; a weak ratio means the user should check it
    hud["confident"] = hud["score"] < 0.35 * hud["typical"]
    meta["hud"] = hud
    save(pid, "meta.json", meta)
    return hud


# ---- analyze ----

def boxes(meta):
    """Crop rect around both bars, plus hunger and health boxes inside it."""
    h = meta["hud"]
    s, cx, by = h["s"], h["cx"], h["by"]
    if "crop" in meta and meta.get("crop_hud") == [s, cx, by]:
        x0, y0, cw, ch = meta["crop"]
    else:
        x0, y0 = max(0, math.floor(cx - 94 * s)), max(0, math.floor(by - 3 * s))
        cw = min(meta["width"] - x0, math.ceil(188 * s)) // 2 * 2
        ch = min(meta["height_px"] - y0, math.ceil(15 * s)) // 2 * 2
    r = lambda v: int(math.floor(v + 0.5))  # half up; round() would give 364 for 364.5
    hunger = [r(cx + 10 * s - x0), r(by - y0), r(81 * s), r(9 * s)]
    health = [r(cx - 91 * s - x0), r(by - y0), r(81 * s), r(9 * s)]
    return [x0, y0, cw, ch], hunger, health


def start_analyze(pid, trim, hud):
    meta = load(pid, "meta.json")
    if hud:
        meta["hud"] = dict(meta.get("hud", {}), **{k: float(hud[k]) for k in ("s", "cx", "by")})
    a, b = max(0.0, float(trim[0])), min(meta["duration"], float(trim[1]))
    if b - a < 1:
        raise ValueError("The trimmed part must be at least 1 second long.")
    meta.update(trim=[a, b], trim_by_user=True, stage_before_error=meta.get("stage"), stage="analyzing")
    save(pid, "meta.json", meta)
    run_bg(pid, _analyze)


def _analyze(pid):
    d, meta = pdir(pid), load(pid, "meta.json")
    fps, (a, b) = meta["fps"], meta["trim"]
    crop, hunger, health = boxes(meta)
    hud = [meta["hud"]["s"], meta["hud"]["cx"], meta["hud"]["by"]]
    total = max(1, round((b - a) * fps))
    # reuse the HUD crop when it was made with this HUD and already covers the trim
    ca, cb = meta.get("crop_range", [None, None])
    if not (meta.get("crop_hud") == hud and ca is not None and ca <= a + 1e-6 and cb >= b - 1e-6 and os.path.exists(os.path.join(d, "hud.mp4"))):
        _crop_pass(pid, meta, crop, a, b, total)
        meta = load(pid, "meta.json")
        meta.update(crop=crop, crop_hud=hud, crop_range=[a, b], start_frame=round(a * fps))
        save(pid, "meta.json", meta)
    skip = a - meta["crop_range"][0]
    if meta.get("box_dy_hud") != hud:
        meta["box_dx"], meta["box_dy"] = _calibrate(pid, meta, hunger, health, skip, (b - a))
        meta["box_dy_hud"] = hud
        save(pid, "meta.json", meta)
    dx, dy = meta.get("box_dx", 0), meta.get("box_dy", 0)
    hunger, health = [hunger[0] + dx, hunger[1] + dy, *hunger[2:]], [health[0] + dx, health[1] + dy, *health[2:]]
    _scan(pid, meta, hunger, health, skip, total)
    set_job(pid, step="Analyzing jolts", pct=100)
    result = analyze_project(pid)
    meta = load(pid, "meta.json")
    meta.update(stage="done", analyzed=time.time())
    meta.pop("stage_before_error", None)
    save(pid, "meta.json", meta)
    set_job(pid, running=False, step=f"Done: {len(result['points'])} jolts, {sum(c['removed'] > 0 for c in result['cuts'])} jumps", pct=100)


def _crop_pass(pid, meta, crop, a, b, total):
    """One decode: HUD crop video, scene scores, and a tiny view of the menu panel."""
    d, s = pdir(pid), meta["hud"]["s"]
    W, H = meta["width"], meta["height_px"]
    pw, ph = round(176 * s), round(166 * s)
    px, py = max(0, round(W / 2 - 88 * s)), max(0, round(H / 2 - 83 * s))
    x0, y0, cw, ch = crop
    fc = (f"[0:v]split=3[x][y][z];[x]crop={cw}:{ch}:{x0}:{y0}[h];"
          f"[y]scale=320:180,scdet=threshold=8,metadata=print:file=scenes.txt[s];"
          f"[z]crop={min(pw, W - px)}:{min(ph, H - py)}:{px}:{py},scale=22:21:flags=area,format=rgb24[m]")
    cmd = [tool("ffmpeg"), "-v", "error", "-y", "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-i", video_path(pid), "-filter_complex", fc,
           "-map", "[h]", "-an", "-c:v", "libx264", "-crf", "8", "-preset", "veryfast", "-pix_fmt", "yuv444p", "-g", "60", "hud.mp4",
           "-map", "[s]", "-f", "null", os.devnull, "-map", "[m]", "-f", "rawvideo", "pipe:1"]
    set_job(pid, step="Cropping the HUD and finding scene changes and menus", pct=0)
    p = subprocess.Popen(cmd, cwd=d, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    # tscli reads the tiny menu-panel frames straight from ffmpeg and prints one share per frame
    m = subprocess.Popen([TSCLI, "panel"], stdin=p.stdout, stdout=subprocess.PIPE, text=True)
    p.stdout.close()
    err = []
    threading.Thread(target=lambda: err.append(p.stderr.read().decode(errors="replace")), daemon=True).start()
    fr = []
    for line in m.stdout:
        fr.append(line.strip())
        if len(fr) % 120 == 0:
            set_job(pid, pct=min(99.9, len(fr) / total * 100))
    m.wait()
    if p.wait():
        time.sleep(0.2)
        raise RuntimeError("ffmpeg failed while cropping: " + "".join(err).strip()[-300:])
    with open(os.path.join(d, "menus.csv"), "w") as f:
        f.write("frame,panel_frac\n" + "".join(f"{i},{v}\n" for i, v in enumerate(fr)))


def _tscli(d, hunger, health, fps, ss, t=None, on_progress=None):
    """Scan hud.mp4 from ss seconds for t seconds; frames in the result count from ss."""
    env = dict(os.environ, TSCLI_SS=f"{ss:.4f}")
    if t is not None:
        env["TSCLI_T"] = f"{t:.4f}"
    if on_progress:
        env["TSCLI_PROGRESS"] = "1"
    args = [TSCLI, "hud.mp4", *map(str, hunger), f"{fps}", str(MAX_TICK), *map(str, health)]
    p = subprocess.Popen(args, cwd=d, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def prog():
        for line in p.stderr:
            if on_progress and line.startswith("p "):
                on_progress(int(line[2:]))
    th = threading.Thread(target=prog, daemon=True)
    th.start()
    rows = list(csv.DictReader(p.stdout))
    p.wait()
    th.join()
    return rows


def _calibrate(pid, meta, hunger, health, skip, dur):
    """The best box height can sit a pixel or two off the drawn HUD at some resolutions.
    Scan samples at a few heights and keep the one where the most jolts agree with neighbours."""
    d, fps = pdir(pid), meta["fps"]
    win = min(30.0, dur / 4)
    starts = [skip + (dur - win) * k / 3 for k in range(4)]
    set_job(pid, step="Calibrating the scan boxes", pct=0)
    shifts = [(0, dy) for dy in (-1, 0, 1, 2)]

    def score(shifts, pct0, pct1):
        tasks = [(sh, ss) for sh in shifts for ss in starts]
        done = [0]
        def run(task):
            (dx, dy), ss = task
            mv = lambda b: [b[0] + dx, b[1] + dy, *b[2:]]
            rows = _tscli(d, mv(hunger), mv(health), fps, ss, win)
            done[0] += 1
            set_job(pid, pct=pct0 + (pct1 - pct0) * done[0] / len(tasks))
            return task, [_row(r, int(r["frame"]) + round(ss * fps), fps) for r in rows]
        with ThreadPoolExecutor(WORKERS) as ex:
            res = list(ex.map(run, tasks))
        return {sh: sum(len(confirm(rows)) for (s2, _), rows in res if s2 == sh) for sh in shifts}

    # height first (it matters most), then sideways at the best height
    count = score(shifts, 0, 60)
    dy = max(count, key=lambda sh: (count[sh], -abs(sh[1])))[1]
    count.update(score([(-1, dy), (1, dy)], 60, 100))
    best = max(count, key=lambda sh: (count[sh], -abs(sh[0]) - abs(sh[1])))
    meta["box_calibration"] = {f"{x},{y}": n for (x, y), n in count.items()}
    return best


def _scan(pid, meta, hunger, health, skip, total):
    """skip: seconds from the start of hud.mp4 to the trim start."""
    d, fps = pdir(pid), meta["fps"]
    n = WORKERS
    span = total / fps / n
    done = [0] * n
    set_job(pid, step="Scanning jolts and reading health", pct=0)

    def part(k):
        def prog(fr):
            done[k] = fr
            set_job(pid, pct=min(99.9, sum(done) / total * 100))
        rows = _tscli(d, hunger, health, fps, skip + k * span, span if k < n - 1 else None, prog)
        off = round((skip + k * span) * fps)
        return [dict(r, frame=int(r["frame"]) + off) for r in rows]

    with ThreadPoolExecutor(n) as ex:
        parts = list(ex.map(part, range(n)))
    sf = meta.get("start_frame", 0)
    with open(os.path.join(d, "jolts.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "food", "pattern", "hp", "game_ticks", "low_health_ticks"])
        for rows in parts:
            for r in rows:
                w.writerow([r["frame"] + sf, r["food"], r["pattern"], r["hp"], r["game_ticks"], r["low_health_ticks"]])


# ---- analysis: same rules as the cracker site's line method ----
TOL, WIN, GAP = 3, 6, 3


def _row(r, frame, fps):
    cands = [(int(t), False) for t in r["game_ticks"].split()] + [(int(t), True) for t in r["low_health_ticks"].split()]
    return {"frame": frame, "vt": round(frame / fps * 20), "food": int(r["food"]), "pat": r["pattern"], "cands": cands}


def confirm(rows):
    """Keep jolts whose crack agrees with a nearby jolt: [vt, game tick, low health, food, pattern, frame]."""
    rows = sorted(rows, key=lambda r: r["vt"])
    pts = []
    for i, r in enumerate(rows):
        best = None
        for gt, lh in r["cands"]:
            off = gt - r["vt"]
            n = sum(1 for j in range(max(0, i - WIN), min(len(rows), i + WIN + 1))
                    if j != i and any(abs(g - rows[j]["vt"] - off) <= TOL for g, _ in rows[j]["cands"]))
            if n and (best is None or n > best[2]):
                best = (off, lh, n)
        if best:
            pts.append([r["vt"], r["vt"] + best[0], int(best[1]), r["food"], r["pat"], r["frame"]])
    return pts


def analyze_project(pid):
    d, meta = pdir(pid), load(pid, "meta.json")
    fps, (a, b) = meta["fps"], meta["trim"]
    sf = meta.get("start_frame", 0)  # frame of hud.mp4's first frame; scenes and menus count from it
    rows = [_row(r, int(r["frame"]), fps) for r in csv.DictReader(open(os.path.join(d, "jolts.csv")))]
    pts = confirm([r for r in rows if a * fps <= r["frame"] <= b * fps])
    segs = []
    for p in pts:
        off = p[1] - p[0]
        if segs and abs(off - segs[-1]["off"]) <= GAP:
            segs[-1].update(e=p[5] / fps, n=segs[-1]["n"] + 1)
        else:
            segs.append({"s": p[5] / fps, "e": p[5] / fps, "n": 1, "off": off})
    segs = [s for s in segs if s["n"] >= 2]
    # short runs far from both neighbours are chance matches (garbage frames give bursts of "jolts")
    segs = [s for i, s in enumerate(segs) if (s["n"] > 2 and s["e"] - s["s"] >= 2) or not all(
        o is None or abs(o["off"] - s["off"]) > 72000 for o in (segs[i - 1] if i else None, segs[i + 1] if i + 1 < len(segs) else None))]
    pts = [p for p in pts if any(s["s"] - 1e-6 <= p[5] / fps <= s["e"] + 1e-6 and abs(p[1] - p[0] - s["off"]) <= GAP for s in segs)]

    scores = {}
    try:
        n = None
        for line in open(os.path.join(d, "scenes.txt")):
            if line.startswith("frame:"):
                n = int(line.split()[0][6:]) + sf
            elif line.startswith("lavfi.scd.score="):
                scores[n] = float(line.split("=")[1])
    except FileNotFoundError:
        pass
    menus = []
    try:
        fr = [float(r["panel_frac"]) for r in csv.DictReader(open(os.path.join(d, "menus.csv")))]
        s0 = e = None
        for i, v in enumerate(fr + [0.0] * 8):
            if v >= 0.6:
                s0 = i if s0 is None else s0
                e = i
            elif s0 is not None and i - e > 6:
                if e - s0 >= 10 and (e + sf) / fps >= a and (s0 + sf) / fps <= b:
                    menus.append([(s0 + sf) / fps, (e + sf) / fps])
                s0 = None
    except FileNotFoundError:
        pass

    cuts = []
    for x, y in zip(segs, segs[1:]):
        dlt = y["off"] - x["off"]
        f0, f1 = round(x["e"] * fps), round(y["s"] * fps)
        sc, at = max(((scores.get(f, 0.0), f) for f in range(f0, f1 + 1)), default=(0.0, f0))
        cuts.append({"key": str(round(x["e"] * 20)), "t0": x["e"], "t1": y["s"], "removed": dlt / 20, "ja": x["n"], "jb": y["n"],
                     "scene": round(sc, 1) if scores else None, "sceneAt": at / fps,
                     "menu": any(m0 <= y["s"] and m1 >= x["e"] for m0, m1 in menus)})
        # suggested status, from the patterns found by hand in the finale: cuts hide in a menu opening or
        # closing, and a jump with no scene change is a hidden cut until someone checks it
        edge = any(x["e"] - 0.1 <= m <= y["s"] + 0.1 for m0, m1 in menus for m in (m0, m1))
        c = cuts[-1]
        c["suggest"] = None if dlt <= 0 else "menu" if edge else None if c["scene"] is None else "hidden" if c["scene"] < 8 else "visible"
    result = {"points": pts, "segments": segs, "cuts": cuts, "menus": menus, "start": a, "end": b, "fps": fps}
    save(pid, "result.json", result)
    return result


# ---- project management ----

def recycle(path):
    """Send a file or folder to the Windows Recycle Bin, so a delete can be undone."""
    import ctypes
    from ctypes import wintypes
    class OP(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort), ("aborted", wintypes.BOOL), ("maps", ctypes.c_void_p), ("title", wintypes.LPCWSTR)]
    FO_DELETE, ALLOWUNDO, NOCONFIRM, SILENT, NOERRORUI = 3, 0x40, 0x10, 0x4, 0x400
    op = OP(None, FO_DELETE, os.path.abspath(path) + "\0", None, ALLOWUNDO | NOCONFIRM | SILENT | NOERRORUI, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) or op.aborted or os.path.exists(path):
        raise RuntimeError("Windows did not move it to the Recycle Bin. Close anything that is using it and try again.")


def delete_project(pid):
    if jobs.get(pid, {}).get("running"):
        raise ValueError("This project is still working.")
    recycle(pdir(pid))
    jobs.pop(pid, None)


def delete_video(pid):
    """Drop the big video but keep the scan; the small HUD crop stays for rescans of the same trim."""
    v = video_path(pid)
    if not v:
        raise ValueError("This project has no video file.")
    if jobs.get(pid, {}).get("running"):
        raise ValueError("This project is still working.")
    recycle(v)
    meta = load(pid, "meta.json")
    meta["video_deleted"] = time.time()
    save(pid, "meta.json", meta)


def redownload(pid):
    meta = load(pid, "meta.json")
    if not meta.get("url"):
        raise ValueError("This project was opened from a file, so there is nothing to download again.")
    def job(p):
        _fetch(p, meta.get("height", 2160))
        m = load(p, "meta.json")
        m.pop("video_deleted", None)
        save(p, "meta.json", m)
        set_job(p, running=False, step="Video downloaded again", pct=100)
    run_bg(pid, job)


def _rows_for_export(pid):
    meta, res, verd = load(pid, "meta.json"), load(pid, "result.json"), load(pid, "verdicts.json", {})
    if not res:
        raise ValueError("This project has not been scanned yet.")
    yt = youtube_id(meta.get("url") or "")
    out = []
    for c in res["cuts"]:
        v = verd.get(c["key"], {})
        status = "pause" if c["removed"] < 0 else v.get("status", "unchecked")
        out.append({"start": c["t0"], "end": c["t1"], "removed_s": round(c["removed"], 2), "jolts_before": c["ja"], "jolts_after": c["jb"],
                    "scene": c.get("scene"), "menu_in_gap": c.get("menu", False), "suggested": c.get("suggest") or "",
                    "status": status, "note": v.get("note", ""), "link": f"https://youtu.be/{yt}?t={int(c['t0'])}" if yt else ""})
    return meta, res, out


def _clock(sec):
    m, s = divmod(sec, 60)
    h, m = divmod(int(m), 60)
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def export_csv(pid):
    import io
    _, _, rows = _rows_for_export(pid)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["video_start", "video_end", "game_time_removed_s", "jolts_before", "jolts_after", "scene_score", "menu_in_gap", "suggested", "status", "note", "youtube_link"])
    for r in rows:
        w.writerow([_clock(r["start"]), _clock(r["end"]), r["removed_s"], r["jolts_before"], r["jolts_after"], r["scene"], r["menu_in_gap"], r["suggested"], r["status"], r["note"], r["link"]])
    return buf.getvalue()


def export_html(pid):
    """A single self-contained page to share: summary plus every jump with its status and a timestamp link."""
    from html import escape as e
    meta, res, rows = _rows_for_export(pid)
    names = {"hidden": "Hidden cut", "visible": "Visible cut", "menu": "Menu cut", "unsure": "Not sure", "notcut": "Not a cut", "unchecked": "Not checked", "pause": "Pause"}
    removed = sum(r["removed_s"] for r in rows if r["removed_s"] > 0)
    count = lambda s: sum(r["status"] == s for r in rows)
    body = "".join(
        f"<tr class='{r['status']}'><td>{'<a href=\"%s\">%s</a>' % (e(r['link']), _clock(r['start'])) if r['link'] else _clock(r['start'])} → {_clock(r['end'])}</td>"
        f"<td>{r['removed_s']:+.2f} s</td><td>{r['jolts_before']} | {r['jolts_after']}</td><td>{names[r['status']]}</td><td>{e(r['note'])}</td></tr>"
        for r in rows)
    title = e(meta.get("title", pid))
    link = f"<a href='{e(meta['url'])}'>{e(meta['url'])}</a>" if meta.get("url") else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jolt Scan report: {title}</title><style>
body{{background:#07111e;color:#d7e7fb;font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px 16px}} main{{max-width:1000px;margin:0 auto}}
h1{{color:#c6f04d;font-size:22px;margin:0 0 4px}} .m{{color:#8aa4c2}} a{{color:#6fd3ff}}
.s{{display:flex;flex-wrap:wrap;gap:10px;margin:16px 0}} .s div{{border:1px solid #234463;border-radius:8px;padding:8px 12px}} .s b{{display:block;font-size:20px}}
.t{{overflow-x:auto}} table{{border-collapse:collapse;width:100%;min-width:640px;font-size:14px}} th,td{{text-align:left;padding:6px 8px;border-bottom:1px solid #13263c;vertical-align:top}}
th{{color:#8aa4c2;font-size:12px;text-transform:uppercase}} td:nth-child(-n+3){{font-family:ui-monospace,Consolas,monospace;white-space:nowrap}}
tr.hidden td:nth-child(4){{color:#ff6bd5}} tr.menu td:nth-child(4){{color:#f5b84a}} tr.visible td:nth-child(4){{color:#6fd3ff}} tr.unchecked td,tr.pause td{{color:#8aa4c2}}
</style></head><body><main><h1>{title}</h1><p class="m">{link}<br>Jolt Scan report, scanned {_clock(res['start'])} to {_clock(res['end'])} of the video.
Game time comes from cracking Minecraft hunger jolts; a jump means game time passed that the video does not show.</p>
<div class="s"><div><b>{len(res['points'])}</b>confirmed jolts</div><div><b>{sum(r['removed_s'] > 0 for r in rows)}</b>jumps in game time</div>
<div><b>{count('hidden') + count('menu')}</b>marked hidden or menu cuts</div><div><b>{count('visible')}</b>marked visible cuts</div><div><b>{round(removed / 60)} min</b>game time removed</div></div>
<div class="t"><table><thead><tr><th>Video time</th><th>Removed</th><th>Jolts</th><th>Status</th><th>Note</th></tr></thead><tbody>{body}</tbody></table></div>
</main></body></html>"""


def set_verdict(pid, key, status, note):
    v = load(pid, "verdicts.json", {})
    if status == "unchecked" and not note:
        v.pop(key, None)
    else:
        v[key] = {"status": status, "note": note}
    save(pid, "verdicts.json", v)
    return v
