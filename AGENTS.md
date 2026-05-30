# freeflow-linux — AI context

## What is this?

Push-to-talk voice dictation daemon for Linux. Hold a hotkey (default: Right Ctrl) for 1s to record, release to transcribe via Groq Whisper and paste into the focused application.

## How to run

```bash
cd ~/freeflow-linux
.venv/bin/python freeflow_linux.py
# Dry run (check config/devices without starting):
.venv/bin/python freeflow_linux.py --dry-run
```

## Architecture

Single-file daemon (`freeflow_linux.py`). Key components:

- **`AudioRecorder`** — wraps `sounddevice.InputStream`. Two stream modes:
  - `ondemand` (default): opens mic only during active recording, closes after. ~0% idle CPU.
  - `persistent`: stream always open, callback discards when not recording. ~3% idle CPU, zero startup latency.
- **`FreeflowDaemon`** — asyncio event loop reading evdev keyboard events. On hotkey down, starts a 1s timer. On timer fire, calls `AudioRecorder.start_recording()`. On key up, calls `stop_recording()` → transcribe → paste.
- **Transcription** — Groq `whisper-large-v3-turbo`. Post-processing via `llama-4-scout-17b-16e-instruct` (disabled by default via `SKIP_POST_PROCESSING = True`).

## Config

Location: `~/.config/freeflow-linux/config.toml`

Keys:
- `api_key` — Groq API key (or `GROQ_API_KEY` env var)
- `hotkey` — evdev key name, e.g. `KEY_RIGHTCTRL`, `KEY_F9`
- `stream_mode` — `"ondemand"` (default) or `"persistent"`
- `audio_device` — leave empty for system default, or `"pipewire"`
- `api_base_url` — optional custom Groq endpoint

## Dependencies

- Python: `sounddevice`, `soundfile`, `evdev`, `groq`, `numpy`, `toml`
- System: `libportaudio2`, xdotool + xclip (X11) or wl-clipboard + wtype/ydotool (Wayland)
- Group membership: `input` (for `/dev/input` evdev access)

## Paste logic

- X11: `xclip` → `xdotool ctrl+v` (ctrl+shift+v in terminals)
- Wayland (wlroots/KDE): `wl-copy` → `wtype`
- Wayland (GNOME): `wl-copy` → `ydotool raw keycodes`

Currently copying to clipboard without pasting (paste commented out until ydotool command is fixed).

## Key design decisions

- 1s hold threshold prevents accidental triggers (pre-record beep at 0s, record-start beep at 1s)
- stream opened on-demand by default for privacy/CPU — was persistent before, changed May 2026
- Post-processing LLM disabled by default (raw transcription is good enough, and saves API cost)
- Context-gathering (active window title) is X11-only; Wayland has no equivalent API
