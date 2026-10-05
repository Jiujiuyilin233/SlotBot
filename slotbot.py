# -*- coding: utf-8 -*-
"""
LuraSlot 图像识别自动化助手
============================================================
和按键精灵脚本功能一致（投币 -> 下注 -> 拉杆 -> 三个按钮 -> 清币 -> 循环），
支持固定坐标和带搜索范围的彩色模板定位；Credit 使用保守的七段解码。
明显改变视角后需要重新取点或录模板。

依赖：mss, opencv-python, numpy, pydirectinput, keyboard
运行：双击 启动.bat  或  python slotbot.py
停止：F12（或点界面上的「停止」；也可把鼠标猛甩到屏幕左上角）
"""

import base64
import ctypes
import ctypes.wintypes  # noqa: F401  (POINT 结构体)
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
from tkinter import filedialog, messagebox, simpledialog, ttk

import cv2
import numpy as np

# ---------------- DPI 感知：让 tk / 截图 / 鼠标统一用物理像素 ----------------
# 打包成 exe 时由 LuraSlot.manifest 声明 PerMonitorV2（更早、更可靠）；
# 这里是为源码直接运行时的兜底，两者保持一致。
DPI_AWARENESS = 0  # 0=unaware 1=system 2=per-monitor 3=per-monitor-v2
try:
    DPI_AWARENESS = ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(
        ctypes.c_void_p(-4))
except Exception:
    pass
if not DPI_AWARENESS:
    try:  # Win10 1703+：PerMonitorV2，坐标虚拟化问题最小
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    try:
        DPI_AWARENESS = ctypes.windll.user32.GetAwarenessFromDpiAwarenessContext(
            ctypes.c_void_p(-4))
    except Exception:
        DPI_AWARENESS = 0
# manifest 已经声明过感知时，SetProcessDpiAwarenessContext 会失败（awareness 已定），
# 这不代表没生效，所以最终状态一律以查询结果为准。
DPI_AWARE = DPI_AWARENESS in (2, 3)

import mss  # noqa: E402

try:
    import pydirectinput as pdi

    pdi.FAILSAFE = True  # 鼠标甩到左上角即中止，保命用
    pdi.PAUSE = 0.0
except Exception:  # pragma: no cover
    pdi = None

try:
    import keyboard as kb
except Exception:  # pragma: no cover
    kb = None


# ---------------- Win32 底层按键（不依赖第三方库，按住 Tab 靠它） ----------------
user32 = ctypes.windll.user32

KEYEVENTF_KEYUP = 0x0002
VK_TAB = 0x09
SCAN_TAB = 0x0F

# ---------------- 界面配色（浅色主题，统一在这里维护） ----------------
BG = "#f4f6f8"        # 窗口底色
CARD = "#ffffff"      # 卡片底色
BORDER = "#dfe4ea"    # 描边
INK = "#1f2733"       # 主要文字
MUTED = "#6b7684"     # 次要文字
ACCENT = "#2563eb"    # 主色 / 有币
OK = "#0f9d58"        # 通过 / 没币
WARN = "#c47f0a"      # 提示 / 存疑
DANGER = "#d93025"    # 错误 / 需要重做

# 视角漂移提醒阈值：画面整体位移超过这个**原图像素数**才提示。
# 只用来「提个醒」，不拦截操作，所以宁可迟钝也不能误报——
# 误报的提醒会让人忽略所有警告，真出事时反而不信。
# 实测基线（2560×1600 原图）：同视角不同动画 ≈3.6px、亮度噪声 <0.5px；
# 用户在游戏里"没动手"时通常在 10px 以内。取 25px：明显转了才会提示。
DRIFT_SHIFT_PX = 25.0

# 默认不含 Pause。Windows 用 VK_PAUSE 表示"正在拖动窗口"，
# 运行中点一下标题栏拖动窗口就会按下它，导致误停（用户实踩）。
DEFAULT_STOP_KEYS = ("F12", "End")

# 可自定义的停止热键：名字 -> 虚拟键码。故意不含 Ctrl / Alt / Shift，
# 单按修饰键就急停太容易误触。
VK_BY_NAME = {
    "Esc": 0x1B, "Enter": 0x0D, "Space": 0x20, "Backspace": 0x08, "Tab": 0x09,
    "Home": 0x24, "End": 0x23, "Insert": 0x2D, "Delete": 0x2E,
    "PageUp": 0x21, "PageDown": 0x22,
    "Pause": 0x13, "ScrollLock": 0x91, "PrintScreen": 0x2C,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
}
for _i in range(1, 13):
    VK_BY_NAME[f"F{_i}"] = 0x6F + _i
for _i in range(10):
    VK_BY_NAME[str(_i)] = 0x30 + _i
for _i in range(26):
    VK_BY_NAME[chr(ord("A") + _i)] = 0x41 + _i
for _i in range(10):
    VK_BY_NAME[f"Num{_i}"] = 0x60 + _i

NAME_BY_VK = {v: k for k, v in VK_BY_NAME.items()}
EMERGENCY_NAMES = dict(NAME_BY_VK)

# 传给 keyboard 模块的名字（它用小写、多词要空格）
KB_NAME = {
    "PageUp": "page up", "PageDown": "page down", "ScrollLock": "scroll lock",
    "PrintScreen": "print screen", "Up": "up", "Down": "down",
    "Left": "left", "Right": "right", "Space": "space", "Esc": "esc",
    "Enter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Home": "home", "End": "end", "Insert": "insert", "Delete": "delete",
    "Pause": "pause",
}


def stop_key_entries(cfg):
    """当前配置的停止热键 -> [(vk, 显示名), ...]。

    配置里存的是名字列表（stop_keys）。老配置只有 extra_stop_key 时也能兼容。
    """
    raw = cfg.get("stop_keys")
    names = []
    if isinstance(raw, (list, tuple)):
        names = [str(n) for n in raw]
    elif isinstance(raw, str) and raw.strip():
        names = [p.strip() for p in raw.split(",")]
    else:
        names = list(DEFAULT_STOP_KEYS)
    extra = str(cfg.get("extra_stop_key", "") or "").strip()
    if extra:
        names.append(extra)
    out, seen = [], set()
    for n in names:
        n = n.strip()
        vk = VK_BY_NAME.get(n) or VK_BY_NAME.get(n.title()) or VK_BY_NAME.get(n.upper())
        if vk is None or vk in seen:
            continue
        seen.add(vk)
        out.append((vk, NAME_BY_VK[vk]))
    return out or [(VK_BY_NAME[k], k) for k in DEFAULT_STOP_KEYS]


def key_pressed(vk):
    """直接轮询按键状态（比全局钩子可靠，钩子注册失败时这个仍然有效）"""
    try:
        return bool(user32.GetAsyncKeyState(vk) & 0x8000)
    except Exception:
        return False


def cursor_pos():
    try:
        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y
    except Exception:
        return (-1, -1)


def key_down(vk, scan):
    user32.keybd_event(vk, scan, 0, 0)


def key_up(vk, scan):
    user32.keybd_event(vk, scan, KEYEVENTF_KEYUP, 0)


def list_windows(keyword):
    """按标题关键字查找可见窗口，返回 [(hwnd, title), ...]"""
    if not keyword:
        return []
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if keyword.lower() in buf.value.lower():
                found.append((hwnd, buf.value))
        return True

    user32.EnumWindows(_cb, 0)
    return found


def activate_window(hwnd):
    """把窗口切到前台并恢复显示"""
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    except Exception:
        pass
    ok = False
    try:
        ok = bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        pass
    if not ok:
        try:
            user32.SwitchToThisWindow(hwnd, True)
            ok = True
        except Exception:
            pass
    return ok


def foreground_title():
    """当前前台窗口标题"""
    try:
        hwnd = user32.GetForegroundWindow()
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value
    except Exception:
        return ""


class TabHolder(threading.Thread):
    """持续按住 Tab：每隔 interval 秒重发一次 keydown。
    这样即使开始时焦点在工具窗口上，等游戏拿到焦点后也能收到按下的 Tab。"""

    def __init__(self, vk=VK_TAB, scan=SCAN_TAB, interval=0.4, log=None):
        super().__init__(daemon=True)
        self.vk, self.scan, self.interval, self.log = vk, scan, interval, log
        self.stop_flag = threading.Event()

    def run(self):
        while not self.stop_flag.is_set():
            key_down(self.vk, self.scan)
            self.stop_flag.wait(self.interval)
        key_up(self.vk, self.scan)

    def release(self):
        self.stop_flag.set()
        try:
            self.join(timeout=1.5)
        except Exception:
            pass
        key_up(self.vk, self.scan)


# 打包成 exe 后 __file__ 指向临时解包目录，配置/模板必须以 exe 所在目录为准，
# 否则用户会在 C:\Users\xxx\AppData\Local\Temp 里找不到自己的 config.json。
FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

# 版本与署名：底栏、窗口标题、--diag、使用说明、README 都从这里取
APP_VERSION = "1.0.0"
APP_AUTHORS = "Jiujiuyilin_233 / 绝世好裤裆"
# 允许用环境变量指定数据目录（自检/测试时隔离，避免覆盖用户真实配置）
DATA_DIR = os.environ.get("SLOTBOT_DATA_DIR") or APP_DIR
TPL_DIR = os.path.join(DATA_DIR, "templates")
CFG_PATH = os.path.join(DATA_DIR, "config.json")
# 真实数字样本：用户按屏幕实际读数采集，文件名里带真值。
# 这是唯一能证明"识别到底准不准"的数据，比任何合成图都有用。
DIGITS_DIR = os.path.join(DATA_DIR, "digits")
CREDIT_REF_PATH = os.path.join(DATA_DIR, "credit_zero.png")  # 兼容旧版参考图位置

# 数字样本模板库（自我训练）：用带真值的实拍样本给段码解码做交叉验证。
# 样本来源：程序内「采集数字样本」(digits/)、运行中自动采集 (digits/auto*.png)、
# 用户自截 (数字截图/，支持 41-2.png 多视角命名和任意子目录)。零依赖、纯本地。
SAMPLE_DIRS = (DIGITS_DIR,
               os.path.join(APP_DIR, "digits"),
               os.path.join(APP_DIR, "数字截图"))
_SAMPLE_NAME_RE = re.compile(r'^(?:v|auto)?(\d+)(?:[_-].*)?$')
AUTO_MAX_PER_VALUE = 40      # 每个数值最多自动存多少张，防止磁盘被刷爆
AUTO_COOLDOWN = 60.0         # 两次自动采集的最小间隔（秒）
# 总闸：回归测试会关掉它。测试跑的是合成图，一旦让它自动采集，
# 合成字形就会被当成真值写进用户的样本库、再被模板库学走，
# 反过来把真机读数判成未知——测试绝不能产生这种副作用。
AUTO_LEARN_ALLOWED = True


def sample_truth_of(filename):
    """样本文件名 -> 真值：41.png / 41-2.png / v41_时间 / auto41_时间 都认。"""
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _SAMPLE_NAME_RE.match(stem)
    return m.group(1) if m else None


def load_digit_samples():
    """收集所有带真值的数字样本 -> [(真值, 图), ...]。

    cv2.imread 读不了中文路径（Windows），必须 np.fromfile + imdecode。
    """
    out = []
    seen = set()
    for d in SAMPLE_DIRS:
        if not os.path.isdir(d) or d in seen:
            continue
        seen.add(d)
        for cur, _subs, files in os.walk(d):
            for fn in sorted(files):
                if not fn.lower().endswith(".png"):
                    continue
                truth = sample_truth_of(fn)
                if not truth:
                    continue
                try:
                    buf = np.fromfile(os.path.join(cur, fn), dtype=np.uint8)
                    img = cv2.imdecode(buf, cv2.IMREAD_COLOR) if buf.size else None
                except OSError:
                    img = None
                if img is not None and img.size:
                    out.append((truth, img))
    return out


_DIGIT_BANK = None
_DIGIT_BANK_STAMP = 0.0
_BANK_TTL = 600.0          # 模板库有效期（秒）
_BANK_REFRESHING = False   # 后台重建中（避免每次读币都起一个线程）


def _build_bank_now():
    """同步重建模板库：只有在『手里一份都没有』时才允许走这条路。"""
    global _DIGIT_BANK, _DIGIT_BANK_STAMP
    try:
        bank = build_template_bank(load_digit_samples())
    except Exception:  # noqa: BLE001  模板库坏了不能拖死读币
        bank = None
    if bank is None:
        _DIGIT_BANK = _DIGIT_BANK if _DIGIT_BANK is not None else {}
        _DIGIT_BANK_STAMP = time.time()
        return _DIGIT_BANK
    _DIGIT_BANK = bank
    _DIGIT_BANK_STAMP = time.time()
    return _DIGIT_BANK


def _rebuild_bank_async():
    global _BANK_REFRESHING
    try:
        _build_bank_now()
    finally:
        _BANK_REFRESHING = False


def get_digit_bank(force=False):
    """当前模板库（懒加载，10 分钟过期；force=True 立即重建）。

    库里没有任何模板时返回空 dict——所有调用点都必须把「空库」当成
    「没有模板校验」，走回纯段码路径，绝不能因此拒绝读数。

    **绝不能在读币的关键路径上冷启动建库**：几百张样本解码一遍要 0.2 秒，
    而下注确认的整轮超时只有 0.36 秒（回归里就是这么挂的）。所以过期时
    先把手里那份旧的返回去用，另起线程重建。
    """
    global _BANK_REFRESHING
    if force:
        return _build_bank_now()
    now = time.time()
    bank = _DIGIT_BANK
    if bank is not None:
        if now - _DIGIT_BANK_STAMP <= _BANK_TTL:
            return bank
        if not _BANK_REFRESHING:
            _BANK_REFRESHING = True
            threading.Thread(target=_rebuild_bank_async, daemon=True).start()
        return bank
    return _build_bank_now()


def warm_digit_bank():
    """预热模板库：把第一次建库的开销挪到读币之外（引擎创建时调用）。"""
    try:
        get_digit_bank()
    except Exception:  # noqa: BLE001
        pass


def bank_summary(bank):
    """模板库规模的可读描述。"""
    if not bank:
        return "空（还没有样本）"
    per = "、".join(f"{ch}×{len(v)}" for ch, v in sorted(bank.items()))
    total = sum(len(v) for v in bank.values())
    return f"共 {total} 个模板（{per}）"


_AUTO_STATE = {"last": 0.0}


def auto_collect_credit(crop, value, cfg=None, log=None):
    """自我训练采集：把稳定可信的读数存成新样本，喂给模板库。

    门槛（缺一不存——存错一张等于污染模板库）：
    ① 配置开了 auto_learn（默认开）；
    ② 值是纯数字（调用方还要保证它是「连续三帧一致」的稳定读数）；
    ③ 距上次自动采集 ≥ AUTO_COOLDOWN 秒，且该数值存量 < AUTO_MAX_PER_VALUE。
    """
    if not AUTO_LEARN_ALLOWED:
        return None
    if cfg is not None and not cfg.get("auto_learn", True):
        return None
    if crop is None or getattr(crop, "size", 0) == 0 or not str(value).isdigit():
        return None
    now = time.time()
    if now - _AUTO_STATE["last"] < AUTO_COOLDOWN:
        return None
    try:
        os.makedirs(DIGITS_DIR, exist_ok=True)
        prefix = f"auto{value}_"
        count = sum(1 for fn in os.listdir(DIGITS_DIR) if fn.startswith(prefix))
        if count >= AUTO_MAX_PER_VALUE:
            return None
        name = prefix + time.strftime("%Y%m%d_%H%M%S") + ".png"
        path = os.path.join(DIGITS_DIR, name)
        if not cv2.imwrite(path, crop):
            return None
    except Exception:  # noqa: BLE001  采集失败绝不能影响运行
        return None
    _AUTO_STATE["last"] = now
    if log:
        log(f"[自训练] 已自动存样本 {name}（模板库同步重建）")
    return path


# ---------------- 按钮多模板（部件多视角样本）----------------
# 「其他截图/」里是用户拍的部件多视角特写。按钮本体（红色方块）互相长得
# 一模一样，能区分它们的只有**上方的类别文字**（Bet / MaxBet）和**位置**，
# 所以导入时必须把标签一起裁进模板——只裁按钮本体等于把 bet 和 maxbet
# 训练成同一个东西。
BUTTON_SAMPLE_DIRS = {
    "bet": "bet",
    "maxbet": "maxbet",
    "硬币堆（投币处）截图": "coin",
    "摇杆": "lever",
}
PAIR_SAMPLE_DIRS = {
    "带文字标题的bet &maxbet": ("bet", "maxbet"),
    "不带文字标题的bet &maxbet": ("bet", "maxbet"),
}
TRIPLE_SAMPLE_DIRS = {
    "按钮左，中，右": ("btn1", "btn2", "btn3"),
}


def _red_mask(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return (((hsv[:, :, 0] <= 10) | (hsv[:, :, 0] >= 170)) &
            (hsv[:, :, 1] >= 80) & (hsv[:, :, 2] >= 50)).astype(np.uint8)


def red_button_boxes(img, min_frac=.006, min_w=24, min_h=14):
    """找图里的红色实体按钮，按从左到右返回 [(x, y, w, h), ...]。

    两个筛选条件都是被真实样本逼出来的，别再按"直觉"收紧：
    * 宽高比上限必须放到 5：斜视角下按钮会被压成扁条，实测
      bet & maxbet 合照里有三张的按钮宽高比是 3.1 / 3.6，
      卡在 3.0 上就直接漏掉一个按钮，只剩一个就切不出左右。
    * 面积小于最大块 35% 的丢弃：合照边角常有红色装饰条，
      不丢会把装饰当成第三个按钮。
    """
    if img is None or img.size == 0:
        return []
    mask = _red_mask(img)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _lab, stats, _cent = cv2.connectedComponentsWithStats(mask, 8)
    total = float(img.shape[0] * img.shape[1])
    boxes = []
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in stats[i])
        if a < total * min_frac or w < min_w or h < min_h:
            continue
        ar = w / max(1, h)
        if not (0.30 <= ar <= 5.0):
            continue
        boxes.append((x, y, w, h))
    if boxes:
        biggest = max(b[2] * b[3] for b in boxes)
        boxes = [b for b in boxes if b[2] * b[3] >= biggest * .35]
    boxes.sort(key=lambda b: b[0])
    return boxes


def crop_button_with_label(img, box):
    """把红按钮连同它上方的类别标签一起裁出来。

    上方只留 0.5 个按钮高：连通域经常已经把标题字框进来了，再往上留一大截
    只会把空白裁进模板，白拉低匹配分。
    """
    x, y, w, h = box
    H, W = img.shape[:2]
    padx = int(w * .15) + 4
    x0 = max(0, x - padx)
    x1 = min(W, x + w + padx)
    y0 = max(0, y - int(h * .5) - 6)
    y1 = min(H, y + h + int(h * .25) + 4)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    return img[y0:y1, x0:x1]


def _save_variant(dst_key_dir, src_name, img):
    os.makedirs(dst_key_dir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src_name))[0]
    path = os.path.join(dst_key_dir, stem + ".png")
    i = 2
    while os.path.exists(path):
        path = os.path.join(dst_key_dir, f"{stem}-{i}.png")
        i += 1
    return path if cv2.imwrite(path, img) else None


def import_button_samples(src, dst=None, log=None):
    """把部件多视角截图导入为多模板：templates/<key>/<来源名>.png。

    整图直导：bet / maxbet / 硬币堆 / 摇杆（每张就是单个部件的特写）。
    合照裁剪：bet & maxbet 同框 → 检测红色按钮，**左边 bet、右边 maxbet**
    （按钮本体没区别，左右位置是唯一线索）；按钮左中右同框 → btn1/2/3。
    全局布局图不导（整图不是模板）。

    返回 {key: 新增张数}。
    """
    dst = dst or TPL_DIR
    if not os.path.isdir(src):
        return {}
    got = {}

    def add(key, img, src_name):
        if img is None or getattr(img, "size", 0) == 0:
            return
        path = _save_variant(os.path.join(dst, key), src_name, img)
        if path:
            got[key] = got.get(key, 0) + 1
            if log:
                log(f"[部件] {key} ← {os.path.basename(path)}")

    def whole(key, d):
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".png"):
                continue
            img = load_image(os.path.join(d, fn))
            if img is None or min(img.shape[:2]) < 30:
                continue
            add(key, img, fn)

    def grouped(d, keys):
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".png"):
                continue
            img = load_image(os.path.join(d, fn))
            if img is None:
                continue
            boxes = red_button_boxes(img)
            if len(boxes) < len(keys):
                if log:
                    log(f"[部件] 跳过 {fn}：只认出 {len(boxes)} 个红按钮（需要 {len(keys)} 个）")
                continue
            if len(boxes) > len(keys):
                # 多认出来的多半是别处的红色装饰，留面积最大的几个
                boxes = sorted(boxes, key=lambda b: -(b[2] * b[3]))[:len(keys)]
            boxes.sort(key=lambda b: b[0])
            for key, box in zip(keys, boxes):
                crop = crop_button_with_label(img, box)
                if crop is None:
                    continue
                add(key, crop, f"{os.path.splitext(fn)[0]}_{key}")

    for sub, key in BUTTON_SAMPLE_DIRS.items():
        d = os.path.join(src, sub)
        if os.path.isdir(d):
            whole(key, d)
    for sub, keys in PAIR_SAMPLE_DIRS.items():
        d = os.path.join(src, sub)
        if os.path.isdir(d):
            grouped(d, keys)
    for sub, keys in TRIPLE_SAMPLE_DIRS.items():
        d = os.path.join(src, sub)
        if os.path.isdir(d):
            grouped(d, keys)
    return got


def load_template_variants(key, tpl_dir=None):
    """一个 key 的全部模板：主模板 templates/<key>.png + 多视角 templates/<key>/*.png。"""
    tpl_dir = tpl_dir or TPL_DIR
    out = []
    p = os.path.join(tpl_dir, f"{key}.png")
    if os.path.exists(p):
        img = load_image(p)
        if img is not None and img.size:
            out.append(img)
    d = os.path.join(tpl_dir, key)
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.lower().endswith(".png"):
                continue
            img = load_image(os.path.join(d, fn))
            if img is not None and img.size:
                out.append(img)
    return out


