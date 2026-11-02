"""Jolt Scan local app: serves the page and runs the pipeline. Listens on 127.0.0.1 only."""
import json, mimetypes, os, re, sys, threading, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

import pipeline as pl
import updater

scan_running = lambda: any(j.get("running") for j in pl.jobs.values())

PORT = 8765
PAGE = os.path.join(pl.APP, "gui.html")
ICONS = {"full.png": "food.png", "half.png": "food_half.png", "empty.png": "food_empty.png"}  # the scanner's own hunger textures


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        # blocks other sites from reaching the app through DNS rebinding or cross-site posts
        host = (self.headers.get("Host") or "").split(":")[0]
        origin = self.headers.get("Origin")
        return host in ("127.0.0.1", "localhost") and (origin is None or re.match(r"^http://(127\.0\.0\.1|localhost):\d+$", origin))

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, msg, code=400):
        self._json({"error": msg}, code)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._ok_host():
            return self._err("forbidden", 403)
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        try:
            if not parts:
                with open(PAGE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(body)
            if len(parts) == 2 and parts[0] == "icons" and parts[1] in ICONS:
                with open(os.path.join(pl.APP, "tscli", "assets", ICONS[parts[1]]), "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if parts == ["api", "projects"]:
                return self._json(pl.list_projects())
            if len(parts) >= 3 and parts[:2] == ["api", "p"]:
                pid = parts[2]
                pl.pdir(pid)
                if len(parts) == 3:
                    meta = pl.load(pid, "meta.json")
                    if not meta:
                        return self._err("No such project.", 404)
                    return self._json({"id": pid, "meta": meta, "result": pl.load(pid, "result.json"),
                                       "verdicts": pl.load(pid, "verdicts.json", {}), "job": pl.jobs.get(pid)})
                if parts[3] == "video":
                    return self._video(pid)
                if parts[3] in ("export.csv", "export.html"):
                    csv_ = parts[3].endswith("csv")
                    body = (pl.export_csv(pid) if csv_ else pl.export_html(pid)).encode("utf-8")
                    name = re.sub(r"[^\w-]+", "-", (pl.load(pid, "meta.json") or {}).get("title", pid))[:60].strip("-") or pid
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8" if csv_ else "text/html; charset=utf-8")
                    self.send_header("Content-Disposition", f'attachment; filename="jolt-scan-{name}.{"csv" if csv_ else "html"}"')
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    return self.wfile.write(body)
                if parts[3] == "hudtrack":
                    return self._json(pl.load(pid, "hudtrack.json", []))
            if parts == ["api", "jobs"]:
                return self._json(pl.jobs)
            if parts == ["api", "update"]:
                return self._json(dict(updater.status, installed=updater.load_state()))
            self._err("Not found.", 404)
        except (ValueError, KeyError) as e:
            self._err(str(e))
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def _video(self, pid):
        path = pl.video_path(pid)
        if not path:
            return self._err("This project has no video yet.", 404)
        size = os.path.getsize(path)
        a, b = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
        if m:
            if m.group(1):
                a = int(m.group(1))
                b = int(m.group(2)) if m.group(2) else size - 1
            else:
                a = size - int(m.group(2))
            b = min(b, size - 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", mimetypes.guess_type(path)[0] or "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        if m:
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(a)
            left = b - a + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_POST(self):
        if not self._ok_host():
            return self._err("forbidden", 403)
        u = urlparse(self.path)
        parts = [unquote(p) for p in u.path.strip("/").split("/") if p]
        try:
            if parts == ["api", "new"]:
                b = self._body()
                return self._json({"id": pl.create_from_url(b.get("url", ""), int(b.get("height", 2160)))})
            if parts == ["api", "update", "install"]:
                updater.install(scan_running)
                return self._json({"ok": True})
            if parts == ["api", "update", "check"]:
                updater.check_in_background()
                return self._json({"ok": True})
            if parts == ["api", "upload"]:
                name = parse_qs(u.query).get("name", ["video.mp4"])[0]
                return self._json({"id": pl.create_from_upload(name, self.rfile, int(self.headers["Content-Length"]))})
            if len(parts) == 4 and parts[:2] == ["api", "p"]:
                pid, act = parts[2], parts[3]
                pl.pdir(pid)
                if pl.jobs.get(pid, {}).get("running") and act != "verdict":
                    return self._err("This project is still working. Wait for the current step to finish.", 409)
                b = self._body()
                if act == "analyze":
                    pl.start_analyze(pid, b["trim"], b.get("hud"))
                    return self._json({"ok": True})
                if act == "findhud":
                    pl.run_bg(pid, lambda p: (pl.find_hud(p), pl.check_hud(p), pl.set_job(p, running=False, step="HUD found", pct=100)))
                    return self._json({"ok": True})
                if act == "checkhud":
                    if b.get("hud"):
                        meta = pl.load(pid, "meta.json")
                        meta["hud"] = dict(meta.get("hud", {}), **{k: float(b["hud"][k]) for k in ("s", "cx", "by")}, confident=True)
                        pl.save(pid, "meta.json", meta)
                    pl.run_bg(pid, lambda p: (pl.check_hud(p), pl.set_job(p, running=False, step="HUD checked", pct=100)))
                    return self._json({"ok": True})
                if act == "delete":
                    pl.delete_project(pid)
                    return self._json({"ok": True})
                if act == "deletevideo":
                    pl.delete_video(pid)
                    return self._json({"ok": True})
                if act == "redownload":
                    pl.redownload(pid)
                    return self._json({"ok": True})
                if act == "verdict":
                    return self._json(pl.set_verdict(pid, str(b["key"]), b["status"], b.get("note", "")))
            self._err("Not found.", 404)
        except (ValueError, KeyError, RuntimeError) as e:
            self._err(str(e))


def main():
    port = PORT
    while True:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            port += 1
            if port > PORT + 20:
                sys.exit("No free port for Jolt Scan.")
    updater.check_in_background()  # quiet when offline; the page asks before installing
    url = f"http://127.0.0.1:{port}/"
    print(f"Jolt Scan is running at {url}  (close this window to stop it)")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
