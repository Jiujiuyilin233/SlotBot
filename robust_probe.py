"""robust_probe.py — 鲁棒性压测：专打本版新增的三条代码路径。

1) 热键轮询 soak：随机按压/长按 F11(开始)/F10(常亮)，验证「按下沿只触发一次」，
   并注入非法/空配置确认不崩、仍能正确识别。
2) 常亮开关 churn：反复开关，验证 SetThreadExecutionState 的 acquire/release 收支平衡
   （用假 KeepAwake，绝不触碰真实电源状态），且配置持久化一致。
3) wait_win_settled 上界：对「无区域 / 静止 / 永远在变」三种对抗输入测墙钟耗时，
   确认任何一种都在 max_wait 内返回、绝不挂死（空转修复最关键的健壮性属性）。
4) 内存：全程采样私有内存，看是否单调上涨。

绝不向游戏发送任何鼠标/键盘输入。
"""
import ctypes
import ctypes.wintypes as wt
import faulthandler
import json
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

_sandbox = tempfile.TemporaryDirectory(prefix="slotbot-robust-")
os.environ["SLOTBOT_DATA_DIR"] = _sandbox.name

ERR = open("robust_probe_stderr.log", "w", encoding="utf-8", buffering=1)
faulthandler.enable(ERR)
faulthandler.dump_traceback_later(180, exit=True, file=ERR)  # 全局挂死看门狗

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

import numpy as np
import slotbot as s
import original_features as of
import runtime_controls as rc


class PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def mem_mb():
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb)
    return (pmc.PagefileUsage / 1048576.0) if ok else -1.0


# 假 KeepAwake：记录 acquire/release 次数，不调真实电源 API。
FAKE = {"on": 0, "acq": 0, "rel": 0}


class FakeAwake:
    def __init__(self, enabled=True, api=None, log=None):
        self.enabled = enabled
        self.log = log or (lambda m: None)

    def __enter__(self):
        if self.enabled:
            FAKE["on"] += 1
            FAKE["acq"] += 1
        return self

    def __exit__(self, *a):
        if self.enabled:
            FAKE["on"] -= 1
            FAKE["rel"] += 1


of.KeepAwake = FakeAwake

fails = []
peak = {"mem": 0.0}


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + "  " + msg, flush=True)
    if not cond:
        fails.append(msg)


def note_mem(tag):
    m = mem_mb()
    peak["mem"] = max(peak["mem"], m)
    print("       [mem] %s = %.0fMB" % (tag, m), flush=True)
    return m


app = s.App()
app.withdraw()
app.update()
base_mem = note_mem("baseline")

# ---------------- Phase 1: 热键轮询 soak ----------------
print("\n== Phase 1: 热键边沿检测 soak ==")
start_calls = []
awake_calls = []
app.start = lambda: start_calls.append(1)
app.toggle_keep_awake = lambda: awake_calls.append(1)

F11 = s.VK_BY_NAME["F11"]
F10 = s.VK_BY_NAME["F10"]
orig_kp = s.key_pressed
held = set()
exp_start = exp_awake = 0
random.seed(1234)
ITERS = 20000
for i in range(ITERS):
    # 随机改变两个键的按下状态
    for vk, exp in ((F11, "start"), (F10, "awake")):
        if random.random() < 0.5:
            if vk in held:
                held.discard(vk)          # 松开
            else:
                held.add(vk)               # 按下 -> 计一次沿
                if exp == "start":
                    exp_start += 1
                else:
                    exp_awake += 1
    s.key_pressed = lambda vk: vk in held
    app._poll_hotkeys()
    if i == ITERS // 2:
        # 注入对抗配置：非法键名应被丢弃、空列表应回退默认；不能崩
        app.cfg["start_keys"] = ["zzz", "F11"]
        app.cfg["awake_keys"] = []
        note_mem("mid(注入非法配置)")
s.key_pressed = orig_kp
check(len(start_calls) == exp_start,
      "开始沿触发 %d 次 == 期望 %d 次" % (len(start_calls), exp_start))
check(len(awake_calls) == exp_awake,
      "常亮沿触发 %d 次 == 期望 %d 次" % (len(awake_calls), exp_awake))
check([n for _v, n in s.start_key_entries(app.cfg)] == ["F11"], "非法配置回退/保留 F11")
check([n for _v, n in s.awake_key_entries(app.cfg)] == ["F10"], "空 awake 配置回退 F10")
note_mem("after-soak")

# ---------------- Phase 2: 常亮开关 churn ----------------
print("\n== Phase 2: 常亮开关 churn ==")
# 恢复真实 toggle（phase1 stub 掉了）
del app.start  # 交回 App.start
# toggle_keep_awake 仍是实例属性 stub，删掉让类方法生效
app.__dict__.pop("toggle_keep_awake", None)
TOGGLES = 500
for _ in range(TOGGLES):
    app.toggle_keep_awake()
