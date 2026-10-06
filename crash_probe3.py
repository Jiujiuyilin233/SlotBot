"""泄漏定位探针：每轮取点后做 gc 普查 + tracemalloc diff + 线程/GDI 计数。"""
import ctypes
import faulthandler
import gc
import json
import os
import sys
import threading
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

ERR_LOG = open('crash_probe3_stderr.log', 'w', encoding='utf-8', buffering=1)
faulthandler.enable(ERR_LOG)


def mem_mb():
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD)] + [
            (n, ctypes.c_size_t) for n in
            ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
             'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
             'PagefileUsage', 'PeakPagefileUsage')]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb)
    return pmc.PagefileUsage / 1048576.0


def gdi_count():
    user32 = ctypes.windll.user32
    return user32.GetGuiResources(ctypes.windll.kernel32.GetCurrentProcess(), 2)  # GR_GDIOBJECTS


import slotbot
from slotbot import App, PointPicker, RegionPicker

N = 15
tracemalloc.start(5)

app = App()
app.cfg['game_window'] = 'VRChat'
app.cfg['credit_rect'] = None
app.withdraw()
app.update()

log_lines = []


def log(msg):
    line = "[%s] %s" % (time.strftime('%H:%M:%S'), msg)
    log_lines.append(line)
    print(line, flush=True)


state = {'i': 0, 'phase': 'idle', 't0': 0.0, 'clicked': False}


def type_census():
    counts = {}
    for obj in gc.get_objects():
        cn = type(obj).__name__
        if cn in ('PhotoImage', 'PointPicker', 'RegionPicker', 'ndarray', 'Screen', 'Frame',
                  'Toplevel', 'Canvas', 'Thread', 'MSS', 'MssPool'):
            counts[cn] = counts.get(cn, 0) + 1
    counts['threads'] = threading.active_count()
    return counts


def snap_top(prev):
    cur = tracemalloc.take_snapshot()
    top = cur.compare_to(prev, 'lineno') if prev else cur.statistics('lineno')
    out = []
    for st in top[:4]:
        out.append('    %+dB %s' % (st.size_diff if prev else st.size, str(st.traceback).replace('\n', ' @ ')))
    return cur, out


prev_snap = None
census0 = None


def finish(code, why=''):
    log('=== 探针3结束 code=%d 内存 %.0fMB GDI=%d ===' % (code, mem_mb(), gdi_count()))
    with open('crash_probe3_result.json', 'w', encoding='utf-8') as f:
        json.dump({'code': code, 'log': log_lines}, f, ensure_ascii=False, indent=1)
    try:
        app.on_close()
    except Exception:
        pass
    app.after(300, lambda: os._exit(code))


def probe_once():
    state['phase'] = 'pick'
    state['t0'] = time.monotonic()
    state['clicked'] = False
    app.pick_point('coin')
    poll_picker()


def poll_picker():
    if state['phase'] != 'pick':
        return
    if time.monotonic() - state['t0'] > 25:
        finish(2, '超时')
        return
    for w in app.winfo_children():
        if isinstance(w, (PointPicker, RegionPicker)) and not state['clicked']:
            state['clicked'] = True
            cx = w.winfo_screenwidth() // 3
            cy = w.winfo_screenheight() // 3
            app.after(50, lambda w=w, cx=cx, cy=cy: dispatch(w, cx, cy))
            return
    app.after(40, poll_picker)


def dispatch(w, x, y):
    try:
        w.canvas.event_generate('<Motion>', x=x, y=y)
        w.canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        app.after(50, wait_done)
    except Exception as exc:
        finish(3, '派发异常 %r' % (exc,))


def wait_done():
    global prev_snap, census0
    if state['phase'] != 'pick':
        return
    saved = app._operation_busy is False and app.winfo_viewable() and state['clicked']
    gone = not any(isinstance(w, (PointPicker, RegionPicker)) for w in app.winfo_children())
    if saved and gone:
        state['i'] += 1
        i = state['i']
        gc.collect()
        if i == 1:
            census0 = type_census()
            prev_snap, _ = snap_top(None)
            log('第1轮完成 内存=%.0fMB GDI=%d（基线轮）' % (mem_mb(), gdi_count()))
        else:
            census = type_census()
            delta = {k: v - census0.get(k, 0) for k, v in census.items() if k != 'threads'}
            prev_snap, tops = snap_top(prev_snap)
            log('第%d轮 内存=%.0fMB(+%d) GDI=%d(+%d) threads=%d 累计Δ=%s'
                % (i, mem_mb(), mem_mb() - 0 or 0, gdi_count(),
                   gdi_count(), census['threads'], delta))
            for t in tops:
                log(t)
        state['phase'] = 'idle'
        if i >= N:
            finish(0, '完成')
            return
        app.after(150, probe_once)
        return
    if time.monotonic() - state['t0'] > 25:
        finish(2, '点击后超时')
        return
    app.after(50, wait_done)


log('探针3启动 pid=%d' % os.getpid())
app.after(300, probe_once)
app.mainloop()