# ---------------- 自动采集（投币→截图→变角度）----------------
def truth_gate(prev, val, max_step=3):
    """自动采集的真值闸门：投币只会让 Credit 变多。

    返回 (ok, 原因)。宁可停也不把错值写进样本库——
    真值错了，模板库学到的就是错的。
    """
    val = str(val)
    if not val.isdigit():
        return False, "读不出可信数字"
    n = int(val)
    prev = None if prev in (None, "") else str(prev)
    if prev is None:
        return True, ""
    if not prev.isdigit():
        return True, ""
    p = int(prev)
    if n == p:
        return False, "读数没变（可能没投进去，或已到显示上限）"
    if n < p:
        return False, "读数变小了（可能被清币或误读）"
    if n - p > max_step:
        return False, f"一次涨了 {n - p}（超过预期步长 {max_step}），真值不可信"
    return True, ""


def next_sample_name(folder, truth):
    """给定真值挑一个**不覆盖已有文件**的名字：41.png → 41-2.png → 41-3.png。"""
    os.makedirs(folder, exist_ok=True)
    truth = str(truth)
    if not os.path.exists(os.path.join(folder, truth + ".png")):
        return os.path.join(folder, truth + ".png")
    i = 2
    while os.path.exists(os.path.join(folder, f"{truth}-{i}.png")):
        i += 1
    return os.path.join(folder, f"{truth}-{i}.png")


def pick_credit_candidate(cands, near_xy=None, max_dist_frac=.25, screen_w=0,
                          want=None):
    """从扫描候选里挑 Credit 区域。

    优先用**读数**认（候选自带解码结果，等于真值的才可能是 Credit），
    读数对不上时再退回"离上一个位置最近"——机器上不止一个数码管，
    转完视角光靠位置会挑错。给了 want 却一个都对不上时，返回最近的那个，
    由调用方再解码复核，不在这里替它拍板。
    """
    if not cands:
        return None
    pool = list(cands)
    if want is not None:
        same = [c for c in pool if len(c) > 4 and str(c[4]) == str(want)]
        if same:
            pool = same
    if near_xy is None:
        return pool[0]
    limit = (screen_w or 1) * max_dist_frac
    best, best_d = None, None
    for c in pool:
        x, y, w, h = c[:4]
        cx, cy = x + w / 2.0, y + h / 2.0
        d = ((cx - near_xy[0]) ** 2 + (cy - near_xy[1]) ** 2) ** .5
        if best_d is None or d < best_d:
            best, best_d = c, d
    if best is not None and best_d <= max(limit, 120):
        return best
    return None


def parse_seconds(text, default):
    """解析「秒」输入，支持小数。int() 会把 0.8 截成 0（真踩过），所以单独写。"""
    try:
        v = float(str(text).strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def rotate_plan(base_px, mults):
    """把「拖动幅度 × 倍数」展开成视角拖动序列：对称、去零、去重。"""
    out = []
    for m in mults:
        try:
            dx = int(round(float(base_px) * float(m)))
        except (TypeError, ValueError):
            continue
        if dx and dx not in out:
            out.append(dx)
    return out


class LegacyAutoCollector(threading.Thread):
    """自动采集样本：投币 → 读真值 → 截图（正面 + 若干角度）→ 再投币，循环。

    用户要的是「0 到 99 挨个截」，手点 100 次太累；机器自己投币涨数、
    自己截图就行。三个硬约束决定了它的写法：

    1. **文件名就是真值**。真值错了，模板库学到的就是错的，所以每一步都过
       truth_gate：读数没变 / 变小 / 一次涨太多，统统停下来，绝不硬写。
    2. **转视角后 Credit 区域会挪位置**。固定 rect 就截空了，所以每转一次
       都要重新扫描找面板，并且**用"重新读出来还是同一个数"来确认找对了**，
       而不是只看位置近不近。
    3. **转出去必须转回来**。回不到正面就停——后面所有样本的角度都不是
       用户预期的，继续采只会污染样本库。

    这是唯一会主动给游戏发鼠标输入的辅助功能，所以开始前有明确的确认。
    """

    def __init__(self, cfg, tpls, variants, log, screen, params, on_done=None):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.cfg["locate_mode"] = cfg.get("locate_mode", "coord")
        self.log = log
        self.screen = screen
        self.params = params
        self.on_done = on_done
        self.stop_flag = threading.Event()
        # 复用 Engine 的定位/点击，不再复制一份输入逻辑
        self.engine = Engine(self.cfg, tpls, log, screen, variants)
        self.engine.stop_flag = self.stop_flag
        self.saved = []
        self.reason = ""

    # ---------- 基础 ----------
    def _wait(self, seconds):
        return self.stop_flag.wait(max(0, float(seconds)))

    def _out_dir(self):
        return self.params.get("out_dir") or DIGITS_DIR

    def _rect_now(self):
        r = self.cfg.get("credit_rect")
        return [int(v) for v in r] if r and len(r) == 4 else None

    def _center(self, rect):
        return (rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0)

    def read_at(self, rect):
        """在给定区域连读三帧，三帧一致才认。"""
        if not rect or self.screen is None:
            return None
        last, run = None, 0
        deadline = time.monotonic() + max(.5, float(self.params.get("read_timeout", 1.2)))
        while time.monotonic() < deadline and not self.stop_flag.is_set():
            try:
                crop = self.screen.grab_rect(*rect)
            except Exception:
                return None
            val, _info = decode_led_fused(crop, get_digit_bank())
            if val and str(val).isdigit():
                run = run + 1 if val == last else 1
                last = val
                if run >= 3:
                    return crop, val
            else:
                run, last = 0, None
            self._wait(.08)
        return None

    def save(self, crop, truth, tag):
        """按真值存样本；同名不覆盖，往后编号。"""
        name = f"{truth}_{tag}" if tag else str(truth)
        path = next_sample_name(self._out_dir(), name)
        if save_image(path, crop):
            self.saved.append(path)
            self.log(f"[自动采集] 存 {os.path.basename(path)}")
            return True
        self.log(f"[自动采集] 保存失败：{path}")
        return False

    def rotate(self, dx):
        """按住鼠标右键在画面中心水平拖动，转动视角（VRChat 桌面端默认操作）。"""
        if not dx:
            return True
        cx = int(self.screen.left + self.screen.size[0] * .5)
        cy = int(self.screen.top + self.screen.size[1] * .5)
        steps = max(4, min(24, abs(dx) // 20))
        pdi.moveTo(cx, cy)
        self._wait(.05)
        try:
            pdi.mouseDown(button="right")
            for i in range(1, steps + 1):
                if self.stop_flag.is_set():
                    return False
                pdi.moveTo(int(cx + dx * i / float(steps)), cy)
                self._wait(.02)
        finally:
            pdi.mouseUp(button="right")
        self._wait(float(self.params.get("settle", 0.8)))
        return not self.stop_flag.is_set()

    def find_panel(self, want, near_xy):
        """全屏重新扫描数码管面板：先按读数认，再按位置近。"""
        frame = self.screen.grab()
        cands = [(x + self.screen.left, y + self.screen.top, w, h, val, info)
                 for x, y, w, h, val, info in find_digit_regions(frame)]
        if not cands:
            return None
        pick = pick_credit_candidate(
            cands, near_xy=near_xy, screen_w=self.screen.size[0], want=want)
        return list(pick[:4]) if pick else None

    def insert(self, n):
        """投 n 枚币。用引擎同一条定位链路，不另写一套。"""
        pos = self.engine.locate("coin")
        if pos is None:
            return False
        for _ in range(max(1, int(n))):
            if self.stop_flag.is_set():
                return False
            self.engine.click_at(*pos)
            self._wait(max(.05, float(self.cfg.get("coin_delay", 100)) / 1000.0))
        self._wait(max(.3, float(self.cfg.get("last_coin_wait", 800)) / 1000.0))
        return True

    # ---------- 主循环 ----------
    def run(self):
        # 急停看门狗和自动运行共用同一套：热键轮询 + 鼠标甩左上角 + 连按强杀。
        # 它监视的是共享的 stop_flag，所以任何一处触发都会停掉采集。
        try:
            self.engine.start_watchdog()
        except Exception:  # noqa: BLE001
            pass  # 看门狗起不来时还有热键钩子和向导按钮兜底
        try:
            self.reason = self._run()
        except Exception as e:  # noqa: BLE001
            self.reason = f"出错：{type(e).__name__}: {e}"
            self.log(f"[自动采集] {self.reason}")
        finally:
            self.log(f"[自动采集] 结束：{self.reason or '已停止'}，共保存 {len(self.saved)} 张")
            try:
                if self.on_done:
                    self.on_done(self.reason, len(self.saved))
            except Exception:  # noqa: BLE001
                pass  # 向导窗口可能已被关掉，UI 回调失败不能吞掉线程收尾

    def _run(self):
        front = self._rect_now()
        if not front:
            return "没有框选 Credit 区域，先去「定位设置」框一个"
        target = int(self.params.get("target", 99) or 99)
        coins = max(1, int(self.params.get("coins", 1) or 1))
        step = max(1, int(self.params.get("max_step", 3) or 3))
        angles = list(self.params.get("angles") or [])
        cap = int(self.cfg.get("credit_max_display", 0) or 0)
        prev = None
        while not self.stop_flag.is_set():
            self.cfg["credit_rect"] = list(front)
            got = self.read_at(front)
            if not got:
                return f"读不出 Credit（区域 {front}），先做一次「读取测试」"
            crop, val = got
            ok, why = truth_gate(prev, val, max_step=max(1, coins * step))
            if not ok:
                return f"真值校验没过：{why}（上次 {prev}，这次 {val}）"
            prev = val
            if cap and int(val) >= cap:
                return f"已到显示上限 {cap}；再投币画面也不变，真值不可信，停在这里"
            self.save(crop, val, "front" if angles else "")

            for dx in angles:
                if self.stop_flag.is_set():
                    return "已停止"
                if not self.rotate(dx):
                    return "已停止"
                rect = self.find_panel(val, self._center(self.cfg["credit_rect"]))
                if not rect:
                    self.log(f"[自动采集] 转 {dx}px 后找不到 Credit 面板，跳过这个角度")
                else:
                    self.cfg["credit_rect"] = rect
                    got2 = self.read_at(rect)
                    if got2 and got2[1] == val:
                        self.save(got2[0], val, f"a{dx:+d}")
                    else:
                        self.log(
                            f"[自动采集] 转 {dx}px 后读成「{got2[1] if got2 else '读不出'}」"
                            f"，与真值 {val} 不符，不存这张（宁可少一张也不存错真值）")
                if not self.rotate(-dx):
                    return "已停止"
                self.cfg["credit_rect"] = list(front)
                back = self.read_at(front)
                if not back or back[1] != val:
                    return (f"转回正面后读成「{back[1] if back else '读不出'}」，"
                            f"不是 {val}——视角没回到原处，停止以免后面全采歪")

            if int(val) >= target:
                return f"已采集到 {target}，完成"
            if not self.insert(coins):
                return "找不到投币处（coin 坐标/模板），停止"
        return "已停止"


def resolve_ref_path(path):
    """「没币」参考图路径解析：相对路径按程序目录解析，换机器/换工作目录仍有效。"""
    if not path:
        return path
    if os.path.isabs(path) or os.path.exists(path):
        return path
    candidate = os.path.join(APP_DIR, path)
    return candidate if os.path.exists(candidate) else path

# 模板清单：key -> 中文名
TEMPLATES = [
    ("coin", "硬币堆（投币处）"),
    ("maxbet", "MaxBet 按钮"),
    ("bet", "Bet 按钮（1/2 枚押法用）"),
    ("lever", "拉杆手柄"),
    ("btn1", "按钮 1（左）"),
    ("btn2", "按钮 2（中）"),
    ("btn3", "按钮 3（右）"),
]
TPL_LABEL = dict(TEMPLATES)

DEFAULT_CFG = {
    # 定位方式：coord=固定坐标（推荐），image=图像识别
    "locate_mode": "coord",
    "points": {},  # {key: [x, y]} 固定坐标模式下每个目标的屏幕坐标
    # 识别
    "threshold": 0.80,
    "multi_scale": False,
    "scale_min": 0.90,
    "scale_max": 1.25,
    "scale_steps": 4,
    "retries": 10,
    "retry_interval": 0.15,
    # 动作
    "hold_tab": True,
    "tab_repeat": 400,
    "game_window": "VRChat",
    "bet_count": 3,
    "coin_count": 99,
    "draw_count": 33,
    "burn_count": 15,  # 固定空转次数（用来消耗 Replay 退回的币）
    "watch_rect": None,  # [x, y, w, h] 监测区域：建议框住"余额/单元数字"
    "credit_rect": None,  # [x, y, w, h] Credit 数码管区域：直接读数字，=0 就是没币
    "credit_ref": None,  # 只允许清晰的 0 作为参考；不一致表示未知
    "credit_ref_threshold": 8.0,  # 与参考图的平均差异阈值（0~255）
    # 这台机器的 Credit 最多显示 99，真实币数可以更多（例如 120 只显示 99）。
    # 顶到上限时扣币在画面上看不出来，程序不会重试，避免重复扣币。
    # 填 0 表示"不知道上限"，按普通读数处理。
    "credit_max_display": 99,
    # 自我训练：把运行中「连续三帧一致」的可信读数存成样本，喂给模板库。
    # 模板库只做交叉验证，永远不能单独推翻段码——两者不一致一律判未知。
    "auto_learn": False,
    "region_diff_threshold": 3.0,  # 监测区域差异阈值
    "region_wait": 700,  # 下注后等多久再看数字区域（毫秒）
    "bet_retries": 3,  # 仅确认币数完全未变时才重新点击
    "bet_confirm_timeout": 1500,  # 单次下注确认最长等待（毫秒）
    "coin_delay": 100,
    "step_delay": 500,
    "move_delay": 150,
    "last_coin_wait": 800,
    "pull_px": 280,
    # 循环
    "rounds": 0,  # 0 = 无限循环
    "max_minutes": 0,  # >0 时连续运行这么多分钟自动停（保险）
    "corner_stop_px": 40,  # 鼠标停在这个像素范围的左上角即急停
    # 额外急停键：填 pause 可启用 Pause。默认不用——Windows 把 Pause 用作
    # "正在拖动窗口"，运行中点标题栏拖动就会误停。
    "stop_keys": ["F12", "End"],   # 自定义停止热键（名字列表）
    "extra_stop_key": "",          # 兼容旧配置：单个附加键
    "force_quit_presses": 2,       # 连按几次热键直接结束进程
}


UI_FONT = ("Microsoft YaHei UI", 10)
UI_FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
MONO_FONT = ("Consolas", 10)
MONO_BOLD = ("Consolas", 12, "bold")


def setup_theme(root):
    """统一 ttk 外观：底色、卡片、按钮三档样式、日志配色。

    之前界面全是默认 ttk 灰白、控件挤在一起、说明文字硬编码折行宽度，
    换个 DPI 就排版错位。这里改成按主题变量统一控制，缩放时重新计算折行宽度。
    """
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=BG, foreground=INK, font=UI_FONT,
                    borderwidth=0, focuscolor=BG)
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=INK)
    style.configure("Card.TLabel", background=CARD, foreground=INK)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
    style.configure("Section.TLabel", background=BG, foreground=INK,
                    font=("Microsoft YaHei UI", 11, "bold"))
    style.configure("Danger.TLabel", background=BG, foreground=DANGER,
                    font=("Microsoft YaHei UI", 9))
    style.configure("TLabelframe", background=BG, bordercolor=BORDER,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG, foreground=INK,
                    font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("Card.TLabelframe", background=CARD, bordercolor=BORDER)
    style.configure("Card.TLabelframe.Label", background=CARD, foreground=INK,
                    font=("Microsoft YaHei UI", 10, "bold"))

    style.configure("TButton", padding=(12, 6), relief="flat", background="#e8ecf1",
                    foreground=INK, borderwidth=0)
    style.map("TButton",
              background=[("active", "#d8dee6"), ("pressed", "#c8d0da"), ("disabled", "#f0f2f5")],
              foreground=[("disabled", "#aab2bd")])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
    style.map("Accent.TButton",
              background=[("active", "#1d4fd8"), ("pressed", "#1a45bd"), ("disabled", "#9db4e8")],
              foreground=[("disabled", "#eef2fb")])
    style.configure("Danger.TButton", background=DANGER, foreground="#ffffff",
                    font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 8))
    style.map("Danger.TButton",
              background=[("active", "#b3271d"), ("pressed", "#96211a"), ("disabled", "#eaa9a4")])
    style.configure("Ghost.TButton", background=BG, foreground=ACCENT, padding=(10, 5))
    style.map("Ghost.TButton", background=[("active", "#e6edfd")], foreground=[("active", "#1d4fd8")])

    style.configure("TEntry", fieldbackground="#ffffff", bordercolor=BORDER,
                    insertcolor=INK, padding=4)
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
    style.configure("TRadiobutton", background=BG, foreground=INK, padding=(2, 4))
    style.map("TRadiobutton", background=[("active", BG)],
              indicatorcolor=[("selected", ACCENT)])
    style.configure("TCheckbutton", background=BG, foreground=INK, padding=(2, 4))
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorcolor=[("selected", ACCENT)])
    style.configure("TNotebook", background=BG, bordercolor=BORDER, borderwidth=1)
    style.configure("TNotebook.Tab", padding=(20, 9), background="#e8ecf1",
                    foreground=MUTED, borderwidth=0)
    style.map("TNotebook.Tab",
              background=[("selected", CARD), ("active", "#dfe6ee")],
              foreground=[("selected", INK), ("active", INK)],
              font=[("selected", UI_FONT_BOLD)])
    style.configure("TProgressbar", background=ACCENT, troughcolor="#e8ecf1",
                    bordercolor="#e8ecf1", lightcolor=ACCENT, darkcolor=ACCENT)
    return style


def log_level(msg):
    """按内容给日志分级上色：一眼能分出正常、提醒和错误。"""
    if any(k in msg for k in ("[错误]", "[下注失败]", "失败", "异常", "Traceback")):
        return "error"
    if any(k in msg for k in ("[警告]", "[下注重试]", "警告", "建议", "不可信", "变化了")):
        return "warn"
    if any(k in msg for k in ("[下注确认]", "已保存", "已设定", "OK", "正常")):
        return "ok"
    return "info"

class Screen:
    def __init__(self):
        self._local = threading.local()
        self.mon = self.sct.monitors[1]  # 主屏
        self.left = self.mon["left"]
        self.top = self.mon["top"]
        self.size = (self.mon["width"], self.mon["height"])

    @property
    def sct(self):
        # mss keeps Win32 handles in thread-local storage; never share its instance.
        if not hasattr(self._local, 'sct'):
            self._local.sct = mss.mss()
        return self._local.sct

    def grab(self):
        raw = np.array(self.sct.grab(self.mon))
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)

    def grab_rect(self, x, y, w, h):
        """按物理像素抓取屏幕区域（x,y 为屏幕绝对坐标）"""
        raw = np.array(
            self.sct.grab({"left": x, "top": y, "width": w, "height": h})
        )
        return cv2.cvtColor(raw, cv2.COLOR_BGRA2BGR)


def load_image(path):
    """读图（兼容中文路径）"""
    data = np.fromfile(path, dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR)


def save_image(path, img):
    ok, buf = cv2.imencode(".png", img)
    if ok:
        buf.tofile(path)
    return ok


