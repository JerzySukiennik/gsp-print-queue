"""GSP Print Queue: queue runner and web panel for a Bambu Lab X1C on the LAN."""
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import eject
from printer import Printer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
FILES = os.path.join(DATA, "files")
PREP = os.path.join(DATA, "prepared")
QUEUE_FILE = os.path.join(DATA, "queue.json")
STATIC = os.path.join(ROOT, "static")

ACTIVE_STATES = ("RUNNING", "PREPARE", "PAUSE", "SLICING")
URL_BASES = ["file:///sdcard", "file:///mnt/sdcard"]


def load_config():
    path = os.path.join(ROOT, "config.json")
    if not os.path.exists(path):
        raise SystemExit("config.json missing, copy config.example.json and fill in the LAN access code")
    with open(path) as fh:
        return json.load(fh)


def _color_dist(a, b):
    try:
        a = a.lstrip("#")[:6]
        b = b.lstrip("#")[:6]
        return sum((int(a[i:i + 2], 16) - int(b[i:i + 2], 16)) ** 2 for i in (0, 2, 4))
    except Exception:
        return 10 ** 9


def auto_map(filaments, trays):
    mapping = []
    usable = [t for t in trays if not t.get("empty") and t["id"] != 254]
    for f in filaments:
        best = None
        best_d = None
        for t in usable:
            if f.get("type") and t.get("type") and f["type"].upper() != t["type"].upper():
                continue
            d = _color_dist(f.get("color", ""), t.get("color", ""))
            if best is None or d < best_d:
                best, best_d = t["id"], d
        if best is None:
            best = usable[0]["id"] if usable else 0
        mapping.append(best)
    return mapping


