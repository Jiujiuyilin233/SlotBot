"""原版三标签页界面：左侧导航 + 卡片式排版 + 页内滚动 + 滚轮支持。

所有控件名字（nb / pt_vars / tpl_status / vars / btn_start / btn_stop /
lbl_state / lbl_stats / txt / v_mode / v_hold_tab / v_multi …）是回归测试
和功能回调的契约，重排时一律保留。

页签条改成左侧导航栏：Notebook 不拆，只把三个页签头 hidden 掉，
导航点击仍走 nb.select() 切页——hidden 状态下 tabs() 仍返回 3 项、
tab(t,'text') 仍可读（已单独验证），回归测试的契约不受影响。
"""
import tkinter as tk
from tkinter import ttk

# 侧栏导航三态配色（浅色主题，与 slotbot 的主题变量配合）
NAV_SELECTED_BG = "#e8effc"   # 选中项底色
NAV_HOVER_BG = "#eef2fb"      # 悬停底色


class WrapRow(tk.Frame):
    """水平排布的子控件，宽度不够时自动换行（不裁切）。

    子控件只管创建（master 指向本类），由 _reflow 统一用 grid 布局；
    外部不要再自己 pack/grid 它们。窄内容区下按钮不再被右边切掉。
    """

    def __init__(self, master, padx=6, **kw):
        super().__init__(master, **kw)
        self._pad = padx
        self._busy = False
        self.bind("<Configure>", lambda _e: self._reflow())

    def _reflow(self):
        if self._busy:
            return
        kids = list(self.winfo_children())
        if not kids:
            return
        try:
            self._busy = True
            self.update_idletasks()
            avail = max(40, self.winfo_width())
            rows = [[]]
            x = 0
            for c in kids:
                w = c.winfo_reqwidth()
                if x and x + w > avail:
                    rows.append([])
                    x = 0
                rows[-1].append(c)
                x += w + self._pad
            for c in kids:
                c.grid_forget()
            for r, items in enumerate(rows):
                for col, c in enumerate(items):
                    c.grid(row=r, column=col, padx=(0, self._pad), sticky="w")
        finally:
            self._busy = False


