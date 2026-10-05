# -*- coding: utf-8 -*-
"""真实截图基准：用户实拍样本 → 解码 → 与文件名真值对照。

这是唯一可信的准确率。合成基准（digit_bench.py）验证的是"我猜的几何
对不对"，这里验证的是"真机画面到底读得对不对"。

样本来源（三种都认，子目录任意层级——不同机器可以各放一个子目录）：
1. 用户自截，文件名就是真值：数字截图/41.png（如 0.png、17.png）
2. 同一个数字的**不同视角**：41-2.png、41-3.png（后缀只是序号，不算真值）
3. 程序内「采集数字样本」按钮 → digits/v<真值>_<时间戳>.png

每张图解两遍：
- 直接解码：模拟框选略宽的运行时情形（整张截图直接喂给解码器）
- decode_led_best：直接失败时自动收紧到暗色背板再解码的兜底路径

只要有任何一张"读错"，退出码 1——可以直接接进回归门禁。

用法：  python tests/real_bench.py [-v]
"""
import os
import re
import sys

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vision import decode_led_best, decode_led_number, find_led_panel  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAMPLE_DIRS = (os.path.join(ROOT, "数字截图"), os.path.join(ROOT, "digits"))


def imread_cn(path):
    """cv2.imread 在 Windows 上读不了中文路径，必须走 fromfile+imdecode。"""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except OSError:
        return None


def truth_of(filename):
    """从文件名解析真值。

    支持：  41.png   41-2.png / 41-3.png（同数字的不同视角，后缀只是序号）
            v41_20261005_021200.png（程序内「采集数字样本」的格式）
            auto41_20261005_030000.png（运行中自动采集的格式）
    放在子目录里也认（数字截图/我的机器/41-2.png），所以不同机器可以各放一个
    子目录，互不干扰地共享同一套基准。
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = re.match(r'^(?:v|auto)?(\d+)(?:[_-].*)?$', stem)
    return m.group(1) if m else None


def load_samples(dirs=None):
    """收集所有可用的真实样本，返回 [(truth, 相对路径, 图), ...]。"""
    out = []
    for d in (dirs or SAMPLE_DIRS):
        if not os.path.isdir(d):
            continue
        for cur, _subdirs, files in os.walk(d):
            for fn in sorted(files):
                if not fn.lower().endswith(".png"):
                    continue
                truth = truth_of(fn)
                if truth is None:
                    continue
                img = imread_cn(os.path.join(cur, fn))
                if img is not None and img.size:
                    rel = os.path.relpath(os.path.join(cur, fn), d)
                    out.append((truth, rel, img))
    return out


def coverage(samples):
    """样本覆盖情况：不同数值多少个、有几个数值有多个视角。"""
    per = {}
    for truth, _fn, _img in samples:
        per.setdefault(truth, 0)
        per[truth] += 1
    multi = sorted(v for v, c in per.items() if c >= 2)
    single = sorted(v for v, c in per.items() if c == 1)
    return {"values": sorted(int(v) for v in per), "multi": multi,
            "single": single, "per": per}


def evaluate(sample, verbose=False):
    """解码一张样本，返回 (truth, direct, best, direct_ok, best_ok)。"""
    truth, fn, img = sample
    direct, dinfo = decode_led_number(img)
    best, binfo = decode_led_best(img)
    direct_ok = direct == truth
    best_ok = best == truth
    if verbose or not best_ok:
        tag = "OK " if best_ok else "?  " if best is None or "?" in str(best) else "X  "
        note = "" if direct_ok else f"（直接解码 {direct!r}: {dinfo[:36]}）"
        print(f"  {tag}{fn[:26]:28s} 真值={truth:>4s} 读出={str(best):>6s}{note}")
    return truth, direct, best, direct_ok, best_ok


def run(dirs=None, verbose=False):
    """跑全部真实样本，返回汇总 dict；没有样本时返回 None。"""
    samples = load_samples(dirs)
    if not samples:
        for d in (dirs or SAMPLE_DIRS):
            if os.path.isdir(d):
                print(f"目录里没有可用样本：{d}")
        print("（采集方式：程序里点「采集数字样本」，或把截图按 <真值>.png 命名放进 数字截图/）")
        return None
    ok = direct_ok_n = 0
    wrong = unknown = 0
    per = {}
    for s in samples:
        truth, direct, best, d_ok, b_ok = evaluate(s, verbose=verbose)
        if b_ok:
            ok += 1
        elif best is None or "?" in str(best):
            unknown += 1
        else:
            wrong += 1
        direct_ok_n += 1 if d_ok else 0
        per.setdefault(truth, [0, 0, 0])
        per[truth][0 if b_ok else (1 if (best and "?" not in str(best)) else 2)] += 1
    total = len(samples)
    print(f"\n真实样本 {total} 张（来自 {', '.join(os.path.basename(d) or d for d in (dirs or SAMPLE_DIRS) if os.path.isdir(d))}）")
    print(f"直接解码正确   {direct_ok_n}/{total} = {direct_ok_n / total:.1%}")
    print(f"含背板兜底正确 {ok}/{total} = {ok / total:.1%}")
    cov = coverage(samples)
    print(f"覆盖：{len(cov['values'])} 个不同数值，{len(cov['multi'])} 个数值有多视角样本")
    if cov["single"]:
        _show = cov["single"][:24]
        tail = "…" if len(cov["single"]) > 24 else ""
        print(f"只有 1 个视角的数值：{', '.join(_show)}{tail}"
              f"（共 {len(cov['single'])} 个，建议补 41-2 / 41-3 这样的斜视角）")
    if wrong:
        print("读错的对照（真值 -> 读出）：")
        for truth, fn, img in samples:
            val, _ = decode_led_best(img)
            if val != truth and val and "?" not in str(val):
                print(f"   {truth} -> {val}   ({fn})")
    if unknown:
        print(f"读不出 {unknown} 张（上面已列出，多为选区/遮挡问题）")
    return {"total": total, "ok": ok, "wrong": wrong, "unknown": unknown,
            "direct_ok": direct_ok_n, "acc": ok / total, "per": per}


if __name__ == "__main__":
    _summary = run(verbose="-v" in sys.argv)
    if _summary is None:
        sys.exit(0)                        # 没有样本不算失败
    sys.exit(1 if (_summary["wrong"] or _summary["unknown"]) else 0)
