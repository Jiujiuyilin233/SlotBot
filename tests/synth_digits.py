# -*- coding: utf-8 -*-
"""七段数码管的「伪 3D」渲染器：平面数字 + 透视投影 + 真机干扰链。

为什么需要它：用户要「自动转视角、倾斜、各种尽量极端的角度收集图片」，
但真机采集一次只有一个角度（AutoCollector 转一次要投币、要等人）。而
LED 面板本质是**平面**的，转视角在成像上的效果就是单应变换（透视投影）：
    像素' = K · R(yaw,pitch,roll) · K⁻¹ · 像素
所以可以在离线把「转出去的每一个角度」都渲染出来——相当于把 AutoCollector
的采集空间在合成域里铺满。渲染之后走的干扰链（泛光、亮度、噪声、暗背板）
和 digit_bench 一致，保证合成图与真机链路同分布。

用法：
    from tests.synth_digits import synth_samples
    for truth, img, meta in synth_samples():
        ...   # meta 里有 (yaw, pitch, roll, seed)
"""
import os
import sys

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.digit_bench import SEGMENTS, SEG_ORDER, fit_geometry  # noqa: E402

BG_COLOR = (26, 22, 20)          # 深棕机柜背板
LED_COLOR = (40, 60, 235)        # 亮红 LED（BGR）


