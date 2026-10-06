"""Timing, keep-awake and engine policies for the original SlotBot interface."""
from __future__ import annotations

import ctypes
import datetime as dt
import math
import os
import subprocess
import threading
import time
from vision import (ENOUGH_CREDIT, decode_credit_condition, decode_credit_candidate,
                    decode_refill_candidate)

SHANGHAI = dt.timezone(dt.timedelta(hours=8))
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 1
ES_DISPLAY_REQUIRED = 2


def parse_time(value):
    value = str(value or '').strip()
    if not value:
        return None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return dt.datetime.strptime(value, fmt).replace(tzinfo=SHANGHAI).timestamp()
        except ValueError:
            pass
    raise ValueError('时间请填写 YYYY-MM-DD HH:MM（北京时间），或留空。')


def schedule_target(cfg, wall=None):
    wall = time.time() if wall is None else wall
    delay = float(cfg.get('start_delay_minutes', 0) or 0)
    if not math.isfinite(delay) or delay < 0:
        raise ValueError('延迟开始分钟必须是非负有限数字。')
    target = parse_time(cfg.get('start_at'))
    if target is not None and delay:
        raise ValueError('延迟开始和指定开始时间请选择一种。')
    if target is not None and target <= wall:
        raise ValueError('开始时间已经过去，请重新填写。')
    stop = parse_time(cfg.get('stop_at'))
    if stop is not None and stop <= (target or wall + delay * 60):
        raise ValueError('停止时间必须晚于开始时间。')
    return target, delay * 60


def validate_config(cfg):
    counts = ('coin_count', 'draw_count', 'burn_count', 'rounds', 'retries',
              'bet_retries', 'scale_steps', 'credit_max_display')
    waits = ('coin_delay', 'step_delay', 'move_delay', 'last_coin_wait',
             'region_wait', 'tab_repeat', 'bet_confirm_timeout', 'max_minutes',
             'start_delay_minutes', 'shutdown_countdown')
    for key in counts + waits:
        value = float(cfg.get(key, 0) or 0)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f'{key} 必须是非负有限数字。')
        if key in counts and value != int(value):
            raise ValueError(f'{key} 必须是整数。')
    if float(cfg.get('bet_count', 3)) not in (1, 2, 3):
        raise ValueError('下注枚数必须是 1、2 或 3。')
    if not 0 < float(cfg.get('threshold', .8)) <= 1:
        raise ValueError('相似度阈值必须大于 0 且不超过 1。')
    if not all(math.isfinite(float(cfg.get(k, v))) for k, v in (('scale_min', .9), ('scale_max', 1.25))) or float(cfg.get('scale_min', .9)) <= 0 or float(cfg.get('scale_max', 1.25)) < float(cfg.get('scale_min', .9)):
        raise ValueError('多尺度范围不正确。')
    if cfg.get('completion_action', 'stop') not in ('stop', 'exit', 'close_game', 'shutdown'):
        raise ValueError('完成后动作不正确。')
    schedule_target(cfg)


class KeepAwake:
    """Acquire and release on the same thread; never alter the power plan."""
    def __init__(self, enabled=True, api=None, log=None):
        self.enabled = bool(enabled)
        self.api = api or ctypes.windll.kernel32.SetThreadExecutionState
        self.log = log or (lambda msg: None)
        self.acquired = False

    def __enter__(self):
        if self.enabled:
            self.acquired = bool(self.api(ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED))
            self.log('[保活] 已请求保持亮屏和禁止自动睡眠' if self.acquired else '[保活] 请求失败，请检查电源设置')
        return self

    def __exit__(self, *exc):
        if self.acquired:
            self.api(ES_CONTINUOUS)
            self.acquired = False
            self.log('[保活] 已释放，恢复系统正常电源管理')


class GracefulStop(Exception):
    pass


class TimerPolicy:
    def __init__(self, cfg, monotonic=None):
        self.started = time.monotonic() if monotonic is None else monotonic
        self.duration = float(cfg.get('max_minutes', 0) or 0) * 60
        self.stop_at = parse_time(cfg.get('stop_at'))

    def reason(self, monotonic=None, wall=None):
        mono = time.monotonic() if monotonic is None else monotonic
        wall = time.time() if wall is None else wall
        if self.duration > 0 and mono - self.started >= self.duration:
            return '已达到设定运行时长'
        if self.stop_at is not None and wall >= self.stop_at:
            return '已到指定停止时间'
        return ''


