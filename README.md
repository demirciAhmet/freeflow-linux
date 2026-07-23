# freeflow-linux

> **Fork of [wolfgangmeyers/freeflow-linux](https://github.com/wolfgangmeyers/freeflow-linux)** (originally a Linux port of [FreeFlow](https://github.com/zachlatta/freeflow) by Zach Latta).
> This fork adds on-demand mic streaming (privacy / zero-idle-CPU), WAV-based sound effects, configurable stream modes, and a direct-typing Wayland paste fix.

Push-to-talk voice dictation for Linux using Groq Whisper + LLM post-processing.

A Linux equivalent of [FreeFlow](https://github.com/zachlatta/freeflow) (macOS). Hold a
configurable hotkey (default: Right Ctrl) to record your voice — the audio is transcribed
by Groq Whisper and cleaned up by a Groq LLM, then pasted into whatever app you have focused.

## How it works

1. Hold the hotkey for 1 second (a beep confirms recording has started)
2. Speak
3. Release the hotkey — the transcript is cleaned up and inserted into the focused window (xdotool on X11, direct `wtype` typing on Wayland)

The 1-second hold threshold prevents accidental triggers.

## Requirements

### System packages

```bash
# Audio (PortAudio runtime)
sudo apt install libportaudio2 pipewire-alsa

# Paste — X11
sudo apt install xdotool xclip

# Paste — Wayland
sudo apt install wl-clipboard wtype      # wlroots compositors (Sway, Hyprland, KDE)
sudo apt install ydotool                 # GNOME Wayland (also needs ydotoold daemon)
```

### Python packages

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Setup

### 1. Groq API key

Get a free API key at [console.groq.com](https://console.groq.com/).

Set it in the config file (created automatically on first run):

```bash
~/.config/freeflow-linux/config.toml
```

Or export it as an environment variable:

```bash
export GROQ_API_KEY=gsk_...
```

### 2. Input group (required for hotkey capture)

freeflow-linux reads keyboard events directly from `/dev/input` via evdev. This requires
membership in the `input` group:

```bash
sudo usermod -aG input $USER
# Log out and back in for the group to take effect
```

### 3. GNOME Wayland: ydotool setup

If you use GNOME on Wayland, you need `ydotool` for paste to work:

```bash
sudo apt install ydotool
echo 'KERNEL=="uinput", GROUP="input", MODE="0660"' | sudo tee /etc/udev/rules.d/80-uinput.rules
sudo udevadm control --reload-rules
sudo systemctl enable --now ydotool
# Log out and back in
```

## Running

```bash
cd ~/code/freeflow-linux
.venv/bin/python freeflow_linux.py
```

### Dry-run (check config and devices without starting)

```bash
.venv/bin/python freeflow_linux.py --dry-run
```

## Configuration

Config file: `~/.config/freeflow-linux/config.toml` (created automatically on first run)

```toml
api_key = "gsk_..."          # Groq API key (or use GROQ_API_KEY env var)
hotkey = "KEY_RIGHTCTRL"     # Right Ctrl — change to KEY_F9 etc. if preferred
# audio_device = ""          # Leave empty to use system default mic
```

To find available hotkey names, run `evtest` and press the key you want.

## Stream mode: on-demand vs persistent

The `stream_mode` setting controls whether the audio stream stays open at all times:

- **`ondemand`** (default): Mic is opened only when you hold the hotkey past the 1s threshold, and closed after transcription. CPU usage at idle is ~0%. Privacy benefit: the mic hardware is electrically inactive when you're not dictating. Adds ~100ms of stream-init latency on first utterance.
- **`persistent`**: Audio stream runs continuously, the callback discards samples when not recording. Instant recording start. Idle CPU is ~3% (PortAudio poll loop). Mic hardware stays active while the daemon runs.

Example config:
```toml
stream_mode = "ondemand"     # or "persistent"
```

You can also comment/uncomment the option in `~/.config/freeflow-linux/config.toml`.

## Sound effects

freeflow-linux plays a sound when recording starts and when it finishes. WAV files are stored in `sounds/` next to the script, played via `pw-play` (PipeWire).

Includes two sets:
- **Bird** — rising chirp on start, falling chirp on stop (`start_voice_bird.wav`, `stop_voice_bird.wav`)
- **macOS-style** — bright ascending pings on start, glassy descending sweep on stop (`start_macos.wav`, `stop_macos.wav`)

Swap between them by copying the file you want over the active name:
```bash
cp sounds/start_macos.wav sounds/start_voice.wav
cp sounds/stop_macos.wav sounds/stop_voice.wav
```

Drop any WAV file (48kHz, mono or stereo) into `sounds/start_voice.wav` or `sounds/stop_voice.wav` to use your own.

## Autostart (systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cp freeflow-linux.service.example ~/.config/systemd/user/freeflow-linux.service
# Edit the ExecStart path and environment variables for your setup
systemctl --user daemon-reload
systemctl --user enable --now freeflow-linux
```

The example service file ships with Wayland defaults (commented X11 block included). Adjust
`WAYLAND_DISPLAY`, `XDG_CURRENT_DESKTOP`, and `XDG_RUNTIME_DIR` to match your session —
run `echo $WAYLAND_DISPLAY $XDG_CURRENT_DESKTOP` in a terminal to check.

## Paste behavior on Wayland

On Wayland (wlroots/KDE), the transcript is typed directly into the focused window via
`wtype <text>` instead of simulating Ctrl+V. This avoids race conditions with clipboard
managers (clipse, cliphist) where Ctrl+V may paste a stale clipboard entry instead of the
new transcription. The text is still copied to the clipboard as a backup.

## Hotkey compatibility notes

- **Fn key**: Does not work — handled by keyboard firmware, never reaches the kernel
- **Right Ctrl** (default): Reliable, rarely used for other shortcuts
- **F9, ScrollLock, media keys**: All work well as alternatives

## X11 vs Wayland

| Feature | X11 | Wayland |
|---|---|---|
| Hotkey capture (evdev) | Yes | Yes |
| Paste | xclip + xdotool | wl-copy + wtype (direct typing) / ydotool |
| Window context | Yes (xdotool) | No |
| Terminal detection (Ctrl+Shift+V) | Yes | N/A (direct typing, no Ctrl+V needed) |

## Credits

Inspired by [FreeFlow](https://github.com/zachlatta/freeflow) by Zach Latta.  
This is a fork of [wolfgangmeyers/freeflow-linux](https://github.com/wolfgangmeyers/freeflow-linux) (the initial Linux port).  
Uses [Groq](https://groq.com/) for fast Whisper transcription and LLM post-processing.
