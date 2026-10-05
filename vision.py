"""Conservative red seven-segment decoding and stable regional comparison."""
import cv2
import numpy as np

PATTERNS = {'0':'1111110','1':'0110000','2':'1101101','3':'1111001',
            '4':'0110011','5':'1011011','6':'1011111','7':'1110000',
            '8':'1111111','9':'1111011'}

def led_mask(img, led_min=65, color_diff=25):
    b, g, r = [img[:, :, i].astype(np.float32) for i in range(3)]
    # Brown cabinet edges satisfy an absolute R-G difference: require saturation too.
    return ((r > led_min) & (r-g > color_diff) & (r-b > color_diff)
            & (r > g*1.7) & (r > b*1.7))

def clean_mask(img, led_min=65, color_diff=25):
    m = led_mask(img, led_min, color_diff).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(m, 8)
    out = np.zeros_like(m)
    minimum = max(2, int(stats[1:, 4].max()*.1)) if n > 1 else 2
    for i in range(1, n):
        if stats[i, 4] >= minimum:
            out[labels == i] = 1
    return out

def split_segments(vals, ratio=.5, min_peak=.30):
    """把 7 段的填充率判定成"亮/灭"。

    不能用一个固定阈值：机器上的数字是**发光的**，泛光（bloom）会把
    熄灭的段渗亮 —— 实测「0」在泛光下有 42/60 的概率被读成 8，
    就是因为中间那段被辉光渗过了固定阈值。

    改用**相对判据**：一段算亮，当且仅当它的填充率达到"最亮那段"的一半。
    亮段通常 0.6+，被泛光渗染的灭段通常 0.2~0.35，两者分得很开。
    再叠加一个"最大间隙"修正：如果排序后存在明显断层，就按断层切，
    比单纯的比例更精确。
    """
    v = np.asarray(vals, dtype=np.float64)
    peak = float(v.max())
    if peak < min_peak:
        # 整体太暗：根本不像点亮的数码管
        return np.zeros(v.shape, dtype=bool)
    rel = peak * ratio
    if v.size >= 2:
        sv = np.sort(v)
        gaps = np.diff(sv)
        i = int(np.argmax(gaps))
        # 断层明显、且断层位置在合理区间时才按断层切
        if gaps[i] >= .15:
            cut = (sv[i] + sv[i + 1]) / 2
            if min_peak * .5 <= cut <= peak * .92:
                lit = v > cut
                if lit.any() and not lit.all():
                    return lit
    return v >= rel


def _digit_parts(img, led_min=65, color_diff=25):
    """把一张数码管图切成每位数字的掩码（左→右），并给出参考字高。

    返回 (mask, parts, ref_h, height)；parts 是 [(列起, 列止, 该位掩码), ...]。
    返回 None 表示这张图里没有可切的数字块。
    """
    m = clean_mask(img, led_min, color_diff)
    if m.sum() < 12:
        return None
    yy, xx = np.where(m)
    y0, y1 = int(yy.min()), int(yy.max())+1
    height = y1-y0
    if height < 12:
        return None
    # Keep every segment, including disconnected horizontal bars. Split only truly empty columns.
    cols = np.where(m[y0:y1].any(axis=0))[0]
    groups = np.split(cols, np.where(np.diff(cols) > max(2, round(height*.04)))[0]+1)

    # 倾斜（拍摄角度歪一点）会把「1」的上下两截笔画在水平方向错开 3px 左右，
    # 列间隙刚好超过切分阈值就被拆成两组，随后各自按行范围算宽高比都进不了
    # 窄条分支，读出一堆乱码。判据：列间隙很小、且**行范围错开**的组是同一
    # 个数字的上下两截，合并回来；真正的两位数字行范围几乎完全重叠，
    # 不会被这条规则误合并（实测真机位间距 ≥ 10px）。
    def _span(g):
        sub = m[y0:y1, int(g[0]):int(g[-1])+1]
        rr = np.where(sub.any(axis=1))[0]
        return int(rr.min()), int(rr.max())
    if len(groups) > 1:
        spans = [_span(g) for g in groups]
        merged = [groups[0]]
        for k in range(1, len(groups)):
            pa, pb = spans[k-1], spans[k]
            gap = int(groups[k][0]) - int(groups[k-1][-1]) - 1
            hmax = max(pa[1]-pa[0], pb[1]-pb[0]) + 1
            overlap = min(pa[1], pb[1]) - max(pa[0], pb[0]) + 1
            if gap <= max(3, round(hmax*.06)) and overlap < .3*min(pa[1]-pa[0]+1, pb[1]-pb[0]+1):
                merged[-1] = np.concatenate([merged[-1], groups[k]])
                spans[-1] = (min(pa[0], pb[0]), max(pa[1], pb[1]))
            else:
                merged.append(groups[k])
        groups = merged

    # 先按各自行范围切出每个组，再统一决定参考字高。
    parts = []
    for group in groups:
        g0, g1 = int(group[0]), int(group[-1])+1
        # 每个数字按**自己的行范围**取样，不能用整体行范围：混合高度时
        # （比如 47——4 没有顶段、7 有），矮数字的段采样窗会被整体框抬高，
        # 实测 47 里的 4 直接读不出来。这是真机会出现的组合，不是合成图特例。
        sub = m[y0:y1, g0:g1]
        rows = np.where(sub.any(axis=1))[0]
        d = sub[int(rows.min()):int(rows.max())+1]
        parts.append((g0, g1, d))
    # 参考字高：同一面板上的数字高度一致，取「宽高比像数字」的组里最高的。
    # 装饰残片（实测：右缘被切进的红色装饰只有 13~19px 高，真数字 55px+）
    # 会污染整体行范围，所以不能直接用 height 当字高。
    wide_hs = [d.shape[0] for _, _, d in parts
               if .30 <= d.shape[1] / max(1, d.shape[0]) <= 1.05]
    ref_h = max(wide_hs) if wide_hs else max(d.shape[0] for _, _, d in parts)
    return m, parts, ref_h, height


