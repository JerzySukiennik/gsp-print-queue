"""GSP Print Queue: sliced 3MF inspection and auto-eject G-code injection."""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
import zipfile

EXEC_END = "; EXECUTABLE_BLOCK_END"
BED_MAX = 256.0
Y_FRONT = -3.0
Y_BACK_MAX = 258.0
Z_MAX = 250.0
PARK_Z = 150.0
PARK_GCODE = ["G90", "G1 Z150 F600", "G1 X128 Y250 F6000", "M400"]

DEFAULT_PARAMS = {
    "cool_min": 12,
    "push_z": 3.0,
    "push_speed": 100,
    "shroud_w": 40.0,
    "clearance": 20.0,
}


def _parse_time(text):
    total = 0
    for value, unit in re.findall(r"(\d+)\s*([hms])", text):
        total += int(value) * {"h": 3600, "m": 60, "s": 1}[unit]
    return total


def inspect(path):
    plates = []
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        predictions = {}
        filaments = {}
        if "Metadata/slice_info.config" in names:
            try:
                root = ET.fromstring(z.read("Metadata/slice_info.config"))
                for plate in root.findall("plate"):
                    idx = None
                    pred = 0
                    fils = []
                    for m in plate.findall("metadata"):
                        if m.get("key") == "index":
                            idx = int(m.get("value"))
                        if m.get("key") == "prediction":
                            pred = int(float(m.get("value") or 0))
                    for f in plate.findall("filament"):
                        fils.append({
                            "slot": int(f.get("id", "1")),
                            "type": f.get("type", ""),
                            "color": f.get("color", ""),
                            "used_g": f.get("used_g", ""),
                            "used_m": f.get("used_m", ""),
                        })
                    if idx is not None:
                        predictions[idx] = pred
                        filaments[idx] = fils
            except ET.ParseError:
                pass
        for name in sorted(names):
            m = re.fullmatch(r"Metadata/plate_(\d+)\.gcode", name)
            if not m:
                continue
            idx = int(m.group(1))
            head = z.read(name)[:4000].decode("utf-8", "ignore")
            max_z = 0.0
            est = 0
            mz = re.search(r"; max_z_height:\s*([\d.]+)", head)
            if mz:
                max_z = float(mz.group(1))
            mt = re.search(r"total estimated time:\s*([^\n;]+)", head)
            if mt:
                est = _parse_time(mt.group(1))
            objects = []
            jname = "Metadata/plate_%d.json" % idx
            if jname in names:
                try:
                    pj = json.loads(z.read(jname))
                    for o in pj.get("bbox_objects", []):
                        objects.append({"name": o.get("name", ""), "bbox": o.get("bbox", [])})
                except Exception:
                    pass
            plates.append({
                "index": idx,
                "gcode": name,
                "max_z": max_z,
                "time_s": predictions.get(idx) or est,
                "filaments": filaments.get(idx, []),
                "objects": objects,
            })
    return plates


def _passes(objects, shroud_w):
    passes = []
    for o in sorted(objects, key=lambda o: o["bbox"][3] if len(o["bbox"]) == 4 else 0):
        if len(o["bbox"]) != 4:
            continue
        x0, y0, x1, y1 = o["bbox"]
        y_start = min(y1 + 15.0, Y_BACK_MAX)
        width = x1 - x0
        if width <= shroud_w * 0.8:
            xs = [(x0 + x1) / 2.0]
        else:
            step = shroud_w * 0.8
            xs = []
            x = x0 + shroud_w / 2.0
            while x < x1 - shroud_w / 2.0:
                xs.append(x)
                x += step
            xs.append(x1 - shroud_w / 2.0)
        for x in xs:
            passes.append((max(0.0, min(BED_MAX, x)), y_start, o["name"]))
    return passes


def eject_gcode(objects, max_z, params=None):
    p = dict(DEFAULT_PARAMS)
    p.update(params or {})
    safe_z = min(Z_MAX, max(max_z + float(p["clearance"]), 30.0))
    push_z = max(1.0, float(p["push_z"]))
    feed = int(float(p["push_speed"]) * 60)
    cool_s = int(float(p["cool_min"]) * 60)
    lines = [
        "",
        "; ===== GSP Print Queue: auto eject =====",
        "M400",
        "M17 X1.2 Y1.2 Z0.75 ; restore motor current",
        "G28 X ; home XY",
        "M17 X0.8 Y0.8 ; limit push force, motors slip before anything breaks",
        "M140 S0",
        "M104 S0",
        "M106 P2 S255 ; aux fan",
        "M106 P3 S255 ; chamber fan",
        "G90",
        "G1 Z%.1f F600" % safe_z,
        "G1 X128 Y250 F12000",
        "; cool down %d s" % cool_s,
    ]
    remaining = cool_s
    while remaining > 0:
        chunk = min(60, remaining)
        lines.append("M400 S%d" % chunk)
        remaining -= chunk
    lines.append("M106 P2 S0")
    lines.append("M106 P3 S0")
    passes = _passes(objects, float(p["shroud_w"]))
    if not passes:
        passes = [(x, Y_BACK_MAX, "sweep") for x in (20, 52, 84, 116, 148, 180, 212, 244)]
    for x, y_start, name in passes:
        lines.append("; push %s" % name)
        lines.append("G1 X%.1f Y%.1f F12000" % (x, y_start))
        lines.append("G1 Z%.2f F600" % push_z)
        lines.append("G1 Y%.1f F%d" % (Y_FRONT, feed))
        lines.append("M400")
        lines.append("G1 Z%.1f F600" % safe_z)
    lines += [
        "G1 Z%.1f F600 ; park height for the plate check" % max(PARK_Z, safe_z),
        "M17 X1.2 Y1.2 Z0.75",
        "G28 X ; re-home XY",
        "G1 X128 Y250 F3600",
        "M17 X0.8 Y0.8 Z0.5",
        "M400",
        "; ===== end auto eject =====",
        "",
    ]
    return "\n".join(lines)


