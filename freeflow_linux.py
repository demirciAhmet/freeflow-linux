#!/usr/bin/env python3
"""
freeflow-linux: Push-to-talk voice dictation daemon for Linux.

Hold the configured hotkey (default: Right Ctrl) to record, release to transcribe
and paste into the focused application.

Usage:
    python3 freeflow_linux.py           # run daemon
    python3 freeflow_linux.py --dry-run  # check config/devices/session, then exit
"""

import argparse
import asyncio
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Imports (with friendly error messages)
# ---------------------------------------------------------------------------

try:
    import toml
except ImportError:
    sys.exit("Missing dependency: pip install toml")

try:
    import numpy as np
except ImportError:
    sys.exit("Missing dependency: pip install numpy")

try:
    import sounddevice as sd
    import soundfile as sf
except ImportError:
    sys.exit("Missing dependency: pip install sounddevice soundfile")

try:
    from evdev import InputDevice, categorize, ecodes, list_devices
except ImportError:
    sys.exit(
        "Missing dependency: pip install evdev  (also needs 'input' group membership)"
    )

try:
    from groq import Groq
except ImportError:
    sys.exit("Missing dependency: pip install groq")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".config" / "freeflow-linux" / "config.toml"

DEFAULT_CONFIG = """\
# freeflow-linux configuration
api_key = ""            # Groq API key (or set GROQ_API_KEY env var)
hotkey = "KEY_RIGHTCTRL"  # Right Ctrl — change to KEY_F9 etc. if preferred
# stream_mode = "ondemand"  # "ondemand" (mic off when idle) or "persistent" (always-on stream)
# audio_device = ""    # Leave empty to use system default mic
"""

POST_PROCESSING_SYSTEM_PROMPT = """\
You are a dictation post-processor. You receive raw speech-to-text output and return clean text ready to be typed into an application.

Your job:
- Remove filler words (um, uh, you know, like) unless they carry meaning.
- Fix spelling, grammar, and punctuation errors.
- When the transcript already contains a word that is a close misspelling of a name or term from the context or custom vocabulary, correct the spelling. Never insert names or terms from context that the speaker did not say.
- Preserve the speaker's intent, tone, and meaning exactly.

Output rules:
- Return ONLY the cleaned transcript text, nothing else.
- If the transcription is empty, return exactly: EMPTY
- Do not add words, names, or content that are not in the transcription. The context is only for correcting spelling of words already spoken.
- Do not change the meaning of what was said."""

SKIP_POST_PROCESSING = True  # set to True to skip LLM post-processing by default
STREAM_MODE_DEFAULT = (
    "ondemand"  # "ondemand" (mic off when idle) or "persistent" (always-on stream)
)


