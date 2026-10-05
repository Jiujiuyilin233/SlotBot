"""模板库（自我训练）的留一法基准。

为什么单独有一份：准确率不能只看「整套样本全在库里」时的成绩——
那样等于拿答案对答案。留一法才是真机上的情形：这一张图是没见过的。

用法：
    python tests/bank_bench.py

输出三件事：
  ① 松/严门槛在留一法下的 读对 / 读不出 / **读错** —— 读错必须是 0；
  ② 剔除整个数值后（模拟「这个数从没见过」）模板还剩多少泛化能力；
  ③ 段码 vs 融合的对比，用来判断自训练到底有没有带来提升。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vision as v  # noqa: E402
from tests.real_bench import load_samples  # noqa: E402

# 与 vision.decode_led_fused 里的门槛保持一致
LOOSE = dict(max_dist=.16, margin=.05)
STRICT = dict(max_dist=.05, margin=.08)


def loo(samples, **kw):
    """留一法：每张图都用「不含它自己」的库去读。返回 (对, 读不出, 错)。"""
    ok = none = wrong = 0
    for i, (truth, img) in enumerate(samples):
        bank = v.build_template_bank([s for j, s in enumerate(samples) if j != i])
        val, _info = v.decode_by_template(img, bank, **kw)
        if val == truth:
            ok += 1
        elif val is None:
            none += 1
        else:
            wrong += 1
    return ok, none, wrong


def _bank_without_value(samples, truth, cache):
    """不含某个数值全部样本的库（建一次就缓存，104 个数值不要建 104 遍）。"""
    if truth not in cache:
        cache[truth] = v.build_template_bank(
            [(t, img) for t, img in samples if t != truth])
    return cache[truth]


def unseen_value(samples, cache, **kw):
    """剔除该数值的全部样本：模拟「这个数从没采过」。"""
    ok = none = wrong = 0
    for truth, img in samples:
        val, _info = v.decode_by_template(
            img, _bank_without_value(samples, truth, cache), **kw)
        if val == truth:
            ok += 1
        elif val is None:
            none += 1
        else:
            wrong += 1
    return ok, none, wrong


def row(name, ok, none, wrong, n):
    pct = lambda k: 100.0 * k / n if n else 0.0  # noqa: E731
    return f"  {name:<22} 对 {ok:3d} ({pct(ok):4.1f}%) | " \
           f"读不出 {none:3d} ({pct(none):4.1f}%) | 错 {wrong:2d} ({pct(wrong):4.1f}%)"


def run():
    samples = [(t, i) for t, _r, i in load_samples()]
    n = len(samples)
    if not n:
        print("没有样本：把实拍图按 <真值>.png 放进 数字截图/")
        return 1
    print(f"真实样本 {n} 张（留一法：每张图都用不含它自己的模板库去读）")

    print("\n【1】留一法：模板单独读")
    print(row("松门槛（救援用）", *loo(samples, **LOOSE), n))
    print(row("严门槛（否决用）", *loo(samples, **STRICT), n))

    print("\n【2】剔除整个数值：这个数从没采过时还剩多少")
    cache = {}
    print(row("松门槛", *unseen_value(samples, cache, **LOOSE), n))

    print("\n【3】段码 vs 融合（剔除整个数值的库）")
    seg_ok = fus_ok = 0
    for truth, img in samples:
        bank = _bank_without_value(samples, truth, cache)
        if v.decode_led_best(img)[0] == truth:
            seg_ok += 1
        if v.decode_led_fused(img, bank)[0] == truth:
            fus_ok += 1
    print(f"  段码   {seg_ok}/{n} = {100.0 * seg_ok / n:.1f}%")
    print(f"  融合   {fus_ok}/{n} = {100.0 * fus_ok / n:.1f}%")

    bad = loo(samples, **LOOSE)[2] + loo(samples, **STRICT)[2]
    print("\n门禁：两套门槛的错读都必须为 0 —— " + ("通过" if bad == 0 else f"失败（{bad} 次错读）"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
