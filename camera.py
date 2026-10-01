"""GSP Print Queue: chamber camera snapshots and empty-plate comparison via ffmpeg."""
import os
import shutil
import subprocess

ROOT = os.path.dirname(os.path.abspath(__file__))
GRAY_W = 146
GRAY_H = 90


def ffmpeg_path():
    local = os.path.join(ROOT, "bin", "ffmpeg")
    if os.path.exists(local):
        return local
    return shutil.which("ffmpeg")


def available():
    return ffmpeg_path() is not None


def stream_url(ip, access_code):
    return "rtsps://bblp:%s@%s:322/streaming/live/1" % (access_code, ip)


def grab_jpeg(url, path, timeout=45):
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
           "-i", url, "-frames:v", "1", "-q:v", "3", "-y", path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not os.path.exists(path):
        raise RuntimeError("camera grab failed: %s" % r.stderr.strip()[-200:])
    return path


def jpeg_to_gray(path):
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", path,
           "-vf", "scale=%d:%d,format=gray" % (GRAY_W, GRAY_H), "-f", "rawvideo", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=30)
    if r.returncode != 0 or len(r.stdout) != GRAY_W * GRAY_H:
        raise RuntimeError("gray conversion failed")
    return r.stdout


def compare(ref, cur, region):
    x0 = int(region[0] * GRAY_W)
    y0 = int(region[1] * GRAY_H)
    x1 = int(region[2] * GRAY_W)
    y1 = int(region[3] * GRAY_H)
    pix_r = []
    pix_c = []
    for y in range(y0, y1):
        row = y * GRAY_W
        pix_r.extend(ref[row + x0:row + x1])
        pix_c.extend(cur[row + x0:row + x1])
    n = len(pix_r)
    if n == 0:
        return {"mean": 0.0, "changed": 0.0}
    mr = sum(pix_r) / n
    mc = sum(pix_c) / n
    shift = mr - mc
    total = 0
    changed = 0
    for a, b in zip(pix_r, pix_c):
        d = abs(a - (b + shift))
        total += d
        if d > 40:
            changed += 1
    return {"mean": round(total / n, 2), "changed": round(changed / n, 4), "brightness_shift": round(shift, 1)}
