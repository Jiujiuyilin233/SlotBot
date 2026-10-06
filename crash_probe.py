"""固定坐标取点崩溃复现探针（只读桌面，绝不发真实输入、绝不碰游戏窗口）。

真启动 App → 循环执行 pick_point 完整链路：
withdraw → 冻结截图（wait_frames_stable）→ PointPicker 浮层（冻结帧铺底）
→ Motion + ButtonPress 真实 Tk 事件派发 → on_click → _save_point → deiconify。

- faulthandler：原生崩溃时把所有线程 Python 栈写进 crash_probe_stderr.log
- dump_traceback_later：每 20s 定期落所有线程栈，若卡死能看出卡在哪
- 看门狗：单轮 >20s 视为卡死，落盘退出
"""
import faulthandler
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

ERR_LOG = open('crash_probe_stderr.log', 'w', encoding='utf-8', buffering=1)
faulthandler.enable(ERR_LOG)
faulthandler.dump_traceback_later(20, repeat=True, file=ERR_LOG)

import ctypes


def mem_mb():
    """当前进程私有内存（MB），监控取点循环有没有内存暴涨。"""
    import ctypes.wintypes as wt

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

    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    return pmc.PagefileUsage / 1048576.0


import slotbot
from slotbot import App, PointPicker, RegionPicker

N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
MODE = sys.argv[2] if len(sys.argv) > 2 else 'point'   # point | region

app = App()
app.cfg['game_window'] = ''      # 关键：空关键字 → list_windows 返回空 → 绝不激活任何窗口
app.cfg['credit_rect'] = None    # 高频读数不抓屏，变量只留取点链路
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
    log('=== 探针结束 code=%d 总内存 %.0fMB ===' % (code, mem_mb()))
    faulthandler.cancel_dump_traceback_later()
    with open('crash_probe_result.json', 'w', encoding='utf-8') as f:
        json.dump({'code': code, 'mode': MODE, 'rounds': state['i'], 'log': log_lines},
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
    log('--- 第 %d/%d 次 %s 取点 | 内存 %.0fMB ---'
        % (state['i'] + 1, N, MODE, mem_mb()))
    if MODE == 'point':
        app.pick_point('coin')
    else:
        app.pick_credit_rect() if hasattr(app, 'pick_credit_rect') else app.withdraw()
    poll_picker()


def poll_picker():
    if state['phase'] != 'pick':
        return
    if time.monotonic() - state['t0'] > 20:
        # 卡死：把现场全部落盘
        log('[看门狗] 20s 未完成：busy=%s picker_active=%s children=%d viewable=%s state=%s'
            % (app._operation_busy, app._picker_active, len(app.winfo_children()),
               app.winfo_viewable(), app.state()))
        for w in app.winfo_children():
            log('  child: %s 0x%x viewable=%s geom=%s'
                % (type(w).__name__, w.winfo_id(), w.winfo_viewable(), w.winfo_geometry()))
        finish(2, '单轮超时（疑似卡死）')
        return
    for w in app.winfo_children():
        if isinstance(w, (PointPicker, RegionPicker)) and not state['clicked']:
            state['pickers'] += 1
            state['clicked'] = True
            cx = w.winfo_screenwidth() // 3
            cy = w.winfo_screenheight() // 3
            log('浮层出现（第 %d 个），派发事件到 (%d,%d) sx=%.2f sy=%.2f photo=%s'
                % (state['pickers'], cx, cy, w.sx, w.sy, w._photo is not None))
            app.after(60, lambda w=w, cx=cx, cy=cy: dispatch(w, cx, cy))
            break
    app.after(40, poll_picker)


def dispatch(w, x, y):
    try:
        # 真实 Tk 事件派发：先动一下鼠标（on_move），再按一下左键（on_click）
        w.canvas.event_generate('<Motion>', x=x, y=y)
        w.canvas.event_generate('<ButtonPress-1>', x=x, y=y)
        app.after(50, wait_done)   # 唯一的完成轮询，避免多个 after 循环叠加
    except Exception as exc:
        log('事件派发异常: %r' % (exc,))
        finish(3, '事件派发异常')


def wait_done():
    if state['phase'] != 'pick':
        return
    saved = app._operation_busy is False and app.winfo_viewable() and state['clicked']
    gone = not any(isinstance(w, (PointPicker, RegionPicker)) for w in app.winfo_children())
    if saved and gone:
        dt = time.monotonic() - state['t0']
        pt = app.cfg['points'].get('coin')
        log('完成，用时 %.2fs，保存坐标=%s' % (dt, pt))
        state['i'] += 1
        state['phase'] = 'idle'
        if state['i'] >= N:
            finish(0, '全部 %d 轮通过' % N)
            return
        app.after(150, probe_once)
        return
    if time.monotonic() - state['t0'] > 20:
        finish(2, '点击后超时（疑似卡死）')
        return
    app.after(50, wait_done)


log('探针启动：N=%d mode=%s pid=%d 屏逻辑=%dx%d 物理=%dx%d'
    % (N, MODE, os.getpid(), app.winfo_screenwidth(), app.winfo_screenheight(),
       slotbot.user32.GetSystemMetrics(0), slotbot.user32.GetSystemMetrics(1)))
app.after(300, probe_once)
app.mainloop()