def match_template(frame, tpl, threshold, scales=(1.0,)):
    """返回 (score, cx, cy, w, h)；坐标为画面内像素"""
    gray = frame
    if tpl is None or float(tpl.std()) < 4:
        return None
    best = None
    for sc in scales:
        t = tpl
        if abs(sc - 1.0) > 1e-6:
            t = cv2.resize(tpl, None, fx=sc, fy=sc, interpolation=cv2.INTER_AREA)
        th, tw = t.shape[:2]
        if th < 6 or tw < 6 or th > gray.shape[0] or tw > gray.shape[1]:
            continue
        tg = t
        res = cv2.matchTemplate(gray, tg, cv2.TM_CCOEFF_NORMED)
        _, maxv, _, maxloc = cv2.minMaxLoc(res)
        if best is None or maxv > best[0]:
            best = (maxv, maxloc[0] + tw // 2, maxloc[1] + th // 2, tw, th)
    return best


def frame_diff(a, b):
    """两帧差异（0~255），用于判断画面是否发生变化"""
    if a is None or b is None or a.shape != b.shape:
        return 999.0
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    return float(np.mean(np.abs(ga - gb)))


def scene_shift(a, b):
    """估计两帧之间的「整体几何位移」（像素），用来判断视角有没有被转动。

    与 frame_diff 的区别很关键：
    - frame_diff 比的是像素差，游戏动画（转轴、粒子、灯光）会让它一直很大，
      所以它不能用来判断「视角有没有动」；
    - 这里用光流找出画面整体移动了多少。转视角时整幅画面同向平移/缩放，
      位移大；只有局部动画时中位位移很小。

    取中位数而非平均值：动画区域会产生很大的离群位移，中位数能把它们滤掉。

    返回位移像素数；无法计算时返回 None。
    """
    if a is None or b is None or a.shape != b.shape:
        return None
    try:
        # 光流只吃单通道，彩色输入先转灰度
        if a.ndim == 3:
            a = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
            b = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
        if a.dtype != np.uint8:
            a = np.clip(a, 0, 255).astype(np.uint8)
            b = np.clip(b, 0, 255).astype(np.uint8)
        h, w = a.shape[:2]
        # 缩小一半加速，但必须保持原比例——把宽高压成方形会扭曲画面，
        # 算出来的位移量也会跟着失真（实测 20px 被算成 3px）。
        sw, sh = max(32, w // 2), max(32, h // 2)
        sa = cv2.resize(a, (sw, sh), interpolation=cv2.INTER_AREA)
        sb = cv2.resize(b, (sw, sh), interpolation=cv2.INTER_AREA)
        dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
        flow = dis.calc(sa, sb, None)
        mag = np.linalg.norm(flow, axis=2)
        # 归一化回「原图高度对应的像素数」，取 x/y 比例的均值
        scale = ((w / float(sw)) + (h / float(sh))) / 2.0
        return float(np.median(mag)) * scale
    except Exception:
        return None


def merge_red_boxes(mask, min_area=8):
    """把红色发光像素按"同一行、横向相邻"合并成若干候选显示区，返回 [(x, y, w, h, 读数, 说明)]"""
    connected = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_CLOSE,
                                 np.ones((3, 3), np.uint8))
    num, _labels, stats, _c = cv2.connectedComponentsWithStats(connected, connectivity=8)
    comps = [stats[i] for i in range(1, num) if stats[i][4] >= min_area]
    boxes = []
    for c in comps:
        boxes.append([int(c[0]), int(c[1]), int(c[2]), int(c[3])])
    merged = True
    while merged:
        merged = False
        out = []
        while boxes:
            b = boxes.pop()
            bx, by, bw, bh = b
            for o in boxes:
                ox, oy, ow, oh = o
                v_overlap = min(by + bh, oy + oh) - max(by, oy)
                if v_overlap < 0.5 * min(bh, oh):
                    continue
                gap = max(0, max(bx - (ox + ow), ox - (bx + bw)))
                if gap <= 0.8 * max(bh, oh):
                    o[0] = min(bx, ox)
                    o[1] = min(by, oy)
                    o[2] = max(bx + bw, ox + ow) - o[0]
                    o[3] = max(by + bh, oy + oh) - o[1]
                    merged = True
                    break
            else:
                out.append(b)
        boxes = out
    return boxes


# =========================================================================
# 自动化引擎
# =========================================================================
from vision import (
    decode_led_best,
    decode_led_fused,
    build_template_bank,
    led_mask,
    cluster_diff,
    region_diff,
    find_digit_regions,
    digit_fit,
    decode_consensus,
    expand_to_full_number,
    region_fits_one_digit,
)


class LegacyEngine(threading.Thread):
    def __init__(self, cfg, tpls, log, screen, variants=None):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.tpls = tpls  # {key: BGR image}
        # 多视角模板（可选）：{key: [BGR image, ...]}。没有时退回单模板。
        self.tpl_variants = variants or {}
        self.log = log
        self.screen = screen
        self.stop_flag = threading.Event()
        self.state = "idle"
        self.spins_done = 0
        # 运行统计：界面「本次运行」卡片实时显示，结束时写进日志摘要。
        # confirmed 的注才计入 bets_confirmed（没确认过的不知道成没成）。
        self.coins_used = 0
        self.bets_confirmed = 0
        self.rounds_done = 0
        self.started_at = time.time()
        # The active Engine subclass sets this only after a verified debit.
        self.bet_committed = False
        # Whether all expected debit steps were confirmed.
        self.confirmed = False
        # 预热模板库：建库要解码几百张样本（0.2 秒级），必须发生在
        # 第一次读币之前，否则会吃掉下注确认的时间窗。
        warm_digit_bank()

    # ---------- 基础动作 ----------
    def pause(self, seconds):
        if self.stop_flag.wait(max(0, seconds)):
            raise InterruptedError('已请求停止')

    def request_stop(self):
        self.stop_flag.set()

    def start_watchdog(self):
        """独立线程急停监测：F12/End/Pause 轮询、鼠标甩到左上角、超时自动停。
        不依赖全局键盘钩子，钩子失效时照样能停。"""
        def _watch():
            t0 = time.time()
            max_min = float(self.cfg.get("max_minutes", 0) or 0)
            corner = int(self.cfg.get("corner_stop_px", 40))
            # 急停键：默认 F12 / End，可在参数页自定义。
            # Pause 会和"拖动窗口标题栏"冲突，除非用户自己加，否则不启用。
            keys = stop_key_entries(self.cfg)
            need = max(2, int(self.cfg.get("force_quit_presses", 2) or 2))
            pressed = {vk: False for vk, _ in keys}

            def poll_taps():
                """返回这一轮里**新按下**（按下前是松开的）的键，避免按住不放被重复计数。"""
                hits = []
                for vk, name in keys:
                    down = key_pressed(vk)
                    if down and not pressed[vk]:
                        hits.append((vk, name))
                    pressed[vk] = down
                return hits

            taps = 0
            last = None
            while not self.stop_flag.is_set():
                hits = poll_taps()
                if hits:
                    now = time.time()
                    # 间隔超过 2 秒算新的一轮连按
                    taps = taps + len(hits) if (last and now - last <= 2.0) else len(hits)
                    last = now
                    vk, name = hits[0]
                    if taps >= need:
                        self.log(f"[急停] 连按 {name} {taps} 次，直接结束进程")
                        self.stop_flag.set()
                        try:
                            os._exit(0)
                        except Exception:
                            pass
                        return
                    self.log(f"[急停] 检测到 {name} 按下，正在停止…"
                             f"（再按 {need - taps} 次可强制结束进程）")
                    self.stop_flag.set()
                    break
                x, y = cursor_pos()
                if 0 <= x <= corner and 0 <= y <= corner:
                    self.log(f"[急停] 鼠标甩到左上角（{x},{y}），立即停止")
                    self.stop_flag.set()
                    return
                if max_min > 0 and (time.time() - t0) > max_min * 60:
                    self.log(f"[急停] 已连续运行 {max_min:.0f} 分钟，自动停止")
                    self.stop_flag.set()
                    return
                self.stop_flag.wait(0.08)
            # 已经请求停止：再守一小会儿，引擎如果卡住，连按热键可以直接结束进程
            deadline = time.time() + 5.0
            while time.time() < deadline and self.is_alive():
                hits = poll_taps()
                if hits:
                    now = time.time()
                    taps = taps + len(hits) if (last and now - last <= 2.0) else len(hits)
                    last = now
                    if taps >= need:
                        self.log(f"[急停] 连按 {hits[0][1]} 共 {taps} 次，直接结束进程")
                        try:
                            os._exit(0)
                        except Exception:
                            pass
                self.stop_flag.wait(0.08)

        threading.Thread(target=_watch, daemon=True).start()

    def _scales(self):
        c = self.cfg
        if not c["multi_scale"]:
            return (1.0,)
        n = max(2, int(c["scale_steps"]))
        return tuple(
            float(v)
            for v in np.linspace(c["scale_min"], c["scale_max"], n)
        )

    def locate(self, key, retries=None):
        """返回目标屏幕绝对坐标 (x, y) 或 None。
        固定坐标模式：直接返回设定的点（每次运行前设定一次即可）
        图像识别模式：在屏幕上找模板"""
        if self.cfg.get("locate_mode", "coord") == "coord":
            pt = (self.cfg.get("points") or {}).get(key)
            if pt and len(pt) == 2:
                return (int(pt[0]), int(pt[1]))
            self.log(f"[错误] {TPL_LABEL.get(key, key)} 还没设定坐标点")
            return None

        tpl = self.tpls.get(key)
        if tpl is None:
            self.log(f"[错误] 缺少模板：{TPL_LABEL.get(key, key)}")
            return None
        variants = (self.tpl_variants.get(key) or [tpl])[:24]
        retries = self.cfg["retries"] if retries is None else retries
        thr = self.cfg["threshold"]
        scales = self._scales()
        best_score = 0.0
        for _ in range(max(1, retries)):
            if self.stop_flag.is_set():
                return None
            frame = self.screen.grab()
            origin_x, origin_y = self.screen.left, self.screen.top
            # Identical red stop buttons cannot be distinguished by a whole-screen template.
            rect = (self.cfg.get('template_rects') or {}).get(key)
            if rect:
                rx, ry, rw, rh = rect
                radius = max(120, rw, rh)
                x0 = max(0, int(rx-origin_x-radius))
                y0 = max(0, int(ry-origin_y-radius))
                x1 = min(frame.shape[1], int(rx-origin_x+rw+radius))
                y1 = min(frame.shape[0], int(ry-origin_y+rh+radius))
                frame = frame[y0:y1, x0:x1]
                origin_x += x0; origin_y += y0
            r = None
            # 多视角模板：任何一张命中都算认得，取最高分那张。
            for vt in variants:
                rr = match_template(frame, vt, thr, scales)
                if rr and (r is None or rr[0] > r[0]):
                    r = rr
            if r:
                score, cx, cy, _, _ = r
                best_score = max(best_score, score)
                if score >= thr:
                    offset = (self.cfg.get('template_offsets') or {}).get(key, [.5, .5])
                    return (int(origin_x + cx + (offset[0]-.5)*r[3]),
                            int(origin_y + cy + (offset[1]-.5)*r[4]))
            self.pause(self.cfg["retry_interval"])
        self.log(
            f"[未找到] {TPL_LABEL.get(key, key)}（最高相似度 {best_score:.2f} < {thr:.2f}）"
        )
        return None

    def click_at(self, x, y):
        if self.stop_flag.is_set():
            return
        pdi.moveTo(x, y)
        if self.stop_flag.wait(self.cfg["move_delay"] / 1000.0):
            return
        # Give the game at least several frames to observe a pressed button.
        try:
            pdi.mouseDown()
            self.stop_flag.wait(.10)
        finally:
            pdi.mouseUp()
        self.pause(0.05)

    def click_template(self, key):
        if self.stop_flag.is_set():
            return False
        pos = self.locate(key)
        if pos is None:
            return False
        self.click_at(*pos)
        return not self.stop_flag.is_set()

    def drag_template(self, key, dy):
        if self.stop_flag.is_set():
            return False
        pos = self.locate(key)
        if pos is None:
            return False
        x, y = pos
        pdi.moveTo(x, y)
        if self.stop_flag.wait(self.cfg["move_delay"] / 1000.0):
            return False
        try:
            pdi.mouseDown()
            if self.stop_flag.wait(.1): return False
            pdi.moveTo(x, y + int(dy))
            self.stop_flag.wait(.15)
        finally:
            pdi.mouseUp()
        return not self.stop_flag.is_set()

    # ---------- 业务动作 ----------
    def insert_coins(self):
        n = int(self.cfg["coin_count"])
        first = self.locate("coin")
        if first is None:
            return False
        coord_mode = self.cfg.get("locate_mode", "coord") == "coord"
        self.log(f"投币开始：{n} 个（0.1 秒/个，坐标 {first[0]},{first[1]}）")
        x, y = first
        pdi.moveTo(x, y)
        self.pause(self.cfg["move_delay"] / 1000.0)
        for i in range(n):
            if self.stop_flag.is_set():
                return False
            if (not coord_mode) and i > 0 and i % 10 == 0:
                # 图像识别模式：每 10 个重新确认一次位置，防止机器动画/视角漂移
                pos = self.locate("coin", retries=3)
                if pos:
                    x, y = pos
                    pdi.moveTo(x, y)
                else:
                    return False
            pdi.click()
            self.pause(self.cfg["coin_delay"] / 1000.0)
        self.log("投币完成，等待机器吃币…")
        self.pause(self.cfg["last_coin_wait"] / 1000.0)
        return True

    def _watch_region(self):
        """返回监测区域画面（未设定返回 None）"""
        r = self.cfg.get("watch_rect")
        if not r or len(r) != 4 or self.screen is None:
            return None
        try:
            x, y, w, h = [int(v) for v in r]
            if w < 4 or h < 4:
                return None
            return self.screen.grab_rect(x, y, w, h)
        except Exception:
            return None

    def read_credit(self):
        """读 Credit 数码管；熄屏或看不清返回未知。"""
        r = self.cfg.get("credit_rect")
        if not r or len(r) != 4 or self.screen is None:
            return None, "未设 Credit 区域"
        try:
            x, y, w, h = [int(v) for v in r]
            img = self.screen.grab_rect(x, y, w, h)
            return decode_led_fused(img, get_digit_bank())
        except Exception as e:  # noqa: BLE001
            return None, f"读取失败: {e}"

    def grab_credit(self):
        """抓取 Credit 区域画面"""
        r = self.cfg.get("credit_rect")
        if not r or len(r) != 4 or self.screen is None:
            return None
        try:
            x, y, w, h = [int(v) for v in r]
            return self.screen.grab_rect(x, y, w, h)
        except Exception:
            return None

    def credit_ref_diff(self, crop=None):
        """当前画面与「没币参考图」的差异（0~255）。
        比的是"数字块长什么样"（按数字块归一化后比对），所以**换视角/缩放/重新框选都不影响**。"""
        path = resolve_ref_path(self.cfg.get("credit_ref"))
        if not path or not os.path.exists(path):
            return None
        ref = load_image(path)
        if ref is None:
            return None
        cur = crop if crop is not None else self.grab_credit()
        if cur is None:
            return None
        return cluster_diff(ref, cur)

    def recognize_credit(self, crop=None):
        """返回 (值 或 None, 方式, 详细)，无法确认的位用 ? 标识。"""
        cur = crop if crop is not None else self.grab_credit()
        if cur is None:
            return None, "无画面", "未设 Credit 区域或抓不到画面"
        val, info = decode_led_fused(cur, get_digit_bank())
        return val, "段码+模板", info

    def credit_is_empty(self, crop=None):
        """Credit 是否不足当前下注枚数（按本轮用尽处理）。
        ① 数字识别可信 → 以数字为准（参考图只做日志提醒，不推翻数字）
        ② 数字读不出 → 才用「没币」参考图兜底
        （早先写成"矛盾时以参考图为准"，结果一张过期参考图就能把读对的数字推翻，已修正）"""
        crop = crop if crop is not None else self.grab_credit()
        if crop is None:
            return None, "未设 Credit 区域或抓不到画面"

        val, info = decode_led_fused(crop, get_digit_bank())
        confident = val is not None and val != "" and "?" not in val
        if confident:
            required=int(self.cfg.get('bet_count',3))
            if 0 < int(val) < required:
                return True, f'读出「{val}」，不足下注 {required} 枚 → 结束本轮，下一轮正常投币'
            val_empty = val.strip("0") == ""
            d = self.credit_ref_diff(crop)
            thr = float(self.cfg.get("credit_ref_threshold", 8.0) or 8.0)
            if d is not None:
                ref_empty = d < thr
                if ref_empty != val_empty:
                    self.log(
                        f"[清币] 数字读出「{val}」，但与参考图不一致（差异 {d:.1f}）——"
                        f"以数字为准；参考图可能过期或框的位置变了，建议在显示 0 时重录一次"
                    )
                    return val_empty, (
                        f"读出「{val}」→ {'没币' if val_empty else '还有币'}"
                        f"（参考图差异 {d:.1f}，与数字不一致，已按数字判定；建议重录参考图）"
                    )
                return val_empty, (
                    f"读出「{val}」且与参考图一致 → "
                    f"{'没币' if val_empty else '还有币'}（差异 {d:.1f}）"
                )
            return val_empty, f"读出「{val}」→ {'没币' if val_empty else '还有币'}｜{info}"

        # A reference mismatch cannot prove there are coins: it could be obstruction.
        d = self.credit_ref_diff(crop)
        if d is not None:
            thr = float(self.cfg.get("credit_ref_threshold", 8.0) or 8.0)
            ref = load_image(resolve_ref_path(self.cfg.get('credit_ref')))
            ref_val, _ = decode_led_best(ref)
            if ref_val and ref_val.isdigit() and int(ref_val) == 0 and d < thr:
                return True, f"数字读不出，但与「没币」参考一致（差异 {d:.1f}）→ 没币｜{info}"
            return None, f"数字读不出；参考图差异 {d:.1f} 不能证明有币｜{info}"
        return None, f"读不出｜{info}"

    def stable_credit_empty(self):
        states = []
        for _ in range(3):
            state, info = self.credit_is_empty()
            states.append(state)
            if self.stop_flag.wait(.12): return None, '已停止'
        if states[0] is not None and all(v is states[0] for v in states):
            return states[0], info
        return None, '连续三帧读数不稳定或被遮挡，请检查 Credit 区域'

    def stable_watch(self):
        frames=[]
        for _ in range(3):
            frame=self._watch_region()
            if frame is None: return None
            frames.append(frame)
            if self.stop_flag.wait(.1): return None
        return np.median(frames, axis=0).astype(np.uint8)

    def stable_credit_value(self, timeout=1.5, expected=None):
        """Require three identical numeric readings. Never treat unknown as zero.

        When verifying a bet, wait through unchanged frames to allow delayed input.
        Only a stable unchanged final reading permits a click retry.
        """
        deadline=time.monotonic()+max(.36,timeout)
        last=None
        consecutive=0
        stable=None
        while not self.stop_flag.is_set():
            use_rect=bool(self.cfg.get('credit_rect'))
            crop=self.grab_credit() if use_rect else self._watch_region()
            value,info=decode_led_fused(crop, get_digit_bank())
            if not digit_fit(crop)[0]:
                value, info = None, 'Credit 选区截断或没有有效数字'
            number=int(value) if value and value.isdigit() else None
            consecutive=consecutive+1 if number is not None and number==last else (1 if number is not None else 0)
            last=number
            stable=number if consecutive>=3 else None
            if stable is not None and (expected is None or stable==expected):
                if use_rect:
                    self._learn_credit(crop, value)
                return stable,info
            if time.monotonic()>=deadline:
                if stable is not None and use_rect:
                    self._learn_credit(crop, value)
                return stable,info if stable is not None else 'Credit 未能连续三帧确认'
            if self.stop_flag.wait(.12): break
        return None,'已停止'

    def _learn_credit(self, crop, value):
        """自我训练：把「连续三帧一致」的稳定读数存成样本，并重建模板库。

        只在确认稳定后才调用（调用方保证），采集门槛交给 auto_collect_credit。
        任何异常都吞掉——自我训练绝不能影响正常运行。
        """
        try:
            if auto_collect_credit(crop, value, self.cfg, self.log):
                get_digit_bank(force=True)
        except Exception:  # noqa: BLE001
            pass

    def credit_display_capped(self, value):
        """读数是否已经顶到显示上限。

        这台机器的 Credit 最多显示 99：真实币数可以超过（比如 120），
        但屏幕上永远是 99。这时候「扣了 3 币」在画面上**看不出来**——
        120→117，显示都是 99。

        所以顶到上限时，"读数没变化"既可能是没点到，也可能是点到了但看不出来。
        这两种情况必须区别对待，否则会反复重试、每点一次真扣一次币。
        """
        if value is None:
            return False
        cap = self.cfg.get("credit_max_display")
        if cap in (None, "", 0):
            # 0 = 不知道上限；此时不能用"顶格"推断，保持旧行为
            return False
        try:
            return int(value) >= int(cap)
        except (TypeError, ValueError):
            return False

    def place_confirmed_bet(self, credit):
        """A sent mouse event is not proof of a bet: require the exact debit.

        屏幕读数顶到上限时（这台机器最多显示 99，真实币数可以更多），
        扣币在画面上**看不出来**。这时"读数没变"不能证明"没点到"，
        再点一次就可能真的多扣一份。所以顶格时只点一次，不重试，
        改用监测区域的变化来确认这一注有没有生效。
        """
        bet=int(self.cfg['bet_count'])
        clicks=[('maxbet',3)] if bet==3 else [('bet',1)]*bet
        retries=max(1,int(self.cfg.get('bet_retries',3)))
        timeout=max(.36,float(self.cfg.get('bet_confirm_timeout',1500))/1000)
        self.confirmed=True          # 扣币是否被画面确认过
        for key,cost in clicks:
            expected=credit-cost
            capped=self.credit_display_capped(credit)
            # 顶格时重试没有意义：读数不会变，点了也看不出，等于盲扣币
            attempts=1 if capped else retries
            if capped and retries>1:
                self.log(
                    f'[下注] Credit 显示 {credit} 已达上限，扣 {cost} 币在画面上看不出来，'
                    f'本次只点一次 {key}（不重试，避免重复扣币）'
                )
            for attempt in range(attempts):
                if not self.click_template(key):
                    return False
                # 点击已发出：这一注已经交出去了，后面必须把转轮做完。
                self.bet_committed = True
                after,info=self.stable_credit_value(timeout=timeout,expected=expected)
                if after==expected:
                    self.log(f'[下注确认] {key} 已扣 {cost} 币：{credit} → {after}')
                    self.bets_confirmed += 1
                    credit=after
                    break
                if after is None:
                    self.confirmed=False
                    self.log(
                        f'[下注失败] {info}；点击已经发出，无法确认扣币——'
                        f'这一注钱已押上，继续把转轮做完'
                    )
                    return False
                if after!=credit:
                    self.confirmed=False
                    self.log(
                        f'[下注失败] 预期 Credit {expected}，实际 {after}；'
                        f'发生非预期变化。点击已发出，继续把转轮做完'
                    )
                    return False
                if capped:
                    # 顶格 + 读数未变：无法判断这一注有没有生效。
                    # 保守处理：认为已下注（不重复点），但明确告诉用户。
                    self.confirmed=False
                    self.log(
                        f'[下注确认] Credit 顶格（{credit}），扣 {cost} 币无法从显示验证；'
                        f'已按「已下注」继续，且没有重复点击。'
                        f'如果这一注实际没生效，拉杆会被游戏拒绝——'
                        f'可把「credit_max_display」设为 0 并改小每轮投币数来避开封顶'
                    )
                    credit=credit  # 数值未知但按已扣处理，保持原值
                    break
                self.log(f'[下注重试] {key} 后 Credit 仍为 {credit}，第 {attempt+1}/{attempts} 次未扣币')
            else:
                # 重试到头仍没确认扣币：点击早发出去了，这一注已押上，
                # 但扣币没被证实 -> 不能算成一局正常完成的抽奖。
                self.confirmed=False
                self.log(
                    f'[下注失败] 多次点击仍未确认扣币。'
                    + ('但 MaxBet/Bet 已经点下去了，这一注必须做完，'
                       '继续拉杆和三个停止按钮'
                       if self.bet_committed else
                       '点击确实没生效，停止；不会拉杆或按停止按钮')
                )
                return False
        return True

    def spin(self, watch_change=False):
        """先确认可下注余额，再确认精确扣币，之后拉杆和停止转轮。

        **一旦 MaxBet/Bet 已经点下去，这一注就必须做完**（拉杆 + 三个停止按钮）。
        中途因为"读数没确认到扣币"就退出，等于钱已经押上却把转轮晾在那儿，
        那一注直接白押。所以 bet_committed 之后任何失败都继续把动作做完。

        watch_change 时返回 (动作成功, 本次是否执行抽奖)；不足一注不执行动作。
        """
        step = self.cfg["step_delay"] / 1000.0
        self.bet_committed = False
        credit,info=self.stable_credit_value()
        if credit is None:
            self.log(f'[停止] 下注前不能确认 Credit：{info}')
            return (False,True) if watch_change else False
        bet=int(self.cfg['bet_count'])
        if credit < bet:
            self.log(f'[补币] Credit {credit} 小于下注 {bet}，与 0 同样结束本轮，下一轮正常投币')
            return (True,False) if watch_change else False

        self.pause(step)
        if not self.place_confirmed_bet(credit) and not self.bet_committed:
            # 确认失败但**没点出去**（点都没发出去），可以安全放弃
            return (False,True) if watch_change else False
        if self.bet_committed and not self.confirmed:
            self.log(
                '[下注] 没能从画面确认扣币，但 MaxBet/Bet 已经点下去了——'
                '这一注钱已经押上，继续把拉杆和三个停止按钮做完，不留半截'
            )

        # 拉杆（已下注就必须拉）
        self.pause(step)
        if not self.drag_template("lever", self.cfg["pull_px"]):
            self.log('[停止] 拉杆失败。若这一注已下注，转轮可能停在中间，请手动处理')
            return (False, True) if watch_change else False

        # 三个按钮（已下注就必须按完）
        for key in ("btn1", "btn2", "btn3"):
            self.pause(step)
            if not self.click_template(key):
                self.log(f'[停止] {TPL_LABEL[key]} 失败，本局动作未做完整')
                return (False, True) if watch_change else False
        # 扣币没被确认过就只记日志、不计入完成次数：
        # 动作虽然做了，但这一注到底成没成不确定，不能当成正常一局。
        if self.confirmed:
            self.spins_done += 1
        else:
            self.log('[下注] 本局动作已做完，但扣币未能确认，不计入完成次数')
        if watch_change:
            return True, True
        return True

    def run(self):
        c = self.cfg
        self.log("=== 开始运行（F12 停止）===")
        if pdi is None:
            self.log("[错误] 未安装 pydirectinput，无法模拟鼠标键盘")
            return

        holder = None
        try:
            self.start_watchdog()
            # 先把游戏窗口切到前台，再持续续按 Tab
            kw = str(c.get("game_window", "") or "")
            wins = list_windows(kw) if kw else []
            if wins:
                ok = activate_window(wins[0][0])
                self.log(f"已激活游戏窗口：{wins[0][1]}（{'成功' if ok else '可能未成功，请手动点一下游戏'}）")
                self.pause(0.4)
            elif kw:
                self.log(f"[提示] 没找到标题含「{kw}」的窗口，Tab 需要游戏在前台才能生效")

            if c["hold_tab"]:
                holder = TabHolder(interval=max(0.1, float(c.get("tab_repeat", 400)) / 1000.0))
                holder.start()
                self.log(f"已开始按住 Tab（每 {c.get('tab_repeat', 400)} 毫秒续按一次，确保游戏收到）")
                self.pause(0.5)

            # 确认游戏真的拿到焦点，否则 Tab 无法生效
            if kw:
                got = False
                for _ in range(6):
                    if self.stop_flag.is_set():
                        return
                    cur = foreground_title()
                    if kw.lower() in cur.lower():
                        self.log(f"游戏窗口已在前台：{cur}")
                        got = True
                        break
                    if wins:
                        activate_window(wins[0][0])
                    self.pause(0.5)
                if not got:
                    self.log(f"[停止] 当前前台是「{foreground_title() or '未知窗口'}」，游戏没拿到焦点，请激活游戏后重新启动")
                    return

            if c.get('credit_rect') or c.get('watch_rect'):
                known, info = self.stable_credit_value()
                if known is None:
                    self.log('[停止] 运行前 Credit 校验失败：'+info)
                    return
            else:
                self.log('[停止] 未设 Credit 或备用数字区，无法确认下注')
                return

            rounds = int(c["rounds"])
            r = 0
            while not self.stop_flag.is_set():
                r += 1
                self.log(f"------ 第 {r} 轮 ------")

                self.state = "投币"
                if not self.insert_coins():
                    break
                self.coins_used += int(c["coin_count"])

                ok = True
                needs_coins = False
                self.state = "抽奖"
                target = int(c["draw_count"])
                for i in range(target):
                    if self.stop_flag.is_set():
                        ok = False
                        break
                    res, has_coins = self.spin(watch_change=True)
                    if res and not has_coins:
                        needs_coins = True
                        self.log('Credit 不足一注，结束本轮正常抽奖并进入下一轮投币')
                        break
                    if not res:
                        ok = False
                        break
                    self.log(f"抽奖 {i + 1}/{target} 完成")

                if not ok:
                    break

                # 空转清币
                burn = int(c["burn_count"])
                if burn > 0 and not needs_coins:
                    self.state = "空转清币"
                    self.log(f"空转清币开始（最多 {burn} 次）")
                    for i in range(burn):
                        if self.stop_flag.is_set():
                            break
                        res, changed = self.spin(watch_change=True)
                        if not res:
                            ok = False
                            break
                        if not changed:
                            self.log('Credit 不足一注，结束清币，下一轮正常投币')
                            break
                        self.log(f"空转 {i + 1}/{burn} 完成")

                if not ok:
                    self.log("[中断] 有动作失败，已停止。请检查模板是否需要重新录制 / 阈值是否过高")
                    break

                self.rounds_done = r
                if rounds and r >= rounds:
                    self.log("已完成设定的轮数，正常结束")
                    break
        except InterruptedError:
            self.log('[停止] 已中断等待和后续动作')
        except Exception as e:  # noqa: BLE001
            self.log(f"[异常] {type(e).__name__}: {e}")
        finally:
            if holder is not None:
                holder.release()
                self.log("已松开 Tab")
            self.state = "idle"
            mins = max(0.0, (time.time() - self.started_at) / 60.0)
            self.log(
                f"=== 已停止 === 本次运行：{self.rounds_done} 轮 · 抽奖 {self.spins_done} 次 · "
                f"确认下注 {self.bets_confirmed} 注 · 用币 {self.coins_used} 枚 · 用时 {mins:.1f} 分钟"
            )


# =========================================================================
# 框选工具
# =========================================================================
def overlay_take_over_mouse(win):
    """让全屏浮层拿到键盘焦点并独占鼠标：浮层存活期间，游戏收不到任何
    鼠标事件，不会再出现「鼠标被游戏回中、视角跟着转」的情况。"""
    try:
        win.wait_visibility()
        win.focus_force()
        win.grab_set()
    except Exception:
        pass


def _digit_width(crop):
    """从一张数码管截图里量出**单个**数字的宽度（像素）。

    用于判断「选区是不是只够放一位」。注意不能用整段红色笔画的跨度：
    两位数（99）的跨度是两倍，看起来就像"只框了一位"。
    这里按列间隙把笔画分组，取**宽度中位数**作为单字宽（用中位数而不是
    最小值：偶尔会有 1~2 像素的碎屑，用最小值会把单字宽算得过小）。
    """
    if crop is None or getattr(crop, "size", 0) == 0:
        return 0
    m = led_mask(crop)
    if not m.any():
        return 0
    cols = np.where(m.any(axis=0))[0]
    h = crop.shape[0]
    # 用与解码器一致的列间隙阈值切分
    groups = np.split(cols, np.where(np.diff(cols) > max(2, round(h * .04)))[0] + 1)
    widths = [int(g[-1] - g[0] + 1) for g in groups if len(g)]
    widths = [x for x in widths if x >= 3] or widths
    if not widths:
        return 0
    return int(np.median(widths))


def screen_scale(root=None):
    """tkinter 逻辑尺寸 与 截图物理尺寸 之间的比例。

    这是取点错位的头号原因：高缩放屏幕上（例如 150%），tkinter 报的
    winfo_screenwidth() 是逻辑像素 1707，而 mss 抓的是物理像素 2560。
    浮层上的坐标是逻辑像素，截图/识图用的是物理像素，两者混用会让
    「取到的点」和「画面上的点」差一个缩放倍数——表现就是坐标怎么取都不对。

    返回 (scale_x, scale_y)，用物理尺寸除以逻辑尺寸得到。
    """
    try:
        if root is None:
            root = tk._default_root
        logical_w = root.winfo_screenwidth()
        logical_h = root.winfo_screenheight()
        with mss.mss() as probe:
            mon = probe.monitors[1]
        phys_w, phys_h = mon["width"], mon["height"]
        return phys_w / float(logical_w or 1), phys_h / float(logical_h or 1)
    except Exception:
        return 1.0, 1.0


class ScaledOverlay:
    """全屏浮层的基类：统一处理逻辑/物理像素换算与冻结画面铺满。

    子类只要用 self.to_physical() 换算坐标、用 self.from_physical() 换算回来，
    就能保证「浮层上点哪里」== 「截图里的哪个像素」，与系统缩放无关。
    """

    def _init_overlay(self, background=None):
        self.configure(bg="black")
        self.attributes("-topmost", True)
        self.sx, self.sy = screen_scale(self)
        self.frame_size = None
        if background is not None:
            self.frame_h, self.frame_w = background.shape[:2]
            self.frame_size = (self.frame_w, self.frame_h)
        else:
            self.frame_w = self.frame_h = None
        # 浮层按「物理像素」铺满：几何尺寸要用逻辑像素 = 物理 / 缩放
        self.geometry(f"{self.winfo_screenwidth()}x{self.winfo_screenheight()}+0+0")

    def to_physical(self, x, y):
        """浮层逻辑坐标 -> 截图物理坐标"""
        return int(round(x * self.sx)), int(round(y * self.sy))

    def from_physical(self, x, y):
        """截图物理坐标 -> 浮层逻辑坐标"""
        return int(round(x / self.sx)), int(round(y / self.sy))


class MouseLock:
    """在「切游戏前台 → 浮层就位」这段空窗期里锁住物理鼠标。

    背景：旧流程是「切前台 → 睡 0.6 秒 → 截图 → 建浮层 → grab_set」。
    从游戏拿到焦点到浮层拿到鼠标之间有将近一秒，这段时间游戏握着鼠标，
    手一动视角就转了，等浮层出现时画面已经和取点时看到的不是一回事，
    于是之前录的坐标/模板全部作废。

    这里用 SetCursorPos 把光标钉在当前屏幕中心，并在空窗期持续重钉。
    鼠标被钉住 -> 游戏收不到净位移 -> 视角不会转；浮层 grab_set 之后
    鼠标完全归浮层，锁自动失去作用，直接 release 即可。
    """

    def __init__(self, interval=0.008):
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._pinned = None
        self._lock = threading.Lock()

    def _center(self):
        """钉住的位置：优先屏幕中心；拿不到就退回当前光标位置。

        退回当前光标同样能锁住（只要光标不动，视角就不会转），
        比整个锁静默失效要好。
        """
        try:
            return (user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2)
        except Exception:
            try:
                pt = ctypes.wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                return (int(pt.x), int(pt.y))
            except Exception:
                return None

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                pinned = self._pinned
            if pinned is None:
                return
            try:
                user32.SetCursorPos(int(pinned[0]), int(pinned[1]))
            except Exception:
                pass
            self._stop.wait(self.interval)

    def __enter__(self):
        self._pinned = self._center()
        if self._pinned is None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._pinned = None
        return False


def wait_frames_stable(grab, tries=12, gap=0.05, tol=1.5):
    """反复抓帧直到画面「几何稳定」，返回最后一帧。

    旧流程是固定睡 0.6 秒等画面稳定：机器慢时不够，机器快时白等。

    这里注意不要用 frame_diff 判稳定：游戏画面一直在动（转轴、粒子、灯光），
    像素差永远降不下来，会白白耗掉所有重试次数。改用几何位移来判断——
    只要视角/机位没有继续变化就认为稳定了。
    """
    last = None
    last_small = None
    for _ in range(max(2, tries)):
        try:
            frame = grab()
        except Exception:
            frame = None
        if frame is None:
            break
        if last is not None and frame.shape == last.shape:
            small = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (320, 200),
                               interpolation=cv2.INTER_AREA)
            if last_small is not None:
                shift = scene_shift(last_small, small)
                # 画面不再整体移动 = 机位已稳定
                if shift is not None and shift <= tol:
                    return frame
            last_small = small
        else:
            last_small = None
        last = frame
        time.sleep(gap)
    return last


