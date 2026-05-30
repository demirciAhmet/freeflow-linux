# Stream Mode: On-Demand Recording

> **For Hermes:** Implement this plan task-by-task.

**Goal:** Add an on-demand audio stream mode to freeflow-linux so the mic is physically inactive when idle, with the existing persistent-stream mode available as a config option.

**Architecture:** A new `stream_mode` config setting (`ondemand` or `persistent`). Persistent keeps the current behaviour (stream always open, callback discards when not recording). On-demand creates the InputStream only during active recording and closes it afterwards. The daemon currently sits at ~3% CPU idle; on-demand drops that to near zero.

**Tech Stack:** Python, sounddevice, evdev, asyncio, toml

---

**Starting state:** The daemon is already running (`~/freeflow-linux/.venv/bin/python ~/freeflow-linux/freeflow_linux.py`, PID 2951). It will need stopping before code changes and restarting after.

---

### Pre-task: Stop the running daemon

**Objective:** Stop the currently running freeflow daemon so code can be modified.

**Files:** None — just a shell command.

**Step 1: Stop the process**

Run:
```bash
pkill -f freeflow_linux
```
Expected: no output. Verify with `ps aux | grep freeflow` — should show nothing.

---

### Task 1: Add `stream_mode` to config and constants

**Objective:** Add the `ondemand` / `persistent` config option with a default, validate it at load time.

**Files:**
- Modify: `freeflow_linux.py` (constants section, around line 80)
- Modify: `freeflow_linux.py` (`load_config()` function, around line 84)

**Step 1: Add stream mode constant**

After `SKIP_POST_PROCESSING = True` (line 81), add:

```python
STREAM_MODE_DEFAULT = "ondemand"   # "ondemand" or "persistent"
```

**Step 2: Load from config with validation**

In `load_config()`, after the `cfg.setdefault(...)` block (after line 101), add:

```python
    # Stream mode
    stream_mode = cfg.get("stream_mode", STREAM_MODE_DEFAULT).strip().lower()
    if stream_mode not in ("ondemand", "persistent"):
        print(f"[freeflow] WARNING: Unknown stream_mode '{stream_mode}', falling back to '{STREAM_MODE_DEFAULT}'")
        stream_mode = STREAM_MODE_DEFAULT
    cfg["stream_mode"] = stream_mode
```

**Step 3: Update `DEFAULT_CONFIG` string**

Replace the DEFAULT_CONFIG (lines 59-64) with:

```python
DEFAULT_CONFIG = """\
# freeflow-linux configuration
api_key = ""            # Groq API key (or set GROQ_API_KEY env var)
hotkey = "KEY_RIGHTCTRL"  # Right Ctrl — change to KEY_F9 etc. if preferred
# stream_mode = "ondemand"  # "ondemand" (mic off when idle) or "persistent" (always-on stream)
# audio_device = ""    # Leave empty to use system default mic
"""
```

---

### Task 2: Refactor `AudioRecorder` to support both modes

**Objective:** The `AudioRecorder` class needs to support creating the stream on-demand (`open_on_record=True`) vs pre-opened. The stream must be closed after recording in on-demand mode.

**Files:**
- Modify: `freeflow_linux.py` (~lines 232-288)

**Step 1: Update `__init__` to accept `stream_mode`**

Replace the current `AudioRecorder` class with:

```python
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
                device=device, samplerate=self.SAMPLE_RATE,
                channels=self.CHANNELS, dtype=self.DTYPE
            )
            return device
        except Exception:
            pw = _get_pipewire_device()
            if pw is not None:
                print(f"[freeflow] Device doesn't support {self.SAMPLE_RATE} Hz, using PipeWire (device {pw})")
                return pw
            raise

    def start_stream(self):
        """Call once at daemon startup for persistent mode. Keeps stream warm."""
        self._open_stream()

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

    def start_recording(self):
        """Called on key_down (persistent) or after hold-threshold (ondemand)."""
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
        buf.name = "audio.wav"
        return buf
```

