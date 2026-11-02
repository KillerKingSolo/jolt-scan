"""Keeps tscli's copies of the Tick Studio files in step with upstream on GitLab.
Checking never blocks the app and fails quietly when offline. Installing builds and tests
a staged copy first, so a broken upstream change never replaces the working scanner."""
import json, os, shutil, subprocess, threading, time, urllib.parse, urllib.request

APP = os.path.dirname(os.path.abspath(__file__))
TSCLI_DIR = os.path.join(APP, "tscli")
STAGE = os.path.join(APP, "tscli_update")
STATE = os.path.join(TSCLI_DIR, "upstream.json")
API = "https://gitlab.com/api/v4/projects/" + urllib.parse.quote("fanda8571/tick_studio", safe="")
REPO = "https://gitlab.com/fanda8571/tick_studio"
# upstream path -> our path inside tscli
TRACKED = {
    "src/cracker/rand.rs": "src/rand.rs",
    "src/scanner/points.rs": "src/points.rs",
    "src/scanner/process.rs": "src/process.rs",
    "assets/food.png": "assets/food.png",
    "assets/food_half.png": "assets/food_half.png",
    "assets/food_empty.png": "assets/food_empty.png",
}
CARGO = os.path.join(os.path.expanduser("~"), ".cargo", "bin", "cargo.exe")

status = {"state": "idle"}   # what the app shows; see check() and install()
lock = threading.Lock()


def _get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "JoltScan-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def patch(up, data):
    """Fit an upstream file to tscli."""
    if not up.endswith(".rs"):
        return data
    # upstream keeps Jolt and Offset in crate::common; tscli defines them at the crate root
    t = data.decode("utf-8").replace("crate::common::", "crate::")
    # Java int overflow in nextInt's rejection loop: wrap on purpose so a draw near 2^31 is rerolled
    t = t.replace("if bits - val + (n - 1) >= 0 {", "if bits.wrapping_sub(val).wrapping_add(n - 1) >= 0 {")
    return t.encode("utf-8")


def _set(**kw):
    with lock:
        status.clear()
        status.update(kw, t=time.time())


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def check():
    """Compare upstream's tracked files with the ones tscli was built from."""
    _set(state="checking")
    try:
        commit = json.loads(_get(f"{API}/repository/commits?per_page=1"))[0]
        tree = json.loads(_get(f"{API}/repository/tree?recursive=true&per_page=100&ref={commit['id']}"))
    except Exception:
        _set(state="offline")
        return status
    blobs = {e["path"]: e["id"] for e in tree if e["type"] == "blob"}
    missing = [p for p in TRACKED if p not in blobs]
    if missing:
        _set(state="incompatible", commit=commit["short_id"], title=commit["title"], date=commit["committed_date"],
             message="Tick Studio moved or renamed files the scanner uses: " + ", ".join(missing))
        return status
    have = load_state().get("blobs", {})
    changed = [p for p in TRACKED if blobs[p] != have.get(p)]
    _set(state="available" if changed else "current", commit=commit["short_id"], commit_id=commit["id"],
         title=commit["title"], date=commit["committed_date"], changed=changed, blobs={p: blobs[p] for p in TRACKED},
         url=f"{REPO}/-/commit/{commit['id']}")
    return status


def install(scan_running):
    """Download, patch, build and self-test in a staging folder, then swap it in."""
    s = dict(status)
    if s.get("state") != "available":
        raise ValueError("There is no scanner update to install.")
    if scan_running():
        raise ValueError("A scan is running. Install the update when it is done.")
    if not os.path.exists(CARGO):
        raise ValueError("Rust is not installed, so the scanner cannot be rebuilt.")
    threading.Thread(target=_install, args=(s, scan_running), daemon=True).start()


def _install(s, scan_running):
    try:
        _set(**dict(s, state="installing", step="Downloading the changed files"))
        shutil.rmtree(STAGE, ignore_errors=True)
        os.makedirs(STAGE)
        for name in ("src", "assets"):
            shutil.copytree(os.path.join(TSCLI_DIR, name), os.path.join(STAGE, name))
        for name in ("Cargo.toml", "Cargo.lock"):
            shutil.copy2(os.path.join(TSCLI_DIR, name), STAGE)
        for up in s["changed"]:
            data = _get(f"{API}/repository/files/{urllib.parse.quote(up, safe='')}/raw?ref={s['commit_id']}", timeout=30)
            with open(os.path.join(STAGE, TRACKED[up]), "wb") as f:
                f.write(patch(up, data))
        _set(**dict(s, state="installing", step="Building the new scanner"))
        env = dict(os.environ, CARGO_TARGET_DIR=os.path.join(TSCLI_DIR, "target-update"))
        b = subprocess.run([CARGO, "build", "--release"], cwd=STAGE, env=env, capture_output=True, text=True)
        exe = os.path.join(env["CARGO_TARGET_DIR"], "release", "tscli.exe")
        if b.returncode:
            err = [l for l in b.stderr.splitlines() if l.startswith("error")]
            raise RuntimeError("The new Tick Studio files do not build with the scanner yet (" + (err[0] if err else "build error") + "). Your current scanner is unchanged.")
        _set(**dict(s, state="installing", step="Testing the new scanner"))
        t = subprocess.run([exe, "selftest"], capture_output=True, text=True)
        if t.returncode:
            raise RuntimeError("The new scanner failed its self-test (" + t.stdout.strip() + "). Your current scanner is unchanged.")
        if scan_running():
            raise RuntimeError("A scan started during the update. Try again when it is done.")
        for up in s["changed"]:
            shutil.copy2(os.path.join(STAGE, TRACKED[up]), os.path.join(TSCLI_DIR, TRACKED[up]))
        shutil.copy2(exe, os.path.join(TSCLI_DIR, "target", "release", "tscli.exe"))
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump({"commit": s["commit_id"], "title": s["title"], "date": s["date"], "blobs": s["blobs"], "installed": time.time()}, f, indent=1)
        shutil.rmtree(STAGE, ignore_errors=True)
        _set(**dict(s, state="installed", step="", message="The scanner is updated. New scans use it; nothing needs a restart."))
    except Exception as e:
        shutil.rmtree(STAGE, ignore_errors=True)
        _set(**dict(s, state="failed", message=str(e)))


def check_in_background():
    threading.Thread(target=check, daemon=True).start()
