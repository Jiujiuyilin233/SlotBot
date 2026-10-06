"""最小验证：线程本地 mss 实例在线程死亡后是否泄漏原生内存（无 UI、无输入）。"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from slotbot import Screen


class PMC(ctypes.Structure):
    _fields_ = [('cb', wt.DWORD), ('PageFaultCount', wt.DWORD)] + [
        (n, ctypes.c_size_t) for n in
        ('PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
         'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage', 'QuotaNonPagedPoolUsage',
         'PagefileUsage', 'PeakPagefileUsage')]


def mem_mb():
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.c_void_p(-1), ctypes.byref(pmc), pmc.cb)
    return pmc.PagefileUsage / 1048576.0


print('基线 %.0fMB' % mem_mb(), flush=True)
sc = Screen()
for i in range(20):
    t = threading.Thread(target=lambda: sc.grab(), daemon=True)
    t.start()
    t.join()
    import gc
    gc.collect()
    print('第%02d个一次性线程后 %.0fMB (Δ%+.0fMB)'
          % (i + 1, mem_mb(), mem_mb() - 783 if i == 0 else 0), flush=True)

print('--- 对照组：同一线程里连抓 20 次（应基本持平）---')
for j in range(20):
    sc.grab()
print('同线程连抓后 %.0fMB' % mem_mb(), flush=True)
os._exit(0)