def decode_led_number(img, led_min=65, color_diff=25, seg_min=.18, **kw):
    if img is None or img.size == 0:
        return None, '画面为空'
    got = _digit_parts(img, led_min, color_diff)
    if got is None:
        if img is None or img.size == 0:
            return None, '画面为空'
        if clean_mask(img, led_min, color_diff).sum() < 12:
            return '?', '没有足够的数字笔画；遮挡、熄屏或选区错误不能视为 0'
        return '?', '数字太小或只有局部笔画'
    m, parts, ref_h, height = got
    values, patterns = [], []
    for g0, g1, d in parts:
        h, w = d.shape
        if w/h < .30:
            # A 1 has two vertical segments with a waist, not an arbitrary solid rectangle.
            r = d.mean(axis=1)
            waist = r[int(h*.4):int(h*.6)].min()
            if (.05 <= w/h <= .30 and h >= ref_h*.45
                    and d.mean() < .94 and waist < .85):
                values.append('1'); patterns.append('1'); continue
            # 到这里的窄条都不是数字：要么太矮（装饰残片，高度不到真数字的
            # 一半——真数字同面板等高），要么太细（分隔线，实测字高 77px 时
            # 只有 2px 宽）。直接跳过，不参与读数。
            # 竖向截断的真数字高度接近全高，不会掉进这条「跳过」分支，
            # 仍会在下面被拒绝——绝不能把 99 切掉一位后读成 9。
            continue
        # 门限放宽：真实「8」的填充率就有 0.71，加上发光很容易超过旧上限
        # 0.75 —— 旧门限把 8 判成「非数字红色块」，实测 40 张里 39 张读不出。
        if not .22 <= w/h <= 1.05 or not .05 < d.mean() < .94:
            return '?', '存在非数字红色块或选区包含无关物体'
        def seg(fy0,fy1,fx0,fx1):
            z=d[int(fy0*h):max(int(fy0*h)+1,int(fy1*h)),
                int(fx0*w):max(int(fx0*w)+1,int(fx1*w))]
            return float(z.mean())
        # 采样区收窄到段的中心，避开边缘渗光
        raw=[seg(0,.14,.30,.70),seg(.20,.38,.78,1),
             seg(.62,.80,.78,1),seg(.86,1,.30,.70),
             seg(.62,.80,0,.22),seg(.20,.38,0,.22),seg(.45,.55,.30,.70)]
        lit=split_segments(raw)
        p=''.join('1' if b else '0' for b in lit)
        ch=next((c for c,pat in PATTERNS.items() if p==pat), '?')
        # Short hook-shaped 7 used by this machine.
        # Verified on the user's real LuraSlot display: 7 also lights a short
        # left-upper hook. It has no bottom, middle, or left-lower segment.
        if p in ('1100000', '1110010'): ch='7'
        values.append(ch); patterns.append(p)
    if not values:
        # 所有组都被当杂片跳过时返回空串会让上层把「读不出」当成可信读数，
        # 必须维持未知语义——宁可读不出，绝不给一个空值冒充成功。
        return '?', '没有可辨认的数字（笔画可能被截断或全部是杂片）'
    return ''.join(values), '段码='+','.join(patterns)

