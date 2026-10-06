"""Live diagnostic runner for the regression suite.

Applies the same Tk neutralization as the hidden-window harness run, but
streams unittest output live (so we can see exactly which test is executing
when the suite stalls) and arms faulthandler to dump a traceback and exit
if the whole process blocks for >150s. This converts a silent "未响应" hang
into an actionable stack trace.
"""
import faulthandler
import sys

faulthandler.dump_traceback_later(150, exit=True)

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
# Some tests open Toplevels and may call wait_*; neutralize those too so a
# hidden window can never block the suite.
for _cls in (tk.Tk, tk.Toplevel):
    _cls.wait_window = lambda self, *a, **k: None
    _cls.wait_variable = lambda self, *a, **k: None
    _cls.wait_visibility = lambda self, *a, **k: None

import unittest

# Importing the module sets up its module-level sandbox/temp dir and slotbot.
import regression_1005  # noqa: E402

suite = unittest.defaultTestLoader.loadTestsFromTestCase(regression_1005.Regression)
result = unittest.TextTestRunner(verbosity=2, stream=sys.__stdout__).run(suite)
sys.exit(0 if result.wasSuccessful() else 1)
