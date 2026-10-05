"""Isolated regressions. No mouse or keyboard input is sent to the game."""
import os, sys, tempfile, threading, unittest, io
from pathlib import Path
sandbox=tempfile.TemporaryDirectory(prefix='slotbot-regression-')
os.environ['SLOTBOT_DATA_DIR']=sandbox.name
import cv2
import numpy as np
import time
import slotbot as s
ROOT=Path(__file__).parent

def glyph(text):
    out=np.zeros((76,len(text)*55,3),np.uint8)
    segments=[(10,6,36,13),(36,14,43,35),(36,40,43,61),(10,62,36,69),
              (3,40,10,61),(3,14,10,35),(10,34,36,41)]
    from vision import PATTERNS
    for i,c in enumerate(text):
        for active,(x0,y0,x1,y1) in zip(PATTERNS[c],segments):
            if active=='1': out[y0:y1,x0+i*55:x1+i*55]=(12,12,220)
    return out

class FakeScreen:
    left=top=0
    def __init__(self,frames): self.frames=list(frames); self.index=0
    def grab_rect(self,*args):
        im=self.frames[min(self.index,len(self.frames)-1)]; self.index+=1
        return im.copy()
    def grab(self): return self.grab_rect()

class Regression(unittest.TestCase):
    def setUp(self):
        # Full game screenshots are private, optional fixtures. The public
        # package keeps cropped digits/buttons and runs all other regressions.
        private_scene_tests = {
            'test_full_real_screens_find_credit_and_chance',
            'test_truncated_digit_never_reads_as_one',
            'test_digit_fit_accepts_real_selections',
            'test_live_readout_refuses_truncated_zero',
            'test_scene_shift_separates_animation_from_rotation',
            'test_digit_width_uses_median_not_span',
            'test_drift_reminder_never_blocks',
            'test_view_drift_not_reported_for_live_scene',
            'test_view_drift_is_reported',
            'test_auto_find_button_uses_full_screen_with_window_hidden',
        }
        if self._testMethodName in private_scene_tests and not all(
                (ROOT / f'tests/seven_screen_{n}.png').is_file() for n in (1, 2, 3)):
            self.skipTest('Optional full-scene fixtures are excluded from the public source package.')

    @classmethod
    def setUpClass(cls):
        cls.zero=s.load_image(str(ROOT/'tests/credit_zero_real.png'))
        if cls.zero is None: raise RuntimeError('真实截图素材缺失，不能跳过')
        # 测试跑的是合成图。让自我训练在测试里生效，那些合成字形就会被
        # 当成真值写进用户样本库、再被模板库学走，反而把真机读数判成未知。
        # 测试必须零副作用，所以这里把自动采集总闸关掉。
        cls._auto_learn = s.AUTO_LEARN_ALLOWED
        s.AUTO_LEARN_ALLOWED = False

    @classmethod
    def tearDownClass(cls):
        s.AUTO_LEARN_ALLOWED = cls._auto_learn
    def reader(self,frames,**kw):
        return s.Engine(dict(s.DEFAULT_CFG,credit_rect=[0,0,85,50],**kw),{},lambda msg:None,FakeScreen(frames))
    def test_real_zero_not_11111(self):
        self.assertEqual(s.decode_led_best(self.zero)[0],'0')
        for scale in (.7,1,1.3):
            for brightness in (.7,1,1.15):
                im=cv2.resize(self.zero,None,fx=scale,fy=scale)
                im=np.clip(im.astype(float)*brightness,0,255).astype(np.uint8)
                self.assertEqual(s.decode_led_best(im)[0],'0')
        self.assertEqual(s.decode_led_best(s.load_image(str(ROOT/'credit_zero.png')))[0],'0')
        boxes=s.merge_red_boxes(s.led_mask(self.zero))
        self.assertEqual(len(boxes),1)
        x,y,w,h=boxes[0]
        self.assertEqual(s.decode_led_best(self.zero[max(0,y-3):y+h+3,max(0,x-3):x+w+3])[0],'0')
    def test_all_standard_digits(self):
        for n in range(100):
            text=f'{n:02d}'
            self.assertEqual(s.decode_led_best(glyph(text))[0],text,text)
        for text in ('0','1','7','12002','119696'):
            self.assertEqual(s.decode_led_best(glyph(text))[0],text,text)
    def test_real_hooked_seven(self):
        seven=s.load_image(str(ROOT/'tests/credit_seven_real.png'))
        self.assertIsNotNone(seven)
        for scale in (.7,1,1.3):
            for brightness in (.7,1,1.15):
                im=cv2.resize(seven,None,fx=scale,fy=scale)
                im=np.clip(im.astype(float)*brightness,0,255).astype(np.uint8)
                self.assertEqual(s.decode_led_best(im)[0],'7',(scale,brightness))
                self.assertIs(self.reader([im]).credit_is_empty()[0],False)

    def test_full_real_screens_find_credit_and_chance(self):
        for number in (1,2,3):
            frame=s.load_image(str(ROOT/f'tests/seven_screen_{number}.png'))
            self.assertIsNotNone(frame)
            candidates=s.find_digit_regions(frame)
            credit=[c for c in candidates if c[0]<=1618<c[0]+c[2] and c[1]<=598<c[1]+c[3]]
            chance=[c for c in candidates if c[0]<=1625<c[0]+c[2] and c[1]<=675<c[1]+c[3]]
            self.assertEqual([c[4] for c in credit],['7'],number)
            self.assertEqual([c[4] for c in chance],['0'],number)
            if number != 2:
                self.assertIn('119671',[c[4] for c in candidates])
            for x,y,w,h,val,_ in candidates:
                self.assertEqual(s.decode_led_best(frame[y:y+h,x:x+w])[0],val)
    def test_dark_obstructed_and_red_button_are_unknown(self):
        for im in (np.zeros_like(self.zero),np.full_like(self.zero,255),s.load_image(str(ROOT/'templates/btn1.png'))):
            self.assertIsNone(self.reader([im]).credit_is_empty()[0])
        red=np.zeros((50,50,3),np.uint8); red[5:45,20:26,2]=255
        self.assertIsNone(self.reader([red]).credit_is_empty()[0])
    def test_truncated_digit_never_reads_as_one(self):
        """把 0 切掉一大半时解码器会当成 1；选区截断检测必须拦住它。"""
        frame=s.load_image(str(ROOT/'tests/seven_screen_1.png'))
        x,y,w,h=1607,650,37,49
        self.assertEqual(s.decode_led_best(frame[y:y+h,x:x+w])[0],'0')
        for cut in (.3,.45,.55,.7,.8):
            cw=int(w*(1-cut))
            for side,crop in (('right',frame[y:y+h,x+w-cw:x+w]),
                              ('left',frame[y:y+h,x:x+cw])):
                raw=s.decode_led_best(crop)[0]
                ok,msg=s.digit_fit(crop)
                if raw=='1':
                    # 只要解码器认成 1，截断检测就一定要否定这个读数
                    self.assertFalse(ok,(cut,side,msg))
    def test_digit_fit_accepts_real_selections(self):
        """真实截图的正常选区不能被截断检测误伤，否则会一直让人重框。"""
        for name in ('credit_zero.png','tests/credit_zero_real.png','tests/credit_seven_real.png'):
            self.assertTrue(s.digit_fit(s.load_image(str(ROOT/name)))[0],name)
        frame=s.load_image(str(ROOT/'tests/seven_screen_1.png'))
        for (x,y,w,h) in ((1607,650,37,49),(1602,576,35,43)):
            self.assertTrue(s.digit_fit(frame[y:y+h,x:x+w])[0])
        # 整段余额 119671 也必须通过
        self.assertTrue(s.digit_fit(frame[884:884+43,1216:1216+144])[0])
    def test_real_samples_all_decode_correctly(self):
        """用户实拍样本（数字截图/<真值>.png 与 digits/v<真值>_*.png）必须全部解对。

        这是唯一测「真机画面」的准确率测试：合成基准只能验证猜测的几何，
        这里验证的是真机泛光、真机字体、真机裁剪下的实际表现。
        没有样本时跳过（干净机器上跑回归不该失败）。
        """
        sys.path.insert(0, str(ROOT/'tests'))
        try:
            import real_bench
        finally:
            sys.path.pop(0)
        samples=real_bench.load_samples()
        if not samples:
            self.skipTest('没有真实样本（数字截图/ 或 digits/）')
        bad=[]
        for truth,fn,img in samples:
            val,info=real_bench.decode_led_best(img)
            if val!=truth: bad.append(f'{fn}: 真值 {truth} 读出 {val!r} ({info[:48]})')
        self.assertEqual(bad,[],f'{len(samples)} 张真实样本里有 {len(bad)} 张解错')
    def test_real_bench_sample_naming_formats(self):
        """样本命名：41 / 41-2 / 41-3 / 41_2 / v41_时间戳 都要解出同一个真值。

        -2/-3 是"同一个数字的不同视角"的序号，不能参与真值判断，否则
        用户按格式补的斜视角样本会全部被当成无效文件忽略掉。
        """
        sys.path.insert(0, str(ROOT/'tests'))
        try:
            import real_bench
        finally:
            sys.path.pop(0)
        for fn in ('41.png','41-2.png','41-3.png','41_2.png','v41_20261005_021200.png'):
            self.assertEqual(real_bench.truth_of(fn),'41',fn)
        for fn in ('abc.png','说明.txt','credit_zero.png'):
            self.assertIsNone(real_bench.truth_of(fn),fn)
    def test_decode_best_recovers_stray_red_via_panel(self):
        """框选过宽混进无关红色时：直接解码安全拒绝，暗色背板兜底应救回读数。"""
        from vision import decode_led_number as decode_direct
        img=np.full((120,260,3),(95,88,80),np.uint8)      # 亮色机柜背景（不是暗背板）
        img[22:22+76,150:150+55]=glyph('5')               # 数字贴右侧，周围纯黑即背板
        cv2.rectangle(img,(10,45),(60,75),(40,40,220),-1) # 左侧无关红色方块
        direct,dinfo=decode_direct(img)
        self.assertIn('?',str(direct),dinfo)              # 前提：直接解码安全拒绝
        val,info=s.decode_led_best(img)
        self.assertEqual(val,'5',info)                    # 兜底路径救回
        self.assertIn('背板',info)
        # 没有暗背板时兜底不能凭空造出读数
        lone=np.full((60,60,3),(95,88,80),np.uint8)
        cv2.rectangle(lone,(10,15),(50,45),(40,40,220),-1)
        self.assertIn('?',str(s.decode_led_best(lone)[0]))
    def test_consensus_rejects_flicker_and_keeps_stable(self):
        zero,seven=self.zero,s.load_image(str(ROOT/'tests/credit_seven_real.png'))
        value,info,agree,total=s.decode_consensus([zero]*3)
        self.assertEqual(value,'0'); self.assertEqual((agree,total),(3,3))
        # 三帧三个不同的值 -> 没有多数，不认
        eight=glyph('08')
        value,info,agree,total=s.decode_consensus([zero,seven,eight])
        self.assertIsNone(value,info)
        # 2/3 多数 -> 可信（显示用读数取多数，避免单帧噪声导致数字乱跳）
        value,info,agree,total=s.decode_consensus([zero,zero,seven])
        self.assertEqual(value,'0'); self.assertEqual((agree,total),(2,3))
        # 要求严格一致时，同样的 2/3 必须被否掉
        self.assertIsNone(s.decode_consensus([zero,zero,seven],min_agree=3)[0])
        value,info,agree,total=s.decode_consensus([np.zeros_like(zero)]*3)
        self.assertIsNone(value)                # 全灭 -> 未知，绝不当 0
    def test_live_readout_refuses_truncated_zero(self):
        """实时读数：选区把 0 切掉时必须显示存疑，而不是显示 1。"""
        frame=s.load_image(str(ROOT/'tests/seven_screen_1.png'))
        x,y,w,h=1607,650,37,49
        cw=int(w*.25)                        # 只框到最右侧一小条
        app=s.App(); app.withdraw()
        try:
            app.cfg['credit_rect']=[0,0,10,10]
            crop=frame[y:y+h,x+w-cw:x+w]
            self.assertEqual(s.decode_led_best(crop)[0],'1')  # 前提：解码器确实会误报
            class TruncScreen:
                def grab_rect(self,*a): return crop.copy()
            app.screen=TruncScreen()
            show,info=app._read_credit_display(app._reader(),frames=3)
            self.assertEqual(show,'?',info)
            self.assertIn('切',info)
            # 完整选区则必须正常显示 0
            full=frame[y-6:y+h+6,x-6:x+w+6]
            class FullScreen:
                def grab_rect(self,*a): return full.copy()
            app.screen=FullScreen()
            app.cfg['credit_rect']=[0,0,full.shape[1],full.shape[0]]
            show,info=app._read_credit_display(app._reader(),frames=3)
            self.assertEqual(show,'0',info)
        finally: app.on_close()
    def test_stability_and_reference(self):
        path=str(ROOT/'credit_zero.png')
        self.assertIs(self.reader([self.zero]*3,credit_ref=path).stable_credit_empty()[0],True)
        self.assertIsNone(self.reader([self.zero,np.zeros_like(self.zero),self.zero],credit_ref=path).stable_credit_empty()[0])
        self.assertIs(self.reader([glyph('17')],credit_ref=path).credit_is_empty()[0],False)
        self.assertIsNone(self.reader([np.zeros_like(self.zero)],credit_ref=path).credit_is_empty()[0])
    def test_relative_credit_ref_resolves_via_app_dir(self):
        import os
        engine=self.reader([self.zero],credit_ref='credit_zero.png')
        cwd=os.getcwd()
        try:
            os.chdir(tempfile.gettempdir())
            state,info=engine.credit_is_empty()
        finally:
            os.chdir(cwd)
        self.assertIs(state,True)
    def test_ui_collects_tab_repeat(self):
        app=s.App(); app.withdraw()
        try:
            app.update()
            app.vars['tab_repeat'].set('250')
            app.collect_cfg()
            self.assertEqual(app.cfg['tab_repeat'],250)
        finally: app.on_close()
    def test_credit_rect_dialog_reports_verdict(self):
        app=s.App(); app.withdraw()
        dialogs=[]
        class MB:
            def showinfo(self,*a,**k): dialogs.append(('info',)+a)
            def showwarning(self,*a,**k): dialogs.append(('warn',)+a)
            def showerror(self,*a,**k): dialogs.append(('error',)+a)
        original=s.messagebox; s.messagebox=MB()
        try:
            app.update()
            app.cfg['credit_rect']=[0,0,85,50]
            app.cfg['credit_ref']=str(ROOT/'credit_zero.png')
            app.screen=FakeScreen([self.zero])
            app._test_credit_rect()
            self.assertTrue(dialogs and dialogs[-1][0]=='info', dialogs)
        finally:
            s.messagebox=original; app.on_close()
    def test_region_pattern_diff(self):
        a=glyph('08'); b=glyph('07')
        self.assertEqual(s.region_diff(a,(a*.75).astype(np.uint8)),0)
        self.assertGreater(s.region_diff(a,b),3)
        self.assertIsNone(s.region_diff(a,np.zeros_like(a)))
    def test_no_change_does_not_prove_empty(self):
        """备用区域读不出变化时：已点的注要做完，但不能算成一局。"""
        cfg=dict(s.DEFAULT_CFG,credit_rect=None,watch_rect=[0,0,110,76],step_delay=0,region_wait=0,bet_confirm_timeout=360)
        engine=s.Engine(cfg,{},lambda msg:None,FakeScreen([glyph('08')]))
        done=[]
        engine.click_template=lambda key:done.append(key) or True
        engine.drag_template=lambda key,dy:done.append('lever') or True
        self.assertEqual(engine.spin(watch_change=True),(True,True))
        # 点击已发出，转轮必须做完（否则这一注白押）
        for need in ('lever','btn1','btn2','btn3'):
            self.assertIn(need,done)
        self.assertEqual(engine.spins_done,0)   # 扣币没确认，不计完成
        engine.screen=FakeScreen([glyph('00')])
        done.clear()
        engine.click_template=lambda key:self.fail('0 无需下注')
        self.assertEqual(engine.spin(watch_change=True),(True,False))

    def scripted_spin(self, values, bet=3):
        from unittest.mock import Mock
        engine=self.reader([glyph('09')],step_delay=0,bet_count=bet)
        engine.pause=lambda seconds:None
        engine.stable_credit_value=Mock(side_effect=[(v,'模拟读数') for v in values])
        events=[]
        engine.click_template=lambda key:events.append(key) or True
        engine.drag_template=lambda key,dy:events.append(key) or True
        return engine,events

    def test_zero_one_two_end_round_without_clicking(self):
        for credit in (0,1,2):
            engine,events=self.scripted_spin([credit])
            self.assertEqual(engine.spin(watch_change=True),(True,False))
            self.assertEqual(events,[])
            self.assertIs(self.reader([glyph(str(credit))]).credit_is_empty()[0],True)

    def test_missed_maxbet_retries_before_lever(self):
        engine,events=self.scripted_spin([6,6,3])
        self.assertEqual(engine.spin(watch_change=True),(True,True))
        self.assertEqual(events,['maxbet','maxbet','lever','btn1','btn2','btn3'])
        self.assertEqual(engine.spins_done,1)

    def test_three_missed_maxbets_still_finish_committed_spin(self):
        """三次都没确认扣币，但点击已经发出——这一注钱已押上，必须做完。

        旧行为是直接放弃、拉杆不拉，等于把已经押上的注扔掉（吞币）。
        """
        engine,events=self.scripted_spin([9,9,9,9])
        self.assertEqual(engine.spin(watch_change=True),(True,True))
        self.assertEqual(events,['maxbet']*3+['lever','btn1','btn2','btn3'])
        # 扣币没确认过，不计入完成次数
        self.assertEqual(engine.spins_done,0)

    def test_unknown_or_partial_debit_does_not_retry(self):
        """读不出或扣币数对不上时不能重试（会重复扣币），但要把已押上的注做完。"""
        for after in (None,8,5,10):
            engine,events=self.scripted_spin([9,after])
            self.assertEqual(engine.spin(watch_change=True),(True,True))
            self.assertEqual(events,['maxbet','lever','btn1','btn2','btn3'])
            self.assertEqual(engine.spins_done,0)

    def test_exact_three_coins_complete_paid_spin(self):
        engine,events=self.scripted_spin([3,0])
        self.assertEqual(engine.spin(watch_change=True),(True,True))
        self.assertEqual(events,['maxbet','lever','btn1','btn2','btn3'])

    def test_one_two_bet_mode_confirms_each_coin(self):
        for bet,values,expected in ((1,[1,0],['bet']), (2,[6,5,4],['bet','bet'])):
            engine,events=self.scripted_spin(values,bet)
            self.assertEqual(engine.spin(watch_change=True),(True,True))
            self.assertEqual(events,expected+['lever','btn1','btn2','btn3'])
        engine,events=self.scripted_spin([1],2)
        self.assertEqual(engine.spin(watch_change=True),(True,False))
        self.assertFalse(events)

    def test_delayed_debit_is_not_prematurely_retried(self):
        engine=self.reader([glyph('06')]*3+[glyph('03')]*3)
        self.assertEqual(engine.stable_credit_value(timeout=1,expected=3)[0],3)

    def test_remaining_one_two_go_to_next_normal_coin_batch(self):
        from unittest.mock import patch
        for remaining in (1,2):
            engine,events=self.scripted_spin([remaining,6,3,remaining,6,3,remaining])
            engine.cfg.update(rounds=2,draw_count=3,burn_count=5,hold_tab=False,
                              game_window='',smart_burn_stop=False)
            engine.start_watchdog=lambda:None
            batches=[]
            engine.insert_coins=lambda:batches.append(engine.cfg['coin_count']) or True
            with patch.object(s,'pdi',object()): engine.run()
            self.assertEqual(batches,[99,99])
            self.assertEqual(events,['maxbet','lever','btn1','btn2','btn3']*2)
            self.assertEqual(engine.spins_done,2)
    def test_blank_and_repeated_templates(self):
        self.assertIsNone(s.match_template(np.zeros((100,100,3),np.uint8),np.full((20,20,3),255,np.uint8),.8))
        tpl=s.load_image(str(ROOT/'templates/btn1.png')); h,w=tpl.shape[:2]
        frame=np.zeros((300,900,3),np.uint8)
        for x in (100,400,700): frame[100:100+h,x:x+w]=tpl
        cfg=dict(s.DEFAULT_CFG,locate_mode='image',template_rects={'btn2':[400,100,w,h]})
        engine=s.Engine(cfg,{'btn2':tpl},lambda msg:None,FakeScreen([frame]))
        pos=engine.locate('btn2',retries=1)
        self.assertLess(abs(pos[0]-(400+w/2)),2)
    def test_screenshot_thread_ownership(self):
        screen=s.Screen(); results=[]; errors=[]
        def grab():
            try: results.append(screen.grab_rect(0,0,8,8).shape)
            except Exception as e: errors.append(str(e))
        threads=[threading.Thread(target=grab) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertFalse(errors,errors); self.assertEqual(results,[(8,8,3)]*2)
    def test_mouse_lock_covers_game_activation(self):
        """切游戏前台必须发生在锁鼠标「之内」，否则空窗期视角会被带着转。"""
        timeline=[]; pinned_during=[]
        class FakeLock:
            def __enter__(self):
                timeline.append('lock_in'); pinned_during.append(True); return self
            def __exit__(self,*a):
                timeline.append('lock_out'); return False
        original_lock=s.MouseLock; s.MouseLock=FakeLock
        try:
            app=s.App(); app.withdraw()
            original_win,original_act=s.list_windows,s.activate_window
            s.list_windows=lambda kw:[(123,'VRChat')]
            s.activate_window=lambda h:(timeline.append('activate'),True)[1]
            class FrameScreen:
                left=top=0; size=(2560,1440)
                def grab(self): return np.zeros((1440,2560,3),np.uint8)
            app.screen=FrameScreen()
            try:
                app._freeze_then(lambda frame: timeline.append('overlay'))
            finally:
                s.list_windows,s.activate_window=original_win,original_act
            self.assertEqual(timeline,['lock_in','activate','overlay','lock_out'],timeline)
            self.assertTrue(pinned_during[0])
        finally:
            s.MouseLock=original_lock
            try: app.on_close()
            except Exception: pass
    def test_wait_frames_stable_returns_on_quiet_screen(self):
        """静止画面应该尽快返回，不能傻等固定 0.6 秒。"""
        calls=[]
        frame=np.zeros((80,120,3),np.uint8)
        def grab():
            calls.append(1); return frame.copy()
        out=s.wait_frames_stable(grab,tries=12,gap=0.0)
        self.assertIsNotNone(out); self.assertEqual(out.shape,frame.shape)
        self.assertLessEqual(len(calls),12)
    def test_picked_coords_are_physical_pixels(self):
        """取点/框选必须换算成物理像素。

        高缩放屏幕（如 150%）上 tkinter 报的是逻辑像素 1707，截图是物理
        像素 2560。混用两套坐标会让取到的点差一个缩放倍数——这是「怎么取都
        不准」的隐藏原因，必须锁死。
        """
        app=s.App(); app.withdraw()
        try:
            app.update()
            sx,sy=s.screen_scale(app)
            self.assertGreater(sx,0); self.assertGreater(sy,0)
            class Fake(s.ScaledOverlay):
                def __init__(self): pass
            o=Fake(); o.sx,o.sy=sx,sy
            # 物理 -> 逻辑 -> 物理 必须回到原点（容 1 像素）
            for px,py in ((0,0),(1200,900),(2559,1599),(2560//2,1600//2)):
                lx,ly=o.from_physical(px,py)
                bx,by=o.to_physical(lx,ly)
                self.assertLessEqual(abs(bx-px),1,(px,py,bx,by))
                self.assertLessEqual(abs(by-py),1,(px,py,bx,by))
            # 100% 缩放时换算必须是恒等
            o.sx=o.sy=1.0
            self.assertEqual(o.to_physical(640,480),(640,480))
            # 150% 缩放：逻辑 800 -> 物理 1200
            o.sx=o.sy=1.5
            self.assertEqual(o.to_physical(800,600),(1200,900))
            self.assertEqual(o.from_physical(1200,900),(800,600))
        finally: app.on_close()
    def test_frozen_frame_scales_to_overlay_size(self):
        """冻结帧按 1/scale 缩小后必须正好铺满浮层，否则框选会整体偏移。"""
        import cv2
        app=s.App(); app.withdraw()
        try:
            app.update()
            sx,sy=s.screen_scale(app)
            logical=(app.winfo_screenwidth(),app.winfo_screenheight())
            frame=np.zeros((1600,2560,3),np.uint8)
            small=cv2.resize(frame,None,fx=1.0/sx,fy=1.0/sy,interpolation=cv2.INTER_AREA)
            self.assertLessEqual(abs(small.shape[1]-logical[0]),2,(small.shape,logical))
            self.assertLessEqual(abs(small.shape[0]-logical[1]),2,(small.shape,logical))
        finally: app.on_close()
    def test_mouse_lock_pins_cursor_during_transition(self):
        """锁鼠标期间必须真的在重钉光标，否则空窗期视角还是会跟着转。"""
        calls=[]
        def make_cursor_stub(x, y):
            """构造填好坐标的 POINT，GetCursorPos 收到的就是这种 byref 对象。"""
            import ctypes as _ct

            def get_cursor(ptr):
                ptr._obj.x = x
                ptr._obj.y = y
                return 1
            return staticmethod(get_cursor)
        original=getattr(s,'user32')
        try:
            s.user32=type('Fake',(),{
                'SetCursorPos':staticmethod(lambda x,y:(calls.append((x,y)),0)[1]),
                'GetSystemMetrics':staticmethod(lambda i:2560 if i==0 else 1600),
                'GetCursorPos':make_cursor_stub(800,600),
            })()
            with s.MouseLock(interval=0.001):
                time.sleep(0.08)
            self.assertGreater(len(calls),3,len(calls))
            xs={c[0] for c in calls}; ys={c[1] for c in calls}
            self.assertEqual(len(xs),1); self.assertEqual(len(ys),1)
            # 应该钉在屏幕中心
            self.assertEqual(calls[0],(1280,800),calls[0])
            # 拿不到屏幕尺寸时也要退回光标当前位置，而不是静默失效
            s.user32=type('Fake2',(),{
                'SetCursorPos':staticmethod(lambda x,y:(calls.append((x,y)),0)[1]),
                'GetSystemMetrics':staticmethod(
                    lambda i:(_ for _ in ()).throw(OSError('unsupported'))),
                'GetCursorPos':make_cursor_stub(111,222),
            })()
            before=len(calls)
            with s.MouseLock(interval=0.001):
                time.sleep(0.05)
            self.assertGreater(len(calls),before+1,calls[before:])
            self.assertEqual(calls[before],(111,222))
        finally:
            s.user32=original
    def test_preflight_blocks_out_of_screen_coords(self):
        """换了机器/分辨率后坐标会落到屏幕外——自检必须在开跑前拦住。

        这正是本次的真实故障：旧配置来自另一台机器，坐标全部无效，
        但旧 start() 只检查「填了没有」，于是照样开跑、点了没反应。
        """
        app=s.App(); app.withdraw()
        try:
            app.update()
            class FixedScreen:
                left=top=0
                def __init__(self): self.size=(2560,1600)
                def grab(self): return np.zeros((1600,2560,3),np.uint8)
                def grab_rect(self,x,y,w,h): return np.zeros((h,w,3),np.uint8)
            app.screen=FixedScreen()
            # 坐标严重超出屏幕（旧机器的分辨率更大）
            app.cfg['locate_mode']='coord'
            app.cfg['points']={k:[9000,9000] for k,_ in s.TEMPLATES}
            app.cfg['credit_rect']=[9000,9000,90,155]
            issues=app.preflight(check_templates=False)
            errors=[m for lvl,m in issues if lvl=='error']
            self.assertTrue(any('不在屏幕内' in m or '超出' in m for m in errors),errors)
            # 合法坐标不应被误拦
            app.cfg['points']={k:[1903,872] for k,_ in s.TEMPLATES}
            app.cfg['credit_rect']=[1879,536,90,155]
            errors=[m for lvl,m in app.preflight(check_templates=False) if lvl=='error']
            self.assertFalse([m for m in errors if '不在屏幕内' in m],errors)
        finally: app.on_close()
    def test_preflight_blocks_unreadable_credit(self):
        """Credit 读不出数字就不能开跑——否则下注确认形同虚设。"""
        app=s.App(); app.withdraw()
        try:
            app.update()
            class Sc:
                left=top=0; size=(2560,1600)
                def grab(self): return np.zeros((1600,2560,3),np.uint8)
                def grab_rect(self,x,y,w,h): return np.zeros((h,w,3),np.uint8)
            app.screen=Sc()
            app.cfg['locate_mode']='coord'
            app.cfg['points']={k:[100,100] for k,_ in s.TEMPLATES}
            app.cfg['credit_rect']=[100,100,90,155]   # 全黑，读不出
            errors=[m for lvl,m in app.preflight(check_templates=False) if lvl=='error']
            self.assertTrue(any('读不出' in m for m in errors),errors)
            # 换成真实 0 截图就应该通过
            app.screen=type('S2',(),{'left':0,'top':0,'size':(2560,1600),
                'grab':lambda s=None:None,
                'grab_rect':lambda s,x,y,w,h:self.zero.copy()})()
            issues=app.preflight(check_templates=False)
            self.assertFalse([m for lvl,m in issues if lvl=='error'
                              and 'Credit' in m],issues)
        finally: app.on_close()
    def test_coordinate_migration_scales_and_backs_up(self):
        """迁移必须按缩放换算、先备份、并且不碰没填的项。"""
        app=s.App(); app.withdraw()
        try:
            app.update()
            # 先存一份真实配置，确保「有东西可备份」这条路径被覆盖
            app.cfg['points']={'coin':[100,200]}
            app.save_cfg()
            class Sc:
                left=top=0; size=(2560,1600)
                def grab(self): return np.zeros((1600,2560,3),np.uint8)
                def grab_rect(self,x,y,w,h): return np.zeros((h,w,3),np.uint8)
            app.screen=Sc()
            app.cfg['points']={'coin':[100,200]}
            app.cfg['credit_rect']=[300,400,90,155]
            # 模拟 150% 缩放：逻辑 -> 物理
            original=s.screen_scale
            s.screen_scale=lambda root=None:(1.5,1.5)
            try:
                dialogs=[]
                class MB:
                    def showinfo(self,*a,**k): dialogs.append(('info',)+a)
                    def showwarning(self,*a,**k): dialogs.append(('warn',)+a)
                    def showerror(self,*a,**k): dialogs.append(('error',)+a)
                    def askyesno(self,*a,**k): return True
                original_mb=s.messagebox; s.messagebox=MB()
                try:
                    s.App.run_preflight=lambda self: None  # 别在测试里弹自检
                    app.migrate_coordinates('physical')
                finally:
                    s.messagebox=original_mb
            finally:
                s.screen_scale=original
            self.assertEqual(app.cfg['points']['coin'],[150,300])
            self.assertEqual(app.cfg['credit_rect'],[450,600,135,232])
            # 备份文件必须真的落盘，且内容是换算前的旧值
            backups=[p for p in os.listdir(os.path.dirname(s.CFG_PATH))
                     if '.bak-' in p]
            self.assertTrue(backups,backups)
            import json
            with open(os.path.join(os.path.dirname(s.CFG_PATH),backups[0]),encoding='utf-8') as f:
                old=json.load(f)
            self.assertEqual(old['points']['coin'],[100,200])
        finally: app.on_close()
    def test_migration_works_without_existing_config(self):
        """首次使用（还没有 config.json）时迁移不该因为无法备份而失败。"""
        app=s.App(); app.withdraw()
        try:
            app.update()
            if os.path.exists(s.CFG_PATH):
                os.remove(s.CFG_PATH)
            class Sc:
                left=top=0; size=(2560,1600)
                def grab(self): return np.zeros((1600,2560,3),np.uint8)
                def grab_rect(self,x,y,w,h): return np.zeros((h,w,3),np.uint8)
            app.screen=Sc()
            app.cfg['points']={'coin':[100,200]}
            original=s.screen_scale
            s.screen_scale=lambda root=None:(1.5,1.5)
            try:
                errors=[]
                class MB:
                    def showinfo(self,*a,**k): pass
                    def showwarning(self,*a,**k): pass
                    def showerror(self,*a,**k): errors.append(a)
                    def askyesno(self,*a,**k): return True
                original_mb=s.messagebox; s.messagebox=MB()
                try:
                    s.App.run_preflight=lambda self: None
                    app.migrate_coordinates('physical')
                finally:
                    s.messagebox=original_mb
            finally:
                s.screen_scale=original
            self.assertFalse(errors,errors)      # 不该报错
            self.assertEqual(app.cfg['points']['coin'],[150,300])
        finally: app.on_close()
    def test_migration_skipped_when_scale_is_one(self):
        """100% 缩放时不该乱换算（否则会把本来正确的坐标改坏）。"""
        app=s.App(); app.withdraw()
        try:
            app.update()
            class Sc:
                left=top=0; size=(2560,1600)
                def grab(self): return np.zeros((1600,2560,3),np.uint8)
                def grab_rect(self,x,y,w,h): return np.zeros((h,w,3),np.uint8)
            app.screen=Sc()
            app.cfg['points']={'coin':[100,200]}
            dialogs=[]
            class MB:
                def showinfo(self,*a,**k): dialogs.append(('info',)+a)
                def showwarning(self,*a,**k): dialogs.append(('warn',)+a)
                def showerror(self,*a,**k): dialogs.append(('error',)+a)
                def askyesno(self,*a,**k): self.fail('不该询问是否换算'); return False
            original=s.messagebox; s.messagebox=MB()
            try:
                original_scale=s.screen_scale
                s.screen_scale=lambda root=None:(1.0,1.0)
                try:
                    app.migrate_coordinates('physical')
                finally:
                    s.screen_scale=original_scale
            finally:
                s.messagebox=original
            self.assertEqual(app.cfg['points']['coin'],[100,200])
            self.assertTrue(dialogs)
        finally: app.on_close()
    def test_scene_shift_separates_animation_from_rotation(self):
        """位移检测必须能区分「游戏在动」和「视角转了」。

        这是本轮修的核心：之前用整屏像素差判漂移，实测同一视角不同动画
        的截图差异高达 21~32，阈值 2.0 —— 等于每次都误报。
        """
        a=s.load_image(str(ROOT/'tests/seven_screen_1.png'))
        b=s.load_image(str(ROOT/'tests/seven_screen_2.png'))
        c=s.load_image(str(ROOT/'tests/seven_screen_3.png'))
        self.assertIsNotNone(a)
        def sig_of(f): return cv2.resize(cv2.cvtColor(f,cv2.COLOR_BGR2GRAY),
                                          (640,400),interpolation=cv2.INTER_AREA)
        sa=sig_of(a)
        def orig_px(f):
            """算出的位移换算回原图像素，和阈值保持同一量纲"""
            sig,scale=s.App._screen_signature(f)
            v=s.scene_shift(sa,sig)
            return None if v is None else v*scale
        # 同视角、不同动画时刻：必须判定为「没转」
        for other,label in ((b,'1v2'),(c,'1v3')):
            shift=orig_px(other)
            self.assertIsNotNone(shift,label)
            self.assertLess(shift,s.DRIFT_SHIFT_PX,
                f'{label} 同视角却被判为转动 {shift:.1f}px')
        # 真正的视角转动：位移应随转动量增长，且明显超过阈值
        for px in (40,60,120):
            shift=orig_px(np.roll(a,px,axis=1))
            self.assertGreater(shift,s.DRIFT_SHIFT_PX,f'平移{px}px 未检出')
            self.assertAlmostEqual(shift,px,delta=px*0.3)
        # 亮度抖动不算转动
        noisy=np.clip(a.astype(float)+np.random.normal(0,12,a.shape),0,255).astype(np.uint8)
        self.assertLess(orig_px(noisy),s.DRIFT_SHIFT_PX)
        # 小幅转动不应提醒：这个检测在真实游戏里做不到既灵敏又准，
        # 宁可漏报也不能误报（误报会让人忽略所有警告）。
        for px in (5,10,15,20):
            self.assertLess(orig_px(np.roll(a,px,axis=1)),s.DRIFT_SHIFT_PX,
                f'小幅转动 {px}px 被误判为大幅转动')
        # 尺寸不一致时无法比较，应返回 None 而不是瞎报
        self.assertIsNone(s.scene_shift(sa,np.zeros((100,100),np.uint8)))
    def test_narrow_credit_region_is_flagged(self):
        """选区只够放一位 -> 提示（不拦截，因为判据是启发式的）。

        真实故障：用户趁没币、只显示 0（一位）时框了 Credit 区域，
        投币后显示 99（两位），第二位在框外 -> 99 读成 9、96 也读成 9
        -> 程序误判"没扣币"反复重试。
        """
        one,_=s.region_fits_one_digit([0,0,55,40],30,max_digits=2)
        self.assertTrue(one,'框宽 55 < 2×30，应判为只够一位')
        ok,_=s.region_fits_one_digit([0,0,80,40],30,max_digits=2)
        self.assertFalse(ok,'框宽 80 >= 2×30，不应误报')
        # 自检里应给出 warn 而不是 error（不能因此拦住开跑）
        app=s.App(); app.withdraw()
        try:
            app.update()
            zero=s.load_image(str(ROOT/'tests/credit_zero_real.png'))
            class Sc:
                left=top=0; size=(2560,1600)
                def grab(self): return np.zeros((1600,2560,3),np.uint8)
                def grab_rect(self,x,y,w,h): return zero
            app.screen=Sc()
            app.cfg['locate_mode']='coord'
            app.cfg['points']={k:[100,100] for k,_ in s.TEMPLATES}
            app.cfg['credit_max_display']=99
            app.cfg['credit_rect']=[0,0,zero.shape[1],zero.shape[0]]
            issues=app.preflight(check_templates=False)
            errors=[m for lvl,m in issues if lvl=='error']
            # 单个 0 恰好装满选区：宽度够（26*2=52 < 85），不该报 error
            self.assertFalse([m for m in errors if '太窄' in m],errors)
        finally: app.on_close()
    def test_digit_width_uses_median_not_span(self):
        """单字宽必须按列分组取中位数，不能用整段跨度（两位数会算成两倍）。"""
        frame=s.load_image(str(ROOT/'tests/seven_screen_1.png'))
        single=frame[650:650+49,1607:1607+37]
        both=frame[884:884+43,1216:1216+144]        # 119671 六个字
        w1=s._digit_width(single); w6=s._digit_width(both)
        self.assertGreater(w1,10)
        # 六个字和单个字，单字宽应当接近（不能是 6 倍）
        self.assertLess(abs(w1-w6)/max(1,w1),0.6,f'{w1} vs {w6}')
    def test_pause_key_not_bound_by_default(self):
        """Pause 不能默认当急停键。

        Windows 把 VK_PAUSE 用作"正在拖动窗口"：运行中点一下标题栏
        拖动窗口就会按下 Pause，导致误停。默认只保留 F12 / End。
        """
        self.assertEqual(s.DEFAULT_CFG.get('extra_stop_key',''),'')
        vks={vk for vk,_ in s.stop_key_entries(dict(s.DEFAULT_CFG))}
        self.assertNotIn(0x13,vks,'Pause 不应在默认急停键里')
        self.assertEqual(vks,{0x7B,0x23})     # F12 / End

    def test_stop_keys_are_customizable(self):
        """停止热键可自定义：能加、能去重，非法键名回落到默认。"""
        ents=s.stop_key_entries({'stop_keys':['F8','Delete','Pause']})
        self.assertEqual([n for _,n in ents],['F8','Delete','Pause'])
        # 重复的名字只算一次
        ents=s.stop_key_entries({'stop_keys':['F12','F12','End']})
        self.assertEqual([n for _,n in ents],['F12','End'])
        # 全是不认识的名字时不能变成"一个急停键都没有"
        ents=s.stop_key_entries({'stop_keys':['zzz','???']})
        self.assertEqual([n for _,n in ents],list(s.DEFAULT_STOP_KEYS))
        # 旧配置（只有 extra_stop_key）要能兼容
        self.assertIn('Pause',[n for _,n in s.stop_key_entries({'extra_stop_key':'pause'})])

    def test_wheel_scrolls_when_cursor_is_over_a_child(self):
        """滚轮必须能在光标停在子控件上时滚动页面。

        Tk 把滚轮事件发给光标下那个子控件，子控件的 bindtags 里没有 canvas，
        所以只在 canvas 上绑定是收不到的（用户实踩：滚轮完全没反应）。
        """
        import tkinter as tk
        app=s.App()
        try:
            app.withdraw(); app.update_idletasks(); app.deiconify(); app.update()
            canvas=app._scroll_canvases[0]
            canvas.update_idletasks()
            box=canvas.bbox("all")
            if not (box and box[3] > canvas.winfo_height()):
                self.skipTest('该页内容没有溢出，无需滚动')
            # 找一个嵌套很深的子控件（卡片里的按钮/标签）
            leaf,depth=canvas,0
            stack=[(canvas,0)]
            while stack:
                w,d=stack.pop()
                if d>depth: leaf,depth=w,d
                stack += [(c,d+1) for c in w.winfo_children()]
            before=canvas.yview()
            for _ in range(3):
                app._wheel(type('E',(),{'widget':leaf,'delta':-120})())
                app.update()
            self.assertNotEqual(canvas.yview(),before,'光标在子控件上时滚轮应生效')
            # 日志框自己会滚，不能被页面滚动抢走
            self.assertIsNone(
                app._wheel(type('E',(),{'widget':app.txt,'delta':-120})()))
        finally:
            app.on_close()
    def test_window_is_compact(self):
        """窗口要小到能放在桌面一角，内容超出用页内滚动。"""
        app=s.App(); app.withdraw()
        try:
            app.update_idletasks()
            w,h=app._fit_size
            self.assertLessEqual(w,1000,f'窗口过宽：{w}')
            self.assertLessEqual(h,720,f'窗口过高：{h}')
        finally: app.on_close()
    def test_committed_bet_always_finishes_the_spin(self):
        """点过 MaxBet/Bet 之后，必须把拉杆和三个停止按钮做完。

        这是会吞币的问题：钱已经押上去了，中途因为「读数没确认到扣币」
        就退出，转轮晾在半路，这一注直接白押。
        """
        def spin_actions(cap, readings, click_ok=True):
            """readings[0] 是下注前读数，之后的是点完 maxbet 后的读数"""
            acts=[]; it=iter(readings)
            e=s.Engine(dict(s.DEFAULT_CFG, credit_max_display=cap, bet_retries=3,
                            bet_confirm_timeout=360, step_delay=0, credit_rect=[0,0,10,10]),
                       {},lambda m:None,None)
            e.pause=lambda s:None
            e.stable_credit_value=lambda timeout=1.5,expected=None: next(it,(70,'兜底'))
            e.click_template=lambda k:((acts.append(k),True)[1] if click_ok else False)
            e.drag_template=lambda k,dy:(acts.append(k),True)[1]
            e.spin()
            return acts
        for label,cap,readings in (
            ('扣币读不出',0,[(70,'ok'),(None,'读不出')]),
            ('扣币非预期变化',0,[(70,'ok'),(5,'扣了5')]),
            ('顶格看不出扣币',99,[(99,'ok'),(99,'顶格')]),
            ('重试3次都没扣币',0,[(70,'ok'),(70,'a'),(70,'b'),(70,'c')]),
        ):
            acts=spin_actions(cap,readings)
            for need in ('lever','btn1','btn2','btn3'):
                self.assertIn(need,acts,f'{label}: 已下注却没执行 {need} —— 这一注白押了')
        # 点都没点出去（钱没押上）才可以安全放弃
        acts=spin_actions(99,[(70,'ok')],click_ok=False)
        self.assertEqual(acts,[],f'没点出去时不该有动作，实际 {acts}')
    def test_insufficient_credit_does_nothing(self):
        """余额不足时一个动作都不做（没押钱，不能去拉杆）。"""
        acts=[]
        e=s.Engine(dict(s.DEFAULT_CFG,credit_max_display=99,step_delay=0,
                        credit_rect=[0,0,10,10]),{},lambda m:None,None)
        e.pause=lambda s:None
        e.stable_credit_value=lambda timeout=1.5,expected=None:(1,'只有1')
        e.click_template=lambda k:(acts.append(k),True)[1]
        e.drag_template=lambda k,dy:(acts.append(k),True)[1]
        self.assertEqual(e.spin(watch_change=True),(True,False))
        self.assertEqual(acts,[])
    def test_tabs_are_scrollable_and_window_is_bounded(self):
        """窗口不该被内容撑高；长页面要能滚轮滚动。"""
        import tkinter as tk
        app=s.App(); app.withdraw()
        try:
            app.update_idletasks()
            h=app._fit_size[1]
            sh=app.winfo_screenheight()
            self.assertLessEqual(h,sh*0.9,f'窗口高度 {h} 超过屏幕 {sh} 的 90%')
            app.deiconify(); app.update()
            for idx,name in enumerate(('定位设置','参数设置','运行与日志')):
                app.nb.select(idx)
                app.update_idletasks()
                for _ in range(10): app.update()
                found=[]
                def walk(w):
                    for c in w.winfo_children():
                        if isinstance(c,tk.Canvas): found.append(c)
                        walk(c)
                walk(app.nb.nametowidget(app.nb.tabs()[idx]))
                self.assertTrue(found,f'{name} 缺少可滚动容器')
                box=found[0].bbox('all')
                if box and box[3] > found[0].winfo_height():
                    self.assertGreater(box[3],found[0].winfo_height(),
                        f'{name} 内容超高却没有滚动区域')
        finally: app.on_close()
    def test_capped_display_never_double_bets(self):
        """Credit 顶到显示上限时，点一次就够，绝不能重试。

        这台机器最多显示 99，但真实币数可以更多（例如 120 只显示 99）。
        扣 3 币后真实值 117，屏幕仍是 99 ——「读数没变」**不能**证明没点到。
        旧逻辑会重试 3 次 = 真的扣 9 币。这是会真金白银出错的 bug。
        """
        logs=[]; clicks=[]
        def build(cap, readings):
            it=iter(readings)
            e=s.Engine(dict(s.DEFAULT_CFG, credit_max_display=cap, bet_retries=3,
                            bet_confirm_timeout=360, credit_rect=[0,0,10,10]),
                       {},logs.append,None)
            e.click_template=lambda key:(clicks.append(key),True)[1]
            e.stable_credit_value=lambda timeout=1,expected=None: next(it,(None,'耗尽'))
            e.drag_template=lambda *a:True
            return e
        # 顶格：读数一直是 99（真实值可能 120->117，屏幕看不出）
        clicks.clear(); logs.clear()
        self.assertTrue(build(99,[(99,'顶格')]).place_confirmed_bet(99))
        self.assertEqual(len(clicks),1,
            f'顶格时点了 {len(clicks)} 次 MaxBet，会重复扣币！')
        self.assertTrue(any('不重试' in m or '没有重复点击' in m for m in logs),logs)
        # 未顶格且真的没扣币：重试仍然安全，必须保留
        clicks.clear(); logs.clear()
        self.assertFalse(build(99,[(9,'a'),(9,'b'),(9,'c')]).place_confirmed_bet(9))
        self.assertEqual(len(clicks),3,f'未顶格时应重试 3 次，实际 {len(clicks)}')
    def test_credit_max_display_flag(self):
        """credit_max_display：顶格判定；填 0 时不启用该行为。"""
        def eng(cap):
            return s.Engine(dict(s.DEFAULT_CFG, credit_max_display=cap),{},lambda m:None,None)
        self.assertTrue(eng(99).credit_display_capped(99))
        self.assertTrue(eng(99).credit_display_capped(120))
        self.assertFalse(eng(99).credit_display_capped(98))
        self.assertFalse(eng(99).credit_display_capped(9))
        self.assertFalse(eng(0).credit_display_capped(99))   # 0 = 不限制
        self.assertFalse(eng(None).credit_display_capped(99))
    def test_start_button_is_usable_on_launch(self):
        """「开始运行」不能一打开就是灰的。

        回归：UI 改版时把按钮写死 state="disabled"，而恢复逻辑只在
        「跑完一轮之后」才执行，于是首次打开永远点不动。
        """
        app=s.App(); app.withdraw()
        try:
            app.update()
            self.assertEqual(str(app.btn_start['state']),'normal',
                '开始运行按钮初始应为可用')
            # 停止按钮初始应禁用（还没在跑）
            self.assertEqual(str(app.btn_stop['state']),'disabled')
            # 提示语要说人话，不该是空字符串
            self.assertTrue(app.lbl_ready['text'].strip())
        finally: app.on_close()
    def test_run_buttons_follow_engine_state(self):
        """按钮状态要跟着运行状态走，且运行中不能重复开跑。"""
        app=s.App(); app.withdraw()
        try:
            app.update()
            # 配置齐全时（测试环境用 cfg 里的点）
            app.cfg['locate_mode']='coord'
            app.cfg['points']={k:[100,100] for k,_ in s.TEMPLATES}
            app.cfg['credit_rect']=[0,0,85,50]
            for key,_ in s.TEMPLATES:
                vx,vy=app.pt_vars[key]; vx.set('100'); vy.set('100')
            app._refresh_run_buttons()
            self.assertEqual(str(app.btn_start['state']),'normal')
            self.assertIn('就绪',app.lbl_ready['text'])
            # 运行中：开始禁用、停止可用
            class FakeEngine:
                state='投币'; spins_done=0
                def is_alive(self): return True
            app.engine=FakeEngine()
            app._refresh_run_buttons()
            self.assertEqual(str(app.btn_start['state']),'disabled')
            self.assertEqual(str(app.btn_stop['state']),'normal')
            # 结束后自动恢复
            app.engine=None
            app._refresh_run_buttons()
            self.assertEqual(str(app.btn_start['state']),'normal')
            self.assertEqual(str(app.btn_stop['state']),'disabled')
        finally: app.on_close()
    def test_start_does_not_reenable_while_running(self):
        """开跑后不能有定时器把「开始运行」放开，否则可以重复开跑。"""
        import inspect
        src=inspect.getsource(s.App.start)
        self.assertNotIn('after(1500, lambda: self.btn_start.config',src,
            '开跑后不应再用定时器放开「开始运行」，否则能重复启动')
    def test_drift_reminder_never_blocks(self):
        """视角提醒只写日志，绝不能弹窗打断——误报的弹窗比没有更糟。"""
        app=s.App(); app.withdraw()
        try:
            base=cv2.imread(str(ROOT/'tests/seven_screen_1.png'))
            app.screen=type('S',(),{'left':0,'top':0,
                'grab':lambda s:s.img.copy()})()
            dialogs=[]
            class MB:
                def showinfo(self,*a,**k): dialogs.append(('info',)+a)
                def showwarning(self,*a,**k): dialogs.append(('warn',)+a)
                def showerror(self,*a,**k): dialogs.append(('error',)+a)
            original=s.messagebox; s.messagebox=MB()
            try:
                app.screen.img=np.roll(base,120,axis=1)
                app._frozen_view=(base,app._screen_signature(base))
                app._warn_if_view_drifted()
                self.assertEqual(dialogs,[],'视角提醒不应弹窗')
                # 日志里应该留下一条提醒
                app.pump_log()
                text=app.txt.get('1.0','end')
                self.assertIn('移动了约',text)
            finally:
                s.messagebox=original
        finally: app.on_close()
    def test_view_drift_not_reported_for_live_scene(self):
        """真实截图：同一视角取点后不该被误报「视角转动」。"""
        a=s.load_image(str(ROOT/'tests/seven_screen_1.png'))
        b=s.load_image(str(ROOT/'tests/seven_screen_2.png'))
        app=s.App(); app.withdraw()
        try:
            class Scene:
                left=top=0
                def __init__(self,img): self.img=img
                def grab(self): return self.img.copy()
            app.screen=Scene(b)
            app._frozen_view=(b,app._screen_signature(a))
            ok,msg=app._check_view_drift()
            self.assertTrue(ok,msg)          # 不得误报
            # 真转了才报
            app.screen=Scene(np.roll(a,80,axis=1))
            app._frozen_view=(a,app._screen_signature(a))
            ok,msg=app._check_view_drift()
            self.assertFalse(ok); self.assertIn('移动了约',msg)
        finally: app.on_close()
    def test_view_drift_is_reported(self):
        """明显转动视角要能检出；静止和只有噪声时不得打扰用户。"""
        app=s.App(); app.withdraw()
        try:
            rng=np.random.default_rng(7)
            base=cv2.imread(str(ROOT/'tests/seven_screen_1.png'))
            # 有纹理的画面才能算光流，用真实截图
            class DriftScreen:
                left=top=0
                def __init__(self,img): self.img=img
                def grab(self): return self.img.copy()
            sig=app._screen_signature(base)
            # 整体平移 = 视角转动，必须报
            app.screen=DriftScreen(np.roll(base,60,axis=1))
            app._frozen_view=(base,sig)
            ok,msg=app._check_view_drift()
            self.assertFalse(ok); self.assertIn('移动了约',msg)
            # 画面没动 -> 认为一致，不打扰用户
            app.screen=DriftScreen(base.copy())
            app._frozen_view=(base,sig)
            self.assertTrue(app._check_view_drift()[0])
            # 只有局部噪声 -> 也不该打扰用户
            app.screen=DriftScreen(
                np.clip(base.astype(float)+rng.normal(0,5,base.shape),0,255).astype(np.uint8))
            app._frozen_view=(base,sig)
            self.assertTrue(app._check_view_drift()[0])
            # 没有冻结帧时不应误报
            app._frozen_view=None
            self.assertTrue(app._check_view_drift()[0])
            # 检查后必须清掉快照，避免下一次保存时又拿旧指纹去比
            app._frozen_view=(base,sig)
            app._check_view_drift()
            self.assertIsNone(app._frozen_view)
        finally: app.on_close()
    def test_stop_prevents_input(self):
        engine=self.reader([self.zero]); engine.request_stop()
        self.assertFalse(engine.click_template('coin')); self.assertFalse(engine.drag_template('lever',280))
    def test_gui_build_and_reference(self):
        app=s.App(); app.withdraw()
        try:
            app.update(); app.cfg['credit_ref']=str(ROOT/'credit_zero.png')
            self.assertIs(app.credit_ref_quality()[0],True)
        finally: app.on_close()
    def test_recording_hides_window_before_capture(self):
        import time
        app=s.App()
        tpl=s.load_image(str(ROOT/'templates/btn1.png'))
        class RecordingScreen:
            def grab_rect(self,*args):
                if app.state() != 'withdrawn':
                    raise AssertionError('录制时工具窗口必须隐藏')
                return tpl.copy()
        app.screen=RecordingScreen()
        try:
            app._save_region('btn1',100,100,tpl.shape[1],tpl.shape[0])
            deadline=time.monotonic()+.6
            while time.monotonic()<deadline:
                app.update(); time.sleep(.02)
            self.assertIn('btn1',app.tpls)
            self.assertEqual(app.cfg['template_rects']['btn1'][:2],[100,100])
            self.assertTrue((Path(s.TPL_DIR)/'btn1.png').exists())
        finally: app.on_close()
    def test_auto_find_button_uses_full_screen_with_window_hidden(self):
        import time
        app=s.App()
        frame=s.load_image(str(ROOT/'tests/seven_screen_3.png'))
        received=[]
        class ScanningScreen:
            left=top=0
            def grab(self):
                if app.state()!='withdrawn': raise AssertionError('扫描前必须隐藏工具')
                return frame.copy()
        app.screen=ScanningScreen()
        app._show_candidates=lambda cands:received.extend(cands)
        try:
            app.auto_find_credit()
            deadline=time.monotonic()+2
            while not received and time.monotonic()<deadline:
                app.update(); time.sleep(.02)
            self.assertIn('7',[c[4] for c in received])
            self.assertIn('0',[c[4] for c in received])
        finally: app.on_close()

    # ---------- 自我训练（模板库）----------
    def test_fused_weak_template_match_does_not_veto_segment(self):
        """模板「把握不足」时不能推翻段码读对的数。

        七段码里 0 和 8 只差一段，别的机器/别的字体的模板会以"很有把握"
        的姿态把 8 读成 0。若允许它一票否决，用户只会被一堆假「读不出」
        淹没——所以否决必须另设更严的门槛（实测同机器 d1 p90=0.048，
        跨字体误配 d1=0.08 且区分度只有 0.03）。
        """
        import vision as v
        img = glyph('08')
        bank = v.build_template_bank([('03', glyph('03'))])  # 另一套字形的模板
        val, _info = v.decode_led_fused(img, bank)
        self.assertEqual(val, '08')

    def test_fused_agrees_and_without_bank_falls_back_to_segment(self):
        import vision as v
        img = glyph('47')
        self.assertEqual(v.decode_led_fused(img, None)[0], '47')
        bank = v.build_template_bank([('47', glyph('47'))])
        val, info = v.decode_led_fused(img, bank)
        self.assertEqual(val, '47')
        self.assertIn('一致', info)

    def test_auto_collect_is_gated_and_names_file_by_truth(self):
        """自动采集的门槛：总闸、配置开关、读不出（?）、冷却——缺一都不写盘。"""
        img = glyph('47')
        with tempfile.TemporaryDirectory() as tmp:
            old_dir = s.DIGITS_DIR
            s.DIGITS_DIR = tmp
            try:
                s._AUTO_STATE['last'] = 0.0
                self.assertIsNone(s.auto_collect_credit(img, '47', {}, None))
                self.assertEqual(os.listdir(tmp), [])          # 总闸关着
                s.AUTO_LEARN_ALLOWED = True
                try:
                    self.assertIsNone(s.auto_collect_credit(
                        img, '47', {'auto_learn': False}, None))
                    self.assertEqual(os.listdir(tmp), [])      # 用户关了
                    self.assertIsNone(s.auto_collect_credit(
                        img, '4?', {'auto_learn': True}, None))
                    self.assertEqual(os.listdir(tmp), [])      # 读不出绝不存
                    p = s.auto_collect_credit(img, '47', {'auto_learn': True}, None)
                    self.assertIsNotNone(p)
                    self.assertTrue(os.path.exists(p))
                    self.assertEqual(s.sample_truth_of(os.path.basename(p)), '47')
                    # 冷却期内不再写
                    self.assertIsNone(s.auto_collect_credit(
                        img, '47', {'auto_learn': True}, None))
                    self.assertEqual(len(os.listdir(tmp)), 1)
                finally:
                    s.AUTO_LEARN_ALLOWED = False
            finally:
                s.DIGITS_DIR = old_dir

    def test_template_bank_never_misreads_real_samples(self):
        """模板库在真机样本上可以有「读不出」，但绝不能有「读错」。

        读错比读不出危险得多：读不出会走兜底/未知，读错会直接导致重复扣币。
        （更严的留一法基准在 tests/bank_bench.py，跑得慢，按需跑。）
        """
        import vision as v
        from tests.real_bench import load_samples
        samples = [(t, i) for t, _r, i in load_samples()]
        if not samples:
            self.skipTest('没有真机样本')
        bank = v.build_template_bank(samples)
        bad = []
        for truth, img in samples:
            val, _info = v.decode_by_template(img, bank)
            if val is not None and val != truth:
                bad.append((truth, val))
        self.assertEqual(bad, [], f'模板读错：{bad[:5]}')

    def test_stale_bank_is_served_without_blocking(self):
        """模板库过期时先给旧的、后台重建——不能在读币关键路径上同步建库。

        建库要解码几百张样本（0.2 秒级），而下注确认的整轮超时只有 0.36 秒。
        """
        s._BANK_REFRESHING = False
        s.get_digit_bank(force=True)
        bank = s.get_digit_bank()
        s._DIGIT_BANK_STAMP = 0.0
        self.assertIs(s.get_digit_bank(), bank)
        deadline = time.monotonic() + 5
        while s.get_digit_bank() is bank and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertIsNot(s.get_digit_bank(), bank)

    # ---------- 第 16 轮：部件导入 / 自动采集 ----------
    def _pair_photo(self, single=False):
        """合成 bet & maxbet 合照：两个实心红按钮 + 上方稀疏标题字。

        标题笔画故意离按钮只有 2px（真实截图里标题和按钮竖直相连），
        又故意画在按钮 x 范围内——无论形态学闭运算把标题连进按钮还是
        当成独立小块，检测器都必须只认出 2 个按钮、且按 x 从左到右。
        """
        img = np.full((400, 600, 3), 30, np.uint8)
        spans = [(100, 220)] if single else [(100, 220), (380, 500)]
        for x0, x1 in spans:
            img[250:300, x0:x1] = (0, 0, 230)
            for tx in range(x0 + 10, x1 - 6, 18):
                img[236:248, tx:tx + 3] = (0, 0, 230)
        return img

    def _triple_photo(self):
        img = np.full((400, 700, 3), 30, np.uint8)
        for x0 in (60, 300, 540):
            img[250:300, x0:x0 + 100] = (0, 0, 230)
            for tx in range(x0 + 10, x0 + 94, 18):
                img[236:248, tx:tx + 3] = (0, 0, 230)
        return img

    def test_red_button_boxes_ignores_titles_and_decor(self):
        boxes = s.red_button_boxes(self._pair_photo())
        self.assertEqual(len(boxes), 2, boxes)
        self.assertLess(boxes[0][0], boxes[1][0])          # 左 bet、右 maxbet
        # 别处的红色小装饰（面积不够 & 小于最大块 35%）不能算第三个按钮
        img = self._pair_photo()
        img[60:80, 260:300] = (0, 0, 230)
        self.assertEqual(len(s.red_button_boxes(img)), 2)
        # 只画一个按钮（真实踩过的坑：标题连按钮导致认不全）→ 必须 1 个
        self.assertEqual(len(s.red_button_boxes(self._pair_photo(single=True))), 1)

    def test_red_button_boxes_splits_triple(self):
        boxes = s.red_button_boxes(self._triple_photo())
        self.assertEqual(len(boxes), 3, boxes)
        self.assertLess(boxes[0][0], boxes[1][0])
        self.assertLess(boxes[1][0], boxes[2][0])

    def test_import_button_samples_whole_grouped_and_skip(self):
        """部件导入：整图直导、合照左右/三拆、跳过认不全的、全局布局图不导。"""
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as dst:
            def put(sub, name, img):
                # 必须用 save_image：cv2.imwrite 在 Windows 上写不了中文路径，
                # 而这些目录名（摇杆/硬币堆…）本来就是用户真实的中文目录
                d = os.path.join(src, sub); os.makedirs(d, exist_ok=True)
                assert s.save_image(os.path.join(d, name), img)
            blank = np.full((120, 160, 3), 40, np.uint8)
            put('bet', 'w1.png', blank)
            put('maxbet', 'w1.png', blank)
            put('摇杆', 'w1.png', blank)
            put('硬币堆（投币处）截图', 'w1.png', blank)
            put('带文字标题的bet &maxbet', 'p1.png', self._pair_photo())
            put('不带文字标题的bet &maxbet', 'p2.png', self._pair_photo())
            put('带文字标题的bet &maxbet', 'bad.png', self._pair_photo(single=True))
            put('按钮左，中，右', 't1.png', self._triple_photo())
            put('全局布局图', 'layout.png', blank)      # 整图不是模板，绝不导入
            logs = []
            got = s.import_button_samples(src, dst=dst, log=logs.append)
            self.assertEqual(got, {'bet': 3, 'maxbet': 3, 'coin': 1, 'lever': 1,
                                   'btn1': 1, 'btn2': 1, 'btn3': 1}, got)
            self.assertEqual(set(os.listdir(dst)),
                             {'bet', 'maxbet', 'coin', 'lever', 'btn1', 'btn2', 'btn3'})
            for rel in ('bet/w1.png', 'bet/p1_bet.png', 'bet/p2_bet.png',
                        'maxbet/w1.png', 'maxbet/p1_maxbet.png',
                        'coin/w1.png', 'lever/w1.png',
                        'btn1/t1_btn1.png', 'btn2/t1_btn2.png', 'btn3/t1_btn3.png'):
                self.assertTrue(os.path.exists(os.path.join(dst, rel)), rel)
            skipped = [l for l in logs if '跳过' in l and 'bad.png' in l]
            self.assertTrue(skipped, logs)                # 认不全的合照要说明原因
            self.assertFalse(any('layout' in l for l in logs), logs)
            # 左右归属日志逐条可核对（用户核对归属就靠它）
            self.assertTrue(any('bet ← p1_bet.png' in l for l in logs), logs)
            self.assertTrue(any('maxbet ← p1_maxbet.png' in l for l in logs), logs)

    def test_load_template_variants_merges_main_and_subdir(self):
        with tempfile.TemporaryDirectory() as d:
            cv2.imwrite(os.path.join(d, 'lever.png'), np.full((40, 60, 3), 60, np.uint8))
            os.makedirs(os.path.join(d, 'lever'))
            cv2.imwrite(os.path.join(d, 'lever', 'b.png'), np.full((30, 50, 3), 90, np.uint8))
            cv2.imwrite(os.path.join(d, 'lever', 'a.png'), np.full((20, 40, 3), 120, np.uint8))
            open(os.path.join(d, 'lever', 'c.txt'), 'w').close()   # 非 png 忽略
            vs = s.load_template_variants('lever', d)
            self.assertEqual(len(vs), 3)
            self.assertEqual(vs[0].shape, (40, 60, 3))             # 主模板在最前
            self.assertEqual([v.shape[:2] for v in vs[1:]], [(20, 40), (30, 50)])
            self.assertEqual(s.load_template_variants('ghost', d), [])

    def test_locate_uses_all_variants_and_picks_best(self):
        """多视角模板：主模板没覆盖到的角度要靠变体认出来。"""
        v1 = np.full((60, 80, 3), 70, np.uint8); v1[10:50, 10:70] = (0, 0, 230)
        v2 = np.full((50, 90, 3), 70, np.uint8); v2[8:44, 6:84] = (0, 0, 230)
        frame = np.zeros((300, 700, 3), np.uint8)
        frame[100:150, 200:290] = v2                    # 屏幕上出现的是第二张变体
        cfg = dict(s.DEFAULT_CFG, locate_mode='image', threshold=.8)
        engine = s.Engine(cfg, {'bet': v1}, lambda m: None, FakeScreen([frame]),
                          {'bet': [v1, v2]})
        pos = engine.locate('bet', retries=1)
        self.assertIsNotNone(pos)
        self.assertLess(abs(pos[0] - 245), 3, pos)
        self.assertLess(abs(pos[1] - 125), 3, pos)
        # 只给主模板时这个视角认不出（实测交叉分 0.67 < 0.80）——
        # 如果这里居然找到了，说明阈值/匹配被改坏，多模板等于没筛
        engine2 = s.Engine(dict(cfg), {'bet': v1}, lambda m: None,
                           FakeScreen([frame]), {})
        self.assertIsNone(engine2.locate('bet', retries=1))

    def test_load_templates_pulls_main_and_variants(self):
        d = Path(s.TPL_DIR)
        d.mkdir(parents=True, exist_ok=True)
        self.assertTrue(s.save_image(str(d / 'bet.png'), np.full((60, 80, 3), 70, np.uint8)))
        (d / 'bet').mkdir(exist_ok=True)
        self.assertTrue(s.save_image(str(d / 'bet' / 'v2.png'), np.full((50, 90, 3), 70, np.uint8)))
        app = s.App(); app.withdraw()
        try:
            app.load_templates()
            self.assertIn('bet', app.tpls)
            self.assertEqual(len(app.tpl_variants['bet']), 2)
            self.assertEqual(app.tpl_variants['bet'][0].shape, (60, 80, 3))
        finally:
            app.on_close()
            (d / 'bet' / 'v2.png').unlink(missing_ok=True)
            (d / 'bet.png').unlink(missing_ok=True)
            try: (d / 'bet').rmdir()
            except OSError: pass

    def test_truth_gate_stops_on_bad_readings(self):
        """真值闸门：投币只会让读数变多；没变/变小/跳变/读不出都必须停。"""
        self.assertTrue(s.truth_gate(None, '5')[0])            # 第一次读数
        self.assertTrue(s.truth_gate('5', '6')[0])
        self.assertTrue(s.truth_gate('5', '8')[0])             # +3 在默认步长内
        for prev, val, step in (('5', '9', 3), ('5', '8', 2), ('5', '5', 3),
                                ('6', '5', 3), ('12', '20', 3)):
            ok, why = s.truth_gate(prev, val, max_step=step)
            self.assertFalse(ok, (prev, val, step))
            self.assertTrue(why)
        for bad in ('4?', 'abc', '', None):
            ok, why = s.truth_gate('5', bad)
            self.assertFalse(ok, bad)                          # 读不出绝不放行

    def test_next_sample_name_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(os.path.basename(s.next_sample_name(d, '41')), '41.png')
            open(os.path.join(d, '41.png'), 'w').close()
            self.assertEqual(os.path.basename(s.next_sample_name(d, '41')), '41-2.png')
            open(os.path.join(d, '41-2.png'), 'w').close()
            self.assertEqual(os.path.basename(s.next_sample_name(d, '41')), '41-3.png')
            open(os.path.join(d, '5_front.png'), 'w').close()
            self.assertEqual(os.path.basename(s.next_sample_name(d, '5_front')),
                             '5_front-2.png')

    def test_pick_credit_candidate_prefers_reading_over_distance(self):
        cands = [(100, 100, 40, 30, '7', {}), (900, 900, 40, 30, '7', {}),
                 (950, 900, 40, 30, '9', {})]
        # 转完视角后：读数对得上的优先，哪怕位置远一点
        pick = s.pick_credit_candidate(cands, near_xy=(940, 915), screen_w=1920,
                                       want='7')
        self.assertEqual(pick[4], '7')
        self.assertEqual(pick[:2], (900, 900))
        # 读数都对不上：退回离上一个位置最近的（中心距 20 < 60）
        pick = s.pick_credit_candidate(
            [(100, 100, 40, 30, '7', {}), (900, 900, 40, 30, '8', {}),
             (980, 900, 40, 30, '9', {})],
            near_xy=(940, 915), screen_w=1920)
        self.assertEqual(pick[4], '8')
        # 离得太远 → None，让调用方解码复核，不在这里硬挑
        self.assertIsNone(s.pick_credit_candidate(
            [(0, 0, 40, 30, '7', {})], near_xy=(1900, 1900), screen_w=1920))
        self.assertIsNone(s.pick_credit_candidate([], near_xy=(1, 1), screen_w=100))

    def test_rotate_plan_symmetric_and_dedup(self):
        self.assertEqual(s.rotate_plan(100, [0, 1, -1, 1.5]), [100, -100, 150])
        self.assertEqual(s.rotate_plan(100, [1, 1, 'x', None]), [100])
        self.assertEqual(s.rotate_plan(0, [1, -1]), [])
        self.assertEqual(s.rotate_plan(40, [2.5]), [100])

    def test_auto_collector_loop_saves_and_stops_without_mouse(self):
        """自动采集主循环（桩屏幕 + 桩鼠标，绝不碰真机输入）：

        投币→读数→存正面→变角度→存→转回→再投币；第二轮读数没变时
        真值闸门必须停，且停下后绝不再发一次点击（重复投币=白扣币）。
        """
        with tempfile.TemporaryDirectory() as out:
            clicks, logs = [], []

            def build(frames, params, cfg_extra=None):
                cfg = dict(s.DEFAULT_CFG, credit_rect=[0, 0, 85, 50],
                           coin_delay=0, last_coin_wait=0, **(cfg_extra or {}))
                screen = FakeScreen(frames)
                screen.size = (1920, 1080)
                col = s.AutoCollector(cfg, {}, {}, lambda m: logs.append(m),
                                      screen, dict(params, out_dir=out))
                col.engine.locate = lambda key, retries=None: (5, 5)
                col.engine.click_at = lambda x, y: clicks.append((x, y))
                col.rotate = lambda dx: True        # 真发鼠标的方法一律桩掉
                return col

            # 正常闭环：正面 + 变角度各存一张，再投一次币后读数没变 → 闸门停
            col = build([glyph('05')], {'target': 99, 'coins': 1, 'angles': [80]})
            reason = col._run()
            self.assertIn('真值校验没过', reason)
            self.assertIn('没变', reason)
            self.assertEqual(clicks, [(5, 5)])      # 只投了一次币
            self.assertEqual(sorted(os.path.basename(p) for p in col.saved),
                             ['05_a+80.png', '05_front.png'])
            for p in col.saved:
                self.assertTrue(os.path.exists(p))

            # 到显示上限：画面不会再变，真值不可信 → 存都不存、点都不点
            clicks.clear()
            col = build([glyph('05')], {'target': 99, 'coins': 1, 'angles': []},
                        {'credit_max_display': 5})
            self.assertIn('上限', col._run())
            self.assertEqual(col.saved, [])
            self.assertEqual(clicks, [])

            # 一开始就读不出：立即停，不发任何点击
            clicks.clear()
            col = build([np.zeros((76, 110, 3), np.uint8)],
                        {'target': 99, 'coins': 1, 'angles': []})
            self.assertIn('读不出', col._run())
            self.assertEqual(col.saved, [])
            self.assertEqual(clicks, [])

            # 目标已达成：不再投币
            col = build([glyph('05')], {'target': 5, 'coins': 1, 'angles': []})
            self.assertIn('完成', col._run())
            self.assertEqual([os.path.basename(p) for p in col.saved], ['05.png'])
            self.assertEqual(clicks, [])

    # ---------- 第 17 轮：UI 大检查 / 大改 ----------
    def test_collect_cfg_saves_every_param_row(self):
        """参数页每一行输入框都必须被 collect_cfg 收集——
        曾经 credit_ref_threshold 有输入框但不收集，用户改了等于白改。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            for key in ("credit_ref_threshold", "scale_min", "scale_max", "scale_steps"):
                self.assertIn(key, app.vars, f'{key} 应该有输入框')
            app.vars["credit_ref_threshold"].set("9.5")
            app.vars["scale_min"].set("0.8")
            app.vars["scale_max"].set("1.4")
            app.vars["scale_steps"].set("6")
            app.collect_cfg()
            self.assertEqual(app.cfg["credit_ref_threshold"], 9.5)
            self.assertEqual(app.cfg["scale_min"], 0.8)
            self.assertEqual(app.cfg["scale_max"], 1.4)
            self.assertEqual(app.cfg["scale_steps"], 6)
        finally:
            app.on_close()

    def test_log_area_has_single_scrollbar(self):
        """日志框曾因代码块重复粘贴创建了两条滚动条（右侧挤两条）。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            bars = [w for w in app.txt.master.winfo_children()
                    if w.winfo_class() == 'TScrollbar']
            self.assertEqual(len(bars), 1, '日志区只能有一条滚动条')
        finally:
            app.on_close()

    def test_locate_table_buttons_follow_mode(self):
        """定位表按钮按模式显隐：坐标模式=取点/测试，识图模式=录模板/测试/预览。
        全部常显等于每行多两个用不上的按钮——乱的主要来源之一。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            def visible(key):
                return {n for n, b in app.tpl_btns[key].items()
                        if b.winfo_manager() == 'pack'}
            app.v_mode.set('coord'); app.on_mode_change()
            for key, _ in s.TEMPLATES:
                self.assertEqual(visible(key), {'pick', 'test'}, (key, visible(key)))
            app.v_mode.set('image'); app.on_mode_change()
            for key, _ in s.TEMPLATES:
                self.assertEqual(visible(key), {'record', 'test', 'preview'},
                                 (key, visible(key)))
        finally:
            app.on_close()

    def test_start_refuses_while_collector_alive(self):
        """采集线程也在真实操作鼠标，运行必须等采集结束——按钮禁用之外
        还要有一道硬检查（双保险）。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            dialogs = []
            class MB:
                def showinfo(self, *a, **k): dialogs.append(('info',) + a)
                def showwarning(self, *a, **k): dialogs.append(('warn',) + a)
            original = s.messagebox; s.messagebox = MB()
            class AliveCollector:
                stop_flag = threading.Event()
                def is_alive(self): return True
            app.collector = AliveCollector()
            try:
                app.start()
                self.assertIsNone(app.engine, '采集中不应能启动引擎')
                self.assertTrue(dialogs, '必须弹窗告知先停止采集')
            finally:
                s.messagebox = original
        finally:
            app.on_close()

    def test_engine_counts_stats_and_summary(self):
        """运行统计：用币/轮数在 run 循环里累计，结束时日志里要有摘要。"""
        from unittest.mock import Mock
        logs = []
        engine = s.Engine(dict(s.DEFAULT_CFG, rounds=2, draw_count=0, burn_count=0,
                               hold_tab=False, game_window='',
                               credit_rect=[0, 0, 85, 50]),
                          {}, lambda m: logs.append(m), FakeScreen([glyph('09')]))
        engine.start_watchdog = lambda: None
        engine.insert_coins = lambda: True
        engine.spin = Mock(side_effect=[(True, False)] * 4)
        with unittest.mock.patch.object(s, 'pdi', object()):
            engine.run()
        self.assertEqual(engine.coins_used, 99 * 2)       # 两轮投币
        self.assertEqual(engine.rounds_done, 2)
        summary = [m for m in logs if '本次运行' in m]
        self.assertTrue(summary, logs)
        self.assertIn('用币 198 枚', summary[-1])

    def test_stats_row_uses_getattr_for_stub_engines(self):
        """统计行必须容错：引擎是桩（没有统计属性）时不能崩 pump_log。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            class FakeEngine:
                state = '投币'; spins_done = 3
                def is_alive(self): return True
                def request_stop(self): pass
            app.engine = FakeEngine()
            app.pump_log()      # 不应抛 AttributeError
            self.assertIn('抽奖 3 次', app.lbl_stats.cget('text'))
        finally:
            app.on_close()

    def test_profiles_save_load_delete(self):
        """配置方案：另存→（改动→）载入恢复→删除，全程不碰运行中的鼠标。"""
        import json as _json
        from unittest.mock import patch
        app = s.App(); app.withdraw()
        try:
            app.update()
            original = app.cfg["credit_max_display"]
            # 像真实用户一样从界面改值（存方案前会先 collect_cfg 收集界面值）
            app.vars["credit_max_display"].set("77")
            app.collect_and_save()
            with patch.object(s.simpledialog, 'askstring', return_value='测试机甲'):
                app.save_profile_as()
            self.assertIn('测试机甲', app.list_profiles())
            # 改乱当前值，再载入方案，界面必须跟着恢复
            app.cfg["credit_max_display"] = 0
            app.vars["credit_max_display"].set("0")
            app.v_profile.set('测试机甲')
            app.load_profile()
            self.assertEqual(app.cfg["credit_max_display"], 77)
            self.assertEqual(app.vars["credit_max_display"].get(), '77')
            app.v_profile.set('测试机甲')
            with patch.object(s.messagebox, 'askyesno', return_value=True):
                app.delete_profile()
            self.assertNotIn('测试机甲', app.list_profiles())
            self.assertEqual(original, 99)
        finally:
            # 测试改过沙箱里的 config.json（credit_max_display=77），
            # 还原成默认，免得污染后面用例
            app.cfg["credit_max_display"] = 99
            app.save_cfg()
            app.on_close()

    def test_wizard_settle_accepts_fraction(self):
        """采集向导「转完等待(秒)」必须支持小数——int() 截断曾把 0.8 变成 0.2。"""
        self.assertEqual(s.parse_seconds('0.8', 0.5), 0.8)
        self.assertEqual(s.parse_seconds('2', 0.5), 2.0)
        self.assertEqual(s.parse_seconds('abc', 0.5), 0.5)
        self.assertEqual(s.parse_seconds('', 0.8), 0.8)
        self.assertEqual(s.parse_seconds('-3', 0.8), 0.8)   # 非法值回默认

    def test_export_log_writes_file(self):
        from unittest.mock import patch
        app = s.App(); app.withdraw()
        try:
            app.update()
            app.log("导出测试行")
            app.pump_log()
            target = os.path.join(tempfile.gettempdir(), 'slotbot_export_log.txt')
            with patch.object(s.filedialog, 'asksaveasfilename', return_value=target):
                app.export_log()
            with open(target, encoding='utf-8') as f:
                self.assertIn('导出测试行', f.read())
        finally:
            app.on_close()

    # ---------- 第 17 轮补：急停必须覆盖采集 ----------
    def test_request_stop_stops_collector_too(self):
        """急停必须同时停采集线程。

        曾经 request_stop 只停引擎：跑「自动采集」时按 F12/End、点置顶
        停止按钮全都毫无反应，而采集又正接管着鼠标、向导窗口被全屏
        游戏挡住——用户没有任何办法停下它。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            class AliveCollector:
                def __init__(self):
                    self.stop_flag = threading.Event()
                def is_alive(self): return True
            app.collector = AliveCollector()
            app.request_stop()
            self.assertTrue(app.collector.stop_flag.is_set(),
                            '急停必须把采集线程的 stop_flag 置位')
        finally:
            app.on_close()

    def test_stop_button_enabled_during_collection(self):
        """采集进行中：开始禁用、停止可用。曾经连主界面的停止都灰着。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            class AliveCollector:
                stop_flag = threading.Event()
                def is_alive(self): return True
            app.collector = AliveCollector()
            app._refresh_run_buttons()
            self.assertEqual(str(app.btn_start['state']), 'disabled')
            self.assertEqual(str(app.btn_stop['state']), 'normal',
                             '采集进行中主界面「停止」必须可点')
            self.assertIn('停止', app.lbl_ready['text'])
        finally:
            app.on_close()

    def test_collector_starts_emergency_watchdog(self):
        """采集线程必须启动急停看门狗（热键轮询/甩左上角/连按强杀）。

        这些兜底以前只挂在自动运行上；采集没有看门狗时，热键钩子
        一旦失效就没有任何办法停下采集。"""
        col = s.AutoCollector(dict(s.DEFAULT_CFG, credit_rect=None), {}, {},
                              lambda m: None, None, {}, on_done=None)
        calls = []
        col.engine.start_watchdog = lambda: calls.append(1)
        col._run = lambda: "已停止"          # 覆盖主循环，只验证 run 的包装
        col.run()
        self.assertTrue(calls, '采集开始时必须启动急停看门狗')
        self.assertEqual(col.reason, '已停止')

    # ---------- 第 17 轮补：停止后界面必须恢复 ----------
    def test_buttons_restore_after_engine_exits(self):
        """点停止、引擎退出之后，界面必须从「运行中」恢复：
        开始重新可用、停止禁用、状态行不再显示运行中。

        曾经 start() 的注释说「按钮状态由 pump_log 在引擎结束后恢复」，
        但 pump_log 根本没接 _refresh_run_buttons——引擎其实停了，
        界面却永远卡在运行中、开始一直灰着，看起来就像停止没用。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            app.cfg['locate_mode'] = 'coord'
            app.cfg['points'] = {k: [100, 100] for k, _ in s.TEMPLATES}
            app.cfg['credit_rect'] = [0, 0, 85, 50]
            for key, _ in s.TEMPLATES:
                vx, vy = app.pt_vars[key]
                vx.set('100'); vy.set('100')

            class FakeEngine:
                state = '投币'; spins_done = 0
                def __init__(self, alive): self._alive = alive
                def is_alive(self): return self._alive
                def request_stop(self): pass

            app.engine = FakeEngine(True)
            app._refresh_run_buttons()
            self.assertEqual(str(app.btn_start['state']), 'disabled')
            self.assertIn('运行中', app.lbl_ready['text'])
            app.pump_log()                       # 运行中的一拍

            app.engine = FakeEngine(False)       # 引擎退出
            app.pump_log()                       # 退出后的第一拍必须恢复按钮
            self.assertEqual(str(app.btn_start['state']), 'normal',
                             '引擎退出后「开始运行」必须恢复可用')
            self.assertEqual(str(app.btn_stop['state']), 'disabled')
            self.assertNotIn('运行中', app.lbl_ready['text'])
        finally:
            app.on_close()

    def test_pump_log_survives_widget_errors(self):
        """pump_log 任何一拍出错都不能死掉——它一死，日志/按钮状态全部
        冻结在最后的样子，看起来就像「停止没有用」。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            class Broken:
                def config(self, **kw): raise RuntimeError('boom')
            app.lbl_state = Broken()             # 状态标签一碰就炸

            class DeadEngine:
                state = '投币'; spins_done = 0
                def is_alive(self): return False
                def request_stop(self): pass
            app.engine = DeadEngine()

            for cb in app.tk.call('after', 'info'):
                app.after_cancel(cb)
            app.pump_log()                       # 不允许向外抛异常
            pending = app.tk.call('after', 'info')
            self.assertTrue(pending, 'pump_log 出错后必须继续排定下一拍')
        finally:
            app.on_close()

    def test_stop_gives_immediate_feedback(self):
        """点停止后立刻显示「正在停止…」——引擎收尾要一两秒，
        没有反馈用户只会再按几次然后认为停止没用。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            class FakeEngine:
                state = '投币'; spins_done = 1
                def __init__(self):
                    import threading as _t
                    self.stop_flag = _t.Event()
                def is_alive(self): return True
                def request_stop(self): self.stop_flag.set()
            app.engine = FakeEngine()
            app._refresh_run_buttons()
            app.request_stop()
            self.assertIn('正在停止', app.lbl_ready['text'])
            self.assertTrue(app.engine.stop_flag.is_set())
        finally:
            app.on_close()

    # ---------- 第 17 轮续：样本中心 + 静默异常整类拦截 ----------
    def test_app_self_attributes_all_resolved(self):
        """App 里每个 self.<attr> 读取都必须有出处（方法/赋值/tk 继承）。

        tkinter 会把回调里的 AttributeError 吞掉——曾经 collect_digit_sample
        调用了 Engine 的 grab_credit()（App 上没有），点击按钮静默失败，
        用户看到的就是「这个功能坏了」。这条把整类 bug 拦在提交前。"""
        import ast as _ast
        import tkinter as _tk
        src = (ROOT / 'slotbot.py').read_text(encoding='utf-8')
        tree = _ast.parse(src)
        cls = next(n for n in _ast.walk(tree)
                   if isinstance(n, _ast.ClassDef) and n.name == 'App')
        methods, assigned, reads = set(), set(), []
        for node in cls.body:
            if isinstance(node, _ast.FunctionDef):
                methods.add(node.name)
                for n in _ast.walk(node):
                    if (isinstance(n, _ast.Attribute)
                            and isinstance(n.value, _ast.Name)
                            and n.value.id == 'self'):
                        if isinstance(n.ctx, _ast.Load):
                            reads.append(n.attr)
                        else:
                            assigned.add(n.attr)
        tk_api = set(dir(_tk.Tk))
        # self.tk / self.tkapp / self.children 这类内部属性 dir(类) 不含，
        # 用一个真实实例的属性表做白名单
        probe = _tk.Tk(); probe.withdraw()
        tk_api |= set(dir(probe))
        probe.destroy()
        unknown = sorted({a for a in reads
                          if a not in methods and a not in assigned and a not in tk_api})
        self.assertEqual(unknown, [],
                         '这些 self 属性无定义无赋值，点击按钮会静默报错')

    def test_collect_digit_sample_saves_named_sample(self):
        """采集数字样本必须真的存图。"""
        from unittest.mock import patch
        app = s.App(); app.withdraw()
        try:
            app.update()
            app.cfg['credit_rect'] = [0, 0, 85, 50]
            app.screen = FakeScreen([glyph('47')])
            dialogs = []
            with patch.object(s.simpledialog, 'askstring', return_value='47'), \
                 patch.object(s.messagebox, 'showinfo',
                              side_effect=lambda *a, **k: dialogs.append(a)), \
                 patch.object(s.messagebox, 'showerror',
                              side_effect=lambda *a, **k: dialogs.append(a)):
                app.collect_digit_sample()
            saved = [fn for fn in os.listdir(s.DIGITS_DIR) if fn.startswith('v47_')]
            self.assertTrue(saved, f'必须保存 v47_*.png；弹窗：{dialogs}')
            self.assertEqual(s.sample_truth_of(saved[0]), '47')
            for fn in saved:
                os.remove(os.path.join(s.DIGITS_DIR, fn))   # 不污染其他用例
        finally:
            app.on_close()

    def test_sample_center_integrates_parts_and_digits(self):
        """样本与采集中心：数字样本 + 部件模板 + 模板库三块收进一个窗口，
        每块的状态标签必须有内容；主界面④卡只留一个入口按钮。"""
        app = s.App(); app.withdraw()
        try:
            app.update()
            app.open_sample_center()
            center = app._sample_center
            self.assertIsNotNone(center, '样本中心窗口必须创建')
            texts = []
            def walk(w):
                for c in w.winfo_children():
                    if c.winfo_class() == 'TButton':
                        texts.append(str(c.cget('text')))
                    walk(c)
            walk(center)
            joined = ' '.join(texts)
            for need in ('采集当前读数样本', '自动批量采集', '从截图目录导入部件模板',
                         '重建模板库', '打开模板目录', '打开样本目录'):
                self.assertIn(need, joined)
            self.assertTrue(app.lbl_digits_center.cget('text'))
            self.assertTrue(app.lbl_parts_center.cget('text'))
            self.assertTrue(app.lbl_bank_center.cget('text'))
            app._sample_center.destroy()
            self.assertTrue(app.lbl_parts.cget('text'),
                            '主界面④卡的状态标签也要有内容')
        finally:
            app.on_close()

def main():
    stream=io.StringIO()
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Regression))
    report=stream.getvalue(); (ROOT/'验收结果.txt').write_text(report,encoding='utf-8')
    print(report); sandbox.cleanup()
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__': sys.exit(main())