class Queue:
    def __init__(self, printer, cfg):
        self.printer = printer
        self.cfg = cfg
        self.jobs = []
        self.running = False
        self.current = None
        self.alerts = []
        self.lock = threading.RLock()
        self.busy = False
        self._sd_warned = 0
        self.load()

    def load(self):
        if os.path.exists(QUEUE_FILE):
            try:
                with open(QUEUE_FILE) as fh:
                    d = json.load(fh)
                self.jobs = d.get("jobs", [])
                self.running = False
                self.current = d.get("current")
                self.alerts = d.get("alerts", [])
            except Exception:
                self.jobs = []
        for j in self.jobs:
            if j["status"] in ("preparing", "uploading", "starting"):
                j["status"] = "queued"
        if self.current and not any(j["id"] == self.current and j["status"] == "printing" for j in self.jobs):
            self.current = None

    def save(self):
        with self.lock:
            tmp = QUEUE_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"jobs": self.jobs, "current": self.current, "alerts": self.alerts[-50:]}, fh, indent=1)
            os.replace(tmp, QUEUE_FILE)

    def alert(self, text, level="error"):
        with self.lock:
            self.alerts.append({"t": time.time(), "text": text, "level": level})
            self.alerts = self.alerts[-50:]
        print("[%s] %s" % (level, text), flush=True)

    def job(self, jid):
        for j in self.jobs:
            if j["id"] == jid:
                return j
        return None

    def add(self, filename, path):
        plates = eject.inspect(path)
        if not plates:
            raise ValueError("No sliced plate found in this 3MF. Export a sliced plate from Bambu Studio (File > Export > Export plate sliced file).")
        trays = self.printer.snapshot()["trays"]
        base = re.sub(r"\.gcode\.3mf$|\.3mf$", "", filename, flags=re.I)
        created = []
        with self.lock:
            for p in plates:
                jid = uuid.uuid4().hex[:8]
                dst = os.path.join(FILES, jid + ".3mf")
                shutil.copyfile(path, dst)
                name = base if len(plates) == 1 else "%s (plate %d)" % (base, p["index"])
                job = {
                    "id": jid,
                    "name": name,
                    "file": dst,
                    "plate": p["index"],
                    "gcode": p["gcode"],
                    "time_s": p["time_s"],
                    "max_z": p["max_z"],
                    "objects": p["objects"],
                    "filaments": p["filaments"],
                    "ams_map": auto_map(p["filaments"], trays),
                    "ams_auto": True,
                    "eject": True,
                    "eject_params": dict(eject.DEFAULT_PARAMS),
                    "bed_levelling": True,
                    "flow_cali": False,
                    "status": "queued",
                    "added": time.time(),
                    "started": None,
                    "finished": None,
                    "error": "",
                }
                self.jobs.append(job)
                created.append(job)
            self.save()
        return created

    def update(self, jid, patch):
        with self.lock:
            j = self.job(jid)
            if not j:
                return None
            for key in ("eject", "bed_levelling", "flow_cali"):
                if key in patch:
                    j[key] = bool(patch[key])
            if "name" in patch and str(patch["name"]).strip():
                j["name"] = str(patch["name"]).strip()[:80]
            if "ams_map" in patch and isinstance(patch["ams_map"], list):
                j["ams_map"] = [int(x) for x in patch["ams_map"]][:len(j["filaments"]) or 1]
                j["ams_auto"] = False
            if "eject_params" in patch and isinstance(patch["eject_params"], dict):
                params = dict(j.get("eject_params") or eject.DEFAULT_PARAMS)
                for key in eject.DEFAULT_PARAMS:
                    if key in patch["eject_params"]:
                        try:
                            params[key] = float(patch["eject_params"][key])
                        except (TypeError, ValueError):
                            pass
                j["eject_params"] = params
            self.save()
            return j

    def move(self, jid, direction):
        with self.lock:
            idx = next((i for i, j in enumerate(self.jobs) if j["id"] == jid), None)
            if idx is None:
                return
            other = idx - 1 if direction == "up" else idx + 1
            if 0 <= other < len(self.jobs) and self.jobs[other]["status"] == "queued" and self.jobs[idx]["status"] == "queued":
                self.jobs[idx], self.jobs[other] = self.jobs[other], self.jobs[idx]
                self.save()

    def remove(self, jid):
        with self.lock:
            j = self.job(jid)
            if not j or j["id"] == self.current:
                return False
            self.jobs = [x for x in self.jobs if x["id"] != jid]
            for path in (j.get("file"), os.path.join(PREP, jid + ".3mf")):
                if path and os.path.exists(path):
                    os.remove(path)
            self.save()
            return True

    def requeue(self, jid):
        with self.lock:
            j = self.job(jid)
            if not j or j["status"] in ("queued", "printing"):
                return None
            new = dict(j)
            new["id"] = uuid.uuid4().hex[:8]
            new["file"] = os.path.join(FILES, new["id"] + ".3mf")
            shutil.copyfile(j["file"], new["file"])
            new.update({"status": "queued", "added": time.time(), "started": None, "finished": None, "error": ""})
            self.jobs.append(new)
            self.save()
            return new

    def clear_done(self):
        with self.lock:
            for j in list(self.jobs):
                if j["status"] in ("done", "failed", "stopped"):
                    self.remove(j["id"])

    def set_running(self, flag):
        with self.lock:
            self.running = bool(flag)
            self.save()

    def tick(self):
        snap = self.printer.snapshot()
        state = snap["gcode_state"]
        with self.lock:
            if snap["trays"]:
                changed = False
                for j in self.jobs:
                    if j["status"] == "queued" and j.get("ams_auto"):
                        m = auto_map(j["filaments"], snap["trays"])
                        if m != j["ams_map"]:
                            j["ams_map"] = m
                            changed = True
                if changed:
                    self.save()
            cur = self.job(self.current) if self.current else None
            if cur:
                if cur["status"] == "printing":
                    if state == "FINISH":
                        self._finish(cur, "done")
                    elif state == "FAILED":
                        self._finish(cur, "failed", "Printer reported FAILED: %s" % ", ".join(snap["errors"]))
                        self.running = False
                    elif state == "IDLE" and time.time() - (cur.get("started") or 0) > 30:
                        self._finish(cur, "stopped", "Print stopped on the printer")
                        self.running = False
                return
            if not self.running or self.busy or not snap["online"]:
                return
            if not snap.get("sdcard"):
                if time.time() - self._sd_warned > 300:
                    self._sd_warned = time.time()
                    self.alert("No microSD card in the printer. Insert one, the queue starts by itself.")
                return
            if state in ACTIVE_STATES:
                return
            if state == "FAILED" and self.jobs and not any(j["status"] == "failed" for j in self.jobs):
                return
            nxt = next((j for j in self.jobs if j["status"] == "queued"), None)
            if not nxt:
                return
            self.busy = True
            threading.Thread(target=self._start_job, args=(nxt["id"],), daemon=True).start()

    def _finish(self, job, status, error=""):
        job["status"] = status
        job["finished"] = time.time()
        job["error"] = error
        self.current = None
        if error:
            self.alert("%s: %s" % (job["name"], error))
        else:
            self.alert("%s finished" % job["name"], "info")
        self.save()

    def _start_job(self, jid):
        try:
            j = self.job(jid)
            if not j:
                return
            j["status"] = "preparing"
            self.save()
            prepared = os.path.join(PREP, jid + ".3mf")
            params = j["eject_params"] if j.get("eject") else None
            eject.prepare(j["file"], prepared, j["plate"], params, j.get("objects"), j.get("max_z", 0))
            j["status"] = "uploading"
            self.save()
            remote = "gspq_%s.3mf" % jid
            self.printer.upload(prepared, remote)
            j["status"] = "starting"
            j["started"] = time.time()
            self.save()
            subtask = j["name"]
            bases = [self.cfg.get("url_base", URL_BASES[0])] + [b for b in URL_BASES if b != self.cfg.get("url_base")]
            started = False
            for base in bases:
                self.printer.start_print(remote, j["gcode"], subtask, j["ams_map"], True,
                                         j.get("bed_levelling", True), j.get("flow_cali", False), base)
                deadline = time.time() + 45
                while time.time() < deadline:
                    time.sleep(2)
                    snap = self.printer.snapshot()
                    if snap["gcode_state"] in ACTIVE_STATES and (snap["subtask_name"] == subtask or snap["gcode_file"].endswith(remote)):
                        started = True
                        break
                    if snap["gcode_state"] in ACTIVE_STATES and time.time() - j["started"] > 20:
                        started = True
                        break
                if started:
                    if base != self.cfg.get("url_base"):
                        self.cfg["url_base"] = base
                        self.alert("Printer accepted url base %s, remembered for this session" % base, "info")
                    break
            with self.lock:
                if started:
                    j["status"] = "printing"
                    self.current = jid
                    self.alert("%s started" % j["name"], "info")
                else:
                    j["status"] = "failed"
                    j["error"] = "Printer did not start the job (check screen, errors: %s)" % ", ".join(self.printer.snapshot()["errors"])
                    self.running = False
                    self.alert("%s: %s" % (j["name"], j["error"]))
                self.save()
        except Exception as e:
            traceback.print_exc()
            with self.lock:
                j = self.job(jid)
                if j:
                    j["status"] = "failed"
                    j["error"] = "%s: %s" % (type(e).__name__, e)
                self.running = False
                self.alert("%s: %s" % (j["name"] if j else jid, e))
                self.save()
        finally:
            self.busy = False

    def public(self):
        with self.lock:
            jobs = json.loads(json.dumps(self.jobs))
            eta = 0
            for j in jobs:
                if j["status"] in ("queued", "printing", "preparing", "uploading", "starting"):
                    eta += j.get("time_s", 0) + (int(j["eject_params"].get("cool_min", 0)) * 60 if j.get("eject") else 0)
            return {"running": self.running, "current": self.current, "jobs": jobs,
                    "alerts": self.alerts[-20:], "eta_s": eta, "busy": self.busy}


