"""Targeted smoke test for the new start/keep-awake hotkeys and the keep-awake toggle.

Runs headless (hidden Tk). Patches KeepAwake to a fake so the test never touches
the real Windows power state. Verifies:
  1. start_key_entries / awake_key_entries defaults and dedupe/validation
  2. App builds the 常亮 button and start/stop hotkey labels
  3. toggle_keep_awake acquires/releases and persists keep_awake_idle
  4. _poll_hotkeys fires start/awake exactly once per press (edge detection)
"""
import os
import sys
import tempfile
import faulthandler

faulthandler.dump_traceback_later(60, exit=True)

_sandbox = tempfile.TemporaryDirectory(prefix="slotbot-smoke-")
os.environ["SLOTBOT_DATA_DIR"] = _sandbox.name

import tkinter as tk

_o = tk.Tk.__init__


def _init(self, *a, **k):
    _o(self, *a, **k)
    try:
        self.withdraw()
    except Exception:
        pass


tk.Tk.__init__ = _init
tk.Tk.mainloop = lambda self, *a, **k: None

import slotbot as s
import original_features as of

# Fake KeepAwake so no real SetThreadExecutionState call happens.
FAKE = {"on": 0}


class FakeAwake:
    def __init__(self, enabled=True, api=None, log=None):
        self.enabled = enabled
        self.log = log or (lambda m: None)

    def __enter__(self):
        if self.enabled:
            FAKE["on"] += 1
            self.log("[fake] acquired")
        return self

    def __exit__(self, *a):
        if self.enabled:
            FAKE["on"] -= 1
            self.log("[fake] released")


of.KeepAwake = FakeAwake

fails = []


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + "  " + msg)
    if not cond:
        fails.append(msg)


# 1) helpers
check([n for _v, n in s.start_key_entries({})] == ["F11"], "start default = F11")
check([n for _v, n in s.awake_key_entries({})] == ["F10"], "awake default = F10")
check([n for _v, n in s.start_key_entries({"start_keys": ["F8", "F8", "zzz"]})] == ["F8"],
      "start dedupe + drop invalid")
check([n for _v, n in s.stop_key_entries({"stop_keys": ["F12", "End"]})] == ["F12", "End"],
      "stop unchanged")

# 2/3/4) App-level behavior
app = s.App()
app.withdraw()
try:
    app.update()
    check(getattr(app, "btn_awake", None) is not None, "常亮 button exists")
    check("F11" in str(app.btn_start.cget("text")), "start button shows F11")
    check("F10" in str(app.btn_awake.cget("text")), "awake button shows F10")
    check(FAKE["on"] == 0, "keep-awake off initially")

    app.toggle_keep_awake()
    app.update()
    check(app._awake_hold is not None and FAKE["on"] == 1, "toggle ON acquires")
    check(app.cfg.get("keep_awake_idle") is True, "toggle ON persists flag")
    app.toggle_keep_awake()
    app.update()
    check(app._awake_hold is None and FAKE["on"] == 0, "toggle OFF releases")
    check(app.cfg.get("keep_awake_idle") is False, "toggle OFF persists flag")

    # edge detection: hold start for several polls -> start() called once
    calls = []
    app.start = lambda: calls.append(1)
    app.core = s  # ensure core present
    held = {s.VK_BY_NAME["F11"]}
    orig_kp = s.key_pressed
    s.key_pressed = lambda vk: vk in held
    try:
        for _ in range(5):
            app._hotkey_down.clear()
            app._hotkey_down.add("start")  # simulate already-held
            app._poll_hotkeys()
    finally:
        s.key_pressed = orig_kp
    # Simulate a clean single press from released state:
    calls.clear()
    held = {s.VK_BY_NAME["F11"]}
    s.key_pressed = lambda vk: vk in held
    try:
        app._hotkey_down.clear()
        app._poll_hotkeys()   # press edge -> fires
        app._poll_hotkeys()   # still held -> no refire
        app._poll_hotkeys()
    finally:
        s.key_pressed = orig_kp
    check(len(calls) == 1, "start hotkey fires once per press (edge detection)")
finally:
    try:
        if app._awake_hold is not None:
            app.toggle_keep_awake()
    except Exception:
        pass
    try:
        app.on_close()
    except Exception:
        pass

print("\nRESULT:", "PASS" if not fails else f"FAIL ({len(fails)})")
sys.exit(0 if not fails else 1)
