"""New controls for the compact original interface. All Tk calls stay on its thread."""
from __future__ import annotations

import copy
import datetime as dt
import json
import os
from pathlib import Path
import queue
import shutil
import threading
import time
from types import SimpleNamespace
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from runtime_controls import (KeepAwake, SHANGHAI, desktop_unlocked, decode_credit_candidate,
                              request_shutdown, schedule_target, validate_config)
from original_ui import OriginalUI


class AppFeatures(OriginalUI):
    def __init__(self):
        self._ui_thread = threading.get_ident()
        self._callbacks = queue.Queue()
        self._closed = False
        self._operation_busy = False
        self._operation_cancel = threading.Event()
        self._generation = 0
        self._preview_busy = False
        self._mouse_lock = None
        self._picker_active = False
        self._armed = False
        self._schedule_cancel = threading.Event()
        self._completion_seen = None
        self._cancel_completion = False
        self._wrappers = []
        self._stop_hook_removers = []
        self._stop_hook_down = set()
        # 只读 Credit 的引擎缓存：配置/屏幕没变就复用，避免每拍重建线程级资源。
        self._cached_credit_reader = None
        self._credit_reader_sig = None
        # 常驻解码线程 + 画面哈希缓存：实时读数是唯一的高频后台活，
        # 每拍建线程/建 mss/重复解码都是白烧的 CPU（见 _decode_loop）。
        self._decode_jobs = queue.Queue()
        self._decode_thread = None
        self._tick_cache = None
        # 界面状态缓存：内容没变就不 configure 控件，Tk 才不会空转重绘。
        self._last_state_text = None
        self._last_stats_text = None
        self._last_start_state = None
        self._last_stop_state = None
        self._last_schedule_text = None
        # 状态横幅/侧栏徽章共用的配色档缓存，以及日志自动滚动开关
        self._last_status_level = None
        self._dot_lit = True
        self._auto_scroll = True
        # pump_log 每拍都会读 _stopping；不能等 _begin_start 才补上
        self._stopping = False
        super().__init__()
        self._background(self.core.get_digit_bank, lambda value, error: None)
        self.after(40, self._pump_callbacks)

    def build_ui(self):
        OriginalUI.build_ui(self)
        self.lbl_header_hint = self.lbl_stats
        self._refresh_run_buttons()

    def _parts_summary(self):
        return '部件模板：' + '　'.join(f'{label} {len(self.tpl_variants.get(key, []))}'
                                     for key, label in self.core.TEMPLATES)

    def _set_state(self, text, level=None):
        """统一的状态行写入：内容没变就不动控件。

        level 是显式配色档（idle/running/stopping/error），不给就按文案猜；
        状态同时喂给运行页横幅和侧栏徽章，两处永远是同一数据源。
        """
        if text != self._last_state_text:
            self.lbl_state.configure(text=text)
            if getattr(self, 'badge_label', None) is not None:
                self.badge_label.configure(text=text)
            self._last_state_text = text
            self._apply_status(level or self._status_level(text))

    def _status_level(self, text):
        """按文案归档状态色：空闲灰 / 运行绿 / 停止中琥珀 / 异常红。"""
        t = str(text or '')
        if any(k in t for k in ('失败', '未通过', '异常', '错误')):
            return 'error'
        if '停止中' in t or '正在停止' in t:
            return 'stopping'
        return 'idle'

    def _apply_status(self, level):
        """状态配色统一入口：运行页横幅的色条+圆点、侧栏徽章圆点一起变。"""
        if level == self._last_status_level:
            return
        self._last_status_level = level
        self._dot_lit = True
        color = self.core.STATE_COLORS.get(level, self.core.STATE_COLORS['idle'])
        strip = getattr(self, 'status_strip', None)
        if strip is not None:
            strip.configure(bg=color)
        for dot in (getattr(self, 'status_dot', None), getattr(self, 'badge_dot', None)):
            if dot is not None:
                dot.itemconfigure('dot', fill=color)

    def _pulse_status_dot(self):
        """运行中圆点呼吸闪烁：借 pump_log 的节拍按时间翻转颜色，
        不新增 after 循环（状态刷新本来就有 150ms 的拍子）。"""
        lit = int(time.time() * 2) % 2 == 0
        if lit == self._dot_lit:
            return
        self._dot_lit = lit
        fill = self.core.RUN_GREEN if lit else self.core.DOT_DIM_GREEN
        for dot in (getattr(self, 'status_dot', None), getattr(self, 'badge_dot', None)):
            if dot is not None:
                dot.itemconfigure('dot', fill=fill)

    def _fit_window(self):
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(980, max(680, sw - 40))
        h = min(700, max(520, sh - 70))
        self._fit_size = (w, h)
        self.geometry(f'{w}x{h}')
        self.minsize(min(820, sw - 40), min(540, sh - 70))

    def _post(self, fn, *args):
        if not self._closed:
            self._callbacks.put((fn, args))

    def _pump_callbacks(self):
        if self._closed:
            return
        for _ in range(30):
            try:
                fn, args = self._callbacks.get_nowait()
            except queue.Empty:
                break
            try:
                fn(*args)
            except Exception as exc:
                self.log(f'[界面] 操作失败：{type(exc).__name__}: {exc}')
        self._tick_schedule()
        self.after(40, self._pump_callbacks)

    def _background(self, work, done):
        def run():
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, exc
            self._post(done, value, error)
        threading.Thread(target=run, daemon=True).start()

    def _submit_decode(self, work, done):
        """把高频解码任务交给常驻线程（实时读数专用；一次性任务仍走 _background）。

        每 400ms 新建一个线程，线程里的 mss 句柄也得跟着重建（Screen 的
        mss 按线程隔离）——常驻线程让 mss 只建一次，线程创建开销归零。
        """
        if self._decode_thread is None or not self._decode_thread.is_alive():
            self._decode_thread = threading.Thread(
                target=self._decode_loop, daemon=True, name='slotbot-decode')
            self._decode_thread.start()
        self._decode_jobs.put((work, done))

    def _decode_loop(self):
        while not self._closed:
            job = self._decode_jobs.get()
            if job is None:
                break
            work, done = job
            try:
                value, error = work(), None
            except Exception as exc:
                value, error = None, exc
            self._post(done, value, error)

    def busy(self):
        return bool(self._operation_busy or self._picker_active or self._armed or getattr(self, '_action_window', None) or
                    (self.engine and self.engine.is_alive()) or
                    (self.collector and self.collector.is_alive()))

    def guard_settings(self):
        if self.busy():
            messagebox.showinfo('请先停止', '运行、采集、校准或定时等待期间不能修改这些设置。', parent=self)
            return False
        return True

    def collect_cfg(self):
        for key, var in self.vars.items():
            if key != 'game_window':
                try:
                    float(var.get().strip())
                except (ValueError, TypeError):
                    raise ValueError(f'{key} 请填写数字。') from None
        super().collect_cfg()
        return self.cfg

    def load_cfg(self):
        super().load_cfg()
        self.cfg.pop('smart_burn_stop', None)
        self.cfg.pop('burn_diff_threshold', None)
        ref = self.cfg.get('credit_ref')
        if ref:
            candidate = os.path.join(self.core.DATA_DIR, os.path.basename(ref))
            if os.path.isfile(candidate):
                self.cfg['credit_ref'] = candidate
        self.cfg['auto_learn'] = False  # Runtime samples never automatically become truth.

    def _refresh_run_buttons(self):
        if not hasattr(self, 'btn_start'):
            return
        start_state = 'disabled' if self.busy() else 'normal'
        stop_state = 'normal' if self.busy() or getattr(self, '_action_window', None) else 'disabled'
        # configure 即使值相同也会走一遍 Tk 状态机；每 150ms 调一次必须先比对。
        if start_state != self._last_start_state:
            self.btn_start.configure(state=start_state)
            self._last_start_state = start_state
        if stop_state != self._last_stop_state:
            self.btn_stop.configure(state=stop_state)
            self._last_stop_state = stop_state

    def _credit_reader(self):
        """取一个只用来读 Credit 的引擎实例（缓存：Credit 区域/参考图/屏幕没变就复用）。

        每 400ms 新建 Engine+mss 会让挂机时的空转开销白白翻倍，这里只在
        影响读数的配置变化时重建。Engine/Screen 本身可跨线程复用（mss 的
        句柄在 Screen 内部按线程隔离）。
        """
        if self.screen is None:
            self.screen = self.core.Screen()
        rect = self.cfg.get('credit_rect')
        sig = (tuple(rect) if rect else None, self.cfg.get('credit_ref'), id(self.screen))
        if self._cached_credit_reader is None or sig != self._credit_reader_sig:
            self._cached_credit_reader = self.core.Engine(copy.deepcopy(self.cfg), {}, lambda msg: None, self.screen)
            self._credit_reader_sig = sig
        return self._cached_credit_reader

    def tick_credit_readout(self, force=False):
        if self._closed:
            return
        rect = copy.deepcopy(self.cfg.get('credit_rect'))
        if rect and not self.busy() and not self._preview_busy:
            self._preview_busy = True
            def work():
                reader = self._credit_reader()
                crop = reader.grab_credit()
                if crop is None:
                    return None, '?', '抓不到 Credit 画面'
                # 画面没变就复用上次读数：空闲时数码管是静止的，绝大多
                # 数拍能整段跳过抓帧和解码——这是挂机占用的大头。
                key = (hash(crop.tobytes()), id(self.core.get_digit_bank()))
                cached = self._tick_cache
                if cached is not None and cached[0] == key:
                    return cached[1]
                crops = [crop]
                for _ in range(2):
                    time.sleep(.08)
                    c = reader.grab_credit()
                    if c is None:
                        return None, '?', '抓不到 Credit 画面'
                    crops.append(c)
                ok, fit = self.core.digit_fit(crops[-1])
                value, info, agree, total = self.core.decode_consensus(crops, bank=self.core.get_digit_bank())
                if not ok:
                    value, info = '?', '选区截断：' + fit
                elif value and str(value).isdigit() and int(value) in (0, 1, 2):
                    ref_path = self.core.resolve_ref_path(self.cfg.get('credit_ref'))
                    reference = self.core.load_image(ref_path) if ref_path else None
                    trusted = [decode_credit_candidate(c, self.core.get_digit_bank(), reference)[0] for c in crops]
                    if not all(v == int(value) for v in trusted):
                        info = '预览低币未通过补币复核，运行时继续按次数'
                    else:
                        info = '低币已通过完整性、模板和三帧复核'
                result = (crops[-1], value or '?', info)
                self._tick_cache = (key, result)
                return result
            def done(result, error):
                self._preview_busy = False
                if self.busy() or self.cfg.get('credit_rect') != rect:
                    return
                if error:
                    self.lbl_credit_live_info.configure(text=f'读取失败：{error}')
                    return
                crop, value, info = result
                self.lbl_credit_live.configure(text=value)
                self.lbl_credit_live_info.configure(text=info[:65])
                if getattr(self, 'monitor', None) is not None and crop is not None:
                    self.monitor.update_reading(crop, value, info, suspect=value == '?')
            self._submit_decode(work, done)
        if not force:
            self.after(400, self.tick_credit_readout)

    def start(self):
        if self.busy():
            return
        try:
            self.collect_cfg()
            validate_config(self.cfg)
            self.save_cfg()
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror('配置错误', str(exc), parent=self)
            return
        target, delay = schedule_target(self.cfg)
        self._cancel_completion = False
        if target is not None or delay > 0:
            self._armed = True
            self._schedule_cfg = copy.deepcopy(self.cfg)
            self._schedule_wall = target
            self._schedule_mono = time.monotonic() + delay if delay else None
            self._schedule_cancel = threading.Event()
            if self.cfg.get('keep_awake_wait', False):
                event = self._schedule_cancel
                def wait_awake():
                    with KeepAwake(log=self.log):
                        event.wait()
                threading.Thread(target=wait_awake, daemon=True).start()
            self._refresh_run_buttons()
            self.log('[定时] 已等待开始；程序须保持打开、游戏可用且桌面解锁')
            return
        self._begin_start(copy.deepcopy(self.cfg), automatic=False)

    def _tick_schedule(self):
        if not self._armed:
            return
        remaining = (self._schedule_wall - time.time() if self._schedule_wall is not None
                     else self._schedule_mono - time.monotonic())
        text = f'定时等待：{max(0, int(remaining))} 秒'
        if text != self._last_schedule_text:
            self._last_schedule_text = text
            # 定时等待用琥珀色：还没在跑，但也不是空闲
            self._set_state(text, level='stopping')
        if remaining <= 0:
            self._armed = False
            self._schedule_cancel.set()
            cfg = copy.deepcopy(self._schedule_cfg)
            self._begin_start(cfg, automatic=True)

    def _begin_start(self, cfg, automatic=False, check_only=False):
        if self._operation_busy or (self.engine and self.engine.is_alive()) or (self.collector and self.collector.is_alive()):
            return
        if self.core.pdi is None and not check_only:
            self.log('[停止] 缺少 pydirectinput')
            return
        self._operation_busy = True
        self._generation += 1
        generation = self._generation
        self._operation_cancel = threading.Event()
        cancel = self._operation_cancel
        self._set_state('运行前自检…', level='running')
        self._refresh_run_buttons()
        self.withdraw()
        templates = dict(self.tpls)
        variants = dict(self.tpl_variants)
        def work():
            if not desktop_unlocked():
                raise RuntimeError('桌面锁定，定时启动已取消，请解锁后重新开始')
            keyword = str(cfg.get('game_window') or '')
            wins = self.core.list_windows(keyword)
            if keyword and not wins:
                raise RuntimeError('找不到游戏窗口，未启动')
            if wins:
                self.core.activate_window(wins[0][0])
            if cancel.wait(.3):
                return None
            screen = self.screen or self.core.Screen()
            proxy = SimpleNamespace(cfg=cfg, tpls=templates, tpl_variants=variants, screen=screen)
            issues = self.core.ModernApp.preflight(proxy)
            if cancel.is_set():
                return None
            engine = None if check_only or any(level == 'error' for level, _ in issues) else self.core.Engine(cfg, templates, self.log, screen, variants)
            return screen, issues, engine
        def done(result, error):
            if generation != self._generation or cancel.is_set():
                return
            self._operation_busy = False
            self.deiconify()
            self._refresh_run_buttons()
            if error or result is None:
                self.log('[自检失败] ' + str(error or '已取消'))
                self._set_state('自检失败，未启动')
                return
            screen, issues, prepared_engine = result
            for level, text in issues:
                self.log(f'[自检 {level}] {text}')
            errors = [text for level, text in issues if level == 'error']
            if errors:
                self._set_state('自检未通过')
                if not automatic:
                    messagebox.showwarning('自检未通过', '\n'.join(errors), parent=self)
                return
            if check_only:
                self._set_state('自检通过', level='running')
                return
            self.screen = screen
            self._cancel_completion = False
            self.engine = prepared_engine
            self._completion_seen = None
            self._stopping = False
            self.overlay = self.core.StopOverlay(self, self.request_stop)
            self.withdraw()
            self.engine.start()
            self._refresh_run_buttons()
        self._background(work, done)

    def run_preflight(self):
        if self.guard_settings():
            try:
                self.collect_cfg()
                validate_config(dict(self.cfg, start_at='', start_delay_minutes=0, stop_at=''))
            except (ValueError, TypeError) as exc:
                messagebox.showerror('配置错误', str(exc), parent=self)
                return
            self._begin_start(copy.deepcopy(self.cfg), check_only=True)

    def request_stop(self):
        # Set input cancellation immediately, even if Tk is busy or frozen.
        self._cancel_completion = True
        self._armed = False
        self._schedule_cancel.set()
        self._operation_cancel.set()
        if self.engine is not None:
            self.engine.request_stop()
        if self.collector is not None:
            self.collector.stop_flag.set()
        if threading.get_ident() != self._ui_thread:
            self._post(self._finish_stop_ui)
            return
        self._finish_stop_ui()

    def _finish_stop_ui(self):
        if self._closed:
            return
        self._cancel_completion = True
        self._armed = False
        self._schedule_cancel.set()
        self._generation += 1
        self._operation_cancel.set()
        self._operation_busy = False
        for picker in self.winfo_children():
            if isinstance(picker, (self.core.PointPicker, self.core.RegionPicker)):
                picker.destroy()
        self._picker_active = False
        if self._mouse_lock is not None:
            self._mouse_lock.__exit__(None, None, None)
            self._mouse_lock = None
        self._cancel_action()
        self._stopping = True
        self._set_state('正在停止…')
        self.deiconify()
        self._refresh_run_buttons()

    def pump_log(self):
        if self._closed:
            return
        try:
            inserted = 0
            for _ in range(100):
                try:
                    line = self.logq.get_nowait()
                except queue.Empty:
                    break
                # 时间戳/[级别] 前缀按级别上色：错误红、警告琥珀，日志一眼分层
                level = self.core.log_level(line)
                if level in ('error', 'warn') and '] ' in line:
                    head = line[:line.find('] ') + 2]
                    self.txt.insert('end', head, level)
                    self.txt.insert('end', line[len(head):] + '\n')
                else:
                    self.txt.insert('end', line + '\n')
                inserted += 1
            if inserted:
                # 没新日志时不要 see('end')：那会强制 Text 整页重绘，是界面空转卡顿的大头。
                if int(self.txt.index('end-1c').split('.')[0]) > 1000:
                    self.txt.delete('1.0', '201.0')
                if self._auto_scroll:
                    self.txt.see('end')
            engine = self.engine
            if engine and engine.is_alive():
                elapsed = int(max(0, time.time() - engine.started_at))
                self._set_state('停止中…' if self._stopping else engine.state,
                                level='stopping' if self._stopping else 'running')
                stats = f'{elapsed // 60:02d}:{elapsed % 60:02d} · ' \
                    f'{engine.spins_done} 局 · {engine.rounds_done} 轮 · ' \
                    f'投币事件 {engine.coins_used} 次 · 下注按钮 {engine.bets_sent} 次'
                if stats != self._last_stats_text:
                    self.lbl_stats.configure(text=stats)
                    self._last_stats_text = stats
                if self._last_status_level == 'running':
                    self._pulse_status_dot()
                if getattr(self, 'overlay', None) is not None:
                    self.overlay.tick(engine.state)
            elif engine and self._completion_seen is not engine:
                self._completion_seen = engine
                self._close_overlay()
                self._set_state(engine.finish_reason or '已停止')
                if engine.completed_normally and not self._cancel_completion:
                    self._finish_action(engine.cfg)
            self._refresh_run_buttons()
        except Exception as exc:
            self.logq.put(f'[界面刷新] {exc}')
        finally:
            if not self._closed:
                self.after(150, self.pump_log)

    def _finish_action(self, cfg):
        action = cfg.get('completion_action', 'stop')
        if action == 'stop':
            return
        if action == 'exit':
            self.on_close()
            return
        if action == 'close_game':
            keyword = str(cfg.get('game_window') or '')
            wins = self.core.list_windows(keyword)
            # Only request graceful close of the exact selected game's HWND.
            if wins:
                self.core.user32.PostMessageW(wins[0][0], 0x0010, 0, 0)
                self.log('[完成动作] 已请求正常关闭游戏')
            return
        if action != 'shutdown':
            return
        win = tk.Toplevel(self)
        self._action_window = win
        win.title('定时完成 · 可取消关机')
        win.attributes('-topmost', True)
        label = ttk.Label(win, padding=20)
        label.pack()
        ttk.Button(win, text='取消关机', command=self._cancel_action).pack(pady=12)
        win.protocol('WM_DELETE_WINDOW', self._cancel_action)
        deadline = time.monotonic() + max(10, float(cfg.get('shutdown_countdown', 60)))
        self._action_token = object()
        token = self._action_token
        def tick():
            if self._cancel_completion or self._action_token is not token:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._cancel_action()
                self.log('[完成动作] 请求正常关机（不强制关闭应用）')
                self._background(lambda: request_shutdown(),
                                 lambda result, error: self.log(f'[关机] {error or "已发送请求"}'))
                return
            label.configure(text=f'{int(remaining) + 1} 秒后请求关机\n可取消；有未保存工作时请取消。')
            win.after(200, tick)
        tick()

    def _cancel_action(self):
        self._action_token = None
        win = getattr(self, '_action_window', None)
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        self._action_window = None

    def open_controls(self):
        if not self.guard_settings():
            return
        self.collect_cfg()
        win = tk.Toplevel(self)
        win.title('定时、保活与高级参数（北京时间）')
        fields = (
            ('start_delay_minutes', '延迟开始（分钟；0=立即）'),
            ('start_at', '指定开始（YYYY-MM-DD HH:MM；留空=不用）'),
            ('stop_at', '指定停止（YYYY-MM-DD HH:MM；留空=不用）'),
            ('max_minutes', '运行时长（分钟；0=不限）'),
            ('shutdown_countdown', '关机前可取消倒计时（秒；至少10）'),
            ('credit_max_display', 'Credit 显示上限（默认99）'),
            ('credit_ref_threshold', '参考图差异阈值'),
            ('scale_min', '多尺度最小缩放'), ('scale_max', '多尺度最大缩放'),
            ('scale_steps', '多尺度档数'),
        )
        variables = {}
        for row, (key, label) in enumerate(fields):
            ttk.Label(win, text=label).grid(row=row, column=0, sticky='w', padx=12, pady=4)
            var = tk.StringVar(value=str(self.cfg.get(key, '')))
            variables[key] = var
            ttk.Entry(win, textvariable=var, width=25).grid(row=row, column=1, padx=12, pady=4)
        row = len(fields)
        awake = tk.BooleanVar(value=bool(self.cfg.get('keep_awake', True)))
        waiting = tk.BooleanVar(value=bool(self.cfg.get('keep_awake_wait', False)))
        samples = tk.BooleanVar(value=bool(self.cfg.get('collect_pending', False)))
        for var, label in ((awake, '运行与采集期间防自动息屏/睡眠'),
                           (waiting, '等待定时开始时也保持亮屏'),
                           (samples, '运行中收集待审核样本（不会自动训练）')):
            ttk.Checkbutton(win, text=label, variable=var).grid(row=row, column=0, columnspan=2, sticky='w', padx=12)
            row += 1
        actions = {'只停止脚本': 'stop', '退出助手': 'exit', '正常关闭游戏': 'close_game', '关机（带取消倒计时）': 'shutdown'}
        action = tk.StringVar(value=next((k for k, v in actions.items() if v == self.cfg.get('completion_action')), '只停止脚本'))
        ttk.Label(win, text='正常完成后的动作').grid(row=row, column=0, sticky='w', padx=12)
        ttk.Combobox(win, textvariable=action, values=list(actions), state='readonly', width=25).grid(row=row, column=1, padx=12)
        row += 1
        stop_keys = tk.StringVar(value=','.join(self.cfg.get('stop_keys', ['F12', 'End'])))
        ttk.Label(win, text='急停键（逗号分隔；默认 F12,End）').grid(row=row, column=0, sticky='w', padx=12)
        ttk.Entry(win, textvariable=stop_keys, width=25).grid(row=row, column=1, padx=12)
        row += 1
        ttk.Label(win, text='开始前检查操作位置；数字仅辅助补币。定时到达完成当前动作局后停止。\n'
                  '程序须保持打开、游戏可见且桌面解锁。金钱条件暂不启用。',
                  foreground='#666').grid(row=row, column=0, columnspan=2, padx=12, pady=10)
        def save():
            if self.busy():
                return
            candidate = copy.deepcopy(self.cfg)
            try:
                for key, var in variables.items():
                    text = var.get().strip()
                    candidate[key] = text if key in ('start_at', 'stop_at') else float(text or 0)
                for key in ('credit_max_display', 'scale_steps'):
                    if candidate[key] != int(candidate[key]):
                        raise ValueError(f'{key} 必须是整数')
                    candidate[key] = int(candidate[key])
                candidate.update(keep_awake=awake.get(), keep_awake_wait=waiting.get(),
                                 collect_pending=samples.get(), completion_action=actions[action.get()])
                names = [n.strip() for n in stop_keys.get().replace('，', ',').split(',') if n.strip()]
                known = {n.lower(): n for n in self.core.VK_BY_NAME}
                if not names or any(n.lower() not in known for n in names):
                    raise ValueError('急停键名称无效；建议 F12,End')
                candidate['stop_keys'] = list(dict.fromkeys(known[n.lower()] for n in names))
                validate_config(candidate)
                self.cfg = candidate
                self.save_cfg()
                self._apply_cfg_to_ui()
                self._bind_stop_hooks()
                win.destroy()
            except (ValueError, TypeError, OSError) as exc:
                messagebox.showerror('设置错误', str(exc), parent=win)
        ttk.Button(win, text='保存设置', command=save).grid(row=row + 1, column=0, columnspan=2, pady=10)

    def _clear_stop_hooks(self):
        for remove in self._stop_hook_removers:
            try:
                remove()
            except Exception:
                pass
        self._stop_hook_removers = []
        self._stop_hook_down.clear()

    def _stop_key_event(self, event, name):
        if self._closed:
            return
        if event.event_type == 'up':
            self._stop_hook_down.discard(name)
        elif event.event_type == 'down' and name not in self._stop_hook_down:
            self._stop_hook_down.add(name)
            self.request_stop()

    def _bind_stop_hooks(self):
        self._clear_stop_hooks()
        if self.core.kb is None:
            self.log('[急停] 按键事件监听不可用；运行时使用 10ms 独立轮询')
            return
        bound = []
        for _, name in self.core.stop_key_entries(self.cfg):
            try:
                remove = self.core.kb.hook_key(self.core.KB_NAME.get(name, name.lower()),
                    lambda event, key=name: self._stop_key_event(event, key), suppress=False)
                self._stop_hook_removers.append(remove)
                bound.append(name)
            except Exception as exc:
                self.log(f'[急停] {name} 事件监听失败：{exc}；仍有独立轮询')
        if bound:
            self.log('[急停] 直接按键监听：' + ' / '.join(bound) + '（按住 Tab 时仍有效）')

    def refresh_stop_keys(self):
        # Advanced dialog creates its own controls; main view stays compact.
        return

    def open_profiles(self):
        if not self.guard_settings():
            return
        win = tk.Toplevel(self)
        win.title('配置方案')
        self.v_profile = tk.StringVar()
        self.cb_profile = ttk.Combobox(win, textvariable=self.v_profile, state='readonly', width=35)
        self.cb_profile.pack(padx=15, pady=12)
        self.lbl_profile = ttk.Label(win)
        self.lbl_profile.pack()
        for label, method in (('载入方案', self.load_profile), ('保存当前配置为方案', self.save_profile_as), ('删除方案', self.delete_profile)):
            ttk.Button(win, text=label, command=method).pack(fill='x', padx=15, pady=5)
        self.refresh_profiles()

    def save_profile_as(self):
        if not self.guard_settings():
            return
        self.collect_cfg()
        name = self.core.simpledialog.askstring('保存方案', '方案名称：', parent=self)
        if not name:
            return
        name = name.strip()
        if not name or any(c in name for c in '<>:"/\\|?*') or name.endswith(('.', ' ')):
            messagebox.showerror('名称无效', '请使用普通名称，不含路径或特殊字符。', parent=self)
            return
        path = Path(self._profile_dir()) / (name + '.json')
        if path.exists() and not messagebox.askyesno('覆盖方案', f'覆盖 {name}？', parent=self):
            return
        path.write_text(json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding='utf-8')
        self.refresh_profiles()
        self.v_profile.set(name)

    def load_profile(self):
        if self.guard_settings():
            super().load_profile()
            self.cfg['auto_learn'] = False
            self._bind_stop_hooks()

    def rebuild_digit_bank(self):
        if not self.guard_settings():
            return
        self._background(lambda: self.core.get_digit_bank(force=True),
                         lambda value, error: self.log(f'[模板库] {error or self.core.bank_summary(value)}'))

    def toggle_autoscroll(self):
        """日志自动滚动开关：往回翻历史时关掉，新日志就不会把视图拽回底部。"""
        self._auto_scroll = not self._auto_scroll
        self.btn_autoscroll.configure(text='自动滚动 ✓' if self._auto_scroll else '自动滚动 ✗')
        if self._auto_scroll:
            self.txt.see('end')

    def clear_log(self):
        """只清界面上的日志文本，不影响日志队列和后续输出。"""
        self.txt.delete('1.0', 'end')

    def collect_and_save(self):
        if not self.guard_settings():
            return
        try:
            self.collect_cfg()
            validate_config(dict(self.cfg, start_at='', start_delay_minutes=0, stop_at=''))
            self.save_cfg()
        except (ValueError, TypeError, OSError) as exc:
            messagebox.showerror('配置错误', str(exc), parent=self)

    def move_test(self, key):
        if not self.guard_settings():
            return
        try:
            pos = tuple(int(v.get()) for v in self.pt_vars[key])
        except ValueError:
            self.log('[移动测试] 请先填写有效坐标')
            return
        keyword = str(self.cfg.get('game_window') or '')
        def work(cancel):
            wins = self.core.list_windows(keyword)
            if not keyword or not wins:
                raise RuntimeError('找不到指定游戏窗口，未移动鼠标')
            self.core.activate_window(wins[0][0])
            if cancel.wait(.4):
                return None
            if keyword.lower() not in self.core.foreground_title().lower():
                raise RuntimeError('游戏未获得焦点，未移动鼠标')
            if self.core.pdi:
                self.core.pdi.moveTo(*pos)
            return pos
        self._diagnostic(work, lambda value: self.log(f'[移动测试] 已移动到 {value}'))

    def _diagnostic(self, work, done):
        if not self.guard_settings():
            return
        self._operation_busy = True
        self._generation += 1
        generation = self._generation
        self._operation_cancel = threading.Event()
        cancel = self._operation_cancel
        self._refresh_run_buttons()
        self.withdraw()
        def run():
            if cancel.wait(.4):
                return None
            return work(cancel)
        def finish(value, error):
            if generation != self._generation or cancel.is_set():
                return
            self._operation_busy = False
            self.deiconify()
            self._refresh_run_buttons()
            if error:
                self.log(f'[诊断] {error}')
            elif value is not None:
                done(value)
        self._background(run, finish)

    def test_credit_rect(self):
        if not self.cfg.get('credit_rect'):
            self.log('[读数测试] 请先框选 Credit 数字区')
            return
        cfg = copy.deepcopy(self.cfg)
        def work(cancel):
            reader = self.core.Engine(cfg, {}, self.log, self.screen or self.core.Screen())
            return reader.stable_credit_value(timeout=1.2)
        def done(result):
            value, info = result
            text = f'Credit：{value if value is not None else "未知"}\n{info}'
            self.log('[读数测试] ' + text.replace('\n', '；'))
            messagebox.showinfo('读取测试', text, parent=self)
        self._diagnostic(work, done)

    def test_watch_rect(self):
        rect = copy.deepcopy(self.cfg.get('watch_rect'))
        if not rect:
            self.log('[噪声测试] 请先框选区域')
            return
        def work(cancel):
            screen = self.screen or self.core.Screen()
            first = screen.grab_rect(*rect)
            if cancel.wait(1.5):
                return None
            value = self.core.region_diff(first, screen.grab_rect(*rect))
            return '未知（没有有效红色笔画）' if value is None else f'{value:.2f}'
        self._diagnostic(work, lambda value: self.log(f'[噪声测试] 差异 {value}；不能单独证明有币或没币'))

    def auto_find_credit(self):
        def work(cancel):
            screen = self.screen or self.core.Screen()
            frame = screen.grab()
            candidates = [(x + screen.left, y + screen.top, w, h, val, info)
                          for x, y, w, h, val, info in self.core.find_digit_regions(frame)]
            return screen, frame, candidates
        def done(result):
            self.screen, self._candidate_frame, candidates = result
            self._show_candidates(candidates)
        self._diagnostic(work, done)

    def test_tab(self):
        if not self.guard_settings():
            return
        self.collect_cfg()
        cfg = copy.deepcopy(self.cfg)
        def work(cancel):
            keyword = str(cfg.get('game_window') or '')
            wins = self.core.list_windows(keyword)
            if not keyword or not wins:
                raise RuntimeError('找不到指定游戏窗口，未发送 Tab')
            self.core.activate_window(wins[0][0])
            if cancel.wait(.5):
                return None
            if keyword.lower() not in self.core.foreground_title().lower():
                raise RuntimeError('游戏没有获得前台焦点，未发送 Tab')
            holder = self.core.TabHolder(interval=max(.1, float(cfg.get('tab_repeat', 400)) / 1000))
            holder.start()
            try:
                deadline = time.monotonic() + 6
                while not cancel.wait(.08) and time.monotonic() < deadline:
                    if keyword.lower() not in self.core.foreground_title().lower():
                        break
            finally:
                holder.release()
                holder.join(timeout=.8)
            return True
        self._diagnostic(work, lambda value: self.log('[Tab 测试] 已松开 Tab'))

    def open_pending_samples(self):
        path = Path(self.core.DATA_DIR) / '待审核数字样本'
        path.mkdir(exist_ok=True)
        os.startfile(path)

    def review_pending_sample(self):
        if not self.guard_settings():
            return
        path = filedialog.askopenfilename(title='选一张待审核样本', initialdir=str(Path(self.core.DATA_DIR) / '待审核数字样本'),
                                         filetypes=[('PNG', '*.png')], parent=self)
        if not path:
            return
        image = self.core.load_image(path)
        if image is None:
            return
        win = tk.Toplevel(self)
        win.title('审核数字样本')
        photo = self.core.np_to_photoimage(image, zoom=3)
        label = ttk.Label(win, image=photo)
        label.image = photo
        label.pack(padx=12, pady=12)
        ttk.Label(win, text='按截图中实际数字填写；看不清则关闭，不加入识别库。').pack(padx=12)
        truth = tk.StringVar()
        ttk.Entry(win, textvariable=truth).pack(pady=6)
        def approve():
            if self.busy():
                return
            value = truth.get().strip()
            if not value.isascii() or not value.isdigit() or len(value) > 6:
                messagebox.showerror('真值无效', '请输入截图中实际看到的数字。', parent=win)
                return
            Path(self.core.DIGITS_DIR).mkdir(exist_ok=True)
            dst = self.core.next_sample_name(self.core.DIGITS_DIR, value)
            if self.core.save_image(dst, image):
                self.log(f'[审核] 加入正式样本库：{Path(dst).name}')
                self.rebuild_digit_bank()
                win.destroy()
        ttk.Button(win, text='确认真值并加入正式库', command=approve).pack(pady=10)

    def _freeze_then(self, build):
        if self._operation_busy or (self.collector and self.collector.is_alive()) or self._armed:
            self.deiconify()
            return
        self._operation_busy = True
        self._generation += 1
        generation = self._generation
        self._operation_cancel = threading.Event()
        cancel = self._operation_cancel
        lock = self.core.MouseLock()
        lock.__enter__()
        self._mouse_lock = lock
        keyword = str(self.cfg.get('game_window') or '')
        def work():
            wins = self.core.list_windows(keyword)
            if keyword and not wins:
                raise RuntimeError('找不到游戏窗口')
            if wins:
                self.core.activate_window(wins[0][0])
            screen = self.screen or self.core.Screen()
            if cancel.is_set():
                return None
            frame = self.core.wait_frames_stable(screen.grab)
            # Capture and scene comparison stay off the Tk thread.
            return screen, frame, self._screen_signature(frame)
        def done(result, error):
            try:
                if cancel.is_set() or generation != self._generation:
                    return
                if error or result is None:
                    self.log(f'[校准] {error or "已取消"}')
                    self.deiconify()
                    return
                self.screen, frame, signature = result
                self._pick_frame = frame
                self._frozen_view = (frame, signature)
                existing = set(self.winfo_children())
                build(frame)
                for picker in set(self.winfo_children()) - existing:
                    if isinstance(picker, (self.core.PointPicker, self.core.RegionPicker)):
                        self._picker_active = True
                        def closed(event, target=picker):
                            if event.widget is target:
                                self._picker_active = False
                        picker.bind('<Destroy>', closed, add='+')
            finally:
                lock.__exit__(None, None, None)
                if self._mouse_lock is lock:
                    self._mouse_lock = None
                if generation == self._generation:
                    self._operation_busy = False
                    self._refresh_run_buttons()
        self._background(work, done)

    def _warn_if_view_drifted(self):
        frozen = getattr(self, '_frozen_view', None)
        self._frozen_view = None
        if not frozen or self.screen is None:
            return True
        def work():
            now, _ = self._screen_signature(self.screen.grab())
            shift = self.core.scene_shift(frozen[1][0], now)
            return (shift or 0) * frozen[1][1]
        self._background(work, lambda value, error: self.log(
            f'[校准提醒] 画面位移约 {value:.0f}px；若未转视角可忽略。')
            if not error and value > self.core.DRIFT_SHIFT_PX else None)
        return True

    def on_close(self):
        if self._closed:
            return
        self.request_stop()
        def finish():
            active = ((self.engine and self.engine.is_alive()) or (self.collector and self.collector.is_alive()))
            if active:
                self.after(80, finish)
                return
            self._closed = True
            self._decode_jobs.put(None)     # 唤醒常驻解码线程让它退出
            self._clear_stop_hooks()
            self._close_overlay()
            for callback in self.tk.call('after', 'info'):
                self.after_cancel(callback)
            self.destroy()
        finish()