class Handler(BaseHTTPRequestHandler):
    queue = None
    printer = None
    cfg = None

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b""
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            path = os.path.join(STATIC, "index.html")
            with open(path, "rb") as fh:
                body = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/state":
            self._json({"printer": self.printer.snapshot(), "queue": self.queue.public(),
                        "config": {"ip": self.cfg["printer_ip"], "serial": self.cfg["serial"]}, "now": time.time()})
            return
        m = re.fullmatch(r"/api/job/([0-9a-f]+)/eject-preview", u.path)
        if m:
            j = self.queue.job(m.group(1))
            if not j:
                return self._json({"error": "not found"}, 404)
            text = eject.eject_gcode(j.get("objects") or [], j.get("max_z", 0), j.get("eject_params"))
            body = text.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":
                name = parse_qs(u.query).get("name", ["upload.3mf"])[0]
                name = os.path.basename(name)
                if not name.lower().endswith(".3mf"):
                    return self._json({"error": "Only .3mf files (sliced) are accepted"}, 400)
                n = int(self.headers.get("Content-Length") or 0)
                tmp = os.path.join(DATA, "upload_%s.tmp" % uuid.uuid4().hex[:6])
                with open(tmp, "wb") as fh:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(1 << 20, left))
                        if not chunk:
                            break
                        fh.write(chunk)
                        left -= len(chunk)
                try:
                    jobs = self.queue.add(name, tmp)
                finally:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                return self._json({"jobs": jobs})
            if u.path == "/api/queue/run":
                self.queue.set_running(self._body().get("running", False))
                return self._json({"ok": True, "running": self.queue.running})
            if u.path == "/api/queue/clear-done":
                self.queue.clear_done()
                return self._json({"ok": True})
            if u.path == "/api/alerts/clear":
                with self.queue.lock:
                    self.queue.alerts = []
                    self.queue.save()
                return self._json({"ok": True})
            if u.path == "/api/printer/pause":
                self.printer.pause()
                return self._json({"ok": True})
            if u.path == "/api/printer/resume":
                self.printer.resume()
                return self._json({"ok": True})
            if u.path == "/api/printer/stop":
                self.printer.stop()
                return self._json({"ok": True})
            if u.path == "/api/printer/refresh":
                self.printer.pushall()
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/job/([0-9a-f]+)/(move|update|delete|requeue)", u.path)
            if m:
                jid, action = m.group(1), m.group(2)
                body = self._body()
                if action == "move":
                    self.queue.move(jid, body.get("dir", "up"))
                    return self._json({"ok": True})
                if action == "update":
                    j = self.queue.update(jid, body)
                    return self._json({"ok": j is not None, "job": j})
                if action == "delete":
                    return self._json({"ok": self.queue.remove(jid)})
                if action == "requeue":
                    return self._json({"ok": True, "job": self.queue.requeue(jid)})
            return self._json({"error": "not found"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            traceback.print_exc()
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)


def main():
    cfg = load_config()
    for d in (DATA, FILES, PREP):
        os.makedirs(d, exist_ok=True)
    printer = Printer(cfg["printer_ip"], cfg["serial"], cfg["access_code"])
    printer.start()
    queue = Queue(printer, cfg)
    Handler.queue = queue
    Handler.printer = printer
    Handler.cfg = cfg

    def runner():
        last_push = 0
        while True:
            try:
                queue.tick()
                if time.time() - last_push > 60:
                    printer.pushall()
                    last_push = time.time()
            except Exception:
                traceback.print_exc()
            time.sleep(2)

    threading.Thread(target=runner, daemon=True).start()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(cfg.get("port", 8745))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print("GSP Print Queue on http://0.0.0.0:%d (printer %s)" % (port, cfg["printer_ip"]), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
