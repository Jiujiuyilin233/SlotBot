"""原版三标签页界面：卡片式排版 + 页内滚动 + 滚轮支持。

所有控件名字（nb / pt_vars / tpl_status / vars / btn_start / btn_stop /
lbl_state / lbl_stats / txt / v_mode / v_hold_tab / v_multi …）是回归测试
和功能回调的契约，重排时一律保留。
"""
import tkinter as tk
from tkinter import ttk


class OriginalUI:
    def build_ui(self):
        self._scroll_canvases = []
        # 滚轮必须 bind_all：Tk 把滚轮事件发给光标下的子控件，子控件的
        # bindtags 里没有 canvas，只在 canvas 上绑等于收不到（实踩过的坑）。
        self.bind_all("<MouseWheel>", self._wheel, add="+")

        nb = ttk.Notebook(self)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=10, pady=(8, 2))

        # ============ 1. 定位设置 ============
        page1 = self._scroll_page(nb, "1. 定位设置")

        tools = ttk.Frame(page1)
        tools.pack(fill="x", padx=14, pady=(10, 0))
        for label, command in (("样本与采集", self.open_sample_center),
                               ("运行前自检", self.run_preflight),
                               ("坐标诊断", self.show_coordinate_report)):
            ttk.Button(tools, text=label, style="Ghost.TButton",
                       command=command).pack(side="left", padx=(0, 6))

        body = self._card(page1, "定位方式")
        self.v_mode = tk.StringVar(value=self.cfg.get("locate_mode", "coord"))
        radios = ttk.Frame(body, style="Card.TFrame")
        radios.pack(anchor="w")
        ttk.Radiobutton(radios, text="固定坐标（推荐，快而稳）", value="coord",
                        variable=self.v_mode, command=self.on_mode_change).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(radios, text="图像识别（模板匹配）", value="image",
                        variable=self.v_mode, command=self.on_mode_change).pack(side="left")
        self.lbl_tip = ttk.Label(body, text="", justify="left", style="CardMuted.TLabel",
                                 wraplength=760)
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
        for i, (key, label) in enumerate(self.core.TEMPLATES, start=1):
            ttk.Label(grid, text=label, style="Card.TLabel").grid(
                row=i, column=0, sticky="w", padx=5, pady=3)
            pt = (self.cfg.get("points") or {}).get(key, ["", ""])
            vx = tk.StringVar(value="" if pt[0] in (None, "") else str(pt[0]))
            vy = tk.StringVar(value="" if pt[1] in (None, "") else str(pt[1]))
            self.pt_vars[key] = (vx, vy)
            ttk.Entry(grid, textvariable=vx, width=7).grid(row=i, column=1, padx=4, pady=3)
            ttk.Entry(grid, textvariable=vy, width=7).grid(row=i, column=2, padx=4, pady=3)
            st = ttk.Label(grid, text="—", style="CardMuted.TLabel")
            st.grid(row=i, column=3, sticky="w", padx=5)
            self.tpl_status[key] = st
            box = ttk.Frame(grid, style="Card.TFrame")
            box.grid(row=i, column=4, sticky="w", padx=5)
            self.btn_pick = ttk.Button(box, text="取点", width=7,
                                       command=lambda k=key: self.pick_point(k))
            self.btn_pick.pack(side="left", padx=2)
            self.btn_move = ttk.Button(box, text="移动测试", width=9,
                                       command=lambda k=key: self.move_test(k))
            self.btn_move.pack(side="left", padx=2)
            self.btn_rec = ttk.Button(box, text="录模板", width=7,
                                      command=lambda k=key: self.record(k))
            self.btn_rec.pack(side="left", padx=2)
            self.btn_test = ttk.Button(box, text="识图测试", width=9,
                                       command=lambda k=key: self.test(k))
            self.btn_test.pack(side="left", padx=2)
            ttk.Button(box, text="预览", width=5, style="Ghost.TButton",
                       command=lambda k=key: self.preview(k)).pack(side="left", padx=2)
        self.hint = ttk.Label(body, text="", justify="left", style="CardMuted.TLabel",
                              wraplength=760)
        self.hint.pack(anchor="w", pady=(8, 0))

        body = self._card(
            page1, "Credit 数字区（清币判断）",
            "框住机器上红色的 Credit 数码管（就是那个显示 0 的红色数字），"
            "可靠读到低币（MaxBet 时为 0、1、2）会提前结束本轮并投币。"
            "低币须通过完整性、模板及连续三帧复核；看不清时继续设定的抽奖/空转流程，每局重新尝试识别。")
        crow = ttk.Frame(body, style="Card.TFrame")
        crow.pack(anchor="w")
        ttk.Button(crow, text="框选 Credit 区域", command=self.pick_credit_rect).pack(side="left", padx=(0, 3))
        ttk.Button(crow, text="自动找红色数字区", command=self.auto_find_credit).pack(side="left", padx=3)
        ttk.Button(crow, text="读取测试", command=self.test_credit_rect).pack(side="left", padx=3)
        ttk.Button(crow, text="实时监测窗口", command=self.open_credit_monitor).pack(side="left", padx=3)
        ttk.Button(crow, text="清除", style="Ghost.TButton", command=self.clear_credit_rect).pack(side="left", padx=3)
        self.lbl_credit = ttk.Label(crow, text="", style="Card.TLabel", foreground="#bb8800")
        self.lbl_credit.pack(side="left", padx=8)
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

        refrow = ttk.Frame(body, style="Card.TFrame")
        refrow.pack(anchor="w", pady=(8, 0))
        ttk.Label(refrow, text="① 先框选上面的 Credit 区域    ② 在【机器显示 0 / 没币】时点：",
                  style="CardMuted.TLabel").pack(side="left")
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

        body = self._card(page2, "数量")
        r = 0
        add_row(body, r, "coin_count", "每轮投币数", "99"); r += 1
        add_row(body, r, "draw_count", "正常抽奖次数", "33（每次吃 3 币）"); r += 1
        add_row(body, r, "burn_count", "空转清币次数", "读不清时的连续空转数；读到仍有币则继续抽"); r += 1
        add_row(body, r, "bet_count", "下注枚数", "3=MaxBet；1 或 2=点 Bet 按钮"); r += 1
        add_row(body, r, "rounds", "运行轮数", "0 = 一直循环"); r += 1
        add_row(body, r, "max_minutes", "最长运行分钟", "0 = 不限；填 30 就是半小时自动收工")

        body = self._card(page2, "时间（毫秒）")
        r = 0
        add_row(body, r, "coin_delay", "投币间隔", "100"); r += 1
        add_row(body, r, "last_coin_wait", "最后一枚投完等待", "800"); r += 1
        add_row(body, r, "step_delay", "各步骤间隔", "500"); r += 1
        add_row(body, r, "move_delay", "鼠标移到目标后停顿", "150"); r += 1
        add_row(body, r, "pull_px", "拉杆下拉距离(像素)", "280")

        body = self._card(page2, "识别")
        r = 0
        add_row(body, r, "threshold", "相似度阈值", "0.70~0.90，找不到就调低，点错就调高"); r += 1
        add_row(body, r, "retries", "找不到时重试次数", "10"); r += 1
        add_row(body, r, "region_diff_threshold", "监测区域差异阈值", "数字区域噪声大就调大（默认 3.0）"); r += 1
        add_row(body, r, "region_wait", "下注后等多久看数字(毫秒)", "700"); r += 1
        self.v_multi = tk.BooleanVar(value=bool(self.cfg["multi_scale"]))
        ttk.Checkbutton(body, text="多尺度匹配（视角远近变化时更稳，稍慢）",
                        variable=self.v_multi, style="Card.TCheckbutton").grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3)
        r += 1
        ttk.Label(body, text="数字只辅助提前补币；读不清继续抽奖和空转，每局重新尝试识别",
                  style="CardMuted.TLabel").grid(row=r, column=0, columnspan=3, sticky="w", pady=3)

        body = self._card(page2, "窗口与按键")
        self.v_hold_tab = tk.BooleanVar(value=bool(self.cfg["hold_tab"]))
        ttk.Checkbutton(body, text="运行期间按住 Tab（呼出光标）",
                        variable=self.v_hold_tab, style="Card.TCheckbutton").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=3)
        add_row(body, 1, "tab_repeat", "Tab 续按间隔(毫秒)", "400，没生效就改 200")
        add_row(body, 2, "game_window", "游戏窗口标题关键字", "默认 VRChat，用于自动激活游戏窗口")

        extra = ttk.Frame(page2)
        extra.pack(pady=10)
        ttk.Button(extra, text="保存配置", style="Accent.TButton",
                   command=self.collect_and_save).pack(side="left", padx=4)
        ttk.Button(extra, text="定时 / 保活 / 高级参数",
                   command=self.open_controls).pack(side="left", padx=4)
        ttk.Button(extra, text="配置方案", command=self.open_profiles).pack(side="left", padx=4)

        # ============ 3. 运行 ============
        page3 = self._scroll_page(nb, "3. 运行")

        body = self._card(page3, "运行控制")
        bar = ttk.Frame(body, style="Card.TFrame")
        bar.pack(fill="x")
        self.btn_start = ttk.Button(bar, text="开始运行", style="Accent.TButton", command=self.start)
        self.btn_start.pack(side="left", padx=(0, 6))
        self.btn_stop = ttk.Button(bar, text="停止 (F12)", style="Danger.TButton", command=self.request_stop)
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(bar, text="重新加载模板", style="Ghost.TButton",
                   command=self.reload_tpl).pack(side="left", padx=6)
        self.lbl_state = ttk.Label(bar, text="状态：空闲", style="Card.TLabel", foreground="#0077cc")
        self.lbl_state.pack(side="left", padx=14)

        bar2 = ttk.Frame(body, style="Card.TFrame")
        bar2.pack(fill="x", pady=(10, 0))
        ttk.Button(bar2, text="激活游戏窗口", style="Ghost.TButton",
                   command=self.activate_game).pack(side="left", padx=(0, 4))
        ttk.Button(bar2, text="测试按住 Tab（6 秒）", style="Ghost.TButton",
                   command=self.test_tab).pack(side="left", padx=4)
        ttk.Label(bar2, text="← 先在游戏里站好，点它看光标会不会出来"
                  "（游戏要全屏/窗口化可见，别被本窗口挡住）",
                  style="CardMuted.TLabel", wraplength=430, justify="left").pack(side="left", padx=8)

        self.lbl_stats = ttk.Label(body, text="还没有运行记录", style="CardMuted.TLabel")
        self.lbl_stats.pack(anchor="w", pady=(10, 0))
        self.lbl_ready = self.lbl_state

        logcard = self._card(page3, "运行日志")
        self.txt = tk.Text(logcard, height=16, font=("Consolas", 9),
                           bg="#ffffff", fg="#1f2733", relief="flat",
                           highlightthickness=1, highlightbackground=self.core.BORDER)
        textbar = ttk.Scrollbar(logcard, command=self.txt.yview)
        textbar.pack(side="right", fill="y")
        self.txt.configure(yscrollcommand=textbar.set)
        self.txt.pack(fill="both", expand=True)

        actions = ttk.Frame(page3)
        actions.pack(fill="x", padx=14, pady=(0, 4))
        ttk.Button(actions, text="定时 / 保活", command=self.open_controls).pack(side="left", padx=4)
        ttk.Button(actions, text="导出日志", style="Ghost.TButton",
                   command=self.export_log).pack(side="left", padx=4)

        ttk.Label(
            self,
            text="急停方式（任选其一）：① 点右上角置顶的红色「停止运行」按钮 ② 按 设置的急停键（默认 F12 / End） "
                 "③ 把鼠标甩到屏幕左上角 ④ 在参数页设「最长运行分钟」自动收工",
            foreground="#a33",
            wraplength=860,
            justify="left",
        ).pack(pady=(0, 6))

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
        TEMPLATES = self.core.TEMPLATES
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
        for key, _ in TEMPLATES:
            self.tpl_status[key].config(
                text=("待设定" if coord else "使用识图"), foreground=("#bb8800" if coord else "#0077cc")
            )
        self.refresh_tpl_status()
