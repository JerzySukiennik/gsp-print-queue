"""GSP Print Queue: Bambu Lab X1C LAN client (MQTT control + FTPS upload)."""
import ftplib
import json
import ssl
import threading
import time

import paho.mqtt.client as mqtt


class ImplicitFTPS(ftplib.FTP_TLS):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        return self._sock

    @sock.setter
    def sock(self, value):
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd, rest=None):
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size


def _ftps_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class Printer:
    def __init__(self, ip, serial, access_code):
        self.ip = ip
        self.serial = serial
        self.access_code = access_code
        self.state = {}
        self.version = {}
        self.connected = False
        self.last_report = 0.0
        self.last_error = ""
        self._seq = 0
        self._lock = threading.Lock()
        self._client = None

    def start(self):
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="gsp-print-queue", protocol=mqtt.MQTTv311)
        c.username_pw_set("bblp", self.access_code)
        c.tls_set(cert_reqs=ssl.CERT_NONE)
        c.tls_insecure_set(True)
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        c.on_connect_fail = self._on_connect_fail
        c.reconnect_delay_set(2, 30)
        self._client = c
        c.connect_async(self.ip, 8883, keepalive=30)
        c.loop_start()

    def _on_connect(self, client, userdata, flags, rc, props=None):
        self.connected = (rc == 0)
        if rc == 0:
            client.subscribe("device/%s/report" % self.serial)
            self.pushall()
            self.get_version()
        else:
            self.last_error = "MQTT connect rc=%s" % rc

    def _on_disconnect(self, client, userdata, flags, rc, props=None):
        self.connected = False

    def _on_connect_fail(self, client, userdata):
        self.last_error = "MQTT connect failed (%s)" % time.strftime("%H:%M:%S")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload)
        except Exception:
            return
        with self._lock:
            if "print" in data and isinstance(data["print"], dict):
                self.state.update(data["print"])
                self.last_report = time.time()
            if "info" in data and isinstance(data["info"], dict):
                for mod in data["info"].get("module", []):
                    self.version[mod.get("name", "")] = mod.get("sw_ver", "")

    def _next_seq(self):
        self._seq += 1
        return str(self._seq)

    def publish(self, payload, qos=1):
        if not self._client:
            return
        self._client.publish("device/%s/request" % self.serial, json.dumps(payload), qos=qos)

    def pushall(self):
        self.publish({"pushing": {"sequence_id": self._next_seq(), "command": "pushall"}})

    def get_version(self):
        self.publish({"info": {"sequence_id": self._next_seq(), "command": "get_version"}})

    def pause(self):
        self.publish({"print": {"sequence_id": self._next_seq(), "command": "pause", "param": ""}})

    def resume(self):
        self.publish({"print": {"sequence_id": self._next_seq(), "command": "resume", "param": ""}})

    def stop(self):
        self.publish({"print": {"sequence_id": self._next_seq(), "command": "stop", "param": ""}})

    def light(self, on):
        self.publish({"system": {"sequence_id": self._next_seq(), "command": "ledctrl", "led_node": "chamber_light",
                                 "led_mode": "on" if on else "off", "led_on_time": 500, "led_off_time": 500,
                                 "loop_times": 0, "interval_time": 0}})

    def gcode(self, lines):
        self.publish({"print": {"sequence_id": self._next_seq(), "command": "gcode_line", "param": "\n".join(lines) + "\n"}})

    def start_print(self, remote_name, plate_gcode, subtask_name, ams_mapping, use_ams=True,
                    bed_levelling=True, flow_cali=False, url_base="file:///sdcard", layer_inspect=True):
        payload = {"print": {
            "sequence_id": self._next_seq(),
            "command": "project_file",
            "param": plate_gcode,
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": subtask_name,
            "file": "",
            "url": "%s/cache/%s" % (url_base, remote_name),
            "md5": "",
            "timelapse": False,
            "bed_type": "auto",
            "bed_levelling": bool(bed_levelling),
            "flow_cali": bool(flow_cali),
            "vibration_cali": False,
            "layer_inspect": bool(layer_inspect),
            "ams_mapping": list(ams_mapping) if use_ams else [],
            "use_ams": bool(use_ams),
        }}
        self.publish(payload)

    def _ftps(self):
        f = ImplicitFTPS(context=_ftps_context(), timeout=30)
        f.connect(self.ip, 990)
        f.login("bblp", self.access_code)
        f.prot_p()
        return f

    def upload(self, local_path, remote_name):
        f = self._ftps()
        try:
            try:
                f.mkd("cache")
            except ftplib.error_perm:
                pass
            f.cwd("cache")
            with open(local_path, "rb") as fh:
                f.storbinary("STOR " + remote_name, fh, blocksize=65536)
            size = f.size(remote_name)
        finally:
            try:
                f.quit()
            except Exception:
                pass
        return size

    def delete_remote(self, remote_name):
        f = self._ftps()
        try:
            f.cwd("cache")
            f.delete(remote_name)
        except Exception:
            pass
        finally:
            try:
                f.quit()
            except Exception:
                pass

    def snapshot(self):
        with self._lock:
            s = dict(self.state)
            ver = dict(self.version)
        trays = []
        ams = s.get("ams", {})
        for unit in ams.get("ams", []) if isinstance(ams, dict) else []:
            base = int(unit.get("id", 0)) * 4
            for tray in unit.get("tray", []):
                tid = base + int(tray.get("id", 0))
                ttype = tray.get("tray_type") or ""
                trays.append({
                    "id": tid,
                    "type": ttype,
                    "color": ("#" + tray.get("tray_color", "")[:6]) if tray.get("tray_color") else "",
                    "name": tray.get("tray_sub_brands") or ttype,
                    "remain": tray.get("remain", -1),
                    "empty": ttype == "",
                })
        vt = s.get("vt_tray") or {}
        if vt.get("tray_type"):
            trays.append({"id": 254, "type": vt.get("tray_type", ""),
                          "color": ("#" + vt.get("tray_color", "")[:6]) if vt.get("tray_color") else "",
                          "name": vt.get("tray_sub_brands") or vt.get("tray_type", ""), "remain": -1, "empty": False})
        errors = []
        for h in s.get("hms", []) or []:
            errors.append("HMS %08X %08X" % (int(h.get("attr", 0)), int(h.get("code", 0))))
        pe = s.get("print_error", 0)
        if pe:
            errors.append("print_error %s" % pe)
        online = self.connected and (time.time() - self.last_report) < 90
        return {
            "online": online,
            "connected": self.connected,
            "last_report_age": round(time.time() - self.last_report) if self.last_report else None,
            "firmware": ver.get("ota", ""),
            "gcode_state": s.get("gcode_state", ""),
            "stage": s.get("mc_print_stage", ""),
            "stg_cur": s.get("stg_cur", -1),
            "percent": s.get("mc_percent", 0),
            "remaining_min": s.get("mc_remaining_time", 0),
            "layer": s.get("layer_num", 0),
            "total_layers": s.get("total_layer_num", 0),
            "subtask_name": s.get("subtask_name", ""),
            "gcode_file": s.get("gcode_file", ""),
            "nozzle": s.get("nozzle_temper", 0),
            "nozzle_target": s.get("nozzle_target_temper", 0),
            "bed": s.get("bed_temper", 0),
            "bed_target": s.get("bed_target_temper", 0),
            "chamber": s.get("chamber_temper", 0),
            "wifi": s.get("wifi_signal", ""),
            "sdcard": bool(s.get("sdcard", False)),
            "light": any(l.get("node") == "chamber_light" and l.get("mode") == "on" for l in (s.get("lights_report") or [])),
            "trays": trays,
            "errors": errors,
            "last_error": self.last_error,
        }
