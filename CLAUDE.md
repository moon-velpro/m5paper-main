# M5Paper Home Dashboard

E-ink dashboard for M5Paper S3 showing weather, temperature chart, and bus departures.

## Project Structure

- `m5paper_hw/` — ESP-IDF firmware for M5Paper S3 (ESP32-S3, e-ink 960x540)
- `backend/` — Python backend serving dashboard JSON
- `simulator/` — Python dashboard screenshot generator (standalone, not part of C build)

## ESP-IDF Setup

- **ESP-IDF version:** v5.3 at `~/esp/esp-idf-v5.3`
- **ESP-IDF Python venv:** `~/.espressif/python_env/idf5.3_py3.11_env/bin/python`
- **Old ESP-IDF (v4, do NOT use):** `~/esp/esp-idf/` — does not support ESP32-S3
- **Important:** Must set `IDF_PATH=~/esp/esp-idf-v5.3` explicitly, as the shell env has the old path
- **USB Port:** `/dev/cu.usbmodem101`
- **Flash command:** `task flash-monitor` (see `Taskfile.yml`)
- If device is in deep sleep and won't connect: hold **Boot button**, press **Reset**, release Boot, then flash immediately

## Build

All build/flash commands go through `Taskfile.yml` which handles Python env and IDF_PATH:

```
task build          # compile
task flash          # flash to device
task monitor        # serial monitor
task flash-monitor  # flash + monitor
task menuconfig     # ESP-IDF config
task clean          # clean build
```

---
Updated: 2026-04-04
