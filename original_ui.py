import tkinter as tk
from tkinter import ttk

class OriginalUI:
    def build_ui(self):
        TEMPLATES = self.core.TEMPLATES
        pad = {"padx": 6, "pady": 4}

        nb = ttk.Notebook(self)
        self.nb = nb
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # --- 定位页 ---
        tab1 = ttk.Frame(nb)
        nb.add(tab1, text="1. 定位设置")

        tools = ttk.Frame(tab1)
        tools.pack(fill="x", padx=8, pady=4)
        for label, command in (("样本与采集", self.open_sample_center), ("运行前自检", self.run_preflight), ("坐标诊断", self.show_coordinate_report)):
            ttk.Button(tools, text=label, command=command).pack(side="left", padx=3)
        modebar = ttk.Frame(tab1)
        modebar.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(modebar, text="定位方式：", font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        self.v_mode = tk.StringVar(value=self.cfg.get("locate_mode", "coord"))
        ttk.Radiobutton(
            modebar, text="固定坐标（推荐，快而稳）", value="coord",
            variable=self.v_mode, command=self.on_mode_change,
        ).pack(side="left", padx=6)
        ttk.Radiobutton(
            modebar, text="图像识别（模板匹配）", value="image",
            variable=self.v_mode, command=self.on_mode_change,
        ).pack(side="left", padx=6)

        self.lbl_tip = ttk.Label(tab1, text="", justify="left", foreground="#555")
        self.lbl_tip.pack(anchor="w", padx=8, pady=4)

        grid = ttk.Frame(tab1)
        grid.pack(fill="x", padx=8)
        for c, txt in enumerate(("目标", "坐标 X", "坐标 Y", "状态", "操作")):
            ttk.Label(grid, text=txt, font=("Microsoft YaHei", 10, "bold")).grid(
                row=0, column=c, sticky="w", padx=5, pady=4
            )
        self.tpl_status = {}
        self.pt_vars = {}
        for i, (key, label) in enumerate(TEMPLATES, start=1):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", padx=5, pady=3)
            pt = (self.cfg.get("points") or {}).get(key, ["", ""])
            vx = tk.StringVar(value="" if pt[0] in (None, "") else str(pt[0]))
            vy = tk.StringVar(value="" if pt[1] in (None, "") else str(pt[1]))
            self.pt_vars[key] = (vx, vy)
            ttk.Entry(grid, textvariable=vx, width=7).grid(row=i, column=1, padx=4)
            ttk.Entry(grid, textvariable=vy, width=7).grid(row=i, column=2, padx=4)
            st = ttk.Label(grid, text="—", foreground="#999")
            st.grid(row=i, column=3, sticky="w", padx=5)
            self.tpl_status[key] = st
            box = ttk.Frame(grid)
            box.grid(row=i, column=4, sticky="w", padx=5)
            self.btn_pick = ttk.Button(
                box, text="取点", width=7, command=lambda k=key: self.pick_point(k)
            )
            self.btn_pick.pack(side="left", padx=2)
            self.btn_move = ttk.Button(
                box, text="移动测试", width=9, command=lambda k=key: self.move_test(k)
            )
            self.btn_move.pack(side="left", padx=2)
            self.btn_rec = ttk.Button(
                box, text="录模板", width=7, command=lambda k=key: self.record(k)
            )
            self.btn_rec.pack(side="left", padx=2)
            self.btn_test = ttk.Button(
                box, text="识图测试", width=9, command=lambda k=key: self.test(k)
            )
            self.btn_test.pack(side="left", padx=2)
            ttk.Button(box, text="预览", width=5, command=lambda k=key: self.preview(k)).pack(side="left", padx=2)

        self.hint = ttk.Label(
            tab1,
            text="",
            justify="left",
            foreground="#666",
            wraplength=840,
        )
        self.hint.pack(anchor="w", padx=8, pady=(8, 4))

        # ---- Credit 数字区（判断"还有没有币"）----
        wr = ttk.LabelFrame(tab1, text="清币判断：Credit 数字区（推荐，直接读数字）")
        wr.pack(fill="x", padx=8, pady=(4, 4))
        ttk.Label(
            wr,
            text="框住机器上红色的 Credit 数码管（就是那个显示 0 的红色数字），"
                 "可靠读到低币（MaxBet 时为 0、1、2）会提前结束本轮并投币。\n"
                 "低币须通过完整性、模板及连续三帧复核；看不清时继续设定的抽奖/空转流程，每局重新尝试识别。",
            justify="left",
            foreground="#666",
        ).pack(anchor="w", padx=6, pady=4)
        crow = ttk.Frame(wr)
        crow.pack(anchor="w", padx=6, pady=(0, 4))
        ttk.Button(crow, text="框选 Credit 区域", command=self.pick_credit_rect).pack(side="left", padx=3)
        ttk.Button(crow, text="自动找红色数字区", command=self.auto_find_credit).pack(side="left", padx=3)
        ttk.Button(crow, text="读取测试", command=self.test_credit_rect).pack(side="left", padx=3)
        ttk.Button(crow, text="实时监测窗口", command=self.open_credit_monitor).pack(side="left", padx=3)
        ttk.Button(crow, text="清除", command=self.clear_credit_rect).pack(side="left", padx=3)
        self.lbl_credit = ttk.Label(crow, text="", foreground="#bb8800")
        self.lbl_credit.pack(side="left", padx=8)
        self.update_credit_label()

        live = ttk.Frame(wr)
        live.pack(anchor="w", padx=6, pady=(0, 4))
        ttk.Label(live, text="当前实时读数：", font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        self.lbl_credit_live = ttk.Label(
            live, text="—", font=("Consolas", 16, "bold"), foreground="#888"
        )
        self.lbl_credit_live.pack(side="left", padx=6)
        self.lbl_credit_live_info = ttk.Label(live, text="", foreground="#888")
        self.lbl_credit_live_info.pack(side="left", padx=6)

        refrow = ttk.Frame(wr)
        refrow.pack(anchor="w", padx=6, pady=(0, 6))
        ttk.Label(
            refrow,
            text="① 先框选上面的 Credit 区域    ② 在【机器显示 0 / 没币】时点：",
            foreground="#666",
        ).pack(side="left")
        ttk.Button(
            refrow, text="记为「没币」参考", command=self.capture_credit_ref
        ).pack(side="left", padx=4)
        ttk.Button(refrow, text="清除参考", command=self.clear_credit_ref).pack(side="left", padx=4)
        self.lbl_credit_ref = ttk.Label(refrow, text="", foreground="#bb8800")
        self.lbl_credit_ref.pack(side="left", padx=8)
        self.update_credit_ref_label()

        # ---- 备用：区域差异监测 ----
        wr2 = ttk.LabelFrame(tab1, text="备用数字区域（辅助识别；读不清仍按次数继续）")
        wr2.pack(fill="x", padx=8, pady=(0, 8))
        wrow = ttk.Frame(wr2)
        wrow.pack(anchor="w", padx=6, pady=6)
        ttk.Button(wrow, text="框选监测区域", command=self.pick_watch_rect).pack(side="left", padx=3)
        ttk.Button(wrow, text="测试噪声（1.5 秒）", command=self.test_watch_rect).pack(side="left", padx=3)
        ttk.Button(wrow, text="清除", command=self.clear_watch_rect).pack(side="left", padx=3)
        self.lbl_watch = ttk.Label(wrow, text="", foreground="#bb8800")
        self.lbl_watch.pack(side="left", padx=8)
        self.update_watch_label()
        self.on_mode_change()

        # --- 参数页 ---
        tab2 = ttk.Frame(nb)
        nb.add(tab2, text="2. 参数设置")
        pf = ttk.Frame(tab2)
        pf.pack(fill="both", expand=True, padx=10, pady=8)

        def add_row(r, key, label, tip=""):
            ttk.Label(pf, text=label).grid(row=r, column=0, sticky="w", pady=3)
            var = tk.StringVar(value=str(self.cfg.get(key, "")))
            self.vars[key] = var
            e = ttk.Entry(pf, textvariable=var, width=10)
            e.grid(row=r, column=1, sticky="w", padx=6)
            if tip:
                ttk.Label(pf, text=tip, foreground="#777").grid(
                    row=r, column=2, sticky="w", padx=6
                )
            return var

        r = 0
        ttk.Label(pf, text="— 数量 —", font=("Microsoft YaHei", 10, "bold")).grid(
            row=r, column=0, sticky="w", pady=(4, 2)
        )
        r += 1
        add_row(r, "coin_count", "每轮投币数", "99")
        r += 1
        add_row(r, "draw_count", "正常抽奖次数", "33（每次吃 3 币）")
        r += 1
        add_row(r, "burn_count", "空转清币次数", "读不清时的连续空转数；读到仍有币则继续抽")
        r += 1
        add_row(r, "bet_count", "下注枚数", "3=MaxBet；1 或 2=点 Bet 按钮")
        r += 1
        add_row(r, "rounds", "运行轮数", "0 = 一直循环")
        r += 1
        add_row(r, "max_minutes", "最长运行分钟", "0 = 不限；填 30 就是半小时自动收工")

        r += 1
        ttk.Label(pf, text="— 时间（毫秒）—", font=("Microsoft YaHei", 10, "bold")).grid(
            row=r, column=0, sticky="w", pady=(10, 2)
        )
        r += 1
        add_row(r, "coin_delay", "投币间隔", "100")
        r += 1
        add_row(r, "last_coin_wait", "最后一枚投完等待", "800")
        r += 1
        add_row(r, "step_delay", "各步骤间隔", "500")
        r += 1
        add_row(r, "move_delay", "鼠标移到目标后停顿", "150")
        r += 1
        add_row(r, "pull_px", "拉杆下拉距离(像素)", "280")

        r += 1
        ttk.Label(pf, text="— 识别 —", font=("Microsoft YaHei", 10, "bold")).grid(
            row=r, column=0, sticky="w", pady=(10, 2)
        )
        r += 1
        add_row(r, "threshold", "相似度阈值", "0.70~0.90，找不到就调低，点错就调高")
        r += 1
        add_row(r, "retries", "找不到时重试次数", "10")
        r += 1
        add_row(r, "region_diff_threshold", "监测区域差异阈值", "数字区域噪声大就调大（默认 3.0）")
        r += 1
        add_row(r, "region_wait", "下注后等多久看数字(毫秒)", "700")
        r += 1

        self.v_hold_tab = tk.BooleanVar(value=bool(self.cfg["hold_tab"]))
        ttk.Checkbutton(pf, text="运行期间按住 Tab（呼出光标）", variable=self.v_hold_tab).grid(
            row=r, column=0, columnspan=2, sticky="w", pady=3
        )
        r += 1
        add_row(r, "tab_repeat", "Tab 续按间隔(毫秒)", "400，没生效就改 200")
        r += 1
        add_row(r, "game_window", "游戏窗口标题关键字", "默认 VRChat，用于自动激活游戏窗口")
        r += 1
        self.v_multi = tk.BooleanVar(value=bool(self.cfg["multi_scale"]))
        ttk.Checkbutton(
            pf, text="多尺度匹配（视角远近变化时更稳，稍慢）", variable=self.v_multi
        ).grid(row=r, column=0, columnspan=2, sticky="w", pady=3)
        r += 1
        ttk.Label(pf, text='数字只辅助提前补币；读不清继续抽奖和空转，每局重新尝试识别').grid(
            row=r, column=0, columnspan=3, sticky='w', pady=3)

        extra = ttk.Frame(tab2)
        extra.pack(pady=6)
        for label, command in (("保存配置", self.collect_and_save), ("定时 / 保活 / 高级参数", self.open_controls), ("配置方案", self.open_profiles)):
            ttk.Button(extra, text=label, command=command).pack(side="left", padx=4)

        # --- 运行页 ---
        tab3 = ttk.Frame(nb)
        nb.add(tab3, text="3. 运行")
        bar = ttk.Frame(tab3)
        bar.pack(fill="x", padx=10, pady=8)
        self.btn_start = ttk.Button(bar, text="开始运行", command=self.start)
        self.btn_start.pack(side="left", padx=4)
        self.btn_stop = ttk.Button(bar, text="停止 (F12)", command=self.request_stop)
        self.btn_stop.pack(side="left", padx=4)
        ttk.Button(bar, text="重新加载模板", command=self.reload_tpl).pack(side="left", padx=4)
        self.lbl_state = ttk.Label(bar, text="状态：空闲", foreground="#0077cc")
        self.lbl_state.pack(side="left", padx=14)

        # Tab / 窗口 诊断工具
        bar2 = ttk.Frame(tab3)
        bar2.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(bar2, text="激活游戏窗口", command=self.activate_game).pack(side="left", padx=4)
        ttk.Button(bar2, text="测试按住 Tab（6 秒）", command=self.test_tab).pack(side="left", padx=4)
        ttk.Label(
            bar2,
            text="← 先在游戏里站好，点它看光标会不会出来（游戏要全屏/窗口化可见，别被本窗口挡住）",
            foreground="#666",
        ).pack(side="left", padx=8)

        self.lbl_stats = ttk.Label(tab3, text="还没有运行记录")
        self.lbl_stats.pack(anchor="w", padx=14)
        self.lbl_ready = self.lbl_state
        actions = ttk.Frame(tab3)
        actions.pack(fill="x", padx=10, pady=3)
        ttk.Button(actions, text="定时 / 保活", command=self.open_controls).pack(side="left", padx=4)
        ttk.Button(actions, text="导出日志", command=self.export_log).pack(side="left", padx=4)
        self.txt = tk.Text(tab3, height=20, font=("Consolas", 9))
        textbar = ttk.Scrollbar(tab3, command=self.txt.yview)
        textbar.pack(side="right", fill="y", pady=(0,10))
        self.txt.configure(yscrollcommand=textbar.set)
        self.txt.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        ttk.Label(
            self,
            text="急停方式（任选其一）：① 点右上角置顶的红色「停止运行」按钮 ② 按 设置的急停键（默认 F12 / End） "
                 "③ 把鼠标甩到屏幕左上角 ④ 在参数页设「最长运行分钟」自动收工",
            foreground="#a33",
            wraplength=840,
            justify="left",
        ).pack(pady=(0, 6))

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
                "建议顺序：① 硬币堆 ② MaxBet ③ 拉杆 ④ 按钮1/2/3（Bet 按钮只在押 1/2 枚时才需要）。\n"
                "取完点可以点「移动测试」验证鼠标会不会停到目标上。"
                if coord else
                "框选技巧：只框按钮本体，别带太多背景；录完点「识图测试」看相似度，建议 0.9 以上。"
            )
        )
        for key, _ in TEMPLATES:
            self.tpl_status[key].config(
                text=("待设定" if coord else "使用识图"), foreground=("#bb8800" if coord else "#0077cc")
            )
        self.refresh_tpl_status()
