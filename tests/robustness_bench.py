# -*- coding: utf-8 -*-
"""对抗性鲁棒性基准：往解码链上泼真实世界会发生的脏水。

与现有基准的分工：
- digit_bench      常规干扰（泛光/缩放/抖动/亮度/噪声/旋转）下的准确率；
- extreme_bench    转视角/倾斜的几何鲁棒性；
- robustness_bench（本文件）恶劣环境与内容陷阱——极暗/极亮/偏色/遮挡/
  模糊/压缩伪影/熄屏/贴边截断/前导零/切位/帧序列污染/残缺模板库。

红线指标：**绝不给出会翻转补币决策的错读**。
- 真值足够下注（>=bet_count）却读出 0/1/2  -> 误触发提前补币（重复投币）；
- 真值不足一注（<bet_count）却读出「足够」 -> 带着空币继续抽奖。
读不出、判未知都是安全侧。任何一类红线违例都让本基准退出码非 0。

用法：  python tests/robustness_bench.py [-v]
"""
import contextlib
import io
import os
import sys
import tempfile
from collections import defaultdict

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import slotbot as s  # noqa: E402
from tests.digit_bench import fit_geometry  # noqa: E402
from tests.real_bench import load_samples  # noqa: E402
from tests.synth_digits import synth_one  # noqa: E402
import vision as v  # noqa: E402
from vision import (clean_mask, decode_consensus, decode_credit_condition,  # noqa: E402
                    decode_led_best, decode_led_fused)

BET_COUNT = 3
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------- 干扰函数（BGR -> BGR） ----------------
def p_dark(img):
    return cv2.convertScaleAbs(img, alpha=.45, beta=-6)


def p_bright(img):
    return cv2.convertScaleAbs(img, alpha=1.65, beta=28)


def p_green_cast(img):
    """VRChat 世界的绿色氛围灯：G 通道被环境光抬高。"""
    out = img.astype(np.float32)
    out[:, :, 1] *= 1.55
    out[:, :, 2] *= .92
    return np.clip(out, 0, 255).astype(np.uint8)


def p_blue_cast(img):
    out = img.astype(np.float32)
    out[:, :, 0] *= 1.6
    return np.clip(out, 0, 255).astype(np.uint8)


def p_low_contrast_red(img):
    """环境光把红色压淡：r-g 差值向 led_mask 的 25 阈值逼近。"""
    r = img[:, :, 2].astype(np.float32)
    g = img[:, :, 1].astype(np.float32)
    mixed = r * .62 + g * .38
    out = img.copy()
    out[:, :, 2] = mixed.astype(np.uint8)
    return out


def p_defocus(img):
    return cv2.GaussianBlur(img, (9, 9), 2.2)


def p_motion(img):
    k = np.zeros((1, 9), np.float32)
    k[0, :] = 1 / 9
    return cv2.filter2D(img, -1, k)


def p_jpeg25(img):
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 25])
    return cv2.imdecode(buf, cv2.IMREAD_COLOR) if ok else img