class RegionPicker(tk.Toplevel, ScaledOverlay):
    """全屏框选：优先用冻结截图当背景（画面定格、鼠标被浮层独占），
    拖框选区，松开鼠标即完成；没有冻结帧时退回半透明遮罩。

    冻结帧是物理像素（如 2560x1600），浮层是逻辑像素（如 1707x1067）。
    冻结帧按比例缩放贴到浮层左上角，回调时再把框换算回物理坐标——
    否则高缩放下取到的坐标会差一个缩放倍数。
    """
    def __init__(self, master, callback, on_close=None, background=None):
        super().__init__(master)
        self.callback = callback
        self.on_close = on_close
        self.attributes("-fullscreen", True)
        self._init_overlay(background)
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)
        self._photo = None
        self.fit = 1.0
        if background is not None:
            # 冻结帧按 1/scale 缩小后贴图：显示尺寸 == 浮层逻辑尺寸
            import cv2 as _cv2

            scaled = _cv2.resize(
                background, None, fx=1.0 / self.sx, fy=1.0 / self.sy,
                interpolation=_cv2.INTER_AREA,
            )
            self._photo = np_to_photoimage(scaled, zoom=1)
        if self._photo is not None:
            self.attributes("-alpha", 1.0)
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            self.attributes("-alpha", 0.3)
        self.start = None
        self.rect = None
        tip = ("画面已冻结：拖动鼠标框选目标（例如 MaxBet 按钮），松开鼠标即完成，按 Esc 取消"
               if self._photo is not None else
               "拖动鼠标框选目标（例如 MaxBet 按钮），松开鼠标即完成，按 Esc 取消")
        self.canvas.create_text(
            self.winfo_screenwidth() // 2,
            40,
            text=tip,
            fill="#ffdd55",
            font=UI_FONT_BOLD,
        )
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", self._on_destroy)
        overlay_take_over_mouse(self)

    def _on_destroy(self, event):
        if event.widget is self and self.on_close:
            self.on_close()

    def on_press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#00ffcc", width=2
        )

    def on_drag(self, e):
        if self.rect:
            self.canvas.coords(self.rect, self.start[0], self.start[1], e.x, e.y)

    def on_release(self, e):
        x1, y1 = self.start
        x2, y2 = e.x, e.y
        lx, ly = min(x1, x2), min(y1, y2)
        lw, lh = abs(x2 - x1), abs(y2 - y1)
        self.destroy()
        if lw >= 8 and lh >= 8:
            # 逻辑坐标 -> 物理坐标，和截图/识图保持同一套像素
            x, y = self.to_physical(lx, ly)
            x2p, y2p = self.to_physical(lx + lw, ly + lh)
            self.callback(x, y, x2p - x, y2p - y)


class StopOverlay(tk.Toplevel):
    """运行期间置顶的红色急停按钮：窗口最小化也点得到"""

    def __init__(self, master, on_stop):
        super().__init__(master)
        self.on_stop = on_stop
        self.overrideredirect(False)
        self.title("急停")
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.95)
        self.configure(bg=DANGER)
        w, h = 220, 74
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{sw - w - 20}+{sh - h - 60}")
        btn = tk.Button(
            self,
            text="■  停止运行\n(F12 / End 也可以)",
            command=self._click,
            bg=DANGER,
            fg="white",
            activebackground="#b3271d",
            activeforeground="white",
            font=UI_FONT_BOLD,
            relief="flat",
            bd=0,
            cursor="hand2",
        )
        btn.pack(fill="both", expand=True, padx=4, pady=(4, 0))
        self.lbl = tk.Label(self, text="", bg=DANGER, fg="#ffd9d6", font=("Microsoft YaHei UI", 8))
        self.lbl.pack(fill="x", pady=(0, 4))

    def _click(self):
        try:
            self.on_stop()
        except Exception:
            pass

    def tick(self, text):
        try:
            self.lbl.config(text=text)
        except Exception:
            pass


class PointPicker(tk.Toplevel, ScaledOverlay):
    """全屏取点：优先用冻结截图当背景（画面定格、鼠标被浮层独占），
    把鼠标移到一个点上点一下，就记下这个坐标；没有冻结帧时退回半透明遮罩。

    冻结帧按缩放比例缩小显示，取点时换算回物理坐标——高缩放屏幕上
    否则记下来的是逻辑像素，和截图坐标差一个缩放倍数。
    """

    def __init__(self, master, name, callback, on_close=None, background=None):
        super().__init__(master)
        self.callback = callback
        self.on_close = on_close
        self.attributes("-fullscreen", True)
        self._init_overlay(background)
        self.canvas = tk.Canvas(self, bg="black", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self._photo = None
        if background is not None:
            import cv2 as _cv2

            scaled = _cv2.resize(
                background, None, fx=1.0 / self.sx, fy=1.0 / self.sy,
                interpolation=_cv2.INTER_AREA,
            )
            self._photo = np_to_photoimage(scaled, zoom=1)
        if self._photo is not None:
            self.attributes("-alpha", 1.0)
            self.canvas.create_image(0, 0, image=self._photo, anchor="nw")
        else:
            self.attributes("-alpha", 0.25)
        tip = (f"画面已冻结：把鼠标移到「{name}」上点一下即可记录坐标（按 Esc 取消）"
               if self._photo is not None else
               f"把鼠标移到「{name}」上点一下即可记录坐标（按 Esc 取消）")
        self.text = self.canvas.create_text(
            self.winfo_screenwidth() // 2,
            40,
            text=tip,
            fill="#ffdd55",
            font=UI_FONT_BOLD,
        )
        self.cross_h = self.canvas.create_line(0, 0, 0, 0, fill="#00ffcc", width=1)
        self.cross_v = self.canvas.create_line(0, 0, 0, 0, fill="#00ffcc", width=1)
        self.pos = self.canvas.create_text(
            self.winfo_screenwidth() // 2, 70, text="", fill="#00ffcc",
            font=MONO_BOLD,
        )
        self.canvas.bind("<Motion>", self.on_move)
        self.canvas.bind("<ButtonPress-1>", self.on_click)
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Destroy>", self._on_destroy)
        overlay_take_over_mouse(self)

    def _on_destroy(self, event):
        if event.widget is self and self.on_close:
            self.on_close()

    def on_move(self, e):
        self.canvas.coords(self.cross_h, 0, e.y, self.winfo_screenwidth(), e.y)
        self.canvas.coords(self.cross_v, e.x, 0, e.x, self.winfo_screenheight())
        px, py = self.to_physical(e.x, e.y)
        self.canvas.itemconfig(self.pos, text=f"屏幕坐标 {px}, {py}")

    def on_click(self, e):
        # 存物理坐标：截图、识图、鼠标点击全都用物理像素
        x, y = self.to_physical(e.x, e.y)
        self.destroy()
        self.callback(x, y)


def np_to_photoimage(img_bgr, zoom=1):
    """把 BGR ndarray 转成 tkinter 能显示的 PhotoImage（内置 PNG 支持，不需要 PIL）"""
    if img_bgr is None or img_bgr.size == 0:
        return None
    if zoom > 1:
        img_bgr = cv2.resize(
            img_bgr, None, fx=zoom, fy=zoom, interpolation=cv2.INTER_NEAREST
        )
    ok, buf = cv2.imencode(".png", img_bgr)
    if not ok:
        return None
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()).decode("ascii"))


class CreditMonitor(tk.Toplevel):
    """置顶实时读数窗口：左边是识别区域放大图，右边是识别出的数字"""

    def __init__(self, master, on_close=None):
        super().__init__(master)
        self.on_close = on_close
        self.title("Credit 实时读数")
        self.attributes("-topmost", True)
        self.configure(bg="#111")
        self.geometry("400x210+40+40")
        self.resizable(False, False)

        self.lbl_img = tk.Label(self, bg="#111", bd=1, relief="solid")
        self.lbl_img.pack(side="left", padx=10, pady=10)
        box = tk.Frame(self, bg="#111")
        box.pack(side="left", fill="both", expand=True, padx=(0, 10), pady=10)
        ttk.Label(box, text="识别结果", background="#111", foreground="#888").pack(anchor="w")
        self.lbl_val = tk.Label(
            box, text="—", bg="#111", fg="#7fff7f", font=("Consolas", 34, "bold")
        )
        self.lbl_val.pack(anchor="w", pady=4)
        self.lbl_info = tk.Label(
            box, text="", bg="#111", fg="#aaa", font=("Microsoft YaHei", 8),
            wraplength=210, justify="left",
        )
        self.lbl_info.pack(anchor="w")
        self._img_ref = None
        self.bind("<Destroy>", self._on_destroy)

    def _on_destroy(self, event):
        if event.widget is self and self.on_close:
            self.on_close()

    def update_reading(self, crop, value, info, suspect=False):
        """crop: 识别区域画面；value: 识别出的字符串或 None（读不出/存疑）"""
        photo = np_to_photoimage(crop, zoom=3)
        if photo is not None:
            self._img_ref = photo
            self.lbl_img.config(image=photo)
        if value is None:
            # 读数存疑时明确说「不可信」，绝不再显示一个可能是假的具体数字
            self.lbl_val.config(text="?", fg="#ff9d3d" if suspect else "#ff7777")
            self.lbl_info.config(text=str(info), fg="#ff9d3d" if suspect else "#aaa")
        elif "?" in value:
            self.lbl_val.config(text=value, fg="#ff7777")
            self.lbl_info.config(text="有段落没认出来，重新框选试试\n" + str(info))
        else:
            self.lbl_val.config(text=value, fg="#7fff7f")
            bet = int(self.master.cfg.get("bet_count", 3))
            self.lbl_info.config(
                text=("判定：不足一注，需要补币" if int(value) < bet else "判定：可以下注")
                + "\n"
                + str(info)
            )