def digit_fit(img, tol=2):
    """检查数字块是否被选区边界切到。

    这是「明明是 0 却读成 1」的根因：选区一旦偏了，数码管只剩一根竖笔画，
    而 decode_led_number 的窄条启发式会把「一根竖笔画」当成合法的 1。
    真实数字块永远不会贴住选区左右边界，被切到的一定有一侧贴边。

    返回 (ok, 说明)。ok=False 表示选区把数字切掉了，这次读数不可信。
    """
    if img is None or img.size == 0:
        return True, '选区为空'
    m = clean_mask(img)
    if not m.any():
        return True, '选区里没有红色笔画'
    cols = np.where(m.any(axis=0))[0]
    width = img.shape[1]
    touch_left = int(cols.min()) <= tol
    touch_right = int(cols.max()) >= width - 1 - tol
    if touch_left and touch_right:
        # 两侧都贴边：选区几乎正好卡住数字宽度，仍然算可疑但不能直接否掉
        return False, '数字宽度顶满选区，两侧都贴边，建议把框放宽一点'
    if touch_left or touch_right:
        side = '左' if touch_left else '右'
        return False, f'数字被选区{side}边界切掉，读到的是残缺笔画'
    return True, '数字完整落在选区内'


def decode_consensus(frames, min_agree=2, bank=None):
    """多帧一致性解码：单帧噪声、动画、遮挡都会让读数跳变。

    只有至少 min_agree 帧解出同一个可信数字时才认；否则一律当未知，
    绝不把「跳来跳去的读数」当成当前币数。
    传入 bank（真机样本模板库）时每帧走「段码+模板」交叉验证。
    返回 (value, info, agree, total)。value 为 None 表示没共识。
    """
    frames = [f for f in frames if f is not None and getattr(f, 'size', 0)]
    if not frames:
        return None, '没有可用画面', 0, 0
    total = len(frames)
    votes = {}
    infos = []
    for f in frames:
        val, info = decode_led_fused(f, bank) if bank else decode_led_best(f)
        if val and '?' not in val:
            votes[val] = votes.get(val, 0) + 1
        else:
            infos.append(info)
    if not votes:
        return None, (infos[0] if infos else '读不出'), 0, total
    best, count = max(votes.items(), key=lambda kv: kv[1])
    if count < min_agree:
        detail = '、'.join(f'{k}×{v}' for k, v in sorted(votes.items()))
        return None, f'{total} 帧读数不一致（{detail}），画面还在变化或被遮挡', count, total
    return best, f'{count}/{total} 帧一致', count, total