def p_occlude_half(img):
    """.avatar 路过挡住半串数字。"""
    out = img.copy()
    h, w = out.shape[:2]
    out[:, w // 2:] = (26, 22, 20)
    return out


def p_occlude_band(img):
    out = img.copy()
    h = out.shape[0]
    out[h // 3:2 * h // 3, :] = (26, 22, 20)
    return out


def p_far(img):
    """站远了看：分辨率掉一半再放大回来（信息已经丢了）。"""
    h, w = img.shape[:2]
    small = cv2.resize(img, (max(4, w // 2), max(4, h // 2)), interpolation=cv2.INTER_AREA)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def p_heavy_noise(img):
    out = img.astype(np.float32) + np.random.normal(0, 22, img.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


PERTURBATIONS = (
    ('极暗', p_dark), ('极亮', p_bright), ('绿氛围灯', p_green_cast),
    ('蓝氛围灯', p_blue_cast), ('红被压淡', p_low_contrast_red),
    ('失焦', p_defocus), ('运动模糊', p_motion), ('JPEG25', p_jpeg25),
    ('遮挡半边', p_occlude_half), ('遮挡中带', p_occlude_band),
    ('站远一半', p_far), ('重噪声', p_heavy_noise),
)


# ---------------- 内容陷阱帧 ----------------
def hazard_frames(geom):
    h, w = geom[0], geom[1]
    dark = np.full((h, w, 3), (26, 22, 20), np.uint8)
    blob = dark.copy()
    blob[:, :] = (60, 90, 255)                      # 过曝的红色实心块
    rng = np.random.RandomState(5)
    noise = np.clip(rng.normal(30, 25, (h, w, 3)), 0, 255).astype(np.uint8)
    noise[:, :, 2] = np.clip(noise[:, :, 2].astype(np.int32) + 90, 0, 255).astype(np.uint8)
    textish = dark.copy()
    for y in range(4, h - 4, max(6, h // 6)):       # 横向红条纹（像 UI 红字）
        cv2.rectangle(textish, (3, y), (w - 4, y + 2), (40, 60, 235), -1)
    return (('熄屏', dark), ('过曝实心块', blob), ('红色噪声', noise), ('红条纹', textish))


def render(truth, geom, yaw=0.0, pitch=0.0, roll=0.0):
    return synth_one(truth, geom, yaw, pitch, roll, rng=np.random.RandomState(11))


def classify_credit(truth, credit):
    """补币决策安全性：返回 '决策翻转' / '错读' / '安全'。"""
    if credit is None or credit == 'unknown':
        return '安全'
    if isinstance(credit, str) and credit == 'enough':
        return '决策翻转' if int(truth) < BET_COUNT else '安全'
    if isinstance(credit, int):
        if int(truth) < BET_COUNT:
            return '安全' if credit == int(truth) else ('错读' if credit < BET_COUNT else '决策翻转')
        return '决策翻转' if credit < BET_COUNT else '安全'
    return '安全'


def run(verbose=False):
    geom = fit_geometry(os.path.join(HERE, 'credit_zero_real.png'),
                        os.path.join(HERE, 'credit_seven_real.png'))
    real = [(t, img) for t, _p, img in load_samples()]
    bank = s.build_template_bank(real) if real else {}
    if not bank:
        print('!! 没有真机样本可建库，补币决策安全指标跳过')
    truths = ('0', '1', '2', '7', '8', '47', '99', '10', '31', '96')
    stats = defaultdict(lambda: [0, 0, 0])          # 对/错/读不出
    red_low = []          # 足够下注却被读成低币（误补币）
    red_high = []         # 低币却被读成足够（空币硬抽）
    misreads = []
    seq_flags = []

    def one(truth, img, tag):
        val, info = decode_led_best(img)
        g, w_, u = stats[tag]
        if val == truth:
            stats[tag] = [g + 1, w_, u]
        elif val is None or '?' in str(val):
            stats[tag] = [g, w_, u + 1]
            if verbose:
                misreads.append(f'   ? [{tag}] 真值 {truth} | {str(info)[:52]}')
        else:
            stats[tag] = [g, w_ + 1, u]
            if verbose:
                misreads.append(f'   X [{tag}] 真值 {truth} 读成 {val} | {str(info)[:52]}')
        if bank and truth.isdigit() and len(truth) <= 2:
            credit, _ci = decode_credit_condition(img, bank, None, BET_COUNT)
            verdict = classify_credit(truth, credit)
            if verdict == '决策翻转':
                (red_low if int(truth) >= BET_COUNT else red_high).append(
                    f'[{tag}] {truth} -> {credit}')

    for truth in truths:
        base = render(truth, geom)
        one(truth, base, '基准正视角')
        for name, fn in PERTURBATIONS:
            one(truth, fn(base), name)
        # 轻斜 + 重噪声组合
        one(truth, p_heavy_noise(render(truth, geom, 14, 8, 4)), '斜+噪声')
    # 内容陷阱：期望全部走「读不出/拒绝」，读出任何自信值都记读错
    for name, img in hazard_frames(geom):
        one('-', img, f'陷阱·{name}')
    # 贴边截断：把数字裁到笔画贴住边界，digit_fit 必须拒绝
    for truth in ('0', '8', '47'):
        img = render(truth, geom)
        m = clean_mask(img)
        ys, xs = np.where(m)
        tight = img[max(0, ys.min() - 1):ys.max() + 2, max(0, xs.min() - 1):xs.max() + 2]
        one(truth, tight, '贴边截断')
    # 切位：'99' 只留一位宽 —— LED 层读出 '9' 是像素级正确（画面里确实
    # 只有一位），决策层的宽度门必须拒判，绝不能拿它当完整 Credit 用
    img99 = render('99', geom)
    m = clean_mask(img99)
    ys, xs = np.where(m)
    half = img99[ys.min():ys.max() + 1, xs.min():xs.min() + (xs.max() - xs.min()) // 2 + 1]
    if bank:
        cut_credit, _ci = decode_credit_condition(half, bank, None, BET_COUNT)
        cut_ok = cut_credit is None
        seq_flags.append(('切位宽度门拒判', cut_ok))
        print(f"  {'OK ' if cut_ok else 'FAIL'} 切掉一位后决策层拒判: {cut_credit}")

    total = sum(sum(v) for v in stats.values())
    ok = sum(v[0] for v in stats.values())
    wrong = sum(v[1] for v in stats.values())
    unknown = sum(v[2] for v in stats.values())
    print(f"鲁棒性样本 {total} 张（干扰 x 数字 + 内容陷阱 + 截断）\n")
    print(f"{'条件':<14}{'对':>5}{'读错':>5}{'读不出':>6}")
    for tag, (g, w_, u) in stats.items():
        mark = '  <<<' if w_ and tag.startswith('陷阱') else ''
        print(f"{tag:<16}{g:>5}{w_:>5}{u:>6}{mark}")
    print(f"\n总计：读对 {ok}  读错 {wrong}  读不出 {unknown}（读对率 {ok / total:.1%}）")
    print(f"补币决策翻转：足够->低币 {len(red_low)} 起 / 低币->足够 {len(red_high)} 起"
          f"{'  —— 红线违例!!!' if red_low or red_high else '（0，安全）'}")

    # ---- 帧序列语义 ----
    a = render('47', geom)
    b = render('8', geom)
    c = render('31', geom)
    seq_checks = (
        (('47', '47', '47'), '47', (3, 3)),
        (('47', '47', '8'), '47', (2, 3)),
        (('47', '8', '31'), None, None),
        (('47', '?', '47'), '47', None),
    )
    print('\n帧序列语义：')
    frames = {'47': a, '8': b, '31': c}
    for names, want, want_counts in seq_checks:
        fr = [frames[n] for n in names if n != '?']
        val, _i, agree, total_f = decode_consensus(fr)
        good = val == want and (want_counts is None or (agree, total_f) == want_counts)
        seq_flags.append((f'帧序列 {names}', good))
        print(f"  {'OK ' if good else 'FAIL'} {names} -> {val} ({agree},{total_f})")

    # ---- 残缺模板库 ----
    print('残缺模板库：')
    empty_fused = decode_led_fused(a, {})
    print(f"  {'OK ' if empty_fused[0] == '47' else 'FAIL'} 空库回退纯段码: {empty_fused[0]}")
    seq_flags.append(('空库回退纯段码', empty_fused[0] == '47'))
    partial = {ch: masks for ch, masks in bank.items() if ch in '047'}
    if partial and len(partial) < 10:
        credit, _ = decode_credit_condition(render('8', geom), partial, None, BET_COUNT)
        good = credit is None
        seq_flags.append(('残缺库拒判高币', good))
        print(f"  {'OK ' if good else 'FAIL'} 缺 6 个数字的库拒判高币: {credit}")
    # 损坏文件容忍：一个坏 png 不能拖垮整库
    with tempfile.TemporaryDirectory() as tmp:
        for i, (t, img) in enumerate(real[:12]):
            cv2.imencode('.png', img)[1].tofile(os.path.join(tmp, f'v{t}_{i:03d}.png'))
        with open(os.path.join(tmp, 'v9_broken.png'), 'wb') as fh:
            fh.write(bytes([0x89]) + b'PNG broken payload')
        old_dirs = s.SAMPLE_DIRS
        s.SAMPLE_DIRS = [tmp]
        try:
            b2 = s.build_template_bank(s.load_digit_samples())
            n2 = sum(len(v) for v in b2.values())
            expect = sum(len(v.digit_masks(img)) for t, img in real[:12]
                         if len(v.digit_masks(img)) == len(t))
            good = n2 >= expect and n2 > 0
            seq_flags.append(('损坏样本跳过', good))
            print(f"  {'OK ' if good else 'FAIL'} 损坏样本被跳过，好样本保留: {n2} 个模板")
        finally:
            s.SAMPLE_DIRS = old_dirs

    # ---- 多种子稳定性（digit_bench 合成基准跑 6 个种子）----
    print('多种子稳定性（digit_bench，种子 1-6）：')
    import tests.digit_bench as db
    rows = []
    for seed in range(1, 7):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ok_r, wrong_r, unk_r = db.run(per_digit=30, seed=seed)
        rows.append((seed, ok_r, wrong_r, unk_r))
        print(f"  seed {seed}: 对 {ok_r:.1%}  错 {wrong_r:.1%}  读不出 {unk_r:.1%}")
    w_spread = max(r[2] for r in rows) - min(r[2] for r in rows)
    print(f"  读错率极差 {w_spread:.1%}（<=2% 视为稳定）")

    seq_ok = all(g for _n, g in seq_flags)
    verdict = (not red_low and not red_high and seq_ok)
    print(f"\n结论：{'PASS' if verdict else 'FAIL —— 存在红线违例，见上'}")
    if verbose and misreads:
        print('\n读不出/读错明细（前 60 条）：')
        for line in misreads[:60]:
            print(line)
    return 0 if verdict else 1


if __name__ == '__main__':
    sys.exit(run(verbose='-v' in sys.argv))