LOAD_MARK = ";===== nozzle load line"
PURGE_MARK = ";===== purge line to wipe the nozzle"
LASER_MARK = ";=========== laser and rgb calibration"
START_END = "; MACHINE_START_GCODE_END"


def _block(text, start_mark, end_pred):
    base = text.find("; EXECUTABLE_BLOCK_START")
    i = text.find(start_mark, base if base >= 0 else 0)
    if i < 0:
        return None
    j = text.find("\n", i) + 1
    lines = text[j:].split("\n")
    k = 0
    for k, line in enumerate(lines):
        if end_pred(line):
            break
    else:
        return None
    end = j + sum(len(l) + 1 for l in lines[:k])
    return i, end


def clean_plate(text):
    """Move the two priming lines from the plate into the waste chute, leaving the plate empty."""
    changed = False
    span = _block(text, LOAD_MARK, lambda l: l.startswith(";====="))
    if span:
        body = text[span[0]:span[1]]
        temp = re.search(r"^M109 S\S+", body, re.M)
        repl = [
            ";===== nozzle load line (GSP Print Queue: primed into the chute, nothing on the plate) =====",
            "M975 S1", "G90", "M83", "T1000",
            "G1 X65 Y265 Z0.8 F18000 ; chute",
            temp.group(0) if temp else "M109 S220",
            "G1 Z0.2",
            "G1 E2 F300",
            "G1 E15 F300",
            "G1 X100 F12000 ; wipe",
            "G1 X65 F12000",
            "M400", "", "",
        ]
        text = text[:span[0]] + "\n".join(repl) + text[span[1]:]
        changed = True
    span = _block(text, PURGE_MARK, lambda l: l.startswith(START_END))
    if span:
        body = text[span[0]:span[1]]
        temp = re.search(r"^M109 S\S+", body, re.M)
        retr = re.search(r"^G1 E-(\S+) F(\S+)", body, re.M)
        rl = retr.group(1) if retr else "0.8"
        rf = retr.group(2) if retr else "1800"
        repl = [
            ";===== purge line (GSP Print Queue: primed into the chute, nothing on the plate) =====",
            "G1 E-%s F%s" % (rl, rf),
            "G1 X65 Y265 Z0.8 F18000 ; chute",
            "G1 E%s F%s" % (rl, rf),
            temp.group(0) if temp else "M109 S220",
            "G1 Z0.2",
            "G1 E6 F300",
            "G1 X100 F12000 ; wipe",
            "G1 X65 F12000",
            "M400",
        ]
        text = text[:span[0]] + "\n".join(repl) + "\n" + text[span[1]:]
        changed = True
    span = _block(text, LASER_MARK, lambda l: l.startswith(";====="))
    if span:
        repl = [
            ";=========== laser and rgb calibration (GSP Print Queue: skipped, no line on the plate to calibrate on) =====",
            "M400", "M18 E", "M500 R", "", "",
        ]
        text = text[:span[0]] + "\n".join(repl) + text[span[1]:]
        changed = True
    return text, changed


def prepare(src, dst, plate_index=1, eject_params=None, objects=None, max_z=0.0, clean=False):
    gcode_name = "Metadata/plate_%d.gcode" % plate_index
    md5_name = gcode_name + ".md5"
    modify = eject_params is not None or clean
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if modify and item.filename == gcode_name:
                text = data.decode("utf-8", "ignore")
                if clean:
                    text, _ = clean_plate(text)
                if eject_params is not None:
                    block = eject_gcode(objects or [], max_z, eject_params)
                    pos = text.rfind(EXEC_END)
                    if pos >= 0:
                        text = text[:pos] + block + text[pos:]
                    else:
                        text = text + block
                data = text.encode("utf-8")
                zout.writestr(item, data)
                continue
            if modify and item.filename == md5_name:
                continue
            zout.writestr(item, data)
    if modify:
        with zipfile.ZipFile(dst) as z:
            digest = hashlib.md5(z.read(gcode_name)).hexdigest().upper()
        with zipfile.ZipFile(dst, "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr(md5_name, digest)
    return dst