**Step 2: Verify no regressions in class interface**

Check that all call sites still work:
- `self._recorder.start_stream()` — still exists
- `self._recorder.start_recording()` — still exists
- `self._recorder.stop_recording()` — still exists

---

### Task 3: Wire `stream_mode` through `FreeflowDaemon`

**Objective:** Pass `stream_mode` from config to `AudioRecorder`, and conditionally call `start_stream()` only in persistent mode.

**Files:**
- Modify: `freeflow_linux.py` (`FreeflowDaemon.__init__` around line 382, `run()` around line 480)

**Step 1: Update `FreeflowDaemon.__init__`**

Change line 388 from:
```python
self._recorder = AudioRecorder(device=cfg.get("audio_device") or None)
```
to:
```python
self._recorder = AudioRecorder(
    device=cfg.get("audio_device") or None,
    stream_mode=cfg.get("stream_mode", "ondemand"),
)
```

**Step 2: Conditionally call `start_stream` in `run()`**

In `run()`, replace:
```python
self._recorder.start_stream()
```
with:
```python
# In persistent mode, pre-open the stream for zero-latency recording
if self._recorder._stream_mode == "persistent":
    self._recorder.start_stream()
```

---

### Task 4: Update README with the new config option

**Objective:** Document the new `stream_mode` setting.

**Files:**
- Modify: `README.md` (config section, after line 100)

**Step 1: Add `stream_mode` to the config documentation**

After line 100 (`# audio_device`), or in the config block, add:

```toml
stream_mode = "ondemand"     # "ondemand" (mic off when idle, ~100ms startup delay)
                             # or "persistent" (always-on stream, ~3% idle CPU)
```

**Step 2: Add a note about CPU usage / privacy tradeoff**

In the "Configuration" section, add:

> **stream_mode tradeoffs:**
> - `ondemand` (default): Mic is opened only when you hold the hotkey and released after. Zero CPU and mic is electrically inactive when idle. Adds ~100ms of stream-init latency — you may miss the first syllable of very fast speech.
> - `persistent`: Audio stream runs continuously, callback discards data when not recording. ~3% idle CPU on a typical laptop. Instant recording start. Mic hardware stays active while the daemon runs.

---

### Task 5: Restart the daemon and verify

**Objective:** Start the daemon in on-demand mode and confirm idle CPU is near zero.

**Files:** None — shell commands only.

**Step 1: Start the daemon**

```bash
cd ~/freeflow-linux
.venv/bin/python freeflow_linux.py &
```

Wait 3 seconds for it to settle.

**Step 2: Measure idle CPU**

```bash
PID=$(pgrep -f freeflow_linux | head -1)
ps -T -p $PID -o tid,pcpu,comm,wchan:20 2>/dev/null | sort -k2 -rn | head -10
```

Expected: main threads all show ~0.0% CPU (or very small fractions not rounding up). No thread showing >0.3%.

**Step 3: Press and hold the hotkey, speak something, release**

Confirm the beeps work and transcription comes through.

---

### Where should the code live?

The repo is currently at `~/freeflow-linux/`. This isn't unusual for a personal project, but if you want to move it:

| Location | Pros | Cons |
|---|---|---|
| `~/code/freeflow-linux/` | Standard dev convention, keeps home dir tidy | Need to update systemd service path |
| `~/.local/share/freeflow-linux/` | XDG-compliant data dir | Weird for a git repo you actively develop |
| Keep at `~/freeflow-linux/` | Works now, no migration | Home dir a bit noisy (but you control that) |

I'd suggest moving to `~/code/freeflow-linux/` since the README already references it. But that's a separate task (update .service.example, update autostart, move the dir).

---

## Summary

That's the plan. 5 tasks, all in one file (`freeflow_linux.py`), plus a small readme update.

Want me to implement it? I'll do it task by task: stop the daemon, refactor `AudioRecorder`, wire up the config, update docs, then restart and measure.