def request_shutdown(runner=None):
    """Countdown is owned by the GUI, /t > 0 would imply forced shutdown."""
    runner = runner or subprocess.run
    return runner(['shutdown.exe', '/s', '/t', '0'], check=True,
                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def desktop_unlocked():
    api = ctypes.windll.user32
    api.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    api.OpenInputDesktop.restype = ctypes.c_void_p
    api.CloseDesktop.argtypes = [ctypes.c_void_p]
    api.SwitchDesktop.argtypes = [ctypes.c_void_p]
    desktop = api.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return False
    try:
        return bool(api.SwitchDesktop(desktop))
    finally:
        api.CloseDesktop(desktop)


class EngineFeatures:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.finish_reason = ''
        self.completed_normally = False
        self.timer = None
        self.soft_stop = threading.Event()
        self._input_active = False
        self.debit_coins = 0
        self.confirmed_spins = 0
        self.bets_sent = 0
        self.credit_status = ''
        self._zero_reference_loaded = False
        self._zero_reference = None

    def request_stop(self):
        self.completed_normally = False
        self.finish_reason = '手动急停或操作取消'
        self.stop_flag.set()

    def condition_reason(self):
        return self.finish_reason if self.soft_stop.is_set() else (self.timer.reason() if self.timer else '')

    def boundary(self):
        if self.stop_flag.is_set():
            raise InterruptedError('已急停')
        reason = self.condition_reason()
        if reason:
            raise GracefulStop(reason)

    def require_focus(self):
        if self._input_active:
            keyword = str(self.cfg.get('game_window') or '')
            if keyword and keyword.lower() not in self.core.foreground_title().lower():
                self.stop_flag.set()
                raise InterruptedError('游戏失去前台焦点，停止输入')

    def click_at(self, x, y):
        self.require_focus()
        return super().click_at(x, y)

    def click_coin_at(self, x, y, move=False):
        """Original fast coin path: one positioning wait, one click per interval."""
        self.boundary()
        self.require_focus()
        if move:
            self.core.pdi.moveTo(x, y)
            self.pause(float(self.cfg['move_delay']) / 1000)
        self.boundary()
        self.require_focus()
        self.core.pdi.click()

    def drag_template(self, key, dy):
        self.require_focus()
        return super().drag_template(key, dy)

    def start_watchdog(self):
        core = self.core
        def watch():
            keys = core.stop_key_entries(self.cfg)
            previous = {vk: False for vk, _ in keys}
            taps, last = 0, 0.0
            while not self.stop_flag.is_set():
                for vk, name in keys:
                    down = core.key_pressed(vk)
                    if down and not previous[vk]:
                        now = time.monotonic()
                        taps = taps + 1 if now - last < 2 else 1
                        last = now
                        self.finish_reason = f'急停键 {name}'
                        self.stop_flag.set()
                    previous[vk] = down
                x, y = core.cursor_pos()
                corner = int(self.cfg.get('corner_stop_px', 40))
                if 0 <= x <= corner and 0 <= y <= corner:
                    self.finish_reason = '鼠标左上角急停'
                    self.stop_flag.set()
                reason = self.timer.reason() if self.timer else ''
                if reason:
                    self.finish_reason = reason
                    self.soft_stop.set()
                    self.state = '定时到达，当前局完成后停止'
                try:
                    self.require_focus()
                except InterruptedError as exc:
                    self.finish_reason = str(exc)
                self.stop_flag.wait(.01)
            # If input is blocked during cleanup, a second press may exit the process.
            if not taps:
                taps, last = 1, time.monotonic()
                previous = {vk: core.key_pressed(vk) for vk, _ in keys}
            deadline = time.monotonic() + 5
            owner = getattr(self, 'watchdog_owner', self)
            while owner.is_alive() and time.monotonic() < deadline:
                for vk, name in keys:
                    down = core.key_pressed(vk)
                    if down and not previous[vk]:
                        now = time.monotonic()
                        taps = taps + 1 if now - last < 2 else 1
                        last = now
                        if taps >= max(2, int(self.cfg.get('force_quit_presses', 2))):
                            core.key_up(core.VK_TAB, core.SCAN_TAB)
                            os._exit(0)
                    previous[vk] = down
                time.sleep(.08)
        threading.Thread(target=watch, daemon=True, name='slotbot-emergency').start()

    def insert_coins(self):
        self.boundary()
        count = int(self.cfg['coin_count'])
        if count == 0:
            self.log('[投币] 每轮投币数为 0，跳过投币')
            return True
        pos = self.locate('coin')
        if pos is None:
            return False
        for index in range(count):
            self.boundary()
            self.require_focus()
            move = index == 0
            if self.cfg.get('locate_mode') == 'image' and index and index % 10 == 0:
                pos = self.locate('coin', retries=3)
                if pos is None:
                    return False
                move = True
            self.click_coin_at(*pos, move=move)
            if self.stop_flag.is_set():
                return False
            self.coins_used += 1  # Sent insert events, not a claim of wallet deduction.
            self.pause(float(self.cfg['coin_delay']) / 1000)
        self.pause(float(self.cfg['last_coin_wait']) / 1000)
        self.log(f'[投币] 本轮按设定发送 {count} 次投币')
        return True

    def read_refill_credit(self):
        """Three consecutive independent Credit frames, at least 80 ms apart.

        A failed frame invalidates the whole attempt. Try again next spin without
        delaying normal gameplay for a long confirmation timeout.
        """
        try:
            bank = self.core.get_digit_bank()
            if not self._zero_reference_loaded:
                self._zero_reference_loaded = True
                path = self.core.resolve_ref_path(self.cfg.get('credit_ref'))
                self._zero_reference = self.core.load_image(path) if path else None
            candidate = None
            for index in range(3):
                self.boundary()
                if index:
                    self.pause(.08)
                crop = self.grab_credit() if self.cfg.get('credit_rect') else self._watch_region()
                value, info = decode_credit_condition(crop, bank, self._zero_reference,
                                                       int(self.cfg['bet_count']))
                if value is None:
                    return None, info
                if index and value != candidate:
                    return None, '连续帧的低币值或足够下注状态不一致'
                candidate = value
            return candidate, '3/3 连续帧一致；' + info
        except (InterruptedError, GracefulStop):
            raise
        except Exception as exc:
            return None, f'数字辅助读取不可用：{type(exc).__name__}'

    def credit_has_bet(self, credit):
        return credit == ENOUGH_CREDIT or (credit is not None and credit >= int(self.cfg['bet_count']))

    def _scene_probe(self):
        """Return a frame to judge whether the scene is still animating.

        Prefer the Credit region: on a win payout the Credit number animates up,
        so its motion is the most direct "the game is busy and ignoring clicks"
        signal for the empty-spin bug. Fall back to the watch region (reel/scene
        motion) when Credit isn't configured. Either is far more likely to be set
        than neither. Returns None if no region is available.
        """
        cr = self.cfg.get('credit_rect')
        if cr and len(cr) == 4 and self.screen is not None:
            try:
                x, y, w, h = [int(v) for v in cr]
                if w >= 4 and h >= 4:
                    return self.screen.grab_rect(x, y, w, h)
            except Exception:
                pass
        region = self._watch_region()
        if region is not None:
            return region
        return None

    def wait_win_settled(self, default_wait=0.3, max_wait=1.5, quiet_frames=2,
                         quiet_threshold=4.0, poll=0.05):
        """After the last stop button, hold until the watched region stops
        changing, so the next round's bet click isn't swallowed by the game's
        post-win "input ignored" window (that caused empty spins).

        Uses a *cheap pixel difference* on the small Credit (or watch) region
        rather than heavy optical flow: a settled region reads ~0, an animating
        win-payout digit reads large. This keeps the call fast even when invoked
        on every spin (the old DIS-optical-flow version was slow enough to stall
        the regression suite).

        The region is normally static after a non-win spin, so this returns
        after `default_wait`. After a real win the digit animates up and we hold
        until it settles — bounded by `max_wait` so latency is never unbounded.
        """
        from slotbot import frame_diff
        region = self._scene_probe()
        if region is None:
            # Nothing to watch: pay a fixed, bounded delay. We never add
            # unbounded latency and we never under-wait badly for a no-win spin.
            self.pause(default_wait)
            return False
        min_until = time.monotonic() + default_wait
        deadline = time.monotonic() + max_wait
        prev = region
        quiet = 0
        while time.monotonic() < deadline:
            self.boundary()
            cur = self._scene_probe()
            if cur is not None:
                diff = frame_diff(prev, cur)
                if diff is not None and diff <= quiet_threshold:
                    quiet += 1
                else:
                    quiet = 0
                prev = cur
            if time.monotonic() >= min_until and quiet >= quiet_frames:
                return True
            self.pause(poll)
        return False

    def place_bet(self):
        bet = int(self.cfg['bet_count'])
        steps = [('maxbet', 3)] if bet == 3 else [('bet', 1)] * bet
        self.bet_committed = False
        self.confirmed = False
        for key, _ in steps:
            if not self.bet_committed:
                self.boundary()
            if not self.click_template(key):
                return False
            self.bet_committed = True
            self.bets_sent += 1
        return True

    def spin(self, watch_change=False, credit_reading=None):
        self.boundary()
        self.bet_committed = False
        credit, info = credit_reading if credit_reading is not None else self.read_refill_credit()
        enough = self.credit_has_bet(credit)
        status = ('可靠低币' if credit is not None and not enough else
                  '确认足够下注，继续抽奖' if enough else '按次数继续')
        if status != self.credit_status:
            self.log(f'[数字辅助] {status}：{info}')
            self.credit_status = status
        if credit is not None and not enough:
            self.log(f'[补币基点] 可靠 Credit={credit}，结束本轮并开始下一轮投币')
            return (True, False) if watch_change else False
        if not self.place_bet():
            return (False, True) if watch_change else False
        # Finish the sent bet's action sequence even if the normal timer expires.
        self.pause(float(self.cfg['step_delay']) / 1000)
        if not self.drag_template('lever', self.cfg['pull_px']):
            return (False, True) if watch_change else False
        for key in ('btn1', 'btn2', 'btn3'):
            self.pause(float(self.cfg['step_delay']) / 1000)
            if not self.click_template(key):
                return (False, True) if watch_change else False
        # Let a win payout finish so the next round's bet click isn't swallowed
        # by the "screen ignores input" window — that was causing empty spins.
        self.wait_win_settled()
        self.spins_done += 1
        return (True, True) if watch_change else True

    def run(self):
        core = self.core
        holder = None
        self.started_at = time.time()
        try:
            self.timer = TimerPolicy(self.cfg)
            with KeepAwake(self.cfg.get('keep_awake', True), log=self.log):
                if core.pdi is None:
                    raise RuntimeError('没有鼠标输入依赖')
                self.boundary()
                self.start_watchdog()
                kw = str(self.cfg.get('game_window') or '')
                if kw:
                    wins = core.list_windows(kw)
                    if not wins:
                        raise RuntimeError('找不到游戏窗口')
                    core.activate_window(wins[0][0])
                    self.pause(.4)
                    if kw.lower() not in core.foreground_title().lower():
                        raise RuntimeError('游戏未获得前台焦点')
                self._input_active = True
                if self.cfg.get('hold_tab'):
                    holder = core.TabHolder(interval=max(.1, float(self.cfg.get('tab_repeat', 400)) / 1000))
                    holder.start()
                    self.pause(.5)
                while not self.stop_flag.is_set():
                    self.boundary()
                    credit, info = self.read_refill_credit()
                    if self.credit_has_bet(credit):
                        self.log('[余币] 已确认足够一次下注，本轮不投币，继续抽奖')
                    else:
                        self.state = '投币'
                        why = f'已确认 Credit={credit} 不足一注' if credit is not None else f'数字未知，使用固定投币流程：{info}'
                        self.log('[投币原因] ' + why)
                        if not self.insert_coins():
                            raise RuntimeError('投币检查或定位失败')
                    need_coins = False
                    self.state = '抽奖'
                    for _ in range(int(self.cfg['draw_count'])):
                        self.boundary()
                        ok, played = self.spin(watch_change=True)
                        if not ok:
                            raise RuntimeError('定位或抽奖动作失败，请检查现场')
                        if not played:
                            need_coins = True
                            break
                    if not need_coins:
                        self.state = '空转清币'
                        unknown_spins = 0
                        fallback = int(self.cfg['burn_count'])
                        extra_spins = 0
                        while True:
                            self.boundary()
                            reading = self.read_refill_credit()
                            credit, info = reading
                            if credit is None:
                                if unknown_spins >= fallback:
                                    self.log(f'[空转] 连续 {unknown_spins} 次读数未知，已走完设定空转；下一轮重新检查再决定投币')
                                    break
                                unknown_spins += 1
                            else:
                                unknown_spins = 0
                                if self.credit_has_bet(credit) and (extra_spins == 0 or extra_spins % 10 == 0):
                                    self.log('[余币] 已确认足够一次下注，继续清币，不按空转次数强制投币')
                            ok, played = self.spin(watch_change=True, credit_reading=reading)
                            if not ok:
                                raise RuntimeError('清币动作失败，请检查现场')
                            if not played:
                                break
                            extra_spins += 1
                    self.rounds_done += 1
                    limit = int(self.cfg.get('rounds', 0))
                    if limit and self.rounds_done >= limit:
                        self.finish_reason = '已完成设定轮数'
                        self.completed_normally = True
                        break
        except GracefulStop as exc:
            self.finish_reason = str(exc)
            self.completed_normally = True
        except InterruptedError as exc:
            self.finish_reason = self.finish_reason or str(exc)
        except Exception as exc:
            self.finish_reason = f'异常停止：{type(exc).__name__}: {exc}'
        finally:
            if holder:
                holder.release()
                holder.join(timeout=.8)
            if core.pdi is not None and self._input_active:
                for button in ('left', 'right'):
                    try:
                        core.pdi.mouseUp(button=button)
                    except Exception:
                        pass
            self._input_active = False
            self.state = 'idle'
            if self.stop_flag.is_set():
                self.completed_normally = False
            self.log(f'[结束] {self.finish_reason or "已停止"}；完成 {self.rounds_done} 轮 / {self.spins_done} 局；'
                     f'发送投币 {self.coins_used} 次 / 下注按钮 {self.bets_sent} 次（不代表确认扣币）')