def region_fits_one_digit(rect, digit_w, max_digits=2):
    """选区宽度够不够放下机器的**最大**显示位数。

    这是"没币显示 0、投币后变 99"的根源：用户是在显示 0（一位数）的时候
    框的，框刚好罩住那一位；投币后变成两位，第二位落在框外，
    于是 99 读成 9、96 也读成 9，程序就以为"没扣币"。

    单看选区内部发现不了——框内那一位是完整的、没贴边。判据只能是宽度。

    返回 (不够, 说明)
    """
    if not rect or not digit_w:
        return False, ''
    w = int(rect[2])
    need = digit_w * max_digits
    if w < need:
        fits = max(1, int(w // max(1, int(digit_w))))
        return True, (f'选区只够放 {fits} 位，但机器最多显示 {max_digits} 位'
                      f'（框宽 {w}px，至少需要约 {int(need)}px）')
    return False, ''


def expand_to_full_number(frame, rect, probe=60, gap=4, pad=4):
    """把选区自动扩到"完整的一串数字"。

    用户框选时很容易只框住一位：机器没币时显示 0（一位），框刚好；
    投币后显示 99（两位），第二位落在框外，于是读数永远是第一位——
    投币前后都读到 "9"，程序会误判成"没扣币"。

    判据：选区里的笔画**贴住某侧边界**，且**紧邻的框外几像素内还有笔画**
    （说明数字被边界切断）。只满足"框外远处有红"不算，避免把旁边无关的
    红色（按钮、装饰灯）也吸进来。

    返回 (新rect 或 None, 说明)。None 表示看起来本来就是完整的。
    """
    if frame is None or not rect or len(rect) != 4:
        return None, '没有选区'
    x, y, w, h = [int(v) for v in rect]
    H, W = frame.shape[:2]
    if w < 4 or h < 4:
        return None, '选区太小'
    # 1) 选区内部的笔画范围（用原选区判断，才知道是否贴边）
    inner = clean_mask(frame[max(0, y):min(H, y + h), max(0, x):min(W, x + w)])
    if not inner.any():
        return None, '选区里没有红色笔画'
    icols = np.where(inner.any(axis=0))[0]
    ilo, ihi = int(icols.min()), int(icols.max())
    touch_left = ilo <= 2
    touch_right = ihi >= w - 3
    # 选区里只有一位、左右很空 —— 也可能是"只框了一位"（见 region_fits_one_digit）
    digit_w = ihi - ilo + 1
    lonely = (ilo > max(6, w * 0.12)) and (ihi < min(w - 7, w * 0.88)) and w > digit_w * 1.5
    if not (touch_left or touch_right or lonely):
        return None, '选区看起来已经包含完整数字'

    # 2) 向两侧多看一段，找紧邻边界外的笔画（全部换算成屏幕绝对列）
    px0, px1 = max(0, x - probe), min(W, x + w + probe)
    wide = clean_mask(frame[max(0, y):min(H, y + h), px0:px1])
    if not wide.any():
        return None, '选区看起来已经包含完整数字'
    wcols = np.where(wide.any(axis=0))[0] + px0      # 绝对列
    new_lo, new_hi = x, x + w - 1
    grew = False
    if touch_left:
        near = wcols[(wcols >= x - gap) & (wcols < x)]
        if len(near):
            # 不只是补齐那几像素：把左边相邻的**整个数字**都并进来，
            # 否则放宽后仍然缺笔画、读出来是残缺的。
            extra = wcols[wcols < x]
            new_lo = int(extra.min()) - pad
            grew = True
    if touch_right or lonely:
        # lonely：框内那一位没贴边，但左右空着 —— 右边很可能还有下一位
        start_from = (x + w - 1) if touch_right else (x + ihi)
        near = wcols[(wcols > start_from) & (wcols <= start_from + max(gap, digit_w))]
        if len(near):
            extra = sorted(int(c) for c in wcols if c > start_from)
            limit = start_from + int(h * 2.5)    # 最多向右看 2.5 倍字高
            max_gap = max(3, int(h * 0.12))     # 列间隙超过它就算断开
            run_end = start_from
            for c in extra:
                if c > limit or (c - run_end) > max_gap:
                    break
                run_end = c
            new_hi = run_end + pad
            grew = True
    if not grew:
        return None, '选区看起来已经包含完整数字'
    nx = max(0, new_lo)
    nw = min(W, new_hi + 1) - nx
    ny = max(0, y - pad)
    return [nx, ny, max(8, nw), min(H, y + h + pad) - ny], '已自动放宽到完整数字'


def find_led_panel(img, dark_max=65, min_frac=.02):
    """在截图里找「暗色数码管背板」的最大暗色连通域，返回裁剪图或 None。

    用户框选难免框宽一点——框进机柜装饰、旁边的红色灯。直接解码遇到
    无关红色块会整体拒绝（这是刻意的安全设计，宁可不读也不读错）。
    但数字永远显示在近黑的背板上，所以先找到那块背板、裁出来再解码，
    等于自动把选区收紧到数字所在的面板。

    实测（2026-10-05 用户实拍 32 张 0~31）：兜底路径全部解对。
    """
    if img is None or img.size == 0:
        return None
    dark = (img.max(axis=2) < dark_max).astype(np.uint8)
    if dark.mean() < min_frac:
        return None                       # 画面里几乎没有暗区，不像数码管照片
    dark = cv2.morphologyEx(dark, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(dark, 8)
    if n <= 1:
        return None
    i = 1 + int(np.argmax(stats[1:, 4]))
    x, y, w, h = [int(v) for v in stats[i, :4]]
    if w < 24 or h < 16:
        return None                       # 太小的暗块不像是数字背板
    return img[y:y + h, x:x + w]


def deskew_led(img):
    """斜视角矫正：数码管整体歪的时候先转正再解码。

    换站位/视角漂移后画面里的数字会整体倾斜（实测用户实拍 0-2/1-2 斜
    11° 时段码采样窗全部错位）。用笔画的最小外接矩形估计倾角，
    超过 4° 就把画面转回来。

    返回 (矫正后图, 角度)。角度为 0 表示不需要矫正（返回原图）。
    """
    if img is None or img.size == 0:
        return img, 0.0
    m = clean_mask(img)
    if m.sum() < 12:
        return img, 0.0
    pts = cv2.findNonZero(m.astype(np.uint8))
    if pts is None:
        return img, 0.0
    (cx, cy), _size, ang = cv2.minAreaRect(pts)
    if ang > 45:
        ang -= 90
    if ang < -45:
        ang += 90
    if abs(ang) < 4:
        return img, 0.0
    M = cv2.getRotationMatrix2D((float(cx), float(cy)), float(ang), 1.0)
    fixed = cv2.warpAffine(img, M, (img.shape[1], img.shape[0]),
                           flags=cv2.INTER_LINEAR, borderValue=(20, 16, 14))
    return fixed, float(ang)


def decode_led_best(img, **kw):
    """运行时统一入口：依次尝试 直接 → 转正 → 背板 → 背板+转正。

    每一步都只在「读不出」时才继续尝试；任何一步读出了就用它。
    全部失败时返回第一步的失败原因——宁可读不出，绝不把读不出当 0。
    """
    val, info = decode_led_number(img, **kw)
    if val is not None and '?' not in str(val):
        return val, info
    first_val, first_info = val, info
    fixed, ang = deskew_led(img)
    if fixed is not img:
        fval, finfo = decode_led_number(fixed, **kw)
        if fval is not None and '?' not in str(fval):
            return fval, f'{finfo}（画面斜 {ang:.0f}°，已转正）'
    panel = find_led_panel(img)
    if panel is not None and panel.shape != img.shape:
        pval, pinfo = decode_led_number(panel, **kw)
        if pval is not None and '?' not in str(pval):
            return pval, f'{pinfo}（已自动收紧到数码管背板）'
        fixedp, pang = deskew_led(panel)
        if fixedp is not panel:
            qval, qinfo = decode_led_number(fixedp, **kw)
            if qval is not None and '?' not in str(qval):
                return qval, f'{qinfo}（背板+转正 {pang:.0f}°）'
    return first_val, first_info


# ---------------------------------------------------------------- 模板库
# 「自己训练自己」的第二条腿：用带真值的实拍样本建每个数字的形状模板，
# 与段码互相交叉验证。零依赖（不引入 TensorFlow），样本越多越准，
# 而且可以放心共享——别人的机器各放一个子目录，匹配不上只会被忽略。

TEMPLATE_SIZE = (48, 64)          # 归一化模板尺寸 (宽, 高)


def _norm_mask(d):
    """把一个数字掩码归一化成固定大小的浮点图（0~1），抗缩放抗字高差。"""
    if d.size == 0:
        return None
    return cv2.resize(d.astype(np.uint8), TEMPLATE_SIZE,
                      interpolation=cv2.INTER_AREA).astype(np.float32)


def digit_masks(img):
    """把一张含 1~N 位数字的图切成每位数字的归一化掩码（左→右）。"""
    got = _digit_parts(img)
    if got is None:
        return []
    return [nm for nm in (_norm_mask(d) for _, _, d in got[1]) if nm is not None]


def build_template_bank(samples):
    """用带真值的样本建模板库：{数字: [归一化掩码, ...]}。

    只有「切出来的位数 == 真值位数」的样本才收录——位数对不上说明
    图里混进了装饰残片或被截断，那种图当模板会把别的数字带偏。
    samples 是 (真值, 图) 对；真值可以是多位（按位对齐收录）。
    """
    bank = {}
    for truth, img in samples:
        masks = digit_masks(img)
        if len(masks) != len(str(truth)):
            continue
        for ch, mask in zip(str(truth), masks):
            bank.setdefault(ch, []).append(mask)
    return bank


def match_digit(mask, bank):
    """单个数字掩码在模板库里的最佳匹配。

    返回 (数字, 最佳距离, 次优距离)。距离 = 归一化掩码的平均差（0~1）。
    """
    if not bank or mask is None:
        return None, 1.0, 1.0
    scores = []
    for ch, masks in bank.items():
        d = min(float(np.mean(np.abs(m - mask))) for m in masks)
        scores.append((d, ch))
    if not scores:
        return None, 1.0, 1.0
    scores.sort()
    d1, c1 = scores[0]
    d2 = scores[1][0] if len(scores) > 1 else 1.0
    return c1, d1, d2


def decode_by_template(img, bank, max_dist=.16, margin=.05):
    """用模板库读整串数字，返回 (值 或 None, 说明)。

    判据（都满足才算认得）：
    ① 每一位都能切出来且位数合理；
    ② 每位的最佳距离 ≤ max_dist（形状确实像）；
    ③ 最佳比次优至少好 margin（不是"两个都像"）。
    任何一条不满足都返回 None——模板库只做它有把握的题。
    """
    if not bank:
        return None, '没有模板库'
    masks = digit_masks(img)
    if not masks or len(masks) > 4:
        return None, '切不出数字位'
    out, dists = [], []
    for mask in masks:
        ch, d1, d2 = match_digit(mask, bank)
        if ch is None:
            return None, '模板库为空'
        if d1 > max_dist:
            return None, f'与模板最近差 {d1:.2f}，不像任何已知数字'
        if d2 - d1 < margin:
            return None, f'「{ch}」与次优只差 {d2 - d1:.2f}，区分度不足'
        out.append(ch)
        dists.append(d1)
    return ''.join(out), '模板=' + ','.join(f'{c}:{d:.2f}' for c, d in zip(out, dists))


def decode_led_fused(img, bank=None, veto_dist=.05, veto_margin=.08, **kw):
    """段码 + 模板库交叉验证（自训练的核心融合点）。

    - 两者一致 → 采纳，置信度比单看段码高一截；
    - 段码读不出、模板认得 → 采纳模板（视角歪、泛光异常时救命）；
    - 都能读但**不一致** → 只有模板「非常有把握」时才判未知。

    为什么否决要另设更严的门槛（veto_dist/veto_margin）：七段码里
    0 和 8 只差一段、3 和 8 只差两段，全局形状距离本来就分不干净。
    实测同机器真实模板 d1 中位数 0.023、p90 0.048，而**别的机器的字体**
    能以 d1=0.08、区分度 0.03 的"很有把握"姿态把 8 读成 0——要是让它
    一票否决段码，用户就会被一堆假「读不出」淹没。所以：
      救援（段码读不出时用模板）→ 松门槛，尽量救回来；
      否决（推翻段码）        → 严门槛，只有铁证才动手。

    门槛是量出来的，不是拍的（104 张真机样本留一法）：
      松门槛  读对 89% / 读不出 11% / **读错 0%**
      严门槛  读对 54% / 读不出 46% / **读错 0%**
    两套门槛都没出现错读，所以救援敢用松的；严的那套只用来防
    「别人的机器样本混进来」这种跨字体污染。
    """
    val, info = decode_led_best(img, **kw)
    if not bank:
        return val, info
    fixed, _ang = deskew_led(img)
    tval, tinfo = decode_by_template(img, bank)
    if tval is None and fixed is not img:
        tval, tinfo = decode_by_template(fixed, bank)
    if tval is None:
        return val, info
    if val is None or not str(val) or '?' in str(val):
        return tval, f'{tinfo}（段码读不出，采纳模板）'
    if str(val) == tval:
        return val, f'{info}；{tinfo}（段码与模板一致）'
    # 段码给出了完整读数：模板必须拿出铁证才配推翻它
    vval = decode_by_template(img, bank, max_dist=veto_dist, margin=veto_margin)[0]
    if vval is None and fixed is not img:
        vval = decode_by_template(fixed, bank, max_dist=veto_dist, margin=veto_margin)[0]
    if vval is None:
        return val, (f'{info}（模板另有弱匹配「{tval}」，把握不足，不推翻段码）')
    return '?', f'段码读「{val}」、模板读「{vval}」不一致，判未知'

def _credit_segments(img):
    """Read only intact digits in the full Credit ROI, without rescue cropping."""
    if img is None or not getattr(img, 'size', 0):
        return None, '没有可用数字画面'
    if not digit_fit(img)[0]:
        return None, '数字贴边或选区截断'
    mask = clean_mask(img)
    rows = np.where(mask.any(axis=1))[0]
    if not len(rows):
        return None, '没有有效数字笔画'
    if rows[0] <= 2 or rows[-1] >= img.shape[0] - 3:
        return None, '数字上下贴边，未通过完整性检查'
    height = int(rows[-1] - rows[0] + 1)
    if img.shape[1] < height * 1.25:
        return None, '选区偏窄，不能排除漏掉十位数字'
    value, info = decode_led_number(img)
    if not value or not str(value).isascii() or not str(value).isdigit() or len(str(value)) > 2:
        return None, '未得到完整直接段码 Credit 读数'
    return value, info


def decode_credit_candidate(img, bank, zero_reference=None):
    """Low values require strict evidence; positive credit only prolongs draws.

    An explicitly recorded zero can calibrate that glyph, but never replace
    segment decoding. Require the same full ROI, aligned strokes, a independently
    recognized reference, and strict template separation from every other digit.
    """
    value, info = _credit_segments(img)
    if value is None:
        return None, info
    if not bank or not all(str(n) in bank for n in range(10)):
        return None, '模板覆盖不足，不能可靠区分低币与其他数字'
    low = int(value) in (0, 1, 2)
    template, template_info = decode_by_template(img, bank, max_dist=.05 if low else .16,
                                                margin=.08 if low else .05)
    if template != value and value == '0' and zero_reference is not None:
        ref_value, _ = _credit_segments(zero_reference)
        ref_masks = digit_masks(zero_reference)
        ref_digit, distance, runner_up = match_digit(ref_masks[0], bank) if len(ref_masks) == 1 else (None, 1., 1.)
        aligned = region_diff(img, zero_reference)
        if ref_value == ref_digit == '0' and distance <= .25 and runner_up - distance >= .05 \
                and aligned is not None and aligned <= 2.0:
            local_bank = {ch:list(masks) for ch,masks in bank.items()}
            local_bank['0'] += digit_masks(zero_reference)
            template, template_info = decode_by_template(img, local_bank, max_dist=.05, margin=.08)
            if template == value:
                return 0, '完整直接段码 0；录入零图对齐；高置信度校准模板一致'
    if template != value:
        return None, '段码与模板未共同确认；' + template_info
    return int(value), ('完整数字；段码与高置信度模板一致' if low else
                        '完整余币；段码与模板一致（仅用于继续抽奖）')


ENOUGH_CREDIT = 'enough'


def decode_credit_condition(img, bank, zero_reference=None, bet_count=3):
    """Return a trusted low value, enough-for-one-bet, or unknown.

    Low credit keeps the exact decoder's strict gates. For sufficient credit,
    templates only need to separate the sufficient and insufficient classes;
    ambiguity between, for example, 5 and 9 cannot justify another refill.
    """
    value, info = _credit_segments(img)
    if value is None:
        return None, info
    if int(value) < 3:
        credit, info = decode_credit_candidate(img, bank, zero_reference)
        if credit is None or credit < bet_count:
            return credit, info
        return ENOUGH_CREDIT, '已确认足够一次下注；' + info
    if not bank or not all(str(n) in bank for n in range(10)):
        return None, '模板覆盖不足，不能可靠区分不足与足够一注'
    masks = digit_masks(img)
    if len(masks) != len(value):
        return None, '数字位数与完整段码不一致'

    def separated(mask, sufficient, insufficient):
        scores = {digit: min(float(np.mean(np.abs(t - mask))) for t in templates)
                  for digit, templates in bank.items()}
        best = min(scores[digit] for digit in sufficient)
        alternative = min(scores[digit] for digit in insufficient)
        return best <= .16 and alternative - best >= .05

    # An intact nonzero tens digit already proves enough for a 1/2/3 coin bet.
    if len(value) == 2 and value[0] != '0' and separated(
            masks[0], '123456789', '0'):
        return ENOUGH_CREDIT, '完整两位数字；段码与模板共同排除低币，足够一次下注'
    if int(value[-1]) >= bet_count and separated(
            masks[-1], ''.join(str(n) for n in range(bet_count, 10)),
            ''.join(str(n) for n in range(bet_count))):
        return ENOUGH_CREDIT, '完整数字；段码与模板共同确认足够一次下注，不要求具体余币值'
    return None, '不能可靠区分不足与足够一注，按未知继续流程'


def decode_refill_candidate(img, bank):
    """Compatibility helper: only trusted 0/1/2 may trigger an early refill."""
    value, info = decode_credit_candidate(img, bank)
    if value not in (0, 1, 2):
        return None, '未确认低币；' + info
    return value, info


def normalized(img):
    m=clean_mask(img)
    if not m.any(): return None
    yy,xx=np.where(m)
    return cv2.resize(m[yy.min():yy.max()+1,xx.min():xx.max()+1],(48,64),
                      interpolation=cv2.INTER_NEAREST).astype(bool)

def cluster_diff(a,b):
    aa,bb=normalized(a),normalized(b)
    if aa is None or bb is None: return 255.
    return float(np.mean(aa!=bb)*255)

def region_diff(a,b):
    """Aligned red segment patterns: ignore lighting, preserve digit positions and count."""
    if a is None or b is None or a.shape != b.shape: return None
    aa,bb=clean_mask(a),clean_mask(b)
    if aa.sum()<12 or bb.sum()<12: return None
    # Tolerate one pixel capture jitter without erasing changed segments.
    kernel=np.ones((3,3),np.uint8)
    ad=cv2.dilate(aa,kernel); bd=cv2.dilate(bb,kernel)
    mismatch=((aa>0)&(bd==0))|((bb>0)&(ad==0))
    return float(255*mismatch.sum()/max(1,((aa>0)|(bb>0)).sum()))

def _display_boxes(mask):
    connected=cv2.morphologyEx(mask.astype(np.uint8),cv2.MORPH_CLOSE,np.ones((3,3),np.uint8))
    n,_,stats,_=cv2.connectedComponentsWithStats(connected,8)
    boxes=[list(map(int,stats[i,:4])) for i in range(1,n) if stats[i,4]>=8]
    changed=True
    while changed:
        changed=False
        for i,(x,y,w,h) in enumerate(boxes):
            for j in range(i+1,len(boxes)):
                xx,yy,ww,hh=boxes[j]
                overlap=min(y+h,yy+hh)-max(y,yy)
                gap=max(0,x-(xx+ww),xx-(x+w))
                if overlap>=.5*min(h,hh) and gap<=.8*max(h,hh):
                    x0,y0=min(x,xx),min(y,yy)
                    boxes[i]=[x0,y0,max(x+w,xx+ww)-x0,max(y+h,yy+hh)-y0]
                    boxes.pop(j); changed=True; break
            if changed: break
    return boxes

def find_digit_regions(frame):
    """Find bright LED groups, then decode their full-color padded crops.

    Detection must not use the loose decoder mask across a whole cabinet:
    dark red background connects otherwise separate displays into one huge blob.
    """
    if frame is None or frame.size == 0:
        return []
    found=[]
    for minimum,difference in ((100,40),(140,60)):
        boxes=_display_boxes(led_mask(frame,minimum,difference))
        for x,y,w,h in boxes:
            if not (12 <= h <= 160 and 3 <= w <= 600):
                continue
            pad=max(4,round(h*.12))
            x0,y0=max(0,x-pad),max(0,y-pad)
            x1,y1=min(frame.shape[1],x+w+pad),min(frame.shape[0],y+h+pad)
            crop=frame[y0:y1,x0:x1]
            # Real machine LED displays have a dark backing, unlike red text in the UI.
            if np.mean(np.max(crop,axis=2)<65) < .25:
                continue
            val,info=decode_led_best(crop)
            if not val or not val.isdigit():
                continue
            rect=(x0,y0,x1-x0,y1-y0)
            if any(abs(x0-f[0])<pad*2 and abs(y0-f[1])<pad*2
                   and abs(rect[2]-f[2])<pad*3 and abs(rect[3]-f[3])<pad*3 for f in found):
                continue
            found.append((*rect,val,info))
    return sorted(found,key=lambda f:(f[1],f[0]))
