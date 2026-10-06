"""Run the regression suite excluding the known pre-existing hanging test and
print a compact failure/error summary. Used to diff the failing set before/after
a change (the suite has pre-existing red tests in this environment, so a raw
red/green isn't a signal — the failure SET is).

Usage: python run_reg_summary.py
"""
import faulthandler
import io
import sys

faulthandler.dump_traceback_later(180, exit=True)

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
for _c in (tk.Tk, tk.Toplevel):
    _c.wait_window = lambda self, *a, **k: None
    _c.wait_variable = lambda self, *a, **k: None
    _c.wait_visibility = lambda self, *a, **k: None

import unittest
import regression_1005 as R

# Spins forever in run()'s empty-coin loop regardless of our changes (pre-existing).
EXCLUDE = {"test_remaining_one_two_go_to_next_normal_coin_batch"}
loader = unittest.defaultTestLoader
suite = loader.loadTestsFromTestCase(R.Regression)


def filt(s):
    out = unittest.TestSuite()
    for t in s:
        if isinstance(t, unittest.TestSuite):
            out.addTest(filt(t))
        elif t._testMethodName not in EXCLUDE:
            out.addTest(t)
    return out


res = unittest.TextTestRunner(verbosity=0, stream=io.StringIO()).run(filt(suite))
names = sorted(c._testMethodName for c, _ in list(res.failures) + list(res.errors))
print("ran=%d failures=%d errors=%d" % (res.testsRun, len(res.failures), len(res.errors)))
for n in names:
    print("  BAD", n)
