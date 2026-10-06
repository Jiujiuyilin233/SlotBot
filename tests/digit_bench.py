# -*- coding: utf-8 -*-
"""数字识别准确率基准。

为什么需要它：之前判断「识别准不准」全靠几张真实截图肉眼看，
只能覆盖 0 和 7 两个数字，改完解码器也说不清是变好还是变坏。

这里用**真实显示器几何**拟合出的渲染器生成 0-9，再叠加机器画面里真实
存在的干扰（泛光、缩放、抖动、亮度、噪声、旋转），统计准确率。
数字会写进 stdout，改动前后可以直接对比。

用法：  python tests/digit_bench.py
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vision import clean_mask, decode_led_number, digit_fit  # noqa: E402

# 段码 -> 点亮的段
SEGMENTS = {
    '0': 'abcdef', '1': 'bc', '2': 'abdeg', '3': 'abcdg', '4': 'bcfg',
    '5': 'acdfg', '6': 'acdefg', '7': 'abc', '8': 'abcdefg', '9': 'abcdfg',
}
SEG_ORDER = 'abcdefg'


def render_digit(digit, h, w, t, gap, inset):
    """画一个七段数码管数字，返回 0/1 mask。"""
    img = np.zeros((h, w), np.uint8)
    mid = h // 2
    seg = {
        'a': (slice(0, t), slice(inset, w - inset)),
        'd': (slice(h - t, h), slice(inset, w - inset)),
        'g': (slice(mid - t // 2, mid - t // 2 + t), slice(inset, w - inset)),
        'f': (slice(inset, mid - gap // 2), slice(0, t)),
        'b': (slice(inset, mid - gap // 2), slice(w - t, w)),
        'e': (slice(mid + gap // 2, h - inset), slice(0, t)),
        'c': (slice(mid + gap // 2, h - inset), slice(w - t, w)),
    }
    for s in SEGMENTS[digit]:
        img[seg[s]] = 1
    return img


def bbox(mask):
    ys, xs = np.where(mask)
    return mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def iou(a, b):
    if a.shape != b.shape:
        b = cv2.resize(b.astype(np.uint8), (a.shape[1], a.shape[0]),
                       interpolation=cv2.INTER_NEAREST)
    return float((a & b).sum()) / max(1, int((a | b).sum()))


def fit_geometry(real_zero_path, real_seven_path):
    """用真实截图拟合 (h, w, 笔画粗细 t, 中间缝隙 gap, 内缩 inset)。"""
    out = (39, 26, 5, 3, 2)          # 兜底值：实测 0 的 bbox 差不多就是这个尺寸
    try:
        z = cv2.imread(real_zero_path)
        s = cv2.imread(real_seven_path)
        if z is None or s is None:
            return out
        mz, ms = bbox(clean_mask(z)), bbox(clean_mask(s))
        best = None
        for h in range(30, 46, 2):
            for w in range(20, 34, 2):
                for t in range(3, 9):
                    for gap in range(1, 6):
                        for inset in range(1, 5):
                            sc = (iou(mz, render_digit('0', h, w, t, gap, inset))
                                  + iou(ms, render_digit('7', h, w, t, gap, inset)))
                            if best is None or sc > best[0]:
                                best = (sc, h, w, t, gap, inset)
        if best:
            out = best[1:]
    except Exception:
        pass
    return out


def to_bgr(mask, color=(40, 60, 235)):
    """把 0/1 mask 画成 BGR 图（红字 + 暗背景），模拟机器上的样子。"""
    h, w = mask.shape
    img = np.full((h, w, 3), (26, 22, 20), np.uint8)        # 深棕机柜背景
    img[mask > 0] = color
    return img


def jitter(img, rng, bloom=True, scale=True, shift=True,
           bright=True, noise=True, rotate=True):
    """叠加机器画面里真实存在的干扰。"""
    h, w = img.shape[:2]
    out = img
    if scale:
        f = float(rng.uniform(0.85, 1.2))
        out = cv2.resize(out, (max(4, int(w * f)), max(4, int(h * f))))
        out = cv2.resize(out, (w, h))
    if rotate and rng.random() < .5:
        ang = float(rng.uniform(-5, 5))
        out = cv2.warpAffine(out, cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0),
                             (w, h), borderMode=cv2.BORDER_REPLICATE)
    if shift:
        dx, dy = float(rng.uniform(-3, 3)), float(rng.uniform(-2, 2))
        out = cv2.warpAffine(out, np.float32([[1, 0, dx], [0, 1, dy]]), (w, h),
                             borderMode=cv2.BORDER_REPLICATE)
    # 加一圈边距，模拟"框比数字略大"
    pad = int(rng.uniform(3, 9))
    out = cv2.copyMakeBorder(out, pad, pad, pad, pad, cv2.BORDER_CONSTANT,
                             value=(26, 22, 20))
    if bloom:
        k = int(rng.choice([3, 5, 5, 7]))
        out = cv2.GaussianBlur(out, (k, k), float(rng.uniform(0.6, 1.8)))
        # 发光：把亮部再提一点
        out = cv2.addWeighted(out, 1.15, np.zeros_like(out), 0, 6)
    if bright:
        out = cv2.convertScaleAbs(out, alpha=float(rng.uniform(0.75, 1.3)),
                                  beta=float(rng.uniform(-12, 14)))
    if noise:
        out = np.clip(out.astype(np.float32)
                      + np.random.normal(0, float(rng.uniform(2, 8)), out.shape),
                      0, 255).astype(np.uint8)
    return out


def load_real(digits_dir):
    """读取程序「采集数字样本」存下来的真实截图。

    文件名格式 v<真值>_<时间戳>.png，真值从文件名解析。
    """
    out = []
    if not os.path.isdir(digits_dir):
        return out
    for fn in sorted(os.listdir(digits_dir)):
        if not fn.lower().endswith(".png") or not fn.startswith("v") or "_" not in fn:
            continue
        truth = fn[1:fn.index("_")]
        if not truth.isdigit():
            continue
        img = cv2.imread(os.path.join(digits_dir, fn))
        if img is not None:
            out.append((truth, fn, img))
    return out


def run_real(digits_dir, verbose=False):
    """用真实样本评估——这是唯一可信的准确率。"""
    samples = load_real(digits_dir)
    if not samples:
        print(f"目录里没有样本：{digits_dir}")
        print("用法：在程序里点「采集数字样本」，让机器显示某数字后保存。")
        return None
    ok = wrong = unknown = 0
    per = {}
    for truth, fn, img in samples:
        val, info = decode_led_number(img)
        fit, fmsg = digit_fit(img)
        if val == truth:
            ok += 1
        elif val is None or "?" in str(val):
            unknown += 1
        else:
            wrong += 1
        per.setdefault(truth, [0, 0, 0])
        idx = 0 if val == truth else (1 if (val and "?" not in str(val)) else 2)
        per[truth][idx] += 1
        mark = "OK " if val == truth else ("?  " if (val is None or "?" in str(val)) else "X  ")
        if verbose or val != truth:
            extra = "" if fit else f"  [digit_fit 拒绝: {fmsg}]"
            print(f"  {mark}{fn[:26]:28s} 真值={truth:>4s} 读出={str(val):>6s}{extra}")
    total = len(samples)
    print(f"\n真实样本 {total} 张：正确 {ok}  读错 {wrong}  读不出 {unknown}")
    print(f"准确率 {ok / total:.1%}")
    if wrong:
        print("\n读错的对照（真值 -> 读出）：")
        for truth, fn, img in samples:
            val, _ = decode_led_number(img)
            if val != truth and val and "?" not in str(val):
                print(f"   {truth} -> {val}   ({fn})")
    return ok / total


def run(per_digit=40, seed=7, verbose=False):
    here = os.path.dirname(os.path.abspath(__file__))
    geo = fit_geometry(os.path.join(here, 'credit_zero_real.png'),
                       os.path.join(here, 'credit_seven_real.png'))
    h, w, t, gap, inset = geo
    print(f"拟合几何: 高={h} 宽={w} 笔画={t} 中缝={gap} 内缩={inset}")
    rng = np.random.RandomState(seed)
    np.random.seed(seed)   # jitter 里的噪声走了全局 np.random，必须一并固定，结果才可复现
    ok = wrong = unknown = 0
    per = {}
    for d in '0123456789':
        good, bad, unk = 0, 0, 0
        for _ in range(per_digit):
            img = to_bgr(render_digit(d, h, w, t, gap, inset))
            img = jitter(img, rng)
            val, info = decode_led_number(img)
            if val == d:
                good += 1
            elif val is None or '?' in str(val):
                unk += 1
                if verbose:
                    print(f"   ? {d} -> {val} | {info[:60]}")
            else:
                bad += 1
                if verbose:
                    print(f"   X {d} -> {val} | {info[:60]}")
        per[d] = (good, bad, unk)
        ok += good; wrong += bad; unknown += unk
    total = ok + wrong + unknown
    print(f"\n{'数字':<6}{'正确':<7}{'读错':<7}{'读不出':<7}")
    for d in '0123456789':
        g, b, u = per[d]
        print(f"{d:<8}{g:<9}{b:<9}{u:<9}")
    print(f"\n总计 {total} 张：正确 {ok}  读错 {wrong}  读不出 {unknown}")
    print(f"准确率 {ok / total:.1%}   （读错 {wrong / total:.1%} / 读不出 {unknown / total:.1%}）")
    return ok / total, wrong / total, unknown / total


if __name__ == '__main__':
    # 有真实样本时优先用真实样本（委托 real_bench：同时认「采集数字样本」
    # 存的 v<真值>_<时间戳>.png 和用户自截的 <真值>.png），合成图只作为
    # 还没有样本时的参考。
    import real_bench  # noqa: E402  与本文件同目录
    _summary = real_bench.run(verbose='-v' in sys.argv)
    print()
    if _summary is None:
        print("（没有真实样本，下面跑的是按真实显示器几何生成的合成图，仅供参考）")
    run(verbose='-v' in sys.argv)
