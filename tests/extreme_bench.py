# -*- coding: utf-8 -*-
"""极端视角基准：伪 3D 渲染「转视角/倾斜」的数字，统计解码链成绩。

为什么需要它：真机样本基本都是正视角（数字截图/ 里 97 个数值只有单一
视角），斜视角只有 0-2/1-2/1-3 几张。视角一变（透视+泛光方向变化），
段码采样窗和模板都会失配——用户实际游玩时站位一偏识别就崩，而旧基准
完全测不到这一点。synth_digits 用单应变换把「AutoCollector 转出去的
每一个角度」在合成域铺满，这里按视角桶统计 读对/读错/读不出。

门禁：读错必须为 0（和真机基准同一原则：读不出可以，读错不行）。

用法：  python tests/extreme_bench.py [-v]
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vision import decode_led_best  # noqa: E402
from tests.synth_digits import synth_samples  # noqa: E402


def bucket(yaw, pitch, roll):
    """按综合倾斜程度分桶，观察「越斜越差」的曲线。"""
    tilt = abs(yaw) + abs(pitch) + abs(roll)
    if tilt == 0:
        return '正面'
    if tilt <= 20:
        return '轻斜(≤20°)'
    if tilt <= 40:
        return '中斜(≤40°)'
    return '极端(>40°)'


def run(verbose=False, per_pose=1):
    total = ok = wrong = unknown = 0
    per_bucket = defaultdict(lambda: [0, 0, 0])       # 对/错/读不出
    per_digit = defaultdict(lambda: [0, 0, 0])
    failures = []
    for truth, img, (yaw, pitch, roll) in synth_samples(per_pose=per_pose):
        val, info = decode_led_best(img)
        b = bucket(yaw, pitch, roll)
        total += 1
        if val == truth:
            ok += 1
            per_bucket[b][0] += 1
            per_digit[truth][0] += 1
        elif val is None or '?' in str(val):
            unknown += 1
            per_bucket[b][2] += 1
            per_digit[truth][2] += 1
            if verbose:
                failures.append(f'   ? {truth} 视角 yaw={yaw} pitch={pitch} roll={roll} | {info[:56]}')
        else:
            wrong += 1
            per_bucket[b][1] += 1
            per_digit[truth][1] += 1
            failures.append(f'   X {truth}->{val} 视角 yaw={yaw} pitch={pitch} roll={roll}')
    print(f"合成极端视角样本 {total} 张（视角网格 × 0-9）")
    print(f"\n{'视角桶':<12}{'对':>6}{'读错':>6}{'读不出':>7}")
    for b in ('正面', '轻斜(≤20°)', '中斜(≤40°)', '极端(>40°)'):
        g, w, u = per_bucket.get(b, [0, 0, 0])
        print(f"{b:<14}{g:>6}{w:>6}{u:>7}")
    print(f"\n总计：正确 {ok}  读错 {wrong}  读不出 {unknown}")
    print(f"读对率 {ok / total:.1%}   （读错 {wrong / total:.1%} / 读不出 {unknown / total:.1%}）")
    if wrong:
        print("\n!!! 存在读错 —— 读错是红线，先修再合 !!!")
    if verbose:
        print('\n失败样本：')
        for line in failures[:60]:
            print(line)
    return ok / total, wrong, unknown


if __name__ == '__main__':
    run(verbose='-v' in sys.argv)