def pending_sample(core, crop, value, cfg=None, log=None):
    if not core.AUTO_LEARN_ALLOWED or not (cfg or {}).get('collect_pending', False):
        return
    if crop is None or not str(value).isascii() or not str(value).isdigit():
        return
    now = time.monotonic()
    if now - core._AUTO_STATE.get('pending', 0) < core.AUTO_COOLDOWN:
        return
    folder = Path(core.DATA_DIR) / '待审核数字样本'
    folder.mkdir(exist_ok=True)
    if len(list(folder.glob('*.png'))) >= 1000:
        return
    filename = core.next_sample_name(str(folder), 'auto' + str(value) + '_' + time.strftime('%Y%m%d_%H%M%S'))
    if core.save_image(filename, crop):
        core._AUTO_STATE['pending'] = now
        if log:
            log('[样本] 已保存到待审核区，不自动参与识别')


class CollectorFeatures:
    def rotate(self, dx):
        if self.stop_flag.is_set():
            return False
        self.engine.require_focus()
        return super().rotate(dx)
    def _out_dir(self):
        folder = Path(self.core.DATA_DIR) / '待审核数字样本'
        folder.mkdir(exist_ok=True)
        return str(folder)

    def run(self):
        callback = self.on_done
        self.on_done = None
        holder = None
        self.engine.watchdog_owner = self
        try:
            with KeepAwake(self.cfg.get('keep_awake', True), log=self.log):
                keyword = str(self.cfg.get('game_window') or '')
                wins = self.core.list_windows(keyword)
                if keyword and not wins:
                    raise RuntimeError('找不到游戏窗口')
                if wins:
                    self.core.activate_window(wins[0][0])
                self._wait(.4)
                if keyword and keyword.lower() not in self.core.foreground_title().lower():
                    raise RuntimeError('游戏没有获得前台焦点')
                self.engine._input_active = True
                if self.cfg.get('hold_tab', True):
                    holder = self.core.TabHolder(interval=max(.1, float(self.cfg.get('tab_repeat', 400)) / 1000))
                    holder.start()
                    self._wait(.3)
                super().run()
        except Exception as exc:
            self.reason = f'采集失败：{exc}'
            self.log(self.reason)
        finally:
            self.stop_flag.set()
            if holder:
                holder.release()
                holder.join(timeout=.8)
            if self.core.pdi:
                for button in ('left', 'right'):
                    try:
                        self.core.pdi.mouseUp(button=button)
                    except Exception:
                        pass
            self.engine._input_active = False
            if callback:
                callback(self.reason, len(self.saved))

    def read_at(self, rect):
        if not rect:
            return None
        frames = []
        deadline = time.monotonic() + max(.5, float(self.params.get('read_timeout', 1.2)))
        while not self.stop_flag.is_set() and time.monotonic() < deadline:
            crop = self.screen.grab_rect(*rect)
            if not self.core.digit_fit(crop)[0]:
                return None
            frames.append(crop)
            frames = frames[-3:]
            if len(frames) == 3:
                value, info, agree, total = self.core.decode_consensus(frames, min_agree=3, bank=self.core.get_digit_bank())
                if value is not None and str(value).isdigit():
                    return crop, str(value)
            self._wait(.08)
        return None


# Settings and diagnostic entrypoints share the same exclusion policy. Their
# worker callbacks are handled by AppFeatures, not by calling Tk from a thread.
def _guarded(method_name):
    def invoke(self, *args, **kwargs):
        if self.guard_settings():
            return getattr(super(AppFeatures, self), method_name)(*args, **kwargs)
    return invoke


for _name in ('pick_point', 'pick_credit_rect', 'pick_watch_rect', 'record',
              'test',
              'capture_credit_ref', 'clear_credit_rect', 'clear_credit_ref',
              'clear_watch_rect', 'collect_digit_sample', 'open_auto_collect',
              'import_part_samples', 'migrate_coordinates', 'activate_game', 'reload_tpl'):
    setattr(AppFeatures, _name, _guarded(_name))
