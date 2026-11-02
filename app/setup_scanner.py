"""Downloads the Tick Studio files tscli needs (pinned in tscli/pin.json), checks them, then builds and tests tscli.
These files are not in the repo: Tick Studio has no license and the hunger icons are Minecraft textures."""
import hashlib, json, os, shutil, subprocess, sys, time, urllib.parse

from updater import API, CARGO, STATE, TRACKED, TSCLI_DIR, _get, patch


def main():
    with open(os.path.join(TSCLI_DIR, "pin.json"), encoding="utf-8") as f:
        pin = json.load(f)
    print(f"Downloading Tick Studio files at {pin['commit'][:8]}")
    for up, ours in TRACKED.items():
        data = _get(f"{API}/repository/files/{urllib.parse.quote(up, safe='')}/raw?ref={pin['commit']}", timeout=30)
        # git blob id, so a changed or swapped file is refused
        if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != pin["blobs"][up]:
            sys.exit(f"{up} does not match the pinned version. Nothing was built.")
        os.makedirs(os.path.dirname(os.path.join(TSCLI_DIR, ours)), exist_ok=True)
        with open(os.path.join(TSCLI_DIR, ours), "wb") as f:
            f.write(patch(up, data))
    if not os.path.exists(STATE):
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(dict(pin, installed=time.time()), f, indent=1)
    cargo = shutil.which("cargo") or CARGO
    if not os.path.exists(cargo):
        sys.exit("Rust is not installed. Install it with: winget install Rustlang.Rustup")
    print("Building the scanner")
    if subprocess.run([cargo, "build", "--release"], cwd=TSCLI_DIR).returncode:
        sys.exit("The scanner did not build.")
    t = subprocess.run([os.path.join(TSCLI_DIR, "target", "release", "tscli.exe"), "selftest"])
    if t.returncode:
        sys.exit("The scanner failed its self-test.")
    print("Scanner ready")


if __name__ == "__main__":
    main()