def _round_segment(mask, p1, p2, t):
    """画一根两端圆头的 LED 笔画（真实灯珠不是方头）。"""
    cv2.line(mask, p1, p2, 1, thickness=t)
    r = max(1, t // 2)
    cv2.circle(mask, p1, r, 1, -1)
    cv2.circle(mask, p2, r, 1, -1)


def render_number_mask(text, h, w, t, gap, inset):
    """把一串数字渲染成 (整串 0/1 掩码, 每位的列范围)。逐位单独生成再拼接。

    坐标方案与 digit_bench.render_digit 完全一致（解码器的采样窗就是按
    那套几何标定的），只是笔画换成两端圆头的画法（真 LED 灯珠圆头）。
    """
    masks = []
    r = max(1, t // 2)                 # 端帽半径
    mid = h // 2
    for ch in str(text):
        m = np.zeros((h, w), np.uint8)
        seg = {
            'a': ((inset + r, t // 2), (w - inset - r, t // 2)),
            'd': ((inset + r, h - t // 2), (w - inset - r, h - t // 2)),
            'g': ((inset + r, mid), (w - inset - r, mid)),
            'f': ((t // 2, inset + r), (t // 2, mid - gap // 2)),
            'b': ((w - t // 2, inset + r), (w - t // 2, mid - gap // 2)),
            'e': ((t // 2, mid + gap // 2), (t // 2, h - inset - r)),
            'c': ((w - t // 2, mid + gap // 2), (w - t // 2, h - inset - r)),
        }
        for s in SEGMENTS[ch]:
            p1, p2 = seg[s]
            _round_segment(m, p1, p2, t)
        masks.append(m)
    sep = max(2, round(h * .12))
    total_w = sum(m.shape[1] for m in masks) + sep * (len(masks) - 1)
    out = np.zeros((h, total_w), np.uint8)
    spans = []
    x = 0
    for m in masks:
        out[:, x:x + m.shape[1]] |= m
        spans.append((x, x + m.shape[1]))
        x += m.shape[1] + sep
    return out, spans


def r0(n):
    return max(1, n // 10)


def viewpoint_homo(yaw, pitch, roll, w, h, d_mult=3.0):
    """平面转视角的单应矩阵：七段面板绕**自身中心**转 (yaw,pitch,roll)。

    推导（别用 K·R·K⁻¹ 裸式——那是「旋转相机」，面板会被甩出相机背后，
    warp 出来一片空，本项目实际踩过）：面板 z=d、绕中心 C=(0,0,d) 旋转 R，
    P' = R·P + (I-R)·C；代入 P=(x,y,d) 与 (x,y)=d·K⁻¹·(u,v,1) 化简后第三列
    收敛为 d·ẑ，于是 H = K·[d·r1, d·r2, d·ẑ]·K⁻¹（r1/r2 为 R 前两列）。
    K 建在像素坐标（主点=画面中心）；d=d_mult·面板尺寸，越小透视越强。
    """
    a, b, g = [np.deg2rad(v) for v in (yaw, pitch, roll)]
    Ry = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
    Rx = np.array([[1, 0, 0], [0, np.cos(b), -np.sin(b)], [0, np.sin(b), np.cos(b)]])
    Rz = np.array([[np.cos(g), -np.sin(g), 0], [np.sin(g), np.cos(g), 0], [0, 0, 1]])
    R = Rz @ Rx @ Ry
    focal = 2.6 * max(w, h)
    d = d_mult * max(w, h)
    K = np.array([[focal, 0, w / 2.0], [0, focal, h / 2.0], [0, 0, 1]], np.float64)
    M = np.column_stack([d * R[:, 0], d * R[:, 1], d * np.array([0, 0, 1.0])])
    return K @ M @ np.linalg.inv(K)


def to_color(mask):
    img = np.full((*mask.shape, 3), BG_COLOR, np.uint8)
    img[mask > 0] = LED_COLOR
    return img


def photometric(img, rng, bloom_k=(3, 7), bright=(0.75, 1.3), noise=(2, 8),
                vignette=True):
    """真机干扰链：缩放→泛光→提亮→亮度曲线→噪声→暗角。顺序=物理顺序。"""
    h, w = img.shape[:2]
    out = img
    f = float(rng.uniform(.85, 1.2))
    out = cv2.resize(out, (max(4, int(w * f)), max(4, int(h * f))))
    out = cv2.resize(out, (w, h))
    if bloom_k:
        k = int(rng.choice(bloom_k))
        blur = cv2.GaussianBlur(out, (k, k), float(rng.uniform(.6, 1.8)))
        # 泛光是红 LED 的光晕：红通道满溢，绿/蓝几乎不跟随。若把三通道
        # 一起抬，r > g*1.7 的红色优势判据就会失效（识别器靠它把红字从
        # 背景里分出来）。所以光晕先按通道衰减再做屏幕混合。
        glow = blur.copy()
        glow[:, :, 1] = (glow[:, :, 1].astype(np.float32) * .25).astype(np.uint8)
        glow[:, :, 0] = (glow[:, :, 0].astype(np.float32) * .35).astype(np.uint8)
        # 光晕整体衰减到真机水平：屏幕混合不衰减会把相邻笔画粘连成实心块
        # （实心块的 d.mean()>0.94 会被解码器当非数字块拒绝——过犹不及）
        a = out.astype(np.int32)
        b = (glow.astype(np.float32) * .55).astype(np.int32)
        out = np.clip(255 - (255 - a) * (255 - b) // 255, 0, 255).astype(np.uint8)
        # 提亮也只提红通道（真机泛光是 R 满溢，不是整体曝光）
        rr = out[:, :, 2].astype(np.int32) * 110 // 100 + 4
        out[:, :, 2] = np.clip(rr, 0, 255).astype(np.uint8)
    out = cv2.convertScaleAbs(out, alpha=float(rng.uniform(*bright)),
                              beta=float(rng.uniform(-12, 14)))
    if noise:
        out = np.clip(out.astype(np.float32)
                      + rng.normal(0, float(rng.uniform(*noise)), out.shape),
                      0, 255).astype(np.uint8)
    if vignette:
        yy, xx = np.mgrid[0:h, 0:w]
        d = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
        out = np.clip(out.astype(np.float32) * (1.0 - .25 * np.clip(d - .55, 0, 1))[..., None],
                      0, 255).astype(np.uint8)
    return out


def synth_one(text, geom, yaw=0.0, pitch=0.0, roll=0.0, rng=None, **pho_kw):
    """渲染一张指定视角的数字串图片（支持多位，画布按整串实际尺寸）。"""
    h, w, t, gap, inset = geom
    mask, _spans = render_number_mask(text, h, w, t, gap, inset)
    # 先把掩码内缩一点再透视：极端角度下投影会把角点甩出画面，留出边距
    mh, mw = mask.shape
    pad = max(6, round(h * .18))
    canvas = np.zeros((mh + 2 * pad, mw + 2 * pad), np.uint8)
    canvas[pad:pad + mh, pad:pad + mw] = mask
    H = viewpoint_homo(yaw, pitch, roll, canvas.shape[1], canvas.shape[0])
    warped = cv2.warpPerspective(canvas, H, (canvas.shape[1], canvas.shape[0]),
                                 flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0)
    # 投影后的内容重裁居中，避免内容缩在角落
    ys, xs = np.where(warped)
    if len(ys):
        y0, y1 = ys.min(), ys.max() + 1
        x0, x1 = xs.min(), xs.max() + 1
        warped = warped[y0:y1, x0:x1]
    img = to_color(warped)
    if rng is not None:
        img = photometric(img, rng, **pho_kw)
        p = int(rng.uniform(3, 9))
        img = cv2.copyMakeBorder(img, p, p, p, p, cv2.BORDER_CONSTANT, value=BG_COLOR)
    return img


def synth_samples(per_pose=1, seed=7, digits='0123456789',
                  yaws=(0, -10, 10, -20, 20, -30, 30),
                  pitches=(0, -10, 10, -20, 20),
                  rolls=(0, -4, 4, -8, 8), **pho_kw):
    """遍历视角网格 × 数字，产出 (truth, img, (yaw,pitch,roll))。

    网格即「自动转视角采集」的合成版：7×5×5=175 个视角 × 10 数字。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    geom = fit_geometry(os.path.join(here, 'credit_zero_real.png'),
                        os.path.join(here, 'credit_seven_real.png'))
    rng = np.random.RandomState(seed)
    np.random.seed(seed)
    for yaw in yaws:
        for pitch in pitches:
            for roll in rolls:
                for _ in range(per_pose):
                    for d in digits:
                        img = synth_one(d, geom, yaw, pitch, roll, rng, **pho_kw)
                        yield d, img, (yaw, pitch, roll)
