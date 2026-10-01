# GSP Print Queue

Self-hosted print queue for a single Bambu Lab X1C on the local network. No cloud, no
subscription, no extra hardware. Drop sliced `.3mf` files into a web panel, press
"Start queue", and the printer runs them one after another. Each job can end with an
auto-eject routine: the bed cools, then the toolhead pushes the finished parts off the
front of the plate.

## How it works

- `printer.py` talks to the printer over the LAN: MQTT on port 8883 for status and
  commands, FTPS on port 990 to upload files to the printer's microSD card.
- `eject.py` reads a sliced 3MF (plates, objects, filaments, print time) and injects the
  auto-eject G-code at the end of the plate's G-code, updating the embedded MD5.
- `server.py` runs the queue and serves the panel (`static/index.html`) on port 8745.

Printer requirements: microSD card inserted, LAN access code (Settings > Network on the
printer screen). Firmware up to 01.08.00.00 accepts LAN commands as is; newer firmware
needs LAN-only mode with Developer Mode enabled.

## Auto eject

The injected block restores motor current, homes XY, turns the bed and hotend off, runs
the aux and chamber fans, waits (default 12 min) for the plate to cool so PEI releases
the parts, then for every object on the plate moves behind it, lowers the nozzle to the
push height (default 3 mm) and drives forward to Y=-3 at the push speed (default
100 mm/s). Wide objects get several passes. The printer door must be open or removed and a
box placed in front. Best for flat or squat parts in PLA and PETG; tall narrow parts can
snap instead of sliding. Per-job toggle and settings live in the panel. The pushes run at reduced XY motor
current, so a part that will not budge makes the motors skip steps instead of bending
anything; XY is re-homed afterwards.

## Clean plate

The stock X1C start G-code draws two priming lines at the front of the plate. With
"No purge lines" on (default) the queue rewrites both sections so the filament is primed
into the waste chute at the back and wiped on the nozzle wiper. Nothing is left on the
plate after an eject, so the next job starts on a clean surface.

## Plate check

Before every job the queue photographs the plate with the chamber camera (light on, bed
parked at Z=150, head at the back) and compares it with a reference photo of the empty
plate taken once from the panel. If more than 2% of the plate region differs, the queue
pauses with an alert instead of printing onto a part that did not eject. Bed leveling
stays on as a second net: a probe landing on a leftover part makes the printer abort.
Needs `ffmpeg` on PATH or in `bin/ffmpeg`. Region and threshold are `plate_region` and
`plate_threshold` in `config.json`.

## Run

```bash
cp config.example.json config.json   # fill in the LAN access code
./run.sh
```

Open `http://<host>:8745`. In Bambu Studio slice as usual, then File > Export > Export
plate sliced file, and drop the resulting `.gcode.3mf` on the panel. Multi-plate files
become one job per plate.

## Autostart on macOS

`launchagent/com.gzowo.gsp-print-queue.plist` keeps the server running after login.
Adjust the paths, copy it to `~/Library/LaunchAgents/` and run
`launchctl load ~/Library/LaunchAgents/com.gzowo.gsp-print-queue.plist`.