check(FAKE["on"] == 0, "churn 后常亮净持有=0（未泄漏电源请求）")
check(FAKE["acq"] == FAKE["rel"], "acquire(%d)==release(%d) 收支平衡" % (FAKE["acq"], FAKE["rel"]))
check(app._awake_hold is None, "最终 _awake_hold 已释放")
check(app.cfg.get("keep_awake_idle") is (TOGGLES % 2 == 1), "持久化标志与末态一致")
note_mem("after-churn")

# ---------------- Phase 3: wait_win_settled 上界 ----------------
print("\n== Phase 3: wait_win_settled 对抗输入上界 ==")


class WSProbe:
    wait_win_settled = rc.EngineFeatures.wait_win_settled

    def __init__(self, kind):
        self.kind = kind
        self.n = 0
        self.cfg = {"credit_rect": [0, 0, 85, 50]}

    def _scene_probe(self):
        self.n += 1
        if self.kind == "none":
            return None
        if self.kind == "static":
            return np.zeros((50, 85, 3), np.uint8)
        if self.kind == "drift":
            # 缓慢漂移：相邻帧均值差≈1（<阈值）——应被当成“已静止”，快速返回
            return np.full((50, 85, 3), self.n % 251, np.uint8)
        # chaos：相邻帧在 0/255 间跳变（均值差=255，远超阈值）——永远不静止，
        # 必须在 max_wait 内有界返回，绝不挂死
        return np.full((50, 85, 3), 255 if (self.n % 2) else 0, np.uint8)

    def pause(self, sec):
        time.sleep(sec)

    def boundary(self):
        pass


for kind, expect_ret, upper in (("none", False, 0.9), ("static", True, 0.9),
                                ("drift", True, 0.9), ("chaos", False, 2.1)):
    p = WSProbe(kind)
    t0 = time.monotonic()
    ret = p.wait_win_settled()   # 默认 default_wait=0.3, max_wait=1.5
    dt = time.monotonic() - t0
    check(ret == expect_ret and dt < upper,
          "%-7s 返回=%-5s 耗时=%.2fs (<%.1fs) 不挂死" % (kind, ret, dt, upper))

# ---------------- Phase 4: 真实周期泵集成（含抛错存活） ----------------
print("\n== Phase 4: 真实 after(40) 泵集成 ==")

# 4a: 让 _poll_hotkeys 抛错，_pump_callbacks 必须捕获并仍续上下一次调度
def _boom():
    raise RuntimeError("poll boom")


app._poll_hotkeys = _boom
before = len(app.tk.call("after", "info"))
raised = None
try:
    app._pump_callbacks()
except Exception as exc:  # 不该逃出来
    raised = exc
mid = len(app.tk.call("after", "info"))
check(raised is None, "泵内热键抛错被捕获（未逃逸终止泵）")
check(mid > before, "泵遇抛错仍续上下一次 after 调度（未静默死掉）")
app.__dict__.pop("_poll_hotkeys", None)  # 还原真实热键轮询

# 4b: 真实驱动 after 循环 ~2s，按住 F10，验证经由真实泵触发常亮且心跳持续
heartbeat = [0]


def _beat():
    heartbeat[0] += 1
    if not app._closed:
        app.after(40, _beat)


app.after(40, _beat)
# 清掉 soak 阶段残留的按下沿，模拟「键已松开后重新按下 F10」
app._hotkey_down.clear()
s.key_pressed = lambda vk: vk == F10  # 一直按住 F10
held_from = FAKE["acq"]
t_end = time.monotonic() + 2.0
while time.monotonic() < t_end:
    app.update()
    time.sleep(0.01)
s.key_pressed = orig_kp
check(FAKE["acq"] == held_from + 1, "真实泵按住 F10 只触发一次常亮(边沿正确)")
check(heartbeat[0] > 20, "心跳在 2s 内持续 %d 次(泵未死)" % heartbeat[0])
if FAKE["on"] == 1:  # 收尾释放
    app.toggle_keep_awake()

note_mem("after-pump")

note_mem("after-ws")

# ---------------- 汇总 ----------------
end_mem = note_mem("end")
growth = end_mem - base_mem
check(growth < 150, "全程私有内存增长 %.0fMB (<150MB，未单调泄漏)" % growth)

result = {"base_mem": base_mem, "end_mem": end_mem, "growth": growth,
          "peak_mem": peak["mem"], "fails": fails,
          "iters": ITERS, "exp_start": exp_start, "exp_awake": exp_awake}
with open("robust_probe_result.json", "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=1)

try:
    app.on_close()
except Exception:
    pass
faulthandler.cancel_dump_traceback_later()
print("\n==== RESULT: %s ====" % ("PASS" if not fails else "FAIL(%d): %s" % (len(fails), fails)))
sys.exit(0 if not fails else 1)