class OriginalUI:
    def build_ui(self):
        self._scroll_canvases = []
        self._wrap_rows = []
        # 滚轮必须 bind_all：Tk 把滚轮事件发给光标下的子控件，子控件的
        # bindtags 里没有 canvas，只在 canvas 上绑等于收不到（实踩过的坑）。
        self.bind_all("<MouseWheel>", self._wheel, add="+")

        self._nav_items = []
        self._nav_index = 0
        self._adv_open = False  # 参数页「高级参数」折叠区默认收起

        # ============ 主骨架：左侧导航 + 右侧内容 ============
        side = tk.Frame(self, width=172, bg=self.core.CARD, highlightthickness=0)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        # 侧栏与内容区之间 1px 分隔线
        tk.Frame(self, width=1, bg=self.core.BORDER).pack(side="left", fill="y")
        content = tk.Frame(self, bg=self.core.BG)
        content.pack(side="left", fill="both", expand=True)
        self._build_sidebar(side)

        nb = ttk.Notebook(content)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=10, pady=(6, 6))

        # ============ 1. 定位设置 ============
        page1 = self._scroll_page(nb, "1. 定位设置")
        self._page_header(
            page1, "定位设置",
            "先选定位方式，再给每个目标取点或录模板；Credit 数字区告诉程序什么时候「没币」。")

        tools = ttk.Frame(page1)
        tools.pack(fill="x", padx=14, pady=(10, 0))
        # 按钮排一行、说明另起一行：说明若和按钮同排，pack 的空腔会被
        # 按钮挤到只剩几十像素，文字直接被裁没（实测踩过）。
        btnrow = WrapRow(tools, padx=6, bg=self.core.BG)
        btnrow.pack(fill="x")
        self._wrap_rows.append(btnrow)
        for label, command in (("样本与采集", self.open_sample_center),
                               ("运行前自检", self.run_preflight),
                               ("坐标诊断", self.show_coordinate_report),
                               # 从运行页搬过来的两个试手按钮：跟定位是同一件事（先确认环境）
                               ("激活游戏窗口", self.activate_game),
                               ("测试按住 Tab（6 秒）", self.test_tab)):
            ttk.Button(btnrow, text=label, style="Ghost.TButton",
                       command=command)
        # 提示以动词短语开头：Tk 对中文只在空格处折行，短引号开头会让
        # 首行只挂「测试按住」四个字，很难看
        self._wrap(tools, "先在游戏里站好再点「测试按住 Tab」，看光标会不会出来；"
                          "游戏要全屏/窗口化可见，别被本窗口挡住。").pack(
            anchor="w", fill="x", pady=(4, 0))

        body = self._card(page1, "定位方式")
        self.v_mode = tk.StringVar(value=self.cfg.get("locate_mode", "coord"))
        radios = ttk.Frame(body, style="Card.TFrame")
        radios.pack(anchor="w")
        ttk.Radiobutton(radios, text="固定坐标（推荐，快而稳）", value="coord",
                        variable=self.v_mode, command=self.on_mode_change).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(radios, text="图像识别（模板匹配）", value="image",
                        variable=self.v_mode, command=self.on_mode_change).pack(side="left")
        self.lbl_tip = self._wrap(body, "", "CardMuted.TLabel")
        self.lbl_tip.pack(anchor="w", pady=(6, 0))

        body = self._card(
            page1, "目标位置",
            "建议顺序：① 硬币堆 ② MaxBet ③ 拉杆 ④ 按钮1/2/3（Bet 按钮只在押 1/2 枚时才需要）。"
            "取完点可以点「移动测试」验证鼠标会不会停到目标上。")
        grid = ttk.Frame(body, style="Card.TFrame")
        grid.pack(fill="x")
        for c, txt in enumerate(("目标", "坐标 X", "坐标 Y", "状态", "操作")):
            ttk.Label(grid, text=txt, style="Card.TLabel",
                      font=("Microsoft YaHei UI", 10, "bold")).grid(
                row=0, column=c, sticky="w", padx=5, pady=2)
        self.tpl_status = {}
        self.pt_vars = {}
        self.tpl_btns = {}
        for i, (key, label) in enumerate(self.core.TEMPLATES, start=1):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(
                row=i, column=0, sticky="w", padx=5, pady=3)
            pt = (self.cfg.get("points") or {}).get(key, ["", ""])
            vx = tk.StringVar(value="" if pt[0] in (None, "") else str(pt[0]))
            vy = tk.StringVar(value="" if pt[1] in (None, "") else str(pt[1]))
            self.pt_vars[key] = (vx, vy)
            ttk.Entry(grid, textvariable=vx, width=7).grid(row=i, column=1, padx=4, pady=3)
            ttk.Entry(grid, textvariable=vy, width=7).grid(row=i, column=2, padx=4, pady=3)
            # 状态列只画圆点：绿=已设定/已录模板，琥珀=还没设好（refresh_tpl_status 上色）
            st = ttk.Label(grid, text="●", style="CardMuted.TLabel")
            st.grid(row=i, column=3, sticky="w", padx=5)
            self.tpl_status[key] = st
            box = ttk.Frame(grid, style="Card.TFrame")
            box.grid(row=i, column=4, sticky="w", padx=5)
            # 按钮海收敛：每行只保留当前模式用得上的按钮（on_mode_change 里切显隐）。
            # 不给固定 width，靠主题的 width=-2 按内容收缩，窄内容区才放得下。
            btns = {}
            for c, (name, text, command) in enumerate((
                    ("pick", "取点", lambda k=key: self.pick_point(k)),
                    ("move", "移动测试", lambda k=key: self.move_test(k)),
                    ("record", "录模板", lambda k=key: self.record(k)),
                    ("test", "识图测试", lambda k=key: self.test(k)),
                    ("preview", "预览", lambda k=key: self.preview(k)))):
                b = ttk.Button(box, text=text, command=command)
                b.grid(row=0, column=c, padx=2)
                btns[name] = b
            self.btn_pick = btns["pick"]
            self.btn_move = btns["move"]
            self.btn_rec = btns["record"]
            self.btn_test = btns["test"]
            self.tpl_btns[key] = btns
        self.hint = self._wrap(body, "", "CardMuted.TLabel")
        self.hint.pack(anchor="w", pady=(8, 0))

        body = self._card(
            page1, "Credit 数字区（清币判断）",
            "框住机器上红色的 Credit 数码管（就是那个显示 0 的红色数字），"
            "可靠读到低币（MaxBet 时为 0、1、2）会提前结束本轮并投币。"
            "低币须通过完整性、模板及连续三帧复核；看不清时继续设定的抽奖/空转流程，每局重新尝试识别。")
        crow = WrapRow(body, padx=3, bg=self.core.CARD)
        crow.pack(anchor="w", fill="x")
        self._wrap_rows.append(crow)
        ttk.Button(crow, text="框选 Credit 区域", command=self.pick_credit_rect)
        ttk.Button(crow, text="自动找红色数字区", command=self.auto_find_credit)
        ttk.Button(crow, text="读取测试", command=self.test_credit_rect)
        ttk.Button(crow, text="实时监测窗口", command=self.open_credit_monitor)
        ttk.Button(crow, text="清除", style="Ghost.TButton", command=self.clear_credit_rect)
        self.lbl_credit = ttk.Label(crow, text="", style="Card.TLabel", foreground="#bb8800")
        self.update_credit_label()

        live = ttk.Frame(body, style="Card.TFrame")
        live.pack(anchor="w", pady=(8, 0))
        ttk.Label(live, text="当前实时读数：", style="Card.TLabel",
                  font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.lbl_credit_live = ttk.Label(live, text="—", font=("Consolas", 16, "bold"),
                                         style="Card.TLabel", foreground="#888")
        self.lbl_credit_live.pack(side="left", padx=6)
        self.lbl_credit_live_info = ttk.Label(live, text="", style="CardMuted.TLabel")
        self.lbl_credit_live_info.pack(side="left", padx=6)

        # 两步说明先单独成行（长文案窄内容区放不下），按钮再排一行
        self._wrap(body, "① 先框选上面的 Credit 区域    ② 在【机器显示 0 / 没币】时点：",
                   "CardMuted.TLabel").pack(anchor="w", pady=(8, 0))
        refrow = ttk.Frame(body, style="Card.TFrame")
        refrow.pack(anchor="w", pady=(4, 0))
        ttk.Button(refrow, text="记为「没币」参考", command=self.capture_credit_ref).pack(side="left", padx=4)
        ttk.Button(refrow, text="清除参考", style="Ghost.TButton", command=self.clear_credit_ref).pack(side="left", padx=4)
        self.lbl_credit_ref = ttk.Label(refrow, text="", style="Card.TLabel", foreground="#bb8800")
        self.lbl_credit_ref.pack(side="left", padx=8)
        self.update_credit_ref_label()

        body = self._card(
            page1, "备用数字区域",
            "辅助识别；读不清仍按次数继续。数字区域噪声大就把「监测区域差异阈值」调大。")
        wrow = ttk.Frame(body, style="Card.TFrame")
        wrow.pack(anchor="w")
        ttk.Button(wrow, text="框选监测区域", command=self.pick_watch_rect).pack(side="left", padx=(0, 3))
        ttk.Button(wrow, text="测试噪声（1.5 秒）", command=self.test_watch_rect).pack(side="left", padx=3)
        ttk.Button(wrow, text="清除", style="Ghost.TButton", command=self.clear_watch_rect).pack(side="left", padx=3)
        self.lbl_watch = ttk.Label(wrow, text="", style="Card.TLabel", foreground="#bb8800")
        self.lbl_watch.pack(side="left", padx=8)
        self.update_watch_label()
        self.on_mode_change()

        # ============ 2. 参数设置 ============
        page2 = self._scroll_page(nb, "2. 参数设置")
        self._page_header(
            page2, "参数设置",
            "核心参数决定「跑多少、跑多久」；高级参数保持默认即可，行为不对时再微调。")

        def add_row(parent, r, key, label, tip=""):
            ttk.Label(parent, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.vars[key] = var
            e = ttk.Entry(parent, textvariable=var, width=10)
            e.grid(row=r, column=1, sticky="w", padx=6)
            if tip:
                ttk.Label(parent, text=tip, style="CardMuted.TLabel").grid(
                    row=r, column=2, sticky="w", padx=6)
            return var

        def add_check(parent, r, label, tip, variable):
            # 开关行也走「标签/控件/说明」三列网格：checkbox 不带文字，
            # 长文案放说明列——带长文字的 checkbox 跨列会把标签列撑宽，
            # 两张卡的输入框就不再对齐（grid 实测会摊派溢出宽度）。
            ttk.Label(parent, text=label, style="Card.TLabel").grid(
                row=r, column=0, sticky="w", pady=3)
            ttk.Checkbutton(parent, variable=variable,
                            style="Card.TCheckbutton").grid(
                row=r, column=1, sticky="w", padx=6, pady=3)
            if tip:
                ttk.Label(parent, text=tip, style="CardMuted.TLabel").grid(
                    row=r, column=2, sticky="w", padx=6)

        # 保存配置放在核心参数卡的标题行右侧，不用滚到长页底部去找
        save_btn = ttk.Button(page2, text="保存配置", style="AccentSmall.TButton",
                              command=self.collect_and_save)
        body = self._card(page2, "核心参数", action=save_btn)
        body.columnconfigure(0, minsize=150)  # 标签列统一宽度，各卡的输入框对齐
        r = 0
        # 提示列宽度有限（约 20 个汉字），超长的说明宁可精简也不要被裁掉半句
        add_row(body, r, "coin_count", "每轮投币数", "每轮开始时投进机器的币数"); r += 1
        add_row(body, r, "draw_count", "正常抽奖次数", "投完币后正常抽奖的次数（每次吃 3 币）"); r += 1
        add_row(body, r, "burn_count", "空转清币次数", "读不清时连续空转；读到仍有币会继续抽"); r += 1
        add_row(body, r, "bet_count", "下注枚数", "3 = 点 MaxBet；1 或 2 先点 Bet 按钮"); r += 1
        add_row(body, r, "rounds", "运行轮数", "0 = 不限，一直循环"); r += 1
        add_row(body, r, "max_minutes", "最长运行分钟", "0 = 不限；到点自动收工（比如填 30）")

        # 高级参数默认收起：首屏只留核心参数，密度立起来
        adv_hint = ttk.Label(page2, text="点标题展开 / 收起", style="CardMuted.TLabel")
        adv_body = self._card(page2, "高级参数 ▸", action=adv_hint)
        adv_title = adv_body._title_label
        adv_title.configure(cursor="hand2")
        adv_title.bind("<Button-1>", lambda e: self._toggle_advanced())
        adv = ttk.Frame(adv_body, style="Card.TFrame")
        adv.columnconfigure(0, minsize=150)  # 与核心参数卡同宽的标签列
        adv.grid(row=0, column=0, sticky="w")
        adv.grid_remove()  # 默认收起：grid_remove 记住布局参数，展开时原位恢复
        self._adv_frame = adv
        self._adv_title = adv_title

        r = 0

        def section(text):
            nonlocal r
            ttk.Label(adv, text=text, style="CardMuted.TLabel",
                      font=("Microsoft YaHei UI", 9, "bold")).grid(
                row=r, column=0, columnspan=3, sticky="w", pady=(10, 0))
            r += 1

        # 标签列要跟核心参数卡对齐（minsize 150），标签一律压在 6 个汉字内，
        # 细节语义放进说明列，单位也挪进说明列
        section("时间（毫秒）")
        add_row(adv, r, "coin_delay", "投币间隔", "两枚硬币之间的间隔"); r += 1
        add_row(adv, r, "last_coin_wait", "投完币后等待", "最后一枚投完后的额外等待"); r += 1
        add_row(adv, r, "step_delay", "各步骤间隔", "点按钮、拉杆等动作之间的间隔"); r += 1
        add_row(adv, r, "move_delay", "到目标后停顿", "鼠标到位后的停顿，太快会点空"); r += 1
        add_row(adv, r, "pull_px", "拉杆下拉距离", "往下拖多少像素，不到位会空拉"); r += 1
        section("识别")
        add_row(adv, r, "threshold", "相似度阈值", "找不到目标就调低，点错位置就调高"); r += 1
        add_row(adv, r, "retries", "找不到时重试", "目标一时找不到时重试几次再放弃"); r += 1
        add_row(adv, r, "region_diff_threshold", "区域差异阈值", "数字区域噪声大就调大"); r += 1
        add_row(adv, r, "region_wait", "下注后等待", "等多久看数字（毫秒）"); r += 1
        self.v_multi = tk.BooleanVar(value=bool(self.cfg["multi_scale"]))
        add_check(adv, r, "多尺度匹配", "视角远近变化时更稳，稍慢", self.v_multi)
        r += 1
        section("窗口与按键")
        self.v_hold_tab = tk.BooleanVar(value=bool(self.cfg["hold_tab"]))
        add_check(adv, r, "按住 Tab", "运行期间按住 Tab 呼出光标", self.v_hold_tab)
        r += 1
        add_row(adv, r, "tab_repeat", "Tab 续按间隔", "补按间隔（毫秒）；没生效就调小"); r += 1
        add_row(adv, r, "game_window", "窗口关键字", "游戏窗口标题里的字，用于自动激活")
        r += 1
        ttk.Label(adv, text="数字只辅助提前补币；读不清继续抽奖和空转，每局重新尝试识别",
                  style="CardMuted.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # 次要入口降级成 Ghost 链接，不再跟保存配置抢位置
        links = ttk.Frame(page2)
        links.pack(pady=10)
        ttk.Button(links, text="定时 / 保活 / 高级参数", style="Ghost.TButton",
                   command=self.open_controls).pack(side="left", padx=4)
        ttk.Button(links, text="配置方案", style="Ghost.TButton",
                   command=self.open_profiles).pack(side="left", padx=4)

        # ============ 3. 运行 ============
        page3 = self._scroll_page(nb, "3. 运行")
        self._page_header(
            page3, "运行",
            "开始前先在定位页点「运行前自检」；运行中可点停止、按急停键或把鼠标甩到屏幕左上角。")

        body = self._card(page3, "运行控制")
        # 状态横幅：本页最重要的信息，给足视觉重量（左色条+圆点+大字）
        banner = tk.Frame(body, bg=self.core.CARD, height=48,
                          highlightthickness=1, highlightbackground=self.core.BORDER)
        banner.pack(fill="x")
        banner.pack_propagate(False)
        self.status_strip = tk.Frame(banner, width=4, bg=self.core.STATE_COLORS["idle"])
        self.status_strip.pack(side="left", fill="y")
        self.status_dot = tk.Canvas(banner, width=18, height=18, bg=self.core.CARD,
                                    highlightthickness=0)
        self.status_dot.create_oval(3, 3, 13, 13, fill=self.core.STATE_COLORS["idle"],
                                    outline="", tags="dot")
        self.status_dot.pack(side="left", padx=(12, 8))
        self.lbl_state = ttk.Label(banner, text="空闲", style="Card.TLabel",
                                   font=("Microsoft YaHei UI", 14, "bold"))
        self.lbl_state.pack(side="left")

        bar = WrapRow(body, padx=6, bg=self.core.CARD)
        bar.pack(fill="x", pady=(10, 0))
        self._wrap_rows.append(bar)
        self.btn_start = ttk.Button(bar, text="开始运行", style="Accent.TButton", command=self.start)
        self.btn_stop = ttk.Button(bar, text="停止 (F12)", style="Danger.TButton", command=self.request_stop)
        self.btn_awake = ttk.Button(bar, text="常亮 关 (F10)", style="Ghost.TButton",
                                    command=self.toggle_keep_awake)
        ttk.Button(bar, text="重新加载模板", style="Ghost.TButton",
                   command=self.reload_tpl)
        self._refresh_hotkey_labels()

        self.lbl_stats = ttk.Label(body, text="还没有运行记录", style="CardMuted.TLabel")
        self.lbl_stats.pack(anchor="w", pady=(10, 0))
        self.lbl_ready = self.lbl_state

        # 日志工具条塞进卡片标题行右侧：自动滚动 / 清空 / 导出
        logtools = ttk.Frame(page3)
        self.btn_autoscroll = ttk.Button(logtools, text="自动滚动 ✓", style="GhostSmall.TButton",
                                         command=self.toggle_autoscroll)
        self.btn_autoscroll.pack(side="left", padx=2)
        ttk.Button(logtools, text="清空", style="GhostSmall.TButton",
                   command=self.clear_log).pack(side="left", padx=2)
        ttk.Button(logtools, text="导出", style="GhostSmall.TButton",
                   command=self.export_log).pack(side="left", padx=2)
        logcard = self._card(page3, "运行日志", action=logtools)
        self.txt = tk.Text(logcard, height=16, font=("Consolas", 9),
                           bg="#ffffff", fg="#1f2733", relief="flat",
                           highlightthickness=1, highlightbackground=self.core.BORDER)
        # 日志分层：时间戳/[级别] 前缀按级别上色（pump_log 插入时打 tag）
        self.txt.tag_configure("error", foreground="#d93025")
        self.txt.tag_configure("warn", foreground="#b45309")
        textbar = ttk.Scrollbar(logcard, command=self.txt.yview)
        textbar.pack(side="right", fill="y")
        self.txt.configure(yscrollcommand=textbar.set)
        self.txt.pack(fill="both", expand=True)

        # 页签头已在主题里摘掉渲染（style.layout TNotebook.Tab = []）：
        # hidden 状态会把选中页的内容一起藏掉，不能用；页签对象与
        # tab(t,'text') 契约原样保留。
        self._paint_nav()

        # 首次布局后强制换行重排：确保按钮在窄内容区下换行而非被右边切掉。
        self.after(80, self._reflow_wrap_rows)

    def _reflow_wrap_rows(self):
        for row in getattr(self, "_wrap_rows", ()):
            try:
                row._reflow()
            except Exception:
                pass

    def _refresh_hotkey_labels(self):
        """把开始/停止/常亮按钮上的热键提示同步成当前配置。"""
        def names(fn):
            try:
                return ' / '.join(name for _vk, name in fn(self.cfg))
            except Exception:
                return ''
        try:
            start = names(self.core.start_key_entries)
            stop = names(self.core.stop_key_entries)
        except Exception:
            return
        for attr, base, keys in (('btn_start', '开始运行', start),
                                 ('btn_stop', '停止', stop)):
            btn = getattr(self, attr, None)
            if btn is None:
                continue
            label = f'{base} ({keys})' if keys else base
            try:
                if str(btn.cget('text')) != label:
                    btn.configure(text=label)
            except Exception:
                pass
        self._refresh_awake_ui()

    # ---------- 左侧导航 ----------
    def _build_sidebar(self, bar):
        """侧栏结构：App 名/版本 → 导航项 ×3 → 弹性空隙 → 全局状态徽章 → 急停提示。

        徽章与运行页状态横幅同数据源（pump_log 统一喂），急停提示替代
        原来的整条红色横幅——常驻恐吓变成角落里的一行小字。
        """
        CARD, INK, MUTED = self.core.CARD, self.core.INK, self.core.MUTED
        tk.Label(bar, text="SlotBot", font=("Microsoft YaHei UI", 14, "bold"),
                 bg=CARD, fg=INK).pack(anchor="w", padx=16, pady=(16, 0))
        tk.Label(bar, text=f"v{self.core.APP_VERSION}", font=("Microsoft YaHei UI", 9),
                 bg=CARD, fg=MUTED).pack(anchor="w", padx=16, pady=(0, 14))

        for i, label in enumerate(("定位设置", "参数设置", "运行")):
            item = tk.Frame(bar, bg=CARD, cursor="hand2")
            item.pack(fill="x")
            strip = tk.Frame(item, width=3, bg=CARD)
            strip.pack(side="left", fill="y")
            lbl = tk.Label(item, text=label, font=("Microsoft YaHei UI", 11),
                           bg=CARD, fg=MUTED, anchor="w", padx=15, pady=9)
            lbl.pack(side="left", fill="x", expand=True)
            for w in (item, strip, lbl):
                w.bind("<Button-1>", lambda e, i=i: self._select_page(i))
                w.bind("<Enter>", lambda e, i=i: self._paint_nav(hover=i))
                w.bind("<Leave>", lambda e: self._paint_nav())
            self._nav_items.append((item, strip, lbl))

        # 弹性空隙：把徽章和急停提示压到栏底
        tk.Frame(bar, bg=CARD).pack(expand=True, fill="both")

        badge = tk.Frame(bar, bg=CARD)
        badge.pack(fill="x", padx=14, pady=(0, 6))
        self.badge_dot = tk.Canvas(badge, width=10, height=10, bg=CARD, highlightthickness=0)
        self.badge_dot.create_oval(1, 1, 9, 9, fill=self.core.STATE_COLORS["idle"],
                                   outline="", tags="dot")
        self.badge_dot.pack(side="left")
        self.badge_label = tk.Label(badge, text="空闲", font=("Microsoft YaHei UI", 9),
                                    bg=CARD, fg=MUTED, anchor="w")
        self.badge_label.pack(side="left", padx=(6, 0))

        self.lbl_estop = tk.Label(bar, text="急停：F12 / End\n鼠标甩屏幕左上角",
                                  font=("Microsoft YaHei UI", 9), bg=CARD, fg="#8a1f16",
                                  justify="left")
        self.lbl_estop.pack(anchor="w", padx=14, pady=(0, 12))

    def _paint_nav(self, hover=None):
        """导航项三态：选中（浅蓝底+左缘蓝条）/悬停/普通。"""
        CARD, INK, MUTED, ACCENT = (self.core.CARD, self.core.INK,
                                    self.core.MUTED, self.core.ACCENT)
        for i, (item, strip, lbl) in enumerate(self._nav_items):
            if i == self._nav_index:
                bg, fg, sc = NAV_SELECTED_BG, INK, ACCENT
            elif i == hover:
                bg, fg, sc = NAV_HOVER_BG, INK, NAV_HOVER_BG
            else:
                bg, fg, sc = CARD, MUTED, CARD
            item.configure(bg=bg)
            strip.configure(bg=sc)
            lbl.configure(bg=bg, fg=fg)

    def _select_page(self, idx):
        """导航点击切页：页签头隐藏后仍用 select() 切页，
        nb.tabs() / nb.tab(t,'text') 的测试契约不受 hidden 影响。"""
        self._nav_index = idx
        self._paint_nav()
        self.nb.select(idx)

    def _toggle_advanced(self):
        """展开/收起高级参数：grid_remove 保留布局参数，原位恢复不跳动。"""
        self._adv_open = not self._adv_open
        self._adv_title.config(text="高级参数 ▾" if self._adv_open else "高级参数 ▸")
        if self._adv_open:
            self._adv_frame.grid()
        else:
            self._adv_frame.grid_remove()

    def _page_header(self, page, title, desc):
        """每页顶部标题+说明：页签头隐藏后由它补充页面上下文。"""
        head = ttk.Frame(page)
        head.pack(fill="x", padx=14, pady=(12, 0))
        ttk.Label(head, text=title, font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        ttk.Label(head, text=desc, style="Muted.TLabel",
                  font=("Microsoft YaHei UI", 9)).pack(anchor="w", pady=(2, 2))

    # ---------- 页内滚动 ----------
    def _scroll_page(self, notebook, title):
        """新建一个页内可滚动的页签，返回真正放内容的内层 Frame。"""
        tab = ttk.Frame(notebook)
        notebook.add(tab, text=title)
        canvas = tk.Canvas(tab, highlightthickness=0, bd=0, bg=self.core.BG)
        vbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=inner, anchor="nw", tags="inner")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure("inner", width=e.width))
        self._scroll_canvases.append(canvas)
        return inner

    def _wheel(self, event):
        """滚轮滚动光标下的页面；返回被滚动的 canvas，没接住返回 None。

        Tk 把滚轮发给光标下那个子控件，必须沿 master 链找所属画布；
        Text/Canvas/Listbox 这类自己会滚的控件要跳过，否则滚两层。
        """
        widget = event.widget
        if isinstance(widget, (tk.Text, tk.Listbox, tk.Canvas)):
            return None
        node = widget
        while node is not None:
            if isinstance(node, tk.Canvas) and node in self._scroll_canvases:
                steps = int(round(getattr(event, "delta", 0) / 120.0))
                if steps:
                    # Canvas 的 yview_scroll 只接受 units/pages；units 默认为窗高的 1/10
                    node.yview_scroll(-steps, "units")
                    return node
                return None
            node = node.master
        return None

    def on_mode_change(self):
        coord = self.v_mode.get() == "coord"
        self.lbl_tip.config(
            text=(
                "固定坐标模式：点「取点」→ 屏幕变暗后把鼠标移到目标上点一下即可记录坐标；也可直接手填 X/Y。\n"
                "运行前确认游戏视角和设定时一致（视角一动坐标就失效）。"
                if coord else
                "图像识别模式：点「录模板」→ 拖框框住目标 → 松开保存，运行时会实时搜索目标位置。"
            )
        )
        self.hint.config(
            text=(
                "框选技巧：只框按钮本体，别带太多背景；录完点「识图测试」看相似度，建议 0.9 以上。"
                if not coord else ""
            )
        )
        # 每行只保留当前模式用得上的按钮：坐标=取点/移动测试，识图=录模板/识图测试/预览。
        # grid_remove 会记住布局参数，grid() 原位恢复，不打乱列对齐。
        show = ("pick", "move") if coord else ("record", "test", "preview")
        for key, _ in self.core.TEMPLATES:
            for name, btn in self.tpl_btns[key].items():
                if name in show:
                    btn.grid()
                else:
                    btn.grid_remove()
        self.refresh_tpl_status()