def load_config() -> dict:
    """Load config from file, creating default if missing. Env var overrides api_key."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(DEFAULT_CONFIG)
        print(f"[freeflow] Created default config at {CONFIG_PATH}")
        print(f"[freeflow] Set api_key in config or export GROQ_API_KEY")

    cfg = toml.loads(CONFIG_PATH.read_text())

    # Env var takes priority
    env_key = os.environ.get("GROQ_API_KEY", "").strip()
    if env_key:
        cfg["api_key"] = env_key

    cfg.setdefault("hotkey", "KEY_RIGHTCTRL")
    cfg.setdefault("audio_device", None)
    cfg.setdefault("api_base_url", "")

    # Stream mode validation
    stream_mode = cfg.get("stream_mode", STREAM_MODE_DEFAULT).strip().lower()
    if stream_mode not in ("ondemand", "persistent"):
        print(
            f"[freeflow] WARNING: Unknown stream_mode '{stream_mode}', falling back to '{STREAM_MODE_DEFAULT}'"
        )
        stream_mode = STREAM_MODE_DEFAULT
    cfg["stream_mode"] = stream_mode

    return cfg


# ---------------------------------------------------------------------------
# Session / paste detection
# ---------------------------------------------------------------------------


def get_session_type() -> str:
    session = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if session in ("wayland", "x11"):
        return session
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def get_compositor() -> str:
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "gnome" in desktop:
        return "gnome"
    if "kde" in desktop or "plasma" in desktop:
        return "kde"
    return "other"  # sway, hyprland, wlroots-based


def is_terminal_focused() -> bool:
    try:
        win_id = subprocess.run(
            ["xdotool", "getactivewindow"], capture_output=True, text=True
        ).stdout.strip()
        xprop = subprocess.run(
            ["xprop", "-id", win_id, "WM_CLASS"], capture_output=True, text=True
        ).stdout.lower()
        win_name = subprocess.run(
            ["xdotool", "getactivewindow", "getwindowname"],
            capture_output=True,
            text=True,
        ).stdout.lower()
        terminals = [
            "xterm",
            "alacritty",
            "kitty",
            "gnome-terminal",
            "tilix",
            "wezterm",
            "st",
            "konsole",
            "terminator",
            "urxvt",
            "rxvt",
            "foot",
            "sakura",
            "terminology",
            "hyper",
            "terminal",
            "xfce4-terminal",
            "lxterminal",
            "mate-terminal",
        ]
        combined = xprop + " " + win_name
        return any(t in combined for t in terminals)
    except Exception:
        return False


def paste_text(text: str, session: str):
    """Copy text to clipboard and simulate Ctrl+V in the focused application."""
    encoded = text.encode("utf-8")
    delay = 0.1

    if session == "x11":
        subprocess.run(["xclip", "-selection", "clipboard"], input=encoded, check=True)
        time.sleep(delay)
        if is_terminal_focused():
            subprocess.run(["xdotool", "key", "ctrl+shift+v"])
        else:
            subprocess.run(["xdotool", "key", "ctrl+v"])

    elif session == "wayland":
        compositor = get_compositor()
        subprocess.run(["wl-copy", "--", text], check=True)

        if compositor == "gnome":
            # GNOME Wayland doesn't implement virtual-keyboard-unstable-v1
            # Use ydotool with raw uinput keycodes (29=Ctrl, 47=v)
            subprocess.run(
                ["ydotool", "key", "29:1", "47:1", "47:0", "29:0"],
                check=True,
            )
        else:
            # wlroots/KDE: type text directly — bypasses clipboard-manager races
            try:
                subprocess.run(["wtype", text], check=True)
            except FileNotFoundError:
                subprocess.run(["ydotool", "type", text], check=True)

    else:
        # Unknown session: try xclip (may work via XWayland)
        try:
            subprocess.run(["xclip", "-selection", "clipboard"], input=encoded)
        except FileNotFoundError:
            try:
                subprocess.run(["wl-copy", "--", text])
            except FileNotFoundError:
                pass
        print(
            f"[freeflow] Text copied to clipboard (unknown session — paste manually with Ctrl+V)"
        )


# ---------------------------------------------------------------------------
# Audio recording
# ---------------------------------------------------------------------------

SOUNDS_DIR = Path(__file__).resolve().parent / "sounds"


def play_sound(name: str):
    """Play a WAV file from the sounds/ directory via pw-play.

    Uses pw-play for minimal latency on PipeWire systems (Zorin/GNOME).
    Falls back silently if the file doesn't exist or pw-play is missing.
    """
    wav = SOUNDS_DIR / f"{name}.wav"
    if not wav.exists():
        return
    try:
        subprocess.run(
            ["pw-play", str(wav)],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass  # never crash on sound failure


def _get_pipewire_device() -> int | None:
    """Return PipeWire device index if available, else None."""
    try:
        devices = sd.query_devices()
        for i in range(len(devices)):
            if devices[i]["name"] == "pipewire":
                return i
    except Exception:
        pass
    return None


class AudioRecorder:
    SAMPLE_RATE = 16000
    CHANNELS = 1
    DTYPE = "int16"

    def __init__(self, device=None, stream_mode="ondemand"):
        self._device = device
        self._frames: list = []
        self._recording = False
        self._stream = None
        self._stream_mode = stream_mode

    def _validate_device(self, device):
        """Check that a device supports our sample rate, return valid device index."""
        try:
            sd.check_input_settings(
                device=device,
                samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS,
                dtype=self.DTYPE,
            )
            return device
        except Exception:
            pw = _get_pipewire_device()
            if pw is not None:
                print(
                    f"[freeflow] Device doesn't support {self.SAMPLE_RATE} Hz, using PipeWire (device {pw})"
                )
                return pw
            raise

    def _open_stream(self):
        """Create and start the InputStream."""
        device = self._validate_device(self._device)
        self._stream = sd.InputStream(
            samplerate=self.SAMPLE_RATE,
            channels=self.CHANNELS,
            dtype=self.DTYPE,
            device=device,
            callback=self._callback,
        )
        self._stream.start()

    def _close_stream(self):
        """Stop and close the InputStream."""
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _callback(self, indata, frame_count, time_info, status):
        """Internal callback — store frames only when recording."""
        if self._recording:
            self._frames.append(indata.copy())

    def start_stream(self):
        """Call once at daemon startup for persistent mode — keeps stream warm."""
        if self._stream_mode == "persistent":
            self._open_stream()

    def start_recording(self):
        """Called when the hold threshold is reached."""
        self._frames = []
        if self._stream_mode == "ondemand":
            self._open_stream()
        self._recording = True

    def stop_recording(self) -> io.BytesIO:
        """Called on key_up — stop collecting and return WAV buffer."""
        self._recording = False

        if self._stream_mode == "ondemand":
            self._close_stream()

        if not self._frames:
            return io.BytesIO()

        audio = np.concatenate(self._frames, axis=0)
        buf = io.BytesIO()
        sf.write(buf, audio, self.SAMPLE_RATE, format="WAV", subtype="PCM_16")
        buf.seek(0)
        buf.name = "audio.wav"  # Groq SDK may inspect filename for MIME type detection
        return buf


# ---------------------------------------------------------------------------
# Groq integration
# ---------------------------------------------------------------------------


def transcribe(client: Groq, audio_buf: io.BytesIO) -> str:
    result = client.audio.transcriptions.create(
        model="whisper-large-v3-turbo",
        language="en",
        file=audio_buf,
    )
    return result.text.strip()


def post_process(client: Groq, transcript: str, context: str = "") -> str:
    user_message = (
        f"Instructions: Clean up RAW_TRANSCRIPTION and return only the cleaned "
        f"transcript text without surrounding quotes. Return EMPTY if there should be no result.\n\n"
        f'CONTEXT: "{context}"\n\n'
        f'RAW_TRANSCRIPTION: "{transcript}"'
    )

    response = client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        temperature=0.0,
        messages=[
            {"role": "system", "content": POST_PROCESSING_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    result = response.choices[0].message.content.strip()

    # Strip outer quotes if the LLM wrapped the entire response
    if len(result) >= 2 and result[0] == result[-1] and result[0] in ('"', "'"):
        result = result[1:-1].strip()

    if result == "EMPTY":
        return ""
    return result


# ---------------------------------------------------------------------------
# Context gathering (best-effort, X11 only)
# ---------------------------------------------------------------------------


def get_context(session: str) -> str:
    if session != "x11":
        return ""
    try:
        window_id = (
            subprocess.check_output(
                ["xdotool", "getactivewindow"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        title = (
            subprocess.check_output(
                ["xdotool", "getwindowname", window_id], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
        return f"Active window: {title}"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------


def find_keyboard_devices() -> list:
    """Return all evdev devices that have EV_KEY capability (keyboards)."""
    keyboards = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            if ecodes.EV_KEY in dev.capabilities():
                keyboards.append(dev)
        except Exception:
            pass
    return keyboards


def resolve_hotkey(hotkey_name: str) -> int:
    """Convert a key name like 'KEY_RIGHTCTRL' to its evdev keycode."""
    try:
        return getattr(ecodes, hotkey_name)
    except AttributeError:
        print(
            f"[freeflow] Unknown hotkey '{hotkey_name}', falling back to KEY_RIGHTCTRL"
        )
        return ecodes.KEY_RIGHTCTRL


# ---------------------------------------------------------------------------
# Main daemon logic
# ---------------------------------------------------------------------------


class FreeflowDaemon:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        groq_kwargs = {"api_key": cfg["api_key"]}
        if cfg.get("api_base_url"):
            groq_kwargs["base_url"] = cfg["api_base_url"]
        self._client = Groq(**groq_kwargs)
        self._recorder = AudioRecorder(
            device=cfg.get("audio_device") or None,
            stream_mode=cfg.get("stream_mode", "ondemand"),
        )
        self._hotkey_code = resolve_hotkey(cfg["hotkey"])
        self._session = get_session_type()
        self._recording = False
        self._lock = threading.Lock()
        self._pending_timer: threading.Timer | None = None

    def _activate_recording(self):
        """Called 1s after key_down if the key is still held."""
        with self._lock:
            if self._pending_timer is None:
                return  # cancelled by key_up
            self._pending_timer = None
            self._recording = True
        subprocess.Popen(
            [
                "notify-send",
                "-t",
                "1000",
                "-i",
                "audio-input-microphone",
                "FreeFlow",
                "Recording...",
            ]
        )
        play_sound("start_voice")
        print("[freeflow] Recording... (release key to transcribe)")
        self._recorder.start_recording()

    def on_hotkey_down(self):
        with self._lock:
            if self._recording or self._pending_timer is not None:
                return
            timer = threading.Timer(1.0, self._activate_recording)
            self._pending_timer = timer
        timer.start()

    def on_hotkey_up(self):
        with self._lock:
            if self._pending_timer is not None:
                # Released before 1s — cancel, no beep, no recording
                self._pending_timer.cancel()
                self._pending_timer = None
                return
            if not self._recording:
                return
            self._recording = False

        play_sound("stop_voice")
        print("[freeflow] Processing...")
        audio_buf = self._recorder.stop_recording()

        context = get_context(self._session)

        try:
            raw = transcribe(self._client, audio_buf)
            if not raw:
                print("[freeflow] Empty transcription — nothing to paste")
                return
            print(f"[freeflow] Raw transcript: {raw!r}")

            if SKIP_POST_PROCESSING := True:
                cleaned = raw
                print(
                    "[freeflow] Skipping post-processing (SKIP_POST_PROCESSING=True) — using raw transcript"
                )
            else:
                cleaned = post_process(self._client, raw, context)
                if not cleaned:
                    print("[freeflow] Post-processor returned EMPTY — nothing to paste")
                    return
                print(f"[freeflow] Cleaned: {cleaned!r}")

            subprocess.run(["wl-copy", "--", cleaned], check=True)
            subprocess.Popen(
                [
                    "notify-send",
                    "-t",
                    "1000",
                    "-i",
                    "edit-paste",
                    "FreeFlow",
                    "Transcription ready in clipboard",
                ]
            )
            print("[freeflow] Copied to clipboard.")
            paste_text(cleaned, self._session)
            print("[freeflow] Pasted.")

        except Exception as e:
            print(f"[freeflow] Error: {e}")

    async def _monitor_device(self, dev: InputDevice):
        try:
            async for event in dev.async_read_loop():
                if event.type == ecodes.EV_KEY:
                    e = categorize(event)
                    keycodes = e.keycode if isinstance(e.keycode, list) else [e.keycode]
                    # evdev key names are strings like 'KEY_RIGHTCTRL'
                    hotkey_name = self._cfg["hotkey"]
                    if hotkey_name in keycodes:
                        if e.keystate == e.key_down:
                            # Run blocking handler in thread pool to not block event loop
                            asyncio.get_event_loop().run_in_executor(
                                None, self.on_hotkey_down
                            )
                        elif e.keystate == e.key_up:
                            asyncio.get_event_loop().run_in_executor(
                                None, self.on_hotkey_up
                            )
        except OSError:
            pass  # Device disconnected

    async def run(self, devices: list):
        print(f"[freeflow] Monitoring {len(devices)} keyboard device(s)")
        print(f"[freeflow] Hotkey: {self._cfg['hotkey']}")
        print(f"[freeflow] Session: {self._session}")
        print(f"[freeflow] Stream mode: {self._recorder._stream_mode}")

        self._recorder.start_stream()
        print(f"[freeflow] Ready, hold {self._cfg['hotkey']} to dictate")
        subprocess.Popen(
            [
                "notify-send",
                "-t",
                "2000",
                "-i",
                "microphone-sensitivity-low",
                "FreeFlow",
                "Ready, hold Right Ctrl to dictate",
            ]
        )

        await asyncio.gather(*[self._monitor_device(dev) for dev in devices])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="freeflow-linux voice dictation daemon"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Check config/devices/session and exit"
    )
    args = parser.parse_args()

    cfg = load_config()

    api_key = cfg.get("api_key", "").strip()
    print(
        f"[freeflow] Groq API key: {'set (' + api_key[:8] + '...)' if api_key else 'NOT SET'}"
    )
    print(f"[freeflow] Hotkey: {cfg['hotkey']}")
    print(f"[freeflow] Config: {CONFIG_PATH}")

    # Detect session
    session = get_session_type()
    compositor = get_compositor() if session == "wayland" else "n/a"
    print(
        f"[freeflow] Session: {session}"
        + (f" / compositor: {compositor}" if session == "wayland" else "")
    )

    # Find keyboard devices
    try:
        devices = find_keyboard_devices()
    except PermissionError:
        print(
            "[freeflow] ERROR: Cannot read /dev/input — add yourself to the 'input' group:"
        )
        print("           sudo usermod -aG input $USER  (then log out and back in)")
        sys.exit(1)

    if not devices:
        print("[freeflow] WARNING: No keyboard devices found in /dev/input")
    else:
        print(f"[freeflow] Found {len(devices)} keyboard device(s):")
        for dev in devices:
            print(f"           {dev.path}: {dev.name}")

    if args.dry_run:
        print("[freeflow] Dry-run complete.")
        return

    if not api_key:
        print(
            "[freeflow] ERROR: No Groq API key. Set api_key in config or export GROQ_API_KEY"
        )
        sys.exit(1)

    if not devices:
        print("[freeflow] ERROR: No keyboard devices to monitor. Cannot start.")
        sys.exit(1)

    daemon = FreeflowDaemon(cfg)
    try:
        asyncio.run(daemon.run(devices))
    except KeyboardInterrupt:
        print("\n[freeflow] Stopped.")


if __name__ == "__main__":
    main()
