"""Checks run before every push and in the release build.
usage: python tools/check.py [--tag vX.Y.Z] [--no-build]"""
import os, re, subprocess, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# built from parts so this file does not match itself
SECRETS = re.compile("|".join([
    r"[A-Za-z]:\\" + r"\\?Users", r"/" + r"Users/", r"/" + r"home/",
    r"@(gmail|outlook|hotmail|yahoo|icloud|proton)\." + r"(com|me)",
    r"gh[pousr]_" + r"[A-Za-z0-9]{20,}", r"sk-" + r"[A-Za-z0-9_-]{20,}", r"AKIA" + r"[0-9A-Z]{16}",
    r"-----BEGIN [A-Z ]*" + r"PRIVATE KEY",
]))
UPSTREAM = ("app/tscli/src/rand.rs", "app/tscli/src/points.rs", "app/tscli/src/process.rs", "app/tscli/assets/")
fails = []


def git(*a):
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main():
    args = sys.argv[1:]
    files = git("ls-files").splitlines()
    for f in files:
        if f.startswith(UPSTREAM):
            fails.append(f"{f} is a Tick Studio file and must not be in the repo")
        if f == "tools/check.py" or f.endswith((".png", ".lock")):
            continue
        with open(os.path.join(ROOT, f), encoding="utf-8", errors="replace") as fh:
            for n, line in enumerate(fh, 1):
                if SECRETS.search(line):
                    fails.append(f"{f}:{n} looks like personal info, a key or a local path")
    for f in files:
        if f.endswith(".py"):
            try:
                with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                    compile(fh.read(), f, "exec")
            except SyntaxError as e:
                fails.append(f"{f}:{e.lineno} {e.msg}")
    version = open(os.path.join(ROOT, "VERSION")).read().strip()
    log = open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    m = re.search(r"^## (\S+) - (\d{4}-\d\d-\d\d)\n+(.*)", log, re.M)
    if not m or m.group(1) != version:
        fails.append(f"CHANGELOG.md has no entry for {version} at the top")
    elif not m.group(3).startswith("Why update:"):
        fails.append(f"The {version} changelog entry must start with a 'Why update:' line")
    if "--tag" in args and args[args.index("--tag") + 1] != f"v{version}":
        fails.append(f"Tag {args[args.index('--tag') + 1]} does not match VERSION {version}")
    exe = os.path.join(ROOT, "app", "tscli", "target", "release", "tscli.exe")
    if "--no-build" not in args:
        if subprocess.run(["cargo", "build", "--release", "-q"], cwd=os.path.join(ROOT, "app", "tscli")).returncode:
            fails.append("tscli does not build")
        elif subprocess.run([exe, "selftest"]).returncode:
            fails.append("tscli failed its self-test")
    for f in fails:
        print("FAIL", f)
    print("checks", "failed" if fails else "passed")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
