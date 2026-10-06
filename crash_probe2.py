"""取点崩溃复现探针 v2：带真实游戏窗口激活路径（list_windows/activate_window/MouseLock）。

- game_window='VRChat'：完整复刻用户取点时的窗口激活链路
- 只激活窗口 + 冻结截图 + 钉鼠标，绝不发送鼠标/键盘输入
- 每轮记录私有内存（PagefileUsage），看 40 轮内是否暴涨（RADAR_PRE_LEAK 线索）
"""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

ERR_LOG = open('crash_probe2_stderr.log', 'w', encoding='utf-8', buffering=1)
faulthandler.enable(ERR_LOG)
faulthandler.dump_traceback_later(25, repeat=True, file=ERR_LOG)


class PMC(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD),
                ('PeakWorkingSetSize', ctypes.c_size_t),
                ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t),
                ('PeakPagefileUsage', ctypes.c_size_t)]


def mem_mb():
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(
        ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb)
    return (pmc.PagefileUsage / 1048576.0) if ok else -1.0


import slotbot
from slotbot import App, PointPicker, RegionPicker

N = int(sys.argv[1]) if len(sys.argv) > 1 else 40

app = App()
app.cfg['game_window'] = 'VRChat'   # 真实激活 VRChat（用户取点时的状态）
app.cfg['credit_rect'] = None
app.withdraw()
app.update()

log_lines = []


def log(msg):
    line = "[%s] %s" % (time.strftime('%H:%M:%S'), msg)
    log_lines.append(line)
    print(line, flush=True)


state = {'i': 0, 'phase': 'idle', 't0': 0.0, 'clicked': False, 'pickers': 0}


def finish(code, why=''):
    if why:
        log('结束原因: ' + why)
    log('=== 探针结束 code=%d 末轮内存 %.0fMB ===' % (code, mem_mb()))
    faulthandler.cancel_dump_traceback_later()
    with open('crash_probe2_result.json', 'w', encoding='utf-8') as f:
        json.dump({'code': code, 'rounds': state['i'], 'log': log_lines},
                  f, ensure_ascii=False, indent=1)
    try:
        app.on_close()
    except Exception:
        pass
    app.after(400, lambda: os._exit(code))


def probe_once():
    state['phase'] = 'pick'
    state['t0'] = time.monotonic()
    state['clicked'] = False
    log('--- 第 %d/%d 次取点 | 私有内存 %.0fMB ---' % (state['i'] + 1, N, mem_mb()))
    app.pick_point('coin')
    poll_picker()


def poll_picker():
    if state['phase'] != 'pick':
        return
    if time.monotonic() - state['t0'] > 25:
        log('[看门狗] 25s 未完成：busy=%s picker=%s children=%d state=%s'
            % (app._operation_busy, app._picker_active, len(app.winfo_children()), app.state()))
        finish(2, '单轮超时（疑似卡死）')
        return
    for w in app.winfo_children():
        if isinstance(w, (PointPicker, RegionPicker)) and not state['clicked']:
            state['pickers'] += 1
            state['clicked'] = True
            cx = w.winfo_screenwidth() // 3
            cy = w.winfo_screenheight() // 3
            log('浮层出现，激活后 %.2fs，sx=%.2f photo=%s，派发点击'
                % (time.monotonic() - state['t0'], w.sx, w._photo is not None))
            # 稍作稳定再派发，避免刚检测到 picker 时其 Tk 路径尚在收尾
            app.after(120, lambda w=w, cx=cx, cy=cy: dispatch(w, cx, cy))
            return
    app.after(40, poll_picker)


def live_picker():
    """重新定位当前存活的 picker（避免闭包持有的引用在下一拍已被 Tk 回收）。"""
    best = None
    for c in app.winfo_children():
        if isinstance(c, (PointPicker, RegionPicker)) and c.winfo_exists():
            best = c
    return best


def dispatch(w, x, y, attempt=0):
    target = live_picker() or w
    try:
        if target is not None and target.winfo_exists() and target.canvas.winfo_exists():
            target.canvas.event_generate('<Motion>', x=x, y=y)
            target.canvas.event_generate('<ButtonPress-1>', x=x, y=y)
            app.after(50, wait_done)
            return
        raise RuntimeError('no live canvas')
    except Exception as exc:
        if attempt < 8:
            log('事件派发重试 %d: %r' % (attempt, exc))
            app.after(40, lambda: dispatch(w, x, y, attempt + 1))
        else:
            log('事件派发异常: %r' % (exc,))
            finish(3, '事件派发异常')


def wait_done():
    if state['phase'] != 'pick':
        return
    saved = app._operation_busy is False and app.winfo_viewable() and state['clicked']
    gone = not any(isinstance(w, (PointPicker, RegionPicker)) for w in app.winfo_children())
    if saved and gone:
        log('完成，用时 %.2fs，坐标=%s，内存 %.0fMB'
            % (time.monotonic() - state['t0'], app.cfg['points'].get('coin'), mem_mb()))
        state['i'] += 1
        state['phase'] = 'idle'
        if state['i'] >= N:
            finish(0, '全部 %d 轮通过' % N)
            return
        app.after(120, probe_once)
        return
    if time.monotonic() - state['t0'] > 25:
        finish(2, '点击后超时（疑似卡死）')
        return
    app.after(50, wait_done)


log('探针v2启动：N=%d pid=%d game_window=VRChat 逻辑=%dx%d 物理=%dx%d'
    % (N, os.getpid(), app.winfo_screenwidth(), app.winfo_screenheight(),
       slotbot.user32.GetSystemMetrics(0), slotbot.user32.GetSystemMetrics(1)))
app.after(300, probe_once)
app.mainloop()