# =========================================================================
# 主界面
# =========================================================================
class ModernApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"LuraSlot 自动化助手 v{APP_VERSION}")
        self.cfg = dict(DEFAULT_CFG)
        self.tpls = {}
        self.tpl_variants = {}  # {key: [多视角模板, ...]}
        self.screen = None
        self.engine = None
        self.collector = None  # 自动采集线程（与自动运行互斥）
        self._pick_frame = None  # 取点/框选时的冻结帧（录模板直接从它裁剪）
        self._frozen_view = None  # (冻结帧, 指纹) 用于取点结束后核对视角有没有被转动
        self.logq = queue.Queue()
        self.vars = {}

        os.makedirs(TPL_DIR, exist_ok=True)
        self.load_cfg()
        self.load_templates()
        self.build_ui()
        self._fit_window()
        self.after(120, self.pump_log)
        self.after(600, self.tick_credit_readout)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        if pdi is None:
            self.log("[警告] 未安装 pydirectinput，无法模拟鼠标（请先运行 安装依赖.bat）")
        entries = stop_key_entries(self.cfg)
        names = " / ".join(n for _, n in entries)
        # Single-key events work even while the automation is holding Tab.
        self._bind_stop_hooks()
        if any(vk == VK_BY_NAME["Pause"] for vk, _ in entries):
            self.log("[注意] 你启用了 Pause 作急停键：Windows 把它用作"
                     "「正在拖动窗口」，拖动标题栏时会误触发停止。")

    # ---------- 配置 ----------
    def load_cfg(self):
        if os.path.exists(CFG_PATH):
            try:
                with open(CFG_PATH, "r", encoding="utf-8") as f:
                    self.cfg.update(json.load(f))
            except Exception:
                pass

    def save_cfg(self):
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        self.log("配置已保存")

    def load_templates(self):
        self.tpls.clear()
        self.tpl_variants = {}
        for key, _ in TEMPLATES:
            variants = load_template_variants(key)
            if variants:
                self.tpls[key] = variants[0]
                self.tpl_variants[key] = variants

    # ---------- 界面 ----------
    def _card(self, parent, title=None, subtitle=None):
        """白底卡片容器：所有功能块都放进卡片，视觉上分组而不是糊成一片。"""
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(16, 14, 16, 14))
        body = ttk.Frame(outer, style="Card.TFrame")
        if title:
            ttk.Label(outer, text=title, style="Card.TLabel",
                      font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        if subtitle:
            self._wrap(outer, subtitle, "CardMuted.TLabel").pack(
                anchor="w", fill="x", pady=(4, 10))
        body.pack(fill="both", expand=True, pady=(8 if (title or subtitle) else 0, 0))
        outer.pack(fill="x", padx=14, pady=(0, 12))
        return body

    def _wrap(self, parent, text, style="Muted.TLabel", width=None):
        """带折行的说明文字：宽度跟随窗口，缩放/DPI 变化时不会排版错位。"""
        label = ttk.Label(parent, text=text, style=style, justify="left",
                          wraplength=width or self._wrap_width())
        self._wrappers.append(label)
        return label

    def _wrap_width(self):
        try:
            return max(360, self.winfo_width() - 96)
        except Exception:
            return 840







    # ---------- 样本与采集中心 ----------
    def open_sample_center(self):
        """样本与采集中心：数字样本 + 部件模板 + 模板库收在一个窗口。

        用户反馈这些入口散、找不到（"把部件和数字那块整合到一起"）；
        合并后所有采集/导入动作从这里进，状态每秒自动刷新。
        """
        if getattr(self, "_sample_center", None) is not None:
            try:
                self._sample_center.deiconify()
                self._sample_center.lift()
                return
            except tk.TclError:
                self._sample_center = None
        win = tk.Toplevel(self)
        win.title("样本与采集中心")
        win.transient(self)
        win.resizable(False, False)
        self._sample_center = win

        # —— 数字样本 ——
        dig = self._card(
            win, "数字样本（教程序认 Credit 数字）",
            "手动：机器显示某个数时点「采集当前读数样本」，填上你看到的数字。\n"
            "自动：程序自己投币让 Credit 涨、涨一个截一张（可转视角），会接管鼠标；\n"
            "随时按急停键、点置顶停止按钮或「停止采集」。",
        )
        drow = ttk.Frame(dig, style="Card.TFrame")
        drow.pack(fill="x")
        ttk.Button(drow, text="采集当前读数样本", style="Accent.TButton",
                   command=self.collect_digit_sample).pack(side="left", padx=(0, 8))
        ttk.Button(drow, text="自动批量采集（投币 + 变角度）",
                   command=self.open_auto_collect).pack(side="left", padx=(0, 8))
        ttk.Button(drow, text="打开样本目录", style="Ghost.TButton",
                   command=self.open_sample_dir).pack(side="left")
        review = ttk.Frame(dig)
        review.pack(fill="x", pady=(8, 0))
        ttk.Button(review, text="打开待审核样本", command=self.open_pending_samples).pack(side="left", padx=(0, 8))
        ttk.Button(review, text="审核并填写真实数字", command=self.review_pending_sample).pack(side="left")
        self.lbl_digits_center = self._wrap(dig, "", style="Card.TLabel")
        self.lbl_digits_center.pack(anchor="w", fill="x", pady=(10, 0))

        # —— 部件模板 ——
        part = self._card(
            win, "部件模板（教程序认按钮和摇杆）",
            "把多视角截图按部件放进「其他截图/」的子文件夹（bet / maxbet / 摇杆 / "
            "硬币堆（投币处）截图 / 按钮 / 合照），点「导入」一键变成模板。\n"
            "bet & maxbet 同框自动按「左=bet、右=maxbet」裁；三按钮合照按左中右 = 按钮 1/2/3；"
            "「全局布局图」不导入。导入日志逐条打印归属，不对的直接在模板目录里删。",
        )
        prow = ttk.Frame(part, style="Card.TFrame")
        prow.pack(fill="x")
        ttk.Button(prow, text="从截图目录导入部件模板", style="Accent.TButton",
                   command=self.import_part_samples).pack(side="left", padx=(0, 8))
        ttk.Button(prow, text="打开模板目录", style="Ghost.TButton",
                   command=self.open_tpl_dir).pack(side="left")
        self.lbl_parts_center = self._wrap(part, "", style="Card.TLabel")
        self.lbl_parts_center.pack(anchor="w", fill="x", pady=(10, 0))

        # —— 模板库（自我训练）——
        bank = self._card(
            win, "模板库（自我训练）",
            "所有样本建成模板库，读数时和段码互相印证；样本越多、视角越全，识别越稳。",
        )
        brow = ttk.Frame(bank, style="Card.TFrame")
        brow.pack(fill="x")
        ttk.Button(brow, text="重建模板库", style="Ghost.TButton",
                   command=self.rebuild_digit_bank).pack(side="left")
        self.lbl_bank_center = self._wrap(bank, "", style="CardMuted.TLabel")
        self.lbl_bank_center.pack(anchor="w", fill="x", pady=(10, 0))

        def _refresh():
            """每秒刷新状态（只读文件名和缓存的库摘要，开销可忽略）；
            窗口关掉后 winfo_exists 为 False，链条自然停。"""
            if not win.winfo_exists():
                return
            try:
                stats = self.digit_sample_stats()
                if stats:
                    self.lbl_digits_center.config(
                        text=f"已采集样本：{len(stats)} 个数值 / {sum(stats.values())} 张"
                             f"（覆盖 0~99 越全，识别越稳）")
                else:
                    self.lbl_digits_center.config(text="还没有样本：先用上面的按钮采几张。")
                self.lbl_parts_center.config(text=self._parts_summary())
                self.lbl_bank_center.config(text=f"模板库：{bank_summary(_DIGIT_BANK)}")
            except Exception:  # noqa: BLE001
                pass
            win.after(1000, _refresh)

        _refresh()

    def _parts_summary(self):
        """每个部件有多少张模板（主模板 + 多视角），一行汇总。"""
        parts = []
        for key, label in TEMPLATES:
            n = len(load_template_variants(key))
            parts.append(f"{label} {n}")
        return "部件模板：" + "　".join(parts)

    def update_parts_label(self):
        """每个部件有多少张模板（主模板 + 多视角）。"""
        if not hasattr(self, "lbl_parts"):
            return
        parts = []
        for key, name in TEMPLATES:
            n = len(self.tpl_variants.get(key) or ([] if self.tpls.get(key) is None else [1]))
            if n:
                parts.append(f"{name} {n}")
        self.lbl_parts.config(
            text="现有模板：" + ("、".join(parts) if parts else "还没有，先录模板或导入部件截图"))

    def open_tpl_dir(self):
        """打开模板目录，方便用户自己增删（templates/<部件名>/ 下放多视角图）。"""
        try:
            os.makedirs(TPL_DIR, exist_ok=True)
            os.startfile(TPL_DIR)  # noqa: S606 仅打开本机目录
            self.log(f"[模板] 已打开 {TPL_DIR}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[提示] 打不开模板目录：{e}")

    def import_part_samples(self):
        """把部件多视角截图导入成模板。

        bet 和 maxbet 的按钮本体没有区别，合照只能靠左右位置切；这条规则是
        用户自己在 说明.txt 里写明的，导入日志会把「哪张图的左边归了 bet」
        逐条打出来，方便事后核对、删掉拿不准的。
        """
        default = os.path.join(APP_DIR, "其他截图")
        src = default
        if not os.path.isdir(src):
            src = filedialog.askdirectory(
                title="选择部件截图目录（里面按部件名分文件夹）",
                initialdir=APP_DIR)
            if not src:
                return
        got = import_button_samples(src, log=self.log)
        if not got:
            messagebox.showinfo(
                "没有可导入的",
                f"在\n{src}\n里没找到能识别的部件截图。\n"
                "目录里应该按部件名分文件夹（bet / maxbet / 摇杆 / 硬币堆（投币处）截图 …）。")
            return
        self.load_templates()
        self.update_parts_label()
        detail = "\n".join(
            f"　{TPL_LABEL.get(k, k)}：{n} 张" for k, n in sorted(got.items()))
        self.log(f"[部件] 导入完成，共 {sum(got.values())} 张")
        messagebox.showinfo(
            "导入完成",
            f"共导入 {sum(got.values())} 张：\n{detail}\n\n"
            "合照是按「左=bet、右=maxbet」「左中右=按钮1/2/3」裁的；\n"
            "想核对就点「打开模板目录」，不对的直接删掉。")

    def open_auto_collect(self):
        """自动采集向导：投币 → 读真值 → 截图（含转视角）→ 再投币。"""
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "自动运行还在跑，请先停止")
            return
        if self.collector and self.collector.is_alive():
            messagebox.showwarning("提示", "采集已经在进行中")
            return
        # 只查采集真正用到的两样：Coin 位置（要能投币）和 Credit 区域（要能读真值）。
        # 不能复用开跑前的 _missing_requirements——那个会连拉杆、三个停止按钮一起查，
        # 而采集根本用不到它们。
        missing = []
        if self.cfg.get("locate_mode", "coord") == "coord":
            if not (self.cfg.get("points") or {}).get("coin"):
                missing.append("Coin（投币处）还没取点")
        elif "coin" not in self.tpls:
            missing.append("Coin（投币处）还没录模板")
        if not self.cfg.get("credit_rect"):
            missing.append("还没框选 Credit（游戏币数量）区域")
        if missing:
            messagebox.showinfo("还不能开始", "自动采集需要先具备：\n　· " + "\n　· ".join(missing))
            return
        win = tk.Toplevel(self)
        win.title("自动采集数字样本")
        win.transient(self)
        win.resizable(False, False)
        pad = {"padx": 12, "pady": 6}
        ttk.Label(
            win,
            text="程序会自动投币、读数字、截图，还会转几个角度各截一张。\n"
                 "采集期间会真实操作鼠标，请先把游戏窗口切到前台，然后不要动鼠标。",
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=(12, 8))

        v_target = tk.StringVar(value="10")
        v_coins = tk.StringVar(value="1")
        v_base = tk.StringVar(value="120")
        v_mults = tk.StringVar(value="0")
        v_settle = tk.StringVar(value="0.8")
        v_out = tk.StringVar(value="待审核数字样本")

        rows = (
            ("采集到（含）", v_target, "Credit 涨到几就停，默认 10"),
            ("每次投币数", v_coins, "1 = 一个一个涨，样本最全"),
            ("转视角拖动幅度(像素)", v_base, "视角转多少，先小一点试"),
            ("角度倍数", v_mults, "0=正面；1/-1 左右各转一次；可写 0,1,-1"),
            ("转完等待(秒)", v_settle, "等画面稳定再读，太短会读错"),
            ("保存到", v_out, "自动采集先待审核；确认真值后才进入正式库"),
        )
        for i, (label, var, tip) in enumerate(rows, start=1):
            ttk.Label(win, text=label).grid(row=i, column=0, sticky="w", **pad)
            if var is v_out:
                ttk.Combobox(win, textvariable=var, width=14, state="readonly",
                             values=("待审核数字样本",)).grid(
                    row=i, column=1, sticky="w", **pad)
            else:
                ttk.Entry(win, textvariable=var, width=14).grid(
                    row=i, column=1, sticky="w", **pad)
            ttk.Label(win, text=tip, foreground=MUTED).grid(
                row=i, column=2, sticky="w", **pad)

        r = len(rows) + 1
        status = ttk.Label(win, text="", foreground=MUTED)
        status.grid(row=r, column=0, columnspan=3, sticky="w", padx=12, pady=(4, 0))
        btn_row = ttk.Frame(win)
        btn_row.grid(row=r + 1, column=0, columnspan=3, pady=(10, 12))
        btn_start = ttk.Button(btn_row, text="开始采集", style="Accent.TButton")
        btn_start.pack(side="left", padx=(0, 8))
        btn_stop = ttk.Button(btn_row, text="停止采集", state="disabled")
        btn_stop.pack(side="left")

        def _num(var, default):
            try:
                return int(float(var.get()))
            except (TypeError, ValueError):
                return default

        def _mults():
            out = []
            for piece in v_mults.get().replace("，", ",").split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    out.append(float(piece))
                except ValueError:
                    pass
            return out or [0.0]

        def finish(reason, count):
            def _ui():
                try:
                    btn_start.config(state="normal")
                    btn_stop.config(state="disabled")
                    status.config(text=f"结束：{reason or '已停止'}（本次保存 {count} 张）")
                    self.update_credit_label()
                    self.update_bank_label()
                    self._refresh_run_buttons()
                    if hasattr(self, "lbl_auto"):
                        self.lbl_auto.config(
                            text=f"上次采集：{reason or '已停止'}，保存 {count} 张")
                except tk.TclError:
                    pass  # 向导窗口已被关掉：按钮没了，剩下的状态刷新照常
                self._close_overlay()
            try:
                self._post(_ui)
            except tk.TclError:
                # 向导已被用户关掉：改在主窗口调度收尾，置顶停止按钮必须收掉
                self._post(_ui)

        def start():
            if self.busy():
                return
            self.collect_cfg()
            angles = rotate_plan(_num(v_base, 120), _mults())
            params = {
                "target": _num(v_target, 99),
                "coins": _num(v_coins, 1),
                "angles": angles,
                "settle": parse_seconds(v_settle.get(), 0.8),
                "out_dir": os.path.join(DATA_DIR, "待审核数字样本"),
            }
            if not self.cfg.get("credit_rect"):
                messagebox.showinfo("先框选", "请先「框选 Credit（游戏币数量）区域」。")
                return
            if self.screen is None:
                self.screen = Screen()
            self.collector = AutoCollector(
                self.cfg, dict(self.tpls), dict(self.tpl_variants),
                self.log, self.screen, params, on_done=finish)
            btn_start.config(state="disabled")
            btn_stop.config(state="normal")
            status.config(text=f"采集中：角度 {angles or ['仅正面']}")
            self.log(f"[自动采集] 开始（目标 {params['target']}，每次投 "
                     f"{params['coins']} 枚，角度 {angles or '仅正面'}）")
            self.collector.start()
            self._refresh_run_buttons()
            # 置顶急停按钮：采集会接管鼠标，游戏全屏时向导可能点不到，
            # 必须有一个永远在最上面的「停止」
            try:
                self.overlay = StopOverlay(self, self.request_stop)
                self.overlay.tick("自动采集中")
            except Exception as e:  # noqa: BLE001
                self.log(f"[警告] 置顶停止按钮创建失败：{e}")

        def stop():
            if self.collector:
                self.collector.stop_flag.set()
                status.config(text="正在停止…")

        btn_start.config(command=start)
        btn_stop.config(command=stop)
        win.protocol("WM_DELETE_WINDOW", lambda: (stop(), win.destroy()))
        try:
            win.grab_set()
        except tk.TclError:
            pass


    # ---------- 停止热键（自定义）----------
    def refresh_stop_keys(self):
        """把当前热键画成一排带 ✕ 的标签。"""
        for w in self.hk_frame.winfo_children():
            w.destroy()
        entries = stop_key_entries(self.cfg)
        for _vk, name in entries:
            chip = ttk.Frame(self.hk_frame, style="Card.TFrame")
            chip.pack(side="left", padx=(0, 6))
            ttk.Label(chip, text=name, style="Card.TLabel").pack(side="left")
            ttk.Button(chip, text="X", width=2, style="Ghost.TButton",
                       command=lambda n=name: self.remove_stop_key(n)).pack(
                side="left", padx=(2, 0))
        need = max(2, int(self.cfg.get("force_quit_presses", 2) or 2))
        try:
            self.lbl_hk.configure(
                text=f"当前：{' / '.join(n for _, n in entries)}　·　"
                     f"连按 {need} 次可直接结束进程（普通停止卡住时用）　·　"
                     f"改动在下次开始运行时生效")
        except tk.TclError:
            pass

    def _write_stop_keys(self, names, note):
        self.cfg["stop_keys"] = names
        self.cfg["extra_stop_key"] = ""   # 旧字段作废，避免重复
        self.save_cfg()
        self.refresh_stop_keys()
        self.log(note)
        try:
            entries = stop_key_entries(self.cfg)
            names = " / ".join(n for _, n in entries)
            self.lbl_estop.configure(
                text=f"急停：① 右上角置顶红色「停止运行」 ② {names}（可在参数页自定义）"
                     f" ③ 鼠标甩到屏幕左上角")
            self.btn_stop.configure(text=f"■  停止 ({entries[0][1]})")
        except (tk.TclError, AttributeError):
            pass

    def remove_stop_key(self, name):
        rest = [n for _vk, n in stop_key_entries(self.cfg) if n != name]
        if not rest:
            # 一个都不留就没法停止了，直接回默认而不是留空
            rest = list(DEFAULT_STOP_KEYS)
            self.log("[提示] 至少要留一个停止键，已恢复为默认")
        self._write_stop_keys(rest, f"已移除停止热键：{name}")

    def reset_stop_keys(self):
        self._write_stop_keys(list(DEFAULT_STOP_KEYS),
                              f"停止热键已恢复默认：{' / '.join(DEFAULT_STOP_KEYS)}")

    def add_stop_key(self):
        """弹窗让用户直接按一个键来当停止键（比让他查键名直观）。"""
        win = tk.Toplevel(self)
        win.title("设置停止热键")
        win.transient(self)
        win.geometry("440x170")
        win.resizable(False, False)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        ttk.Label(win, text="请按下你想用作「停止运行」的键",
                  font=("Microsoft YaHei UI", 11, "bold")).pack(pady=(26, 6))
        var = tk.StringVar(value="等待按键…（按 Esc 取消）")
        ttk.Label(win, textvariable=var, style="Muted.TLabel").pack()
        state = {"armed": False, "done": False}

        def finish(name):
            state["done"] = True
            win.destroy()
            if name == "Esc":
                self.log("已取消添加停止热键")
                return
            have = [n for _vk, n in stop_key_entries(self.cfg)]
            if name in have:
                self.log(f"停止热键 {name} 已经在列表里了")
                return
            self._write_stop_keys(
                have + [name],
                f"已添加停止热键：{name}（下次开始运行时生效）")

        def poll():
            if state["done"]:
                return
            down = [vk for vk in VK_BY_NAME.values() if key_pressed(vk)]
            if not down:
                # 先等所有键松开，才认下一次按下，避免把点击时的残留当成输入
                state["armed"] = True
                var.set("等待按键…（按 Esc 取消）")
            elif state["armed"]:
                finish(NAME_BY_VK.get(down[0], "?"))
                return
            win.after(60, poll)

        win.after(250, poll)
        try:
            win.grab_set()
        except tk.TclError:
            pass

    # ---------- 真实数字样本采集 ----------
    def collect_digit_sample(self):
        """把当前 Credit 区域存成一张带真值的样本。

        必须走 grab_credit()（运行时同一条取图链路），而不是让用户拿系统
        截图工具另截一张——那样缩放、选区、DPI 都跟实际运行不一致，
        拿来做基准等于在测另一件事。

        用法：让机器显示某个数字（比如投币到 47），点这个按钮，
        把屏幕上看到的数字填进去。每个数字多采几张最好。
        """
        if not self.cfg.get("credit_rect"):
            messagebox.showinfo("先框选", "请先「框选 Credit（游戏币数量）区域」。")
            return
        # 必须走 grab_credit()（运行时同一条取图链路）。曾经直接写
        # self.grab_credit()——那是 Engine 的方法，App 上没有，点击按钮
        # 抛 AttributeError 被 tkinter 吞掉，按钮看起来毫无反应。
        crop = self._reader().grab_credit()
        if crop is None or getattr(crop, "size", 0) == 0:
            messagebox.showinfo("采集失败", "抓不到 Credit 区域画面。")
            return
        val, _ = decode_led_fused(crop, get_digit_bank())
        guess = val if val and "?" not in str(val) else ""
        ans = simpledialog.askstring(
            "采集数字样本",
            "屏幕上现在显示的数字是？\n"
            f"（程序当前读成：{guess or '读不出'}，以你看到的为准）\n\n"
            "多位数直接连着填，例如 47、99、03。",
            initialvalue=guess, parent=self)
        if not ans:
            return
        truth = ans.strip()
        if not truth.isdigit():
            messagebox.showinfo("格式不对", "请只填数字，例如 0、7、47。")
            return
        try:
            os.makedirs(DIGITS_DIR, exist_ok=True)
            name = f"v{truth}_{time.strftime('%Y%m%d_%H%M%S')}.png"
            path = os.path.join(DIGITS_DIR, name)
            # 必须用 save_image：cv2.imwrite 在 Windows 上写不了中文路径
            if not save_image(path, crop):
                raise OSError("写入失败")
        except Exception as e:  # noqa: BLE001
            self.log(f"[错误] 样本保存失败：{e}")
            return
        self.log(f"[样本] 已保存 {name}（真值 {truth}，程序读成 {guess or '读不出'}）")
        # 存完立刻重建模板库：用户点一次采集，效果当场可见
        get_digit_bank(force=True)
        self.update_credit_label()
        self.update_bank_label()

    def digit_sample_stats(self):
        """已采集样本：{真值: 张数}。

        三个来源都算：程序内手动采集（v*.png）、运行中自动采集（auto*.png）、
        用户自己放进 数字截图/ 的（41.png / 41-2.png…，含子目录）。
        只读文件名不读图，所以放进几百张也不会卡。
        """
        stats = {}
        seen = set()
        for d in SAMPLE_DIRS:
            if not os.path.isdir(d) or d in seen:
                continue
            seen.add(d)
            try:
                for cur, _subs, files in os.walk(d):
                    for fn in files:
                        if not fn.lower().endswith(".png"):
                            continue
                        truth = sample_truth_of(fn)
                        if truth:
                            stats[truth] = stats.get(truth, 0) + 1
            except OSError:
                continue
        return stats

    def rebuild_digit_bank(self):
        """手动重建模板库（用户新拷了一批样本进来时用）。"""
        bank = get_digit_bank(force=True)
        self.update_bank_label()
        self.log(f"[自训练] 模板库已重建：{bank_summary(bank)}")

    def open_sample_dir(self):
        """打开样本目录，方便用户把自己截的图拷进去（41.png / 41-2.png）。

        优先开 数字截图/——那是写进说明文档、用来跟别人共享的地方；
        没有（发布版默认不带）就开程序自己存样本的 digits/。
        """
        shared = os.path.join(APP_DIR, "数字截图")
        target = shared if os.path.isdir(shared) else DIGITS_DIR
        try:
            os.makedirs(target, exist_ok=True)
            os.startfile(target)  # noqa: S606 仅打开本机目录
            self.log(f"[样本] 已打开样本目录：{target}")
        except Exception as e:  # noqa: BLE001
            self.log(f"[提示] 打不开样本目录：{e}")

    def update_bank_label(self):
        """模板库状态：规模 + 是否还在长。"""
        if not hasattr(self, "lbl_bank"):
            return
        bank = get_digit_bank()
        stats = self.digit_sample_stats()
        n_val = len(stats)
        text = f"模板库：{bank_summary(bank)}　|　样本覆盖 {n_val} 个不同数值"
        if not bank:
            text += "\n还没有样本：先点「采集数字样本」采几张，或把截图按 41.png / 41-2.png 命名放进样本目录。"
        try:
            self.lbl_bank.config(text=text)
        except tk.TclError:
            pass

    def _missing_requirements(self):
        """返回开跑前还缺什么（列表），不弹窗、不改状态，只用来更新按钮提示。"""
        missing = []
        try:
            bet = int(self._get_num("bet_count", self.cfg.get("bet_count", 3)))
        except (TypeError, ValueError):
            bet = 3
        need = ["coin", "maxbet", "lever", "btn1", "btn2", "btn3"]
        if bet < 3:
            need.append("bet")
        if self.cfg.get("locate_mode", "coord") == "coord":
            pts = self.cfg.get("points") or {}
            if hasattr(self, "pt_vars"):
                pts = {}
                for key, _ in TEMPLATES:
                    vx, vy = self.pt_vars[key]
                    if vx.get().strip() and vy.get().strip():
                        pts[key] = (vx.get(), vy.get())
            gone = [TPL_LABEL[k] for k in need if not pts.get(k)]
            if gone:
                missing.append("还没取点：" + "、".join(gone))
        else:
            gone = [TPL_LABEL[k] for k in need if k not in self.tpls]
            if gone:
                missing.append("还没录模板：" + "、".join(gone))
        if not (self.cfg.get("credit_rect") or self.cfg.get("watch_rect")):
            missing.append("还没框选 Credit 数字区")
        return missing

    def _refresh_run_buttons(self):
        """按当前配置和运行状态更新「开始运行 / 停止」按钮。

        之前这里把开始按钮写死成 disabled，只有跑完一轮才恢复，
        导致首次打开永远是灰的、点不动。现在状态由配置推导：
        缺什么就显示什么提示，缺得多时才真的禁用。
        """
        running = bool(self.engine and self.engine.is_alive())
        if self.collector and self.collector.is_alive():
            # 采集也在动鼠标：开始必须禁用，但「停止」必须可用——
            # 曾经连停止都灰着，采集时就只剩向导里一个小按钮能停
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
            self.lbl_ready.config(text="自动采集进行中（停止按钮/急停键都可停）", foreground=WARN)
            return
        if running:
            self.btn_start.config(state="disabled")
            self.btn_stop.config(state="normal")
            self.lbl_ready.config(text="运行中…", foreground=ACCENT)
            return
        self.btn_stop.config(state="disabled")
        missing = self._missing_requirements()
        if missing:
            # 还没配好时不禁用按钮：让用户点了能立刻看到缺什么，
            # 灰着不给点反而让人以为是程序坏了。
            self.btn_start.config(state="normal")
            self.lbl_ready.config(text="；".join(missing), foreground=WARN)
        else:
            self.btn_start.config(state="normal")
            self.lbl_ready.config(text="配置就绪，可以开始（建议先跑一次自检）", foreground=OK)



    def _clear_log(self):
        self.txt.configure(state="normal")
        self.txt.delete("1.0", "end")
        self.txt.configure(state="disabled")



    # ---------- Tab / 窗口诊断 ----------
    def activate_game(self):
        kw = self.vars.get("game_window")
        keyword = kw.get().strip() if kw else "VRChat"
        wins = list_windows(keyword)
        if not wins:
            self.log(f"[窗口] 没找到标题含「{keyword}」的可见窗口")
            return
        ok = activate_window(wins[0][0])
        self.log(f"[窗口] 激活「{wins[0][1]}」：{'成功' if ok else '失败，请手动点一下游戏'}")

    def test_tab(self):
        kw = self.vars.get("game_window")
        keyword = kw.get().strip() if kw else "VRChat"
        wins = list_windows(keyword)
        if wins:
            activate_window(wins[0][0])
            self.log(f"[测试] 已激活游戏窗口「{wins[0][1]}」")
        else:
            self.log(f"[测试] 没找到「{keyword}」窗口，3 秒后开始按 Tab（请手动点一下游戏窗口）")
        self.iconify()

        def _hold():
            time.sleep(1.0)
            interval = max(0.1, self._get_num("tab_repeat", 400) / 1000.0)
            holder = TabHolder(interval=interval)
            holder.start()
            self.log(f"[测试] 开始按住 Tab 6 秒（每 {int(interval * 1000)} 毫秒续按）… 看游戏里光标是否出现")
            time.sleep(6)
            holder.release()
            self.log("[测试] 已松开 Tab")
            self.after(300, self.deiconify)

        threading.Thread(target=_hold, daemon=True).start()

    # ---------- 模板 / 坐标点操作 ----------
    def on_mode_change(self):
        coord = self.v_mode.get() == "coord"
        self.lbl_tip.config(
            text=(
                "固定坐标模式：点「取点」→ 游戏自动切前台、鼠标被锁住、画面冻结，"
                "把鼠标移到目标上点一下即可记录坐标；也可直接手填 X/Y。\n"
                "冻结全程视角不会动；取完点如果提示「画面变化了」，说明视角动过，请重取一次。"
                if coord else
                "图像识别模式：点「录模板」→ 游戏画面冻结后拖框框住目标 → 松开保存，"
                "运行时会实时搜索目标位置。模板从冻结帧裁剪，所见即所得。"
            )
        )
        self.hint.config(
            text=(
                "建议顺序：① 硬币堆 ② MaxBet ③ 拉杆 ④ 按钮1/2/3（Bet 按钮只在押 1/2 枚时才需要）。\n"
                "取完点点「测试」：鼠标会真的移到那个坐标上，看停没停在目标上。运行前确认游戏视角和取点时一致。"
                if coord else
                "框选技巧：只框按钮本体，别带太多背景；录完点「测试」在当前画面里找一遍（建议相似度 0.9 以上），"
                "点「预览」放大看录进去的是什么。"
            )
        )
        for key, _ in TEMPLATES:
            self.tpl_status[key].config(
                text=("待设定" if coord else "使用识图"),
                foreground=(WARN if coord else ACCENT),
            )
        self._show_tpl_buttons()
        self.refresh_tpl_status()
        if hasattr(self, "btn_start"):
            self._refresh_run_buttons()

    def _show_tpl_buttons(self):
        """定位表的操作按钮按模式显隐：坐标模式用「取点/测试」，
        识图模式用「录模板/测试/预览」。全部显示等于每行多两个没用的按钮。"""
        coord = self.v_mode.get() == "coord"
        show = ("pick", "test") if coord else ("record", "test", "preview")
        for btns in getattr(self, "tpl_btns", {}).values():
            for b in btns.values():
                b.pack_forget()
            for name in ("pick", "record", "test", "preview"):
                if name in show:
                    btns[name].pack(side="left", padx=(0, 4))

    def refresh_tpl_status(self):
        coord = self.v_mode.get() == "coord"
        for key, _ in TEMPLATES:
            if coord:
                x, y = self.pt_vars[key][0].get().strip(), self.pt_vars[key][1].get().strip()
                if x and y:
                    self.tpl_status[key].config(text="已设定", foreground=OK)
                else:
                    self.tpl_status[key].config(text="未设定", foreground=DANGER)
            else:
                if key in self.tpls:
                    h, w = self.tpls[key].shape[:2]
                    self.tpl_status[key].config(text=f"已录制 {w}×{h}", foreground=OK)
                else:
                    self.tpl_status[key].config(text="未录制", foreground=DANGER)

    def pick_point(self, key):
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "请先停止运行再设定坐标")
            return
        self.withdraw()
        self.after(
            350,
            lambda: self._freeze_then(
                lambda frame: PointPicker(
                    self,
                    TPL_LABEL[key],
                    lambda x, y: self._save_point(key, x, y),
                    on_close=self.deiconify,
                    background=frame,
                )
            ),
        )

    def _freeze_then(self, build):
        """取点/框选流程：锁鼠标 → 把游戏切到前台 → 等画面真正稳定 → 抓一帧冻结画面
        → 用 build(frame) 创建浮层。

        关键在于「锁鼠标」必须发生在「切前台」之前：切前台那一刻游戏就拿到
        鼠标了，如果先切前台再想办法抢鼠标，中间那段空窗里手一动视角就转了，
        浮层里的冻结画面和之后的实际画面对不上，取到的坐标当场作废。
        现在整段空窗期光标都被钉住，游戏收不到净位移，视角不会动。
        """
        v = self.vars.get("game_window")
        kw = (v.get().strip() if v else "") or str(self.cfg.get("game_window", "") or "")
        if self.screen is None:
            try:
                self.screen = Screen()
            except Exception as e:  # noqa: BLE001
                self.log(f"[取点] 截屏初始化失败：{type(e).__name__}: {e}")
                self.screen = None
        try:
            # 注意 with 的范围必须把 build(frame) 也包进去：创建浮层要把整屏
            # 帧编码成 PhotoImage，这一步本身要几百毫秒，放在锁外面等于又留了一段
            # 「游戏在前台 + 鼠标自由」的空窗，取点照样会被视角转动带偏。
            with MouseLock():
                try:
                    if kw:
                        wins = list_windows(kw)
                        if wins:
                            activate_window(wins[0][0])
                    # 等游戏完成光标回中、画面稳定后再冻结（真实稳定检测，慢机器自动多等）
                    frame = wait_frames_stable(self.screen.grab) if self.screen else None
                except Exception as e:  # noqa: BLE001
                    self.log(f"[取点] 冻结画面失败，退回实时遮罩：{type(e).__name__}: {e}")
                    frame = None
                if frame is not None:
                    self._frozen_view = (frame, self._screen_signature(frame))
                self._pick_frame = frame
                if frame is not None:
                    self.log("[取点] 画面已冻结：整个过程鼠标被锁住，视角不会动；画面不对按 Esc 重来")
                build(frame)
        except Exception as e:  # noqa: BLE001
            self.log(f"[取点] 浮层创建失败：{type(e).__name__}: {e}")
            self._pick_frame = None
            self._frozen_view = None
            self.deiconify()

    @staticmethod
    def _screen_signature(frame):
        """画面指纹：灰度 + 缩小，用于后续算位移（省内存、抗细节噪声）。

        返回 (指纹, 缩放系数)，系数用于把指纹上的位移换算回原图像素。
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sig_w, sig_h = 640, 400
        small = cv2.resize(gray, (sig_w, sig_h), interpolation=cv2.INTER_AREA)
        h, w = gray.shape[:2]
        scale = ((w / float(sig_w)) + (h / float(sig_h))) / 2.0
        return small, scale

    def _check_view_drift(self):
        """取点结束后核对：现在的画面和冻结那一帧还一致吗？

        **不能用整屏像素差来判断。** 之前这里比的是 32×18 的整屏指纹，
        阈值 2.0 —— 实测两张同一视角、不同动画时刻的截图，差异高达 21~32，
        也就是说这个检测几乎每次都会误报。游戏画面本来就在不停变化
        （转轴、粒子、灯光），整屏像素永远不会一致。

        改成测「整体几何位移」：用光流算出画面平均移动了多少像素。
        转视角会让整幅画面同向平移/缩放（位移大且方向一致）；
        游戏动画只让局部动（中位位移很小，用中位数就能把离群的动画帧滤掉）。

        实测基线（换算回 2560×1600 原图）：同视角不同动画 ≈3.6px、
        亮度噪声 <0.5px；转动 30/60/120px 时分别是 30/60/120px。
        阈值取 25px：只有明显转了才提示，且提示不拦截操作。
        """
        frozen = getattr(self, "_frozen_view", None)
        self._frozen_view = None
        if not frozen or self.screen is None:
            return True, ""
        sig, scale = frozen[1]
        try:
            now, _ = self._screen_signature(self.screen.grab())
        except Exception:
            return True, ""
        if now.shape != sig.shape:
            return True, ""
        shift = scene_shift(sig, now)
        if shift is None:
            return True, ""
        shift_px = shift * scale          # 换算回原图像素，阈值才有直观意义
        if shift_px < DRIFT_SHIFT_PX:
            return True, ""
        return False, (
            f"画面相对取点时移动了约 {shift_px:.0f} 像素。"
            "如果你确实没动视角就忽略这条；如果刚才动了，"
            "建议用「移动测试」确认坐标还准不准"
        )

    def _save_point(self, key, x, y):
        self.deiconify()
        vx, vy = self.pt_vars[key]
        vx.set(str(x))
        vy.set(str(y))
        self.collect_cfg()
        self.save_cfg()
        self.refresh_tpl_status()
        self.log(f"已设定坐标：{TPL_LABEL[key]} = ({x}, {y})")
        self._refresh_run_buttons()
        self._warn_if_view_drifted()

    def _warn_if_view_drifted(self):
        """保存完坐标/模板后核对视角有没有明显转动。

        **只提示、不拦截。** 这个判断在真实游戏里无法做到既灵敏又准：
        VRChat 画面一直在动（渲染抖动、粒子、头显微晃），判轻了漏报、
        判重了误报。之前两次误报就是不断调阈值调出来的——继续调参
        只会再来第三次误报，而误报的警告比没有警告更糟：会让人习惯性
        忽略所有提醒，真出问题时也不信了。

        所以改成：明显转动（超过阈值很多）才提示一句，措辞说明这只是
        提醒、让你自己确认，不要当成必须重做的命令。坐标是否真的失效，
        由「运行前自检」和「移动测试」来给出确定答案。
        """
        ok, msg = self._check_view_drift()
        if not ok:
            self.log("[提醒] " + msg)
            # 不弹窗打断：写进日志即可，需要时用户自己能核对
        return ok

    # ---------- Credit 参考图（最可靠的"没币"判断）----------
    def credit_ref_quality(self):
        """检查已录的参考图是否可用。返回 (ok, 说明)"""
        p = self.cfg.get("credit_ref")
        if not p or not os.path.exists(p):
            return None, "未录参考图"
        img = load_image(p)
        if img is None:
            return False, "参考图读不出来，建议重录"
        m = led_mask(img)
        if m.sum() == 0:
            return False, "参考图全暗，不能确认是没币，建议重录"
        val, info = decode_led_best(img)
        if val is None or val == "" or "?" in val:
            return False, (
                f"参考图不像一组数字（亮起 {float(m.mean()) * 100:.1f}%，{info}）——"
                f"录的时候多半框歪了，建议在机器显示 0 时重录"
            )
        if int(val) != 0:
            return False, f'参考图读到 {val}，不是 0，不能用作没币参考'
        return True, f"参考图正常（读到「{val}」，亮起 {float(m.mean()) * 100:.1f}%）"

    def update_credit_ref_label(self):
        p = self.cfg.get("credit_ref")
        if not (p and os.path.exists(p)):
            self.lbl_credit_ref.config(text="未录入参考（可选）", foreground="#bb8800")
            return
        ok, msg = self.credit_ref_quality()
        if ok:
            self.lbl_credit_ref.config(text="已录「没币」参考", foreground="#17912f")
        else:
            self.lbl_credit_ref.config(text=f"参考图有问题：{msg}", foreground="#bb2222")

    def capture_credit_ref(self):
        self.withdraw()
        self.after(350, self._capture_credit_ref)

    def _capture_credit_ref(self):
        r = self.cfg.get("credit_rect")
        if not r:
            self.deiconify()
            messagebox.showinfo("提示", "先框选 Credit 区域，再录参考")
            return
        if self.screen is None:
            self.screen = Screen()
        x, y, w, h = [int(v) for v in r]
        try:
            crop = self.screen.grab_rect(x, y, w, h)
        except Exception as e:  # noqa: BLE001
            self.deiconify()
            messagebox.showerror("失败", f"截图失败：{e}")
            return
        self.deiconify()
        val, info = decode_led_best(crop)
        if not val or not val.isdigit() or int(val) != 0:
            messagebox.showwarning('参考无效', f'当前读数 {val}，必须清楚读到 0 才能录为没币参考。\n{info}')
            return
        path = os.path.join(DATA_DIR, time.strftime("credit_zero_%Y%m%d_%H%M%S.png"))
        if not save_image(path, crop):
            messagebox.showerror('保存失败', '参考图写入磁盘失败，旧参考保持不变。')
            return
        old = resolve_ref_path(self.cfg.get("credit_ref"))
        self.cfg["credit_ref"] = path
        self.save_cfg()
        self.update_credit_ref_label()
        # 只清理本次数据目录内的旧参考图；其他任何位置的文件一律不动。
        data_root = (os.path.normpath(DATA_DIR) + os.sep).lower()
        if old and os.path.isabs(old) and old != path and os.path.exists(old) \
                and os.path.normpath(old).lower().startswith(data_root):
            try:
                os.remove(old)
            except Exception:
                pass
        val, info = decode_led_best(crop)
        messagebox.showinfo(
            "已录入「没币」参考",
            f"已把当前画面记为「没币」状态。\n\n当前画面段码识别结果：{val or '全灭'}\n{info}\n\n"
            "以后只要显示和它不一样，就判定为「还有币」。\n"
            "（机器显示 0 时录最准；如果现在不是 0，请等没币时重录）",
        )
        self.log(f"已录入「没币」参考图：{path}（识别={val}）")
        self.tick_credit_readout(force=True)

    def clear_credit_ref(self):
        p = self.cfg.get("credit_ref")
        self.cfg["credit_ref"] = None
        self.save_cfg()
        self.update_credit_ref_label()
        if p and os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass
        self.log("已清除「没币」参考图")

    # ---------- Credit 数字区 ----------
    def update_credit_label(self):
        r = self.cfg.get("credit_rect")
        if r and len(r) == 4:
            text = f"已设：({r[0]},{r[1]}) {r[2]}×{r[3]}"
            fg = "#17912f"
        else:
            text = "未设置（可使用备用数字区；不使用全屏帧差）"
            fg = "#bb2222"
        stats = self.digit_sample_stats()
        if stats:
            # 样本多起来后逐个列会把这行撑爆（见过一百多个数值），
            # 所以只报规模；明细在参数页的「自我训练」卡片里看。
            text += f"　|　已采集样本：{len(stats)} 个数值 / {sum(stats.values())} 张"
        try:
            self.lbl_credit.config(text=text, foreground=fg)
        except tk.TclError:
            pass

    def pick_credit_rect(self):
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "请先停止运行再框选")
            return
        self.withdraw()
        self.after(
            350,
            lambda: self._freeze_then(
                lambda frame: RegionPicker(
                    self, self._save_credit_rect, on_close=self.deiconify, background=frame
                )
            ),
        )

    def _save_credit_rect(self, x, y, w, h):
        self.deiconify()
        self.cfg["credit_rect"] = [x, y, w, h]
        self.save_cfg()
        self.update_credit_label()
        self._refresh_run_buttons()
        self.log(f"已设定 Credit 区域：({x},{y})  {w}×{h}")
        # 框完立刻自动测一次：当场就知道框对没有，不用再手动点「读取测试」
        self.test_credit_rect()

    def clear_credit_rect(self):
        self.cfg["credit_rect"] = None
        self.save_cfg()
        self.update_credit_label()
        self.log("已清除 Credit 区域")

    def test_credit_rect(self):
        self.withdraw()
        self.after(350, self._test_credit_rect)

    def _test_credit_rect(self):
        r = self.cfg.get("credit_rect")
        if not r:
            self.deiconify()
            messagebox.showinfo("提示", "先点「框选 Credit 区域」把那个红色数字框起来")
            return
        if self.engine and self.engine.is_alive():
            self.deiconify()
            messagebox.showwarning("提示", "运行中不要做读取测试")
            return
        if self.screen is None:
            self.screen = Screen()
        x, y, w, h = [int(v) for v in r]
        try:
            crop = self.screen.grab_rect(x, y, w, h)
        except Exception as e:  # noqa: BLE001
            self.deiconify()
            messagebox.showerror("读取失败", f"截图失败：{e}")
            return
        val, info = decode_led_fused(crop, get_digit_bank())
        reader = self._reader()
        empty, einfo = reader.credit_is_empty(crop)
        self.deiconify()
        if empty is None:
            messagebox.showwarning(
                "读不出",
                f"识别结果：{val}\n\n{einfo}\n\n"
                "建议：把 Credit 区域重新框一下（只框那个红色数字），或者把「没币」参考图重录一次。",
            )
        elif empty:
            messagebox.showinfo("Credit 读取测试", f"判定：不足一注（结束本轮并正常补币）\n\n{einfo}")
        else:
            messagebox.showinfo("Credit 读取测试", f"判定：还有币\n\n{einfo}")
        self.log(f"[Credit 测试] 识别「{val}」判定={'没币' if empty else ('还有币' if empty is False else '未知')}｜{einfo}")
        self.tick_credit_readout(force=True)

    def open_credit_monitor(self):
        """打开置顶的实时读数窗口"""
        if getattr(self, "monitor", None) is not None:
            try:
                self.monitor.destroy()
            except Exception:
                pass
        self.monitor = CreditMonitor(self, on_close=self._on_monitor_closed)
        self.log("已打开 Credit 实时读数窗口（置顶，会一直显示识别结果）")
        self.tick_credit_readout(force=True)

    def _on_monitor_closed(self):
        self.monitor = None

    def _reader(self):
        """取一个只用来读 Credit 的引擎实例（每次按最新配置创建，避免配置不同步）"""
        if self.screen is None:
            self.screen = Screen()
        return Engine(dict(self.cfg), {}, self.log, self.screen)

    def _read_credit_display(self, reader, frames=3):
        """读一次用于展示的 Credit 读数。

        这里必须同时做三件事，缺一个就会出现「明明是 0 却显示 1」：
        ① 多帧一致性：单帧噪声/动画不该决定显示值；
        ② 选区截断检测：选区偏了只剩一根竖笔画时，解码器会把它当成合法的 1，
           所以只要数字块贴住选区边界，就判定读数不可信而不是显示那个假 1；
        ③ 熄屏仍然当未知，绝不显示成 0。

        返回 (显示值, 说明)。显示值可能是 '?'（不可信）。
        """
        crops = []
        for _ in range(max(1, frames)):
            crop = reader.grab_credit()
            if crop is None:
                return '?', '抓不到画面，请检查 Credit 区域是否在屏幕内'
            crops.append(crop)
        ok, fit_msg = digit_fit(crops[-1])
        value, info, agree, total = decode_consensus(crops, bank=get_digit_bank())
        if not ok:
            return '?', f'选区把数字切掉了，读数不可信（{fit_msg}）——请重新框选，框比数字略大一点'
        if value is None:
            return '?', info
        suffix = f'（{info}）'
        if value.strip('0') == '':
            return value, f'读出「{value}」→ 没币{suffix}'
        return value, f'读出「{value}」→ 还有币{suffix}'

    def tick_credit_readout(self, force=False):
        """周期性读一次 Credit 并刷新界面/监测窗口"""
        try:
            r = self.cfg.get("credit_rect")
            busy = bool(self.engine and self.engine.is_alive())
            if r and len(r) == 4 and not busy:
                reader = self._reader()
                crop = reader.grab_credit()
                if crop is None:
                    self.lbl_credit_live.config(text="—", foreground=MUTED)
                    self.lbl_credit_live_info.config(text="抓不到画面", foreground=DANGER)
                else:
                    show, info = self._read_credit_display(reader)
                    empty, einfo = reader.credit_is_empty(crop)
                    if show == '?':
                        color = WARN
                    elif empty:
                        color = OK
                    else:
                        color = ACCENT
                    self.lbl_credit_live.config(text=show, foreground=color)
                    detail = einfo if show != '?' else info
                    self.lbl_credit_live_info.config(
                        text=detail, foreground=MUTED if show != '?' else WARN
                    )
                    if getattr(self, "monitor", None) is not None:
                        self.monitor.update_reading(
                            crop, None if show == '?' else show, detail, suspect=show == '?'
                        )
            elif not r:
                self.lbl_credit_live.config(text="—", foreground=MUTED)
                self.lbl_credit_live_info.config(text="还没框选 Credit 区域", foreground=MUTED)
            elif busy:
                self.lbl_credit_live_info.config(text="运行中暂停预览", foreground=MUTED)
        except Exception as e:  # noqa: BLE001
            self.lbl_credit_live_info.config(text=f"读取异常：{e}", foreground=DANGER)
        if not force:
            self.after(400, self.tick_credit_readout)

    # ---------- 监测区域 ----------
    def update_watch_label(self):
        r = self.cfg.get("watch_rect")
        if r and len(r) == 4:
            self.lbl_watch.config(text=f"已设：({r[0]},{r[1]}) {r[2]}×{r[3]}", foreground="#17912f")
        else:
            self.lbl_watch.config(text="未设置（清币判断不可靠）", foreground="#bb2222")

    def pick_watch_rect(self):
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "请先停止运行再框选")
            return
        self.withdraw()
        self.after(
            350,
            lambda: self._freeze_then(
                lambda frame: RegionPicker(
                    self,
                    self._save_watch_rect,
                    on_close=self.deiconify,
                    background=frame,
                )
            ),
        )

    def _save_watch_rect(self, x, y, w, h):
        self.deiconify()
        self.cfg["watch_rect"] = [x, y, w, h]
        self.save_cfg()
        self.update_watch_label()
        self.log(f"已设定监测区域：({x},{y})  {w}×{h}（建议框住余额/单元数字）")
        self.test_watch_rect()

    def clear_watch_rect(self):
        self.cfg["watch_rect"] = None
        self.save_cfg()
        self.update_watch_label()
        self.log("已清除监测区域")

    def test_watch_rect(self):
        r = self.cfg.get("watch_rect")
        if not r:
            messagebox.showinfo("提示", "先框选监测区域")
            return
        if self.screen is None:
            self.screen = Screen()
        self.iconify()

        def _run():
            time.sleep(0.5)
            x, y, w, h = [int(v) for v in r]
            a = self.screen.grab_rect(x, y, w, h)
            time.sleep(1.5)
            b = self.screen.grab_rect(x, y, w, h)
            d = region_diff(a, b)
            if d is None:
                self.log('[监测区域] 未找到足够红色数字笔画，请重新框选 Credit 数字')
                self._post(self.deiconify)
                return
            thr = float(self.cfg.get("region_diff_threshold", 3.0))
            self.log(
                f"[监测区域] 静止 1.5 秒的噪声差异 = {d:.2f}（判定阈值 {thr}）"
                f" → {'噪声偏大，请把阈值调到 ' + format(d * 2.5, '.1f') if d >= thr else 'OK，阈值可用'}"
            )
            self.log("[监测区域] 判断标准：有币下注时数字会变，差异应明显大于上面的噪声值")
            self.after(300, self.deiconify)

        threading.Thread(target=_run, daemon=True).start()

    def auto_find_credit(self):
        """扫描屏幕，把所有像"红色数字"的区域列出来让你挑（省得靠猜位置）"""
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "请先停止运行再扫描")
            return
        if self.screen is None:
            self.screen = Screen()
        self.withdraw()
        self.after(400, self._scan_credit)

    def _scan_credit(self):
        try:
            self._candidate_frame = self.screen.grab()
            cands = [(x+self.screen.left,y+self.screen.top,w,h,val,info)
                     for x,y,w,h,val,info in find_digit_regions(self._candidate_frame)]
            self._show_candidates(cands)
        except Exception as e:
            self.deiconify()
            self.log(f'[扫描失败] {type(e).__name__}: {e}')
            messagebox.showerror('扫描失败', f'无法截屏或扫描：{e}')

    def _show_candidates(self, cands):
        self.deiconify()
        if not cands:
            self.log("[扫描] 屏幕没找到像「红色数字」的区域——确认机器在画面里，或者手动框选")
            messagebox.showinfo("没找到", "屏幕上没找到像红色数字的区域。\n请确认机器在画面里，或改用手动框选。")
            return
        self.log(f"[扫描] 找到 {len(cands)} 个候选，挑一个：")
        for i, (x, y, w, h, val, _info) in enumerate(cands, 1):
            self.log(f"  {i}. 位置({x},{y}) 尺寸{w}×{h} 读数={val or '认不出'}")
        win = tk.Toplevel(self)
        win.title("选择要读的 Credit 数字区")
        win.attributes("-topmost", True)
        ttk.Label(
            win,
            text="下面是从屏幕扫出来的红色数字区，选一个作为 Credit（没币时显示 0 的那个）：",
        ).pack(anchor="w", padx=10, pady=8)
        lb = tk.Listbox(win, width=64, height=min(8, len(cands)), exportselection=False)
        lb.pack(padx=10)
        for i, (x, y, w, h, val, _info) in enumerate(cands):
            lb.insert("end", f"{i + 1}. ({x},{y})  {w}×{h}   读数 = {val if val else '认不出'}")
        lb.selection_set(0)
        preview = ttk.Label(win)
        preview.pack(padx=10,pady=8)

        def show_preview(event=None):
            sel=lb.curselection()
            if not sel: return
            x,y,w,h,_,_=cands[sel[0]]
            x-=self.screen.left; y-=self.screen.top
            photo=np_to_photoimage(self._candidate_frame[y:y+h,x:x+w],zoom=3)
            preview.image=photo
            preview.config(image=photo)
        lb.bind('<<ListboxSelect>>',show_preview)
        show_preview()

        def use_it():
            sel = lb.curselection()
            if not sel:
                return
            x, y, w, h, val, _info = cands[sel[0]]
            self.cfg["credit_rect"] = [x, y, w, h]
            self.save_cfg()
            self.update_credit_label()
            self.log(f"已选用候选 #{sel[0] + 1}：({x},{y}) {w}×{h}，当前读数 {val}")
            win.destroy()
            self.tick_credit_readout(force=True)
            self.test_credit_rect()

        bar = ttk.Frame(win)
        bar.pack(pady=8)
        ttk.Button(bar, text="就用这个", command=use_it).pack(side="left", padx=6)
        ttk.Button(bar, text="取消", command=win.destroy).pack(side="left", padx=6)

    def move_test(self, key):
        x, y = self.pt_vars[key][0].get().strip(), self.pt_vars[key][1].get().strip()
        if not (x and y):
            messagebox.showinfo("提示", f"{TPL_LABEL[key]} 还没设定坐标")
            return
        try:
            x, y = int(float(x)), int(float(y))
        except ValueError:
            messagebox.showwarning("提示", "坐标必须是数字")
            return
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "运行中不要做移动测试")
            return
        self.iconify()

        def _move():
            time.sleep(0.4)
            if pdi:
                pdi.moveTo(x, y)
            self.log(f"[测试] 鼠标已移动到 {TPL_LABEL[key]} ({x}, {y})，看是否停在目标上")
            self.after(300, self.deiconify)

        threading.Thread(target=_move, daemon=True).start()

    def reload_tpl(self):
        self.load_templates()
        self.refresh_tpl_status()
        self.log("模板已重新加载")

    def record(self, key):
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "请先停止运行再录制模板")
            return
        self.withdraw()  # 隐藏主窗口，露出游戏画面
        self.after(
            350,
            lambda: self._freeze_then(
                lambda frame: RegionPicker(
                    self,
                    lambda x, y, w, h: self._save_region(key, x, y, w, h),
                    on_close=self.deiconify,
                    background=frame,
                )
            ),
        )

    def _save_region(self, key, x, y, w, h):
        self.withdraw()
        self.after(350, lambda: self._capture_template(key, x, y, w, h))

    def _capture_template(self, key, x, y, w, h):
        try:
            img = None
            if self._pick_frame is not None:
                # 直接从冻结帧裁剪：录到的模板与用户框选时看到的画面完全一致
                fh, fw = self._pick_frame.shape[:2]
                x0, y0 = max(0, int(x)), max(0, int(y))
                x1, y1 = min(fw, int(x) + int(w)), min(fh, int(y) + int(h))
                if x1 - x0 >= 8 and y1 - y0 >= 8:
                    img = self._pick_frame[y0:y1, x0:x1].copy()
                self._pick_frame = None
            if img is None:
                if self.screen is None:
                    self.screen = Screen()
                img = self.screen.grab_rect(x, y, w, h)
            if float(img.std()) < 4:
                self.log('[错误] 模板几乎全为空白，请重新录制目标本体')
                return
            # 背景占比过高会让匹配变得模糊（主体和背景混在一起），
            # 提前算出来提醒，比事后「识图测试」找不到要省事。
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            bright = float((gray > 165).mean()) * 100
            warn = bright > 55.0
            path = os.path.join(TPL_DIR, f"{key}.png")
            save_image(path, img)
            self.tpls[key] = img
            self.cfg.setdefault('template_rects', {})[key] = [x,y,w,h]
            pt = (self.cfg.get('points') or {}).get(key)
            offset = [(pt[0]-x)/w, (pt[1]-y)/h] if pt and x<=pt[0]<=x+w and y<=pt[1]<=y+h else [.5,.5]
            self.cfg.setdefault('template_offsets', {})[key] = offset
            self.save_cfg()
            self.refresh_tpl_status()
            self.log(f"已保存模板：{TPL_LABEL[key]}  {w}×{h}  ({x},{y})")
            if warn:
                self.log(
                    f"[提醒] {TPL_LABEL[key]} 模板里亮色/背景占 {bright:.0f}%，"
                    f"偏高。框得太松会把背景一起录进去，识图容易不准——"
                    f"建议只框按钮本体再录一次"
                )
            self._warn_if_view_drifted()
        except Exception as e:  # noqa: BLE001
            self.log(f"[错误] 保存模板失败：{e}")
        finally:
            self.deiconify()

    def test(self, key):
        """统一「测试」按钮：坐标模式下试移动鼠标，图像模式下试识图。

        之前是两个按钮（移动测试 / 识图测试），四个按钮横排会把表撑到
        1000px 以外，窗口被迫变宽；合成一个按当前模式自动分派。
        """
        if self.cfg.get("locate_mode", "coord") == "coord":
            return self.move_test(key)
        if key not in self.tpls:
            messagebox.showinfo("提示", "还没有录制该模板")
            return
        self.collect_cfg()
        self.withdraw()
        self.after(350, lambda: self._test_template(key))

    def _test_template(self, key):
        if self.screen is None:
            self.screen = Screen()
        try:
            cfg=dict(self.cfg, locate_mode='image')
            pos=Engine(cfg,self.tpls,self.log,self.screen).locate(key,retries=1)
            if pos: self.log(f'[测试] {TPL_LABEL[key]} 找到于 {pos}（与运行使用相同的搜索范围/尺度）')
        finally:
            self.deiconify()

    def preview(self, key):
        """模板放大预览窗口（OpenCV 窗口，按任意键关闭）。"""
        if key not in self.tpls:
            messagebox.showinfo("提示", "还没有录制该模板")
            return
        img = self.tpls[key]
        h, w = img.shape[:2]
        scale = max(1, 260 // max(1, w))
        big = cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_NEAREST)
        threading.Thread(
            target=lambda: (cv2.imshow(f"{TPL_LABEL.get(key, key)} 模板预览（按任意键关闭）", big),
                            cv2.waitKey(0), cv2.destroyAllWindows()),
            daemon=True,
        ).start()

    # ---------- 运行前自检 ----------
    def preflight(self, check_templates=True):
        """开跑前把「一定会出事」的问题挑出来。

        之前 start() 只检查「有没有填」，但填了不代表对：坐标可能落在屏幕外、
        模板可能已经失效、Credit 可能根本读不出数字。这些问题留到运行中
        表现为「点了没反应」或者「白跑一轮」，很难排查，所以在开跑前拦下来。

        返回 [(级别, 说明), ...]，级别 ∈ {'error','warn'}；error 必须阻止开跑。
        """
        issues = []
        try:
            if self.screen is None:
                self.screen = Screen()
            sw, sh = self.screen.size
        except Exception as e:  # noqa: BLE001
            return [("error", f"无法读取屏幕尺寸：{e}")]

        # 1) 坐标是否在屏幕内
        pts = self.cfg.get("points") or {}
        if self.cfg.get("locate_mode", "coord") == "coord":
            need = ["coin", "maxbet", "lever", "btn1", "btn2", "btn3"]
            if int(self.cfg.get("bet_count", 3)) < 3:
                need.append("bet")
            for key in need:
                pt = pts.get(key)
                if not pt:
                    issues.append(("error", f"{TPL_LABEL[key]}：还没取点"))
                    continue
                x, y = int(pt[0]), int(pt[1])
                if not (0 <= x < sw and 0 <= y < sh):
                    issues.append((
                        "error",
                        f"{TPL_LABEL[key]} 坐标 ({x},{y}) 不在屏幕内"
                        f"（{sw}×{sh}）——多半是换了分辨率或取点时用错了坐标系，请重新取点",
                    ))

        # Credit is an optional early-refill hint, never a prerequisite for input.
        r = self.cfg.get('credit_rect') or self.cfg.get('watch_rect')
        if not r:
            issues.append(('warn', '未设数字区：按设定抽奖/空转次数循环，不能提前识别低币'))
        else:
            try:
                x, y, w, h = [int(v) for v in r]
                if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > sw or y + h > sh:
                    issues.append(('warn', '数字区超出屏幕或无效：辅助识别不可用，仍按次数运行'))
                else:
                    reader = Engine(self.cfg, {}, lambda msg: None, self.screen)
                    value, info = reader.read_refill_credit()
                    if value is not None:
                        state = '已确认足够一次下注' if reader.credit_has_bet(value) else f'已确认低币 Credit={value}'
                        issues.append(('ok', f'{state}：{info}'))
                    else:
                        issues.append(('warn', f'当前 Credit 无法可靠读取：{info}；仍按次数运行并继续尝试识别'))
            except Exception as exc:
                issues.append(('warn', f'辅助数字读取不可用：{exc}；仍按次数运行'))

        # 3) 模板是否还能在当前画面里找到（图像模式）
        if check_templates and self.cfg.get("locate_mode") == "image":
            cfg = dict(self.cfg, locate_mode="image")
            engine = Engine(cfg, self.tpls, lambda m: None, self.screen, getattr(self, "tpl_variants", {}))
            for key, _ in TEMPLATES:
                if key not in self.tpls:
                    issues.append(("error", f"{TPL_LABEL[key]}：还没有录模板"))
                    continue
                if key not in (self.cfg.get("template_rects") or {}):
                    issues.append(("error", f"{TPL_LABEL[key]}：旧模板缺少搜索位置，请重新录制"))
                    continue
                if float(self.tpls[key].std()) < 4:
                    issues.append(("error", f"{TPL_LABEL[key]}：模板是空白图，请重新录制"))
                    continue
                pos = engine.locate(key, retries=1)
                if pos:
                    issues.append(("ok", f"{TPL_LABEL[key]} 能在画面中找到 @ {pos}"))
                else:
                    issues.append((
                        "error",
                        f"{TPL_LABEL[key]}：当前画面里找不到（相似度不够）——"
                        "确认机器在画面里，或重新录制模板",
                    ))
        return issues

    def _show_preflight(self, issues):
        """把自检结果整理成人能读懂的话，并明确说要不要拦下来。"""
        errors = [m for lvl, m in issues if lvl == "error"]
        warns = [lvl for lvl, _ in issues if lvl == "warn"]
        oks = [m for lvl, m in issues if lvl == "ok"]
        for line in oks:
            self.log("[自检] " + line)
        if errors:
            detail = "\n".join("· " + m for m in errors)
            self.log(f"[自检] 发现 {len(errors)} 个必须先解决的问题，已阻止开跑")
            messagebox.showerror(
                "还不能开跑",
                "开跑前请先解决这些问题：\n\n" + detail
                + "\n\n（在「定位设置」里重新取点 / 框选 Credit 区域）",
            )
            return False
        self.log("[自检] 全部通过，可以开跑")
        return True

    def run_preflight(self):
        """手动跑一次自检（按钮入口）。"""
        if self.engine and self.engine.is_alive():
            messagebox.showinfo("提示", "正在运行，先停止再自检")
            return
        self.collect_cfg()
        self.withdraw()

        def _work():
            try:
                issues = self.preflight()
            except Exception as e:  # noqa: BLE001
                self.after(0, lambda: (self.deiconify(),
                                       messagebox.showerror("自检失败", str(e))))
                return
            lines = []
            for lvl, msg in issues:
                mark = {"error": "✗", "warn": "!", "ok": "✓"}[lvl]
                lines.append(f"{mark} {msg}")
            self.after(0, lambda: self._finish_preflight("\n".join(lines), issues))

        self.after(200, lambda: threading.Thread(target=_work, daemon=True).start())

    def _finish_preflight(self, text, issues):
        self.deiconify()
        errors = [m for lvl, m in issues if lvl == "error"]
        messagebox.showerror("自检结果", text) if errors else messagebox.showinfo(
            "自检结果", text
        )

    # ---------- 旧坐标迁移 ----------
    def analyze_coordinates(self):
        """判断现有坐标是「逻辑像素」还是「物理像素」，以及是否落在屏幕内。

        换机器 / 换分辨率 / 改缩放之后，旧坐标可能是按另一套坐标系存的。
        与其让人盲目重取，不如先算清楚差多少倍，再决定是换算还是重取。
        """
        if self.screen is None:
            self.screen = Screen()
        sw, sh = self.screen.size
        sx, sy = screen_scale(self)
        pts = self.cfg.get("points") or {}
        rows = []
        for key, _ in TEMPLATES:
            pt = pts.get(key)
            if not pt:
                continue
            x, y = int(pt[0]), int(pt[1])
            inside = 0 <= x < sw and 0 <= y < sh
            # 换算回逻辑像素后是否落在屏幕内
            lx, ly = int(round(x / sx)), int(round(y / sy))
            rows.append({
                "key": key,
                "label": TPL_LABEL[key],
                "xy": (x, y),
                "inside": inside,
                "logical": (lx, ly),
                "logical_inside": 0 <= lx < sw and 0 <= ly < sh,
            })
        return {
            "screen": (sw, sh),
            "logical_screen": (self.winfo_screenwidth(), self.winfo_screenheight()),
            "scale": (sx, sy),
            "rows": rows,
        }

    def migrate_coordinates(self, scale_to="physical"):
        """按当前缩放关系把旧坐标整体换算一遍。

        scale_to='physical'：逻辑 -> 物理（把按逻辑像素存的坐标转成物理像素）
        scale_to='logical' ：物理 -> 逻辑

        换算前一定备份，换算后一定会跑自检并把结果告诉你，不会闷头改坏。
        """
        if self.engine and self.engine.is_alive():
            messagebox.showwarning("提示", "请先停止运行再迁移坐标")
            return
        info = self.analyze_coordinates()
        sx, sy = info["scale"]
        if abs(sx - 1.0) < 0.01 and abs(sy - 1.0) < 0.01:
            messagebox.showinfo(
                "无需换算",
                "当前缩放是 100%，逻辑像素和物理像素一致，坐标不用换算。\n"
                "如果点击位置仍然不对，多半是换了分辨率或视角变了，请重新取点。",
            )
            return

        factor = sx if scale_to == "physical" else (1.0 / sx)
        fy = sy if scale_to == "physical" else (1.0 / sy)
        preview = []
        for row in info["rows"]:
            nx = int(round(row["xy"][0] * factor))
            ny = int(round(row["xy"][1] * fy))
            preview.append(f"  {row['label']}：{row['xy']} → ({nx},{ny})")

        target = "物理像素（截图/点击用的坐标系）" if scale_to == "physical" else "逻辑像素"
        if not messagebox.askyesno(
            "确认换算",
            f"当前缩放 {sx:.3f}×{sy:.3f}，屏幕 {info['screen'][0]}×{info['screen'][1]}\n\n"
            f"将把 {len(info['rows'])} 个坐标换算到：{target}\n\n"
            + "\n".join(preview)
            + "\n\n换算前会自动备份 config.json。\n继续吗？",
        ):
            return

        backup = CFG_PATH + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
        if os.path.exists(CFG_PATH):
            try:
                with open(CFG_PATH, "rb") as f:
                    raw = f.read()
                with open(backup, "wb") as f:
                    f.write(raw)
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("备份失败", f"没能备份配置，已放弃换算：{e}")
                return
        else:
            # 还没保存过配置（首次使用），没有可备份的东西，不该因此中断换算
            backup = None

        new_pts = {}
        for key, pt in (self.cfg.get("points") or {}).items():
            new_pts[key] = [int(round(int(pt[0]) * factor)),
                            int(round(int(pt[1]) * fy))]
        self.cfg["points"] = new_pts
        r = self.cfg.get("credit_rect")
        if r and len(r) == 4:
            self.cfg["credit_rect"] = [int(round(int(r[0]) * factor)),
                                       int(round(int(r[1]) * fy)),
                                       int(round(int(r[2]) * factor)),
                                       int(round(int(r[3]) * fy))]
        self.save_cfg()
        self._reload_points_to_ui()
        name = os.path.basename(backup) if backup else "（无，首次使用没有旧配置）"
        self.log(f"[迁移] 坐标已按 {factor:.3f}× 换算；备份：{name}")
        messagebox.showinfo(
            "换算完成",
            f"坐标已换算并保存。\n备份文件：{name}\n\n"
            "注意：换算只是按比例推算，不能保证机器在画面里的位置没变。\n"
            "强烈建议接着做一次「运行前自检」，或用「移动测试」逐个确认。",
        )
        self.run_preflight()

    def show_coordinate_report(self):
        """列出所有坐标、当前屏幕、缩放关系，帮你判断该换算还是重取。"""
        try:
            info = self.analyze_coordinates()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("无法检查", str(e))
            return
        sw, sh = info["screen"]
        lw, lh = info["logical_screen"]
        sx, sy = info["scale"]
        lines = [
            f"截图（物理像素）：{sw} × {sh}",
            f"浮层（逻辑像素）：{lw} × {lh}",
            f"缩放比例：{sx:.3f} × {sy:.3f}",
            "",
        ]
        if not info["rows"]:
            lines.append("还没有任何坐标。")
        for row in info["rows"]:
            mark = "✓ 在屏幕内" if row["inside"] else "✗ 不在屏幕内"
            if not row["inside"] and row["logical_inside"]:
                mark += "（换算成逻辑像素后就在范围内，像坐标系用错了）"
            lines.append(f"  {row['label']}：{row['xy']}   {mark}")
        bad = [r for r in info["rows"] if not r["inside"]]
        if bad:
            lines += [
                "",
                f"有 {len(bad)} 个坐标超出屏幕。多半是换机器或改缩放造成的。",
                "如果确认机器位置没变，可以点「按当前缩放换算旧坐标」；",
                "如果视角也变了，请直接重新取点。",
            ]
        else:
            lines += ["", "所有坐标都在屏幕内。"]
        text = "\n".join(lines)
        self.log("[坐标检查] " + text.replace("\n", " | "))
        messagebox.showinfo("坐标检查", text)

    def _reload_points_to_ui(self):
        """配置里的坐标变了，把界面上的输入框同步过来。"""
        if not hasattr(self, "pt_vars"):
            return
        pts = self.cfg.get("points") or {}
        for key, (vx, vy) in self.pt_vars.items():
            pt = pts.get(key)
            if pt:
                vx.set(str(int(pt[0])))
                vy.set(str(int(pt[1])))
        self.refresh_tpl_status()

    # ---------- 运行 ----------
    def _get_num(self, key, default):
        try:
            v = self.vars[key].get().strip()
            return float(v) if "." in v else int(v)
        except Exception:
            return default

    def collect_cfg(self):
        for key in (
            "coin_count",
            "draw_count",
            "burn_count",
            "bet_count",
            "rounds",
            "coin_delay",
            "last_coin_wait",
            "step_delay",
            "move_delay",
            "pull_px",
            "threshold",
            "retries",
            "max_minutes",
            "region_diff_threshold",
            "region_wait",
            "tab_repeat",
            "bet_retries",
            "bet_confirm_timeout",
            "credit_max_display",
            "credit_ref_threshold",
            "scale_min",
            "scale_max",
            "scale_steps",
        ):
            if key in self.vars:
                self.cfg[key] = self._get_num(key, DEFAULT_CFG.get(key))
        self.cfg["hold_tab"] = bool(self.v_hold_tab.get())
        self.cfg["multi_scale"] = bool(self.v_multi.get())
        if hasattr(self, "v_auto_learn"):
            self.cfg["auto_learn"] = bool(self.v_auto_learn.get())
        if "game_window" in self.vars:
            self.cfg["game_window"] = self.vars["game_window"].get().strip()
        # 定位方式与坐标点
        if hasattr(self, "v_mode"):
            self.cfg["locate_mode"] = self.v_mode.get()
        if hasattr(self, "pt_vars"):
            pts = {}
            for key, _ in TEMPLATES:
                vx, vy = self.pt_vars[key]
                xs, ys = vx.get().strip(), vy.get().strip()
                if xs and ys:
                    try:
                        pts[key] = [int(float(xs)), int(float(ys))]
                    except ValueError:
                        pass
            self.cfg["points"] = pts
        return self.cfg

    def collect_and_save(self):
        self.collect_cfg()
        self.save_cfg()
        if hasattr(self, "btn_start"):
            self._refresh_run_buttons()

    # ---------- 配置方案（多机器档案） ----------
    def _profile_dir(self):
        d = os.path.join(DATA_DIR, "配置方案")
        os.makedirs(d, exist_ok=True)
        return d

    def list_profiles(self):
        try:
            return sorted(fn[:-5] for fn in os.listdir(self._profile_dir())
                          if fn.lower().endswith(".json"))
        except OSError:
            return []

    def refresh_profiles(self):
        if not hasattr(self, "cb_profile"):
            return
        names = self.list_profiles()
        self.cb_profile["values"] = names
        if self.v_profile.get() not in names:
            self.v_profile.set(names[0] if names else "")
        try:
            self.lbl_profile.config(
                text=(f"共有 {len(names)} 个方案" if names
                      else "还没有方案。先按当前机器调好，点「把当前配置存为方案…」。"))
        except tk.TclError:
            pass

    def save_profile_as(self):
        self.collect_cfg()
        name = simpledialog.askstring(
            "保存配置方案", "给这台机器/这套设置起个名字（例如：大厅1号机）：", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        path = os.path.join(self._profile_dir(), f"{name}.json")
        if os.path.exists(path) and not messagebox.askyesno(
                "覆盖确认", f"方案「{name}」已存在，覆盖它？", parent=self):
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("保存失败", f"方案写入磁盘失败：{e}", parent=self)
            return
        self.log(f"[方案] 已保存当前配置为方案「{name}」")
        self.refresh_profiles()
        self.v_profile.set(name)

    def load_profile(self):
        name = self.v_profile.get()
        if not name:
            messagebox.showinfo("提示", "先在下拉框里选一个方案", parent=self)
            return
        if (self.engine and self.engine.is_alive()) or \
                (self.collector and self.collector.is_alive()):
            messagebox.showwarning("提示", "运行/采集进行中不能切换方案，请先停止", parent=self)
            return
        path = os.path.join(self._profile_dir(), f"{name}.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("载入失败", f"读不了方案文件：{e}", parent=self)
            return
        if not isinstance(loaded, dict):
            messagebox.showerror("载入失败", "方案文件格式不对（不是配置字典）", parent=self)
            return
        self.cfg = dict(DEFAULT_CFG)
        self.cfg.update(loaded)
        self.save_cfg()
        self._apply_cfg_to_ui()
        self.log(f"[方案] 已载入方案「{name}」，全部设置已切换（坐标/参数/Credit 区域等）")

    def delete_profile(self):
        name = self.v_profile.get()
        if not name:
            messagebox.showinfo("提示", "先在下拉框里选一个方案", parent=self)
            return
        if not messagebox.askyesno("删除确认",
                                   f"确定删除方案「{name}」？删除后不可恢复。", parent=self):
            return
        try:
            os.remove(os.path.join(self._profile_dir(), f"{name}.json"))
        except OSError as e:
            messagebox.showerror("删除失败", str(e), parent=self)
            return
        self.log(f"[方案] 已删除方案「{name}」")
        self.refresh_profiles()

    def _apply_cfg_to_ui(self):
        """把 self.cfg 刷进全部界面控件（载入方案后调用）。"""
        for key, var in self.vars.items():
            var.set(str(self.cfg.get(key, "")))
        if hasattr(self, "pt_vars"):
            pts = self.cfg.get("points") or {}
            for key, _ in TEMPLATES:
                pt = pts.get(key, ["", ""])
                vx, vy = self.pt_vars[key]
                vx.set("" if pt[0] in (None, "") else str(pt[0]))
                vy.set("" if pt[1] in (None, "") else str(pt[1]))
        if hasattr(self, "v_mode"):
            self.v_mode.set(self.cfg.get("locate_mode", "coord"))
        if hasattr(self, "v_hold_tab"):
            self.v_hold_tab.set(bool(self.cfg.get("hold_tab", True)))
        if hasattr(self, "v_multi"):
            self.v_multi.set(bool(self.cfg.get("multi_scale", False)))
        if hasattr(self, "v_auto_learn"):
            self.v_auto_learn.set(bool(self.cfg.get("auto_learn", True)))
        self.refresh_stop_keys()
        self.on_mode_change()
        self.update_credit_label()
        self.update_credit_ref_label()
        self.update_watch_label()
        self.update_parts_label()
        self.update_bank_label()
        self._refresh_run_buttons()

    # ---------- 日志导出 ----------
    def export_log(self):
        text = self.txt.get("1.0", "end").strip()
        if not text:
            messagebox.showinfo("提示", "日志还是空的，没东西可导出")
            return
        path = filedialog.asksaveasfilename(
            title="导出运行日志", defaultextension=".txt",
            initialfile=time.strftime("LuraSlot日志_%Y%m%d_%H%M%S.txt"),
            parent=self)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.log(f"日志已导出：{path}")

    def log(self, msg):
        self.logq.put(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def pump_log(self):
        """日志泵 + 界面状态机心跳（每 150ms 一拍）。

        这一拍里必须完成「引擎退出 → 恢复开始/停止按钮」的交接：
        曾经只有注释说"由 pump_log 在引擎结束后恢复"，实际没接——
        点了停止、引擎确实停了，但界面永远卡在「运行中」、开始一直灰着。
        另外任何一拍抛异常都会杀死 after 链（界面整个冻结，看起来也像
        「停止没用」），所以整拍兜底，after 链永远续上。
        """
        try:
            try:
                self.txt.configure(state="normal")
                while True:
                    line = self.logq.get_nowait()
                    self.txt.insert("end", line + "\n", log_level(line))
                    lines_seen = int(self.txt.index("end-1c").split(".")[0])
                    if lines_seen > 800:
                        self.txt.delete("1.0", "201.0")
                    self.txt.see("end")
                self.txt.configure(state="disabled")
            except queue.Empty:
                pass
            except tk.TclError:
                pass
            was_running = getattr(self, "_engine_was_running", False)
            if self.engine and self.engine.is_alive():
                self._engine_was_running = True
                state = self.engine.state
                done = self.engine.spins_done
                if getattr(self, "_stopping", False):
                    self.lbl_state.config(text="● 停止中…", foreground=WARN)
                else:
                    self.lbl_state.config(text=f"● 运行中 · {state}", foreground=OK)
                self.lbl_header_hint.config(text=f"已抽 {done} 次")
                if getattr(self, "lbl_stats", None) is not None:
                    secs = max(0, int(time.time() - getattr(self.engine, "started_at",
                                                            time.time())))
                    mm, ss = divmod(secs, 60)
                    self.lbl_stats.config(
                        text=f"已运行 {mm:02d}:{ss:02d} · 抽奖 {done} 次 · "
                             f"确认下注 {getattr(self.engine, 'bets_confirmed', 0)} 注 · "
                             f"用币 {getattr(self.engine, 'coins_used', 0)} 枚 · "
                             f"完成 {getattr(self.engine, 'rounds_done', 0)} 轮",
                        foreground=INK)
                if getattr(self, "overlay", None) is not None:
                    self.overlay.tick(f"运行中 · {state} · 已抽 {done} 次")
            elif self.engine is not None:
                self.lbl_state.config(text="● 空闲", foreground=MUTED)
                self.lbl_header_hint.config(text="已停止")
                if was_running:
                    self._engine_was_running = False
                    self._stopping = False
                    self._refresh_run_buttons()
                # 引擎结束了（可能是急停触发的），把置顶按钮收掉、窗口恢复
                if getattr(self, "overlay", None) is not None:
                    self._close_overlay()
        except Exception:  # noqa: BLE001
            # 一拍出错不能杀死刷新循环：记下来，下一拍照常跑
            try:
                self.logq.put(f"[{time.strftime('%H:%M:%S')}] [内部] 界面刷新出了一"
                              f"次错（已自动恢复）。{traceback.format_exc(limit=1)}")
            except Exception:
                pass
        finally:
            try:
                self.after(150, self.pump_log)
            except tk.TclError:
                pass  # 应用已关闭，不用再排下一拍

    def start(self):
        if pdi is None:
            messagebox.showerror("缺少依赖", "未安装 pydirectinput，请先运行 安装依赖.bat")
            return
        if self.engine and self.engine.is_alive():
            return
        if self.collector and self.collector.is_alive():
            # 采集也在真实操作鼠标，两边同时跑会互相打偏
            messagebox.showinfo("提示", "自动采集正在进行，请先点「停止采集」再运行")
            return
        need = ["coin", "maxbet", "lever", "btn1", "btn2", "btn3"]
        if int(self._get_num("bet_count", 3)) < 3:
            need.append("bet")
        self.collect_and_save()
        if int(self.cfg['bet_count']) not in (1,2,3) or not 0 < float(self.cfg['threshold']) <= 1:
            messagebox.showwarning('参数错误', '下注枚数必须是 1、2 或 3，相似度阈值必须大于 0 且不超过 1。')
            return
        try:
            negative=[k for k in ('coin_count','draw_count','burn_count','rounds',
                'coin_delay','step_delay','move_delay','last_coin_wait','region_wait','max_minutes',
                'tab_repeat','bet_retries','bet_confirm_timeout') if float(self.cfg[k]) < 0]
        except (TypeError, ValueError):
            messagebox.showwarning('参数错误', '次数和等待时间必须是数字。')
            return
        if negative:
            messagebox.showwarning('参数错误', '次数和等待时间不能为负数。')
            return
        if self.cfg.get("locate_mode", "coord") == "coord":
            pts = self.cfg.get("points") or {}
            missing = [TPL_LABEL[k] for k in need if not pts.get(k)]
            if missing:
                messagebox.showwarning("坐标不全", "还没设定这些坐标点：\n" + "\n".join(missing))
                return
        else:
            missing = [TPL_LABEL[k] for k in need if k not in self.tpls]
            invalid = [TPL_LABEL[k] for k in need if k in self.tpls and
                       (self.tpls[k].std()<4 or k not in self.cfg.get('template_rects', {}))]
            if invalid:
                messagebox.showwarning('请重新录制模板', '旧模板为空白或没有定位范围，请重新录制：\n'+'\n'.join(invalid))
                return
            if missing:
                messagebox.showwarning("模板不全", "还缺少模板：\n" + "\n".join(missing))
                return
        if not (self.cfg.get('credit_rect') or self.cfg.get('watch_rect')):
            messagebox.showwarning('缺少下注确认区域', '请框选 Credit 数字区域；每次下注都需要确认扣币后才会拉杆。')
            return
        if self.screen is None:
            self.screen = Screen()
        # 上面只检查了「填了没有」，这里再检查「填的对不对」：
        # 坐标是否在屏幕内、Credit 是否读得出、模板是否还能找到。
        # 不通过就开跑的话，表现为「点了没反应」，排查成本极高。
        try:
            issues = self.preflight()
        except Exception as e:  # noqa: BLE001
            issues = [("error", f"自检异常：{e}")]
        if not self._show_preflight(issues):
            return
        self.engine = Engine(self.cfg, dict(self.tpls), self.log, self.screen,
                             dict(self.tpl_variants))
        self._stopping = False
        self.engine.start()
        self._refresh_run_buttons()
        # 置顶急停按钮（窗口最小化也能点）
        try:
            self.overlay = StopOverlay(self, self.request_stop)
        except Exception as e:  # noqa: BLE001
            self.overlay = None
            self.log(f"[警告] 置顶停止按钮创建失败：{e}")
        self.after(900, self.withdraw)  # 用 withdraw 而不是 iconify：置顶急停按钮才不会被一起收走
        # 注意：不要在这里用 after() 把「开始运行」重新放开——
        # 引擎还在跑，那样会导致可以重复开跑。按钮状态统一交给
        # _refresh_run_buttons()，由 pump_log 在引擎结束后恢复。

    def request_stop(self):
        """急停入口（热键 / 置顶按钮 / 主界面按钮都走这里）。

        运行和采集**两个都可能正在动鼠标**，必须都停——
        曾经只停引擎：采集时按 F12 毫无反应，而采集又拽着鼠标、
        向导窗口被全屏游戏挡住，用户就没有任何办法停下它。
        """
        stopped = False
        if self.engine and self.engine.is_alive():
            self.engine.request_stop()
            self.log("已请求停止…")
            stopped = True
        if self.collector and self.collector.is_alive():
            self.collector.stop_flag.set()
            self.log("已请求停止采集…")
            stopped = True
        if not stopped:
            self.log("当前没有正在运行的自动化")
        if stopped:
            # 即时反馈：引擎收尾最多一两秒，这期间必须让用户知道按有效了
            self._stopping = True
            try:
                self.lbl_ready.config(text="正在停止…（等引擎收尾，最多一两秒）",
                                      foreground=WARN)
            except (tk.TclError, AttributeError):
                pass
        self.after(200, self.deiconify)
        self.after(300, self._close_overlay)

    def _close_overlay(self):
        if getattr(self, "overlay", None) is not None:
            try:
                self.overlay.destroy()
            except Exception:
                pass
            self.overlay = None
        self.deiconify()
        self.lift()

    def on_close(self):
        if self.engine and self.engine.is_alive():
            self.engine.request_stop()
            time.sleep(0.3)
        if self.collector and self.collector.is_alive():
            self.collector.stop_flag.set()
            time.sleep(0.2)
        if kb is not None:
            try:
                kb.clear_all_hotkeys()
            except Exception:
                pass
        for callback in self.tk.call('after', 'info'):
            try:
                self.after_cancel(callback)
            except tk.TclError:
                pass
        self.destroy()


from runtime_controls import EngineFeatures, KeepAwake
from original_features import AppFeatures, CollectorFeatures, pending_sample

DEFAULT_CFG.update(keep_awake=True, keep_awake_wait=False, collect_pending=False,
                   start_at="", start_delay_minutes=0, stop_at="", completion_action="stop",
                   shutdown_countdown=60)

class Engine(EngineFeatures, LegacyEngine):
    core = sys.modules[__name__]

class AutoCollector(CollectorFeatures, LegacyAutoCollector):
    core = sys.modules[__name__]

class App(AppFeatures, ModernApp):
    core = sys.modules[__name__]

def auto_collect_credit(crop, value, cfg=None, log=None):
    pending_sample(sys.modules[__name__], crop, value, cfg, log)
    return None  # A predicted value must not immediately train its own recognizer.

def diag():
    """诊断 DPI 与截图链路：截图坐标错位会让所有点击打偏，必须能自查。"""
    import ctypes as _ct

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    awareness = DPI_AWARENESS
    names = {0: "unaware", 1: "system", 2: "per-monitor", 3: "per-monitor-v2"}
    print(f"LuraSlot v{APP_VERSION}  作者：{APP_AUTHORS}")
    print("DPI aware          :", DPI_AWARE,
          "(awareness=%s)" % names.get(awareness, awareness))
    print("GetSystemMetrics   :", user32.GetSystemMetrics(0), "x", user32.GetSystemMetrics(1))
    sc = Screen()
    print("mss monitor        :", sc.size)
    frame = sc.grab()
    print("captured shape     :", frame.shape)
    print("virtual metrics    :",
          user32.GetSystemMetrics(78), "x", user32.GetSystemMetrics(79),
          "(virtual screen; differ => coords are virtualised)")
    same = (frame.shape[1], frame.shape[0]) == (user32.GetSystemMetrics(0),
                                               user32.GetSystemMetrics(1))
    print("capture==physical  :", same)
    return 0 if (DPI_AWARE and same) else 1


def selftest():
    """自检：截图 -> 在同一帧里裁一块当模板 -> 全屏匹配，验证坐标吻合。

    刻意在**同一帧**内裁剪和匹配：桌面内容随时在变（窗口、动画），
    跨两帧比对会因为画面变了而误报失败，那种失败对排查毫无帮助。
    真实的跨帧稳定性由下面的帧间差异单独报告。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    sc = Screen()
    frame = sc.grab()
    print("屏幕分辨率:", sc.size)
    h, w = frame.shape[:2]
    bx, by, bw, bh = w // 2 - 60, h // 2 - 30, 120, 60
    tpl = frame[by:by + bh, bx:bx + bw].copy()
    print("模板区域          :", (bx, by, bw, bh), "标准差 %.1f" % tpl.std())
    if float(tpl.std()) < 4:
        print("SELFTEST_SKIP 屏幕中心是一片纯色（没有可匹配的内容），换到有画面的桌面再试")
        return
    r = match_template(frame, tpl, 0.9, (1.0,))
    print("匹配结果:", r)
    ok = bool(r) and abs(r[1] - (bx + bw // 2)) <= 1 and abs(r[2] - (by + bh // 2)) <= 1
    print("SELFTEST_OK 截图与匹配链路正常，坐标未错位" if ok else "SELFTEST_FAIL")
    frame2 = sc.grab()
    print("帧间差异(静止时应≈0):", round(frame_diff(frame, frame2), 3))


def main():
    """程序入口。抽成函数而不是只写在 __main__ 里：
    打包成 exe 后本文件是被 import 的，__name__ 不再是 "__main__"。"""
    if "--ui-smoke" in sys.argv:
        global kb
        kb = None
        app = App()
        app.cfg['credit_rect'] = None
        app.withdraw()
        app.update()
        tabs = [app.nb.tab(t, 'text') for t in app.nb.tabs()]
        app.open_controls()
        controls = app.winfo_children()[-1]
        controls.withdraw()
        app.update()
        assert len(tabs) == 3
        app.on_close()
        with open(os.path.join(APP_DIR, 'ui-smoke.json'), 'w', encoding='utf-8') as report:
            json.dump({'ok': True, 'tabs': tabs, 'input_sent': False}, report, ensure_ascii=False)
        return
    if "--selftest" in sys.argv:
        selftest()
        return
    if "--diag" in sys.argv:
        sys.exit(diag())
    # pythonw / 打包后的窗口程序没有控制台，把输出重定向到 run.log，方便排查闪退
    log_path = os.path.join(APP_DIR, "run.log")
    try:
        if sys.stderr is None or sys.stdout is None:
            f = open(log_path, "a", encoding="utf-8", buffering=1)
            sys.stdout = f
            sys.stderr = f
        print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] === 程序启动 ===")
    except Exception:
        pass
    try:
        App().mainloop()
    except BaseException:
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write("\n[FATAL]\n" + traceback.format_exc())
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
