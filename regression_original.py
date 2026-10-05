"""Isolated acceptance: game inputs, power requests and window activation mocked."""
import datetime as dt, io, sys, threading, time, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import regression_1005 as inherited
import slotbot as s
import runtime_controls as r
import original_features as f
s.kb=None
ROOT=Path(__file__).parent

class Policies(unittest.TestCase):
    def test_awake_released_on_exception_same_thread(self):
        calls=[]
        def api(v):calls.append((threading.get_ident(),v));return 1
        with self.assertRaises(RuntimeError):
            with r.KeepAwake(api=api):raise RuntimeError('simulated')
        self.assertEqual([v for _,v in calls],[0x80000003,0x80000000])
        self.assertEqual(calls[0][0],calls[1][0])
    def test_disabled_and_failed_awake(self):
        calls=[]
        with r.KeepAwake(False,api=lambda v:calls.append(v)):pass
        self.assertEqual(calls,[])
        with r.KeepAwake(api=lambda v:(calls.append(v),0)[1]):pass
        self.assertEqual(calls,[0x80000003])
    def test_monotonic_duration_and_beijing_absolute_stop(self):
        t=r.TimerPolicy({'max_minutes':1},monotonic=10)
        self.assertFalse(t.reason(monotonic=69,wall=10**12));self.assertTrue(t.reason(monotonic=70,wall=0))
        target=r.parse_time('2030-01-02 08:00')
        self.assertEqual(target,dt.datetime(2030,1,2,tzinfo=dt.timezone.utc).timestamp())
        t=r.TimerPolicy({'stop_at':'2030-01-02 08:00'},monotonic=0)
        self.assertFalse(t.reason(monotonic=999,wall=target-1));self.assertTrue(t.reason(monotonic=0,wall=target))
    def test_schedule_and_invalid_parameters(self):
        self.assertEqual(r.schedule_target({'start_delay_minutes':2},wall=10),(None,120))
        for cfg in ({'start_at':'2000-01-01 00:00'},{'start_delay_minutes':-1},
                    {'start_delay_minutes':float('nan')},{'start_at':'2030-01-02 08:00','start_delay_minutes':2},
                    {'stop_at':'2000-01-01 00:00'}):
            with self.subTest(cfg=cfg),self.assertRaises(ValueError):r.schedule_target(cfg,wall=1700000000)
        for kw in ({'coin_count':1.5},{'bet_count':1.5},{'threshold':float('nan')},
                   {'max_minutes':float('inf')},{'rounds':-1},{'completion_action':'bad'}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):r.validate_config(dict(s.DEFAULT_CFG,**kw))
    def test_shutdown_command_has_no_force(self):
        calls=[];r.request_shutdown(runner=lambda *a,**kw:calls.append((a,kw)))
        self.assertEqual(calls[0][0][0],['shutdown.exe','/s','/t','0']);self.assertTrue(calls[0][1]['check'])
    def test_pending_sample_never_enters_bank(self):
        crop=inherited.glyph('47')
        with patch.object(s,'AUTO_LEARN_ALLOWED',True),patch.object(s,'_AUTO_STATE',{}):
            self.assertIsNone(s.auto_collect_credit(crop,'47',{'collect_pending':True}))
        pending=Path(s.DATA_DIR)/'待审核数字样本'
        self.assertTrue(list(pending.glob('auto47_*.png')))
        self.assertNotIn(str(pending),s.SAMPLE_DIRS)
        self.assertFalse(list(Path(s.DIGITS_DIR).glob('auto47_*.png')))

class EnginePolicy(unittest.TestCase):
    def engine(self, readings, **kw):
        cfg=dict(s.DEFAULT_CFG,keep_awake=False,coin_delay=0,last_coin_wait=0,step_delay=0)
        cfg.update(kw);e=s.Engine(cfg,{},lambda msg:None,None);it=iter(readings);events=[]
        e.read_refill_credit=lambda:(next(it,None),'fake trusted credit or unavailable')
        e.stable_credit_value=lambda **args:(_ for _ in ()).throw(AssertionError('numeric debit gate must not run'))
        e.click_template=lambda key:(events.append(key),not e.stop_flag.is_set())[1]
        e.drag_template=lambda key,dy:(events.append(key),not e.stop_flag.is_set())[1]
        e.locate=lambda key:(10,10)
        e.click_coin_at=lambda *xy,**kw:events.append('coin')
        return e,events
    def run_fake(self,e):
        e.cfg.update(game_window='',hold_tab=False);e.start_watchdog=lambda:None
        with patch.object(s,'pdi',SimpleNamespace(mouseUp=lambda **kw:None)):e.run()
    def test_unknown_digits_execute_whole_spin_no_numeric_gate(self):
        e,events=self.engine([None]);self.assertTrue(e.spin())
        self.assertEqual(events,['maxbet','lever','btn1','btn2','btn3']);self.assertEqual(e.bets_sent,1)
        self.assertEqual(e.debit_coins,0)
    def test_display_cap_does_not_block_spin_or_fixed_insert(self):
        e,events=self.engine([None],credit_max_display=99,coin_count=4)
        self.assertTrue(e.insert_coins());self.assertTrue(e.spin())
        self.assertEqual(events[:4],['coin']*4)
        self.assertEqual(events[4:],['maxbet','lever','btn1','btn2','btn3'])
    def test_trusted_low_credit_ends_round_without_input(self):
        for n in (0,1,2):
            e,events=self.engine([n]);self.assertEqual(e.spin(watch_change=True),(True,False));self.assertEqual(events,[])
    def test_one_two_coin_modes_only_refill_below_their_bet(self):
        for bet,credit in ((1,1),(1,2),(2,2)):
            e,events=self.engine([credit],bet_count=bet);self.assertTrue(e.spin())
            self.assertEqual(events,['bet']*bet+['lever','btn1','btn2','btn3'])
    def test_normal_timer_finishes_sent_spin(self):
        e,events=self.engine([None]);click=e.click_template
        def sent(key):
            result=click(key)
            if key=='maxbet':e.finish_reason='timer';e.soft_stop.set()
            return result
        e.click_template=sent;self.assertTrue(e.spin())
        self.assertEqual(events,['maxbet','lever','btn1','btn2','btn3'])
        with self.assertRaises(r.GracefulStop):e.spin()
    def test_emergency_interrupts_after_sent_bet(self):
        e,events=self.engine([None]);click=e.click_template
        def sent(key):
            result=click(key);e.request_stop();return result
        e.click_template=sent
        with self.assertRaises(InterruptedError):e.spin()
        self.assertEqual(events,['maxbet'])
    def test_all_unknown_runs_draws_burns_and_next_coin_batch(self):
        e,events=self.engine([None]*10,coin_count=2,draw_count=2,burn_count=3,rounds=2)
        self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,10);self.assertEqual(e.rounds_done,2)
        self.assertEqual(events.count('coin'),4);self.assertEqual(events.count('maxbet'),10)
    def test_low_reading_recovers_during_burn_and_refills_next_round(self):
        e,events=self.engine([None,None,None,1]+[None]*6,coin_count=2,draw_count=1,burn_count=5,rounds=2)
        self.run_fake(e);self.assertTrue(e.completed_normally)
        self.assertEqual(events[:14],['coin']*2+['maxbet','lever','btn1','btn2','btn3']*2+['coin']*2)
        self.assertEqual(e.spins_done,8)
    def test_timer_between_insert_events(self):
        e,events=self.engine([],coin_count=10)
        def click(*xy,**kw):events.append('coin');e.finish_reason='timer';e.soft_stop.set()
        e.click_coin_at=click
        with self.assertRaises(r.GracefulStop):e.insert_coins()
        self.assertEqual(events,['coin'])
    def test_focus_loss_blocks_native_input(self):
        e,_=self.engine([]);e._input_active=True
        with patch.object(s,'foreground_title',return_value='Other'):
            with self.assertRaises(InterruptedError):e.require_focus()
        self.assertTrue(e.stop_flag.is_set())
    def test_action_failure_still_stops(self):
        e,_=self.engine([None]);e.click_template=lambda key:False
        self.assertFalse(e.spin())
    def test_failure_is_not_normal_completion(self):
        e,_=self.engine([]);e.insert_coins=lambda:False;self.run_fake(e)
        self.assertFalse(e.completed_normally);self.assertIn('异常停止',e.finish_reason)

    def fast_coin_engine(self, **kw):
        cfg=dict(s.DEFAULT_CFG,coin_count=99,coin_delay=100,move_delay=150,last_coin_wait=800,locate_mode='coord')
        cfg.update(kw);e=s.Engine(cfg,{},lambda msg:None,None);events=[];waits=[]
        e.locate=lambda *args,**kwargs:(10,20)
        e.pause=lambda seconds:waits.append(seconds)
        inputs=SimpleNamespace(moveTo=lambda *xy:events.append(('move',xy)),click=lambda:events.append(('click',)),
                               mouseDown=lambda:(_ for _ in ()).throw(AssertionError('slow coin hold')))
        return e,events,waits,inputs
    def test_coin_batch_restores_original_speed(self):
        e,events,waits,inputs=self.fast_coin_engine()
        with patch.object(s,'pdi',inputs):self.assertTrue(e.insert_coins())
        self.assertEqual(events,[('move',(10,20))]+[('click',)]*99)
        self.assertEqual(waits,[.15]+[.1]*99+[.8]);self.assertAlmostEqual(sum(waits),10.85)
        self.assertEqual(e.coins_used,99)
    def test_image_coins_relocate_every_ten_without_slow_clicks(self):
        e,events,waits,inputs=self.fast_coin_engine(coin_count=21,locate_mode='image')
        with patch.object(s,'pdi',inputs):self.assertTrue(e.insert_coins())
        self.assertEqual(events.count(('move',(10,20))),3);self.assertEqual(events.count(('click',)),21)
        self.assertEqual(waits.count(.15),3);self.assertEqual(waits.count(.1),21)
    def test_emergency_blocks_remaining_fast_coins(self):
        e,events,waits,inputs=self.fast_coin_engine()
        def stop_click():events.append(('click',));e.request_stop()
        inputs.click=stop_click
        with patch.object(s,'pdi',inputs):self.assertFalse(e.insert_coins())
        self.assertEqual(events,[('move',(10,20)),('click',)]);self.assertEqual(waits,[.15])
    def test_watchdog_stops_without_keyboard_hook(self):
        e,_=self.engine([])
        with patch.object(s,'key_pressed',return_value=True),patch.object(s,'cursor_pos',return_value=(-1,-1)):
            e.start_watchdog();self.assertTrue(e.stop_flag.wait(.5))
        self.assertIn('急停键',e.finish_reason)
    def test_seventy_five_credit_continues_past_configured_count_without_inserting(self):
        e,events=self.engine([75,75,75,72,69,66,63,2],coin_count=99,draw_count=2,burn_count=1,rounds=1)
        self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,6)
        self.assertEqual(events.count('coin'),0);self.assertEqual(events.count('maxbet'),6)
    def test_sufficient_category_runs_past_counts_until_trusted_low(self):
        from vision import ENOUGH_CREDIT
        e,events=self.engine([ENOUGH_CREDIT]*4+[2],coin_count=99,draw_count=1,burn_count=1,rounds=1)
        messages=[];e.log=messages.append;self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,3)
        self.assertEqual(events.count('coin'),0)
        self.assertTrue(any('足够一次下注' in m for m in messages))
        self.assertTrue(any('Credit=2' in m for m in messages))
    def test_next_batch_requires_low_or_completed_unknown_fallback(self):
        e,events=self.engine([None,None,75,72,69,2,2,None,None,None,None],
                             coin_count=2,draw_count=1,burn_count=2,rounds=2)
        self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,7)
        self.assertEqual(events[:24],['coin']*2+['maxbet','lever','btn1','btn2','btn3']*4+['coin']*2)
        self.assertEqual(events.count('coin'),4)
    def test_lost_reading_after_positive_credit_still_runs_unknown_burn_budget(self):
        e,events=self.engine([75,75,75,None,None,None],coin_count=2,draw_count=1,burn_count=2,rounds=1)
        self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,4);self.assertEqual(events.count('coin'),0)
    def test_zero_burn_count_does_not_override_known_remaining_credit(self):
        e,events=self.engine([75,75,70,1],coin_count=2,draw_count=1,burn_count=0,rounds=1)
        self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,2);self.assertEqual(events.count('coin'),0)
    def test_timer_stops_extended_burn_after_completing_action(self):
        e,events=self.engine([75]*12,coin_count=99,draw_count=1,burn_count=1,rounds=0)
        click=e.click_template
        def sent(key):
            result=click(key)
            if key=='maxbet' and events.count('maxbet')==4:e.finish_reason='timer';e.soft_stop.set()
            return result
        e.click_template=sent;self.run_fake(e)
        self.assertTrue(e.completed_normally);self.assertEqual(e.spins_done,4)
        self.assertEqual(events,['maxbet','lever','btn1','btn2','btn3']*4)

class RefillRecognition(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import vision as v
        cls.v=v
        cls.bank=v.build_template_bank([(str(n), inherited.glyph(str(n))) for n in range(10)])
    def crop(self,value):
        return inherited.cv2.copyMakeBorder(inherited.glyph(str(value)),5,5,40,40,inherited.cv2.BORDER_CONSTANT,value=0)
    def read_frames(self,frames):
        e=s.Engine(dict(s.DEFAULT_CFG,credit_rect=[0,0,170,86]),{},lambda msg:None,inherited.FakeScreen(frames))
        with patch.object(s,'get_digit_bank',return_value=self.bank):return e.read_refill_credit()
    def test_good_low_frames_are_accepted(self):
        for n in (0,1,2):
            self.assertEqual(self.read_frames([self.crop(n)]*3)[0],n)
    def test_two_of_three_or_changing_low_frames_never_trigger(self):
        for frames in ([self.crop(0),self.crop(1),self.crop(0)],
                       [self.crop(0),inherited.np.zeros_like(self.crop(0)),self.crop(0)]):
            self.assertIsNone(self.read_frames(frames)[0])
    def test_no_screen_or_capture_exception_is_unknown(self):
        e=s.Engine(dict(s.DEFAULT_CFG),{},lambda msg:None,None)
        self.assertIsNone(e.read_refill_credit()[0])
        e.cfg['credit_rect']=[0,0,10,10]
        e.grab_credit=lambda:(_ for _ in ()).throw(RuntimeError('capture unavailable'))
        self.assertIsNone(e.read_refill_credit()[0])
    def test_bank_missing_conflicting_or_weak_match_never_trigger(self):
        crop=self.crop(0)
        self.assertIsNone(self.v.decode_refill_candidate(crop,{})[0])
        for value in ('8',None):
            with patch.object(self.v,'decode_by_template',return_value=(value,'fake')):
                self.assertIsNone(self.v.decode_refill_candidate(crop,self.bank)[0])
    def test_truncated_dark_noise_and_high_digits_never_trigger(self):
        zero=self.crop(0)
        bad=[inherited.np.zeros_like(zero),inherited.np.full_like(zero,255),zero[:,50:82],zero[15:55,:]]
        bad += [self.crop(n) for n in (3,8,10,11,12,20,21,22,80,81,82,99)]
        for crop in bad:self.assertIsNone(self.v.decode_refill_candidate(crop,self.bank)[0])
    def test_real_samples_have_no_false_low_reading(self):
        from tests.real_bench import load_samples
        samples=load_samples();bank=self.v.build_template_bank([(t,i) for t,_,i in samples]);bad=[]
        for truth,name,img in samples:
            value,_=self.v.decode_refill_candidate(img,bank)
            if value is not None and value!=int(truth):bad.append((name,truth,value))
        self.assertEqual(bad,[])
    def test_sufficient_category_stays_consistent_when_exact_credit_changes(self):
        self.assertEqual(self.read_frames([self.crop(75)]*3)[0],self.v.ENOUGH_CREDIT)
        self.assertEqual(self.read_frames([self.crop(67),self.crop(99),self.crop(75)])[0],self.v.ENOUGH_CREDIT)
        self.assertIsNone(self.read_frames([self.crop(75),self.crop(2),self.crop(75)])[0])
    def test_every_value_is_low_or_sufficient_without_exact_high_reading(self):
        for value in range(100):
            with self.subTest(value=value):
                condition,_=self.v.decode_credit_condition(self.crop(value),self.bank)
                self.assertEqual(condition,value if value<3 else self.v.ENOUGH_CREDIT)
    def test_ambiguity_between_sufficient_digits_does_not_require_refill(self):
        bank={k:list(v) for k,v in self.bank.items()}
        bank['5'] += list(bank['9'])
        frame=self.crop(99)
        self.assertIsNone(self.v.decode_credit_candidate(frame,bank)[0])
        self.assertEqual(self.v.decode_credit_condition(frame,bank)[0],self.v.ENOUGH_CREDIT)
    def test_ambiguity_with_low_digits_stays_unknown(self):
        bank={k:list(v) for k,v in self.bank.items()}
        bank['0'] += list(bank['8'])
        self.assertIsNone(self.v.decode_credit_condition(self.crop(8),bank)[0])
        bank={k:list(v) for k,v in self.bank.items()}
        bank['0'] += list(bank['1'])
        self.assertIsNone(self.v.decode_credit_condition(self.crop(10),bank)[0])
    def test_low_template_gate_and_unknown_frame_still_apply_to_conditions(self):
        for frame in (self.crop(0)[:,50:82],self.crop(0)[15:55,:],inherited.np.zeros_like(self.crop(0))):
            self.assertIsNone(self.v.decode_credit_condition(frame,self.bank)[0])
        with patch.object(self.v,'decode_by_template',return_value=('8','conflicting low')):
            self.assertIsNone(self.v.decode_credit_condition(self.crop(0),self.bank)[0])
        self.assertIsNone(self.read_frames([self.crop(99),inherited.np.zeros_like(self.crop(99)),self.crop(99)])[0])
    def test_classification_of_real_samples_never_claims_false_low_or_sufficiency(self):
        from tests.real_bench import load_samples
        samples=load_samples();bank=self.v.build_template_bank([(t,i) for t,_,i in samples])
        for truth,name,frame in samples:
            value,_=self.v.decode_credit_condition(frame,bank)
            with self.subTest(name=name):
                if value==self.v.ENOUGH_CREDIT:self.assertGreaterEqual(int(truth),3)
                elif value is not None:self.assertEqual(value,int(truth))
    def test_one_and_two_coin_mode_preserves_insufficient_threshold(self):
        for bet in (1,2):
            for value in range(4):
                condition,_=self.v.decode_credit_condition(self.crop(value),self.bank,bet_count=bet)
                self.assertEqual(condition,value if value<bet else self.v.ENOUGH_CREDIT)
    def real_zero_bank(self):
        from tests.real_bench import load_samples
        samples=load_samples();return self.v.build_template_bank([(t,i) for t,_,i in samples])
    def test_user_recorded_zero_calibrates_without_relaxing_template_threshold(self):
        zero=s.load_image(str(ROOT/'tests/credit_zero_20261005_162650.png'));bank=self.real_zero_bank()
        self.assertIsNone(self.v.decode_credit_candidate(zero,bank)[0])
        self.assertEqual(self.v.decode_credit_candidate(zero,bank,zero)[0],0)
        e=s.Engine(dict(s.DEFAULT_CFG,credit_rect=[0,0,94,54],credit_ref=str(ROOT/'tests/credit_zero_20261005_162650.png')),
                   {},lambda msg:None,inherited.FakeScreen([zero]*3))
        with patch.object(s,'get_digit_bank',return_value=bank):self.assertEqual(e.read_refill_credit()[0],0)
        self.assertEqual({k:len(v) for k,v in bank.items()},{k:len(v) for k,v in self.real_zero_bank().items()})
    def test_zero_reference_requires_direct_zero_and_full_roi_alignment(self):
        zero=s.load_image(str(ROOT/'tests/credit_zero_20261005_162650.png'));bank=self.real_zero_bank()
        shifted=inherited.np.roll(zero,35,axis=1)
        for ref in (shifted,inherited.np.zeros_like(zero),self.crop(7),zero[:30]):
            self.assertIsNone(self.v.decode_credit_candidate(zero,bank,ref)[0])
        for n in (8,10,20,70,71,72,80,81,82,99):
            value,_=self.v.decode_credit_candidate(self.crop(n),bank,zero)
            self.assertNotIn(value,(0,1,2))
    def test_reference_zero_requires_three_consistent_frames(self):
        zero=s.load_image(str(ROOT/'tests/credit_zero_20261005_162650.png'));bank=self.real_zero_bank()
        e=s.Engine(dict(s.DEFAULT_CFG,credit_rect=[0,0,94,54],credit_ref=str(ROOT/'tests/credit_zero_20261005_162650.png')),
                   {},lambda msg:None,inherited.FakeScreen([zero,inherited.np.zeros_like(zero),zero]))
        with patch.object(s,'get_digit_bank',return_value=bank):self.assertIsNone(e.read_refill_credit()[0])
    def test_real_samples_have_no_false_credit_even_with_recorded_zero(self):
        from tests.real_bench import load_samples
        bank=self.real_zero_bank();zero=s.load_image(str(ROOT/'tests/credit_zero_20261005_162650.png'))
        bad=[]
        for truth,name,img in load_samples():
            value,_=self.v.decode_credit_candidate(img,bank,zero)
            if value is not None and value!=int(truth):bad.append((truth,name,value))
        self.assertEqual(bad,[])

class Interface(unittest.TestCase):
    def setUp(self):
        self.a=s.App();self.a.cfg['credit_rect']=None;self.a.withdraw();self.a.update()
    def tearDown(self):self.a.on_close()
    def pump(self,condition):
        deadline=time.monotonic()+3
        while not condition() and time.monotonic()<deadline:self.a.update();time.sleep(.005)
        self.assertTrue(condition())
    def test_original_tabs_and_bad_entry(self):
        a=self.a;self.assertEqual([a.nb.tab(t,'text') for t in a.nb.tabs()],['1. 定位设置','2. 参数设置','3. 运行'])
        a.vars['coin_count'].set('typo')
        with self.assertRaises(ValueError):a.collect_cfg()
    def test_cancel_scheduled_start(self):
        a=self.a;a.cfg.update(start_delay_minutes=2,keep_awake_wait=False)
        with patch.object(a,'save_cfg'):a.start()
        self.assertTrue(a._armed);self.assertTrue(a.busy());a.request_stop()
        self.assertFalse(a._armed);self.assertTrue(a._schedule_cancel.is_set());self.assertIsNone(a.engine)
    def test_schedule_snapshot_and_single_launch(self):
        a=self.a;calls=[];a._armed=True;a._schedule_wall=None;a._schedule_mono=0
        a._schedule_cfg={'marker':123};a.cfg['marker']=999
        with patch.object(a,'_begin_start',side_effect=lambda cfg,**kw:calls.append((cfg,kw))):a._tick_schedule();a._tick_schedule()
        self.assertEqual(calls,[({'marker':123},{'automatic':True})])
    def test_callbacks_stay_on_ui_thread(self):
        a=self.a;ids=[];a._background(lambda:threading.get_ident(),lambda value,error:ids.append((value,threading.get_ident())))
        self.pump(lambda:bool(ids));self.assertNotEqual(ids[0][0],a._ui_thread);self.assertEqual(ids[0][1],a._ui_thread)
    def test_f12_with_tab_sets_stop_before_ui_pumps(self):
        import keyboard as keyboard
        listener=keyboard._KeyboardListener()
        with patch.object(keyboard._os_keyboard,'init'):listener.init()
        listener.start_if_necessary=lambda:None
        a=self.a;a.cfg['stop_keys']=['F12','End'];a.cfg['extra_stop_key']=''
        a.engine=s.Engine(dict(s.DEFAULT_CFG),{},lambda msg:None,None)
        a.collector=SimpleNamespace(stop_flag=threading.Event())
        old_callbacks=[];listener.nonblocking_hotkeys[(88,)]=[lambda event:old_callbacks.append(event)]
        held={15:keyboard.KeyboardEvent('down',15,name='tab'),88:keyboard.KeyboardEvent('down',88,name='f12')}
        with patch.object(s,'kb',keyboard),patch.object(keyboard,'_listener',listener),patch.object(keyboard,'_hooks',{}),\
             patch.object(keyboard,'key_to_scan_codes',side_effect=lambda key:({'f12':88,'end':79}[key],)),\
             patch.object(keyboard,'_pressed_events',held),patch.object(a,'_finish_stop_ui') as finish:
            a._bind_stop_hooks();a._bind_stop_hooks()
            self.assertEqual(len(listener.nonblocking_keys[88]),1)
            errors=[]
            def dispatch():
                try:listener.pre_process_event(held[88])
                except Exception as exc:errors.append(exc)
            worker=threading.Thread(target=dispatch);worker.start();worker.join(timeout=1)
            self.assertFalse(worker.is_alive());self.assertEqual(errors,[])
            self.assertTrue(a.engine.stop_flag.is_set());self.assertTrue(a.collector.stop_flag.is_set())
            self.assertTrue(a._operation_cancel.is_set());self.assertTrue(a._schedule_cancel.is_set())
            finish.assert_not_called();self.assertEqual(old_callbacks,[])
            a._clear_stop_hooks();self.assertEqual(listener.nonblocking_keys[88],[])
        a.engine=None;a.collector=None
    def test_stop_key_repeat_ignored_until_release(self):
        a=self.a
        with patch.object(a,'request_stop') as stop:
            down=SimpleNamespace(event_type='down');up=SimpleNamespace(event_type='up')
            a._stop_key_event(down,'F12');a._stop_key_event(down,'F12');self.assertEqual(stop.call_count,1)
            a._stop_key_event(up,'F12');a._stop_key_event(down,'F12');self.assertEqual(stop.call_count,2)
    def test_cancelled_preflight_cannot_launch(self):
        a=self.a;gate=threading.Event()
        def unlocked():gate.wait(.5);return True
        with patch.object(f,'desktop_unlocked',side_effect=unlocked),patch.object(s,'list_windows',return_value=[]):
            a._begin_start(dict(a.cfg,game_window='VRChat'),automatic=True);a.request_stop();gate.set()
            deadline=time.monotonic()+.6
            while time.monotonic()<deadline:a.update();time.sleep(.01)
        self.assertIsNone(a.engine);self.assertFalse(a._operation_busy)
    def test_locked_desktop_does_not_start(self):
        a=self.a
        with patch.object(f,'desktop_unlocked',return_value=False):
            a._begin_start(dict(a.cfg),automatic=True);self.pump(lambda:not a._operation_busy)
        self.assertIsNone(a.engine)
    def test_shutdown_cancel_and_start_exclusion(self):
        a=self.a
        with patch.object(f,'request_shutdown') as shutdown:
            a._finish_action({'completion_action':'shutdown','shutdown_countdown':10})
            self.assertTrue(a.busy());self.assertIsNotNone(a._action_window)
            a.request_stop();self.assertIsNone(a._action_window);a.update();shutdown.assert_not_called()
    def test_preflight_unreadable_missing_or_bad_digit_region_is_warning(self):
        a=self.a
        class Screen:
            size=(2560,1600);left=top=0
            def grab_rect(self,x,y,w,h):return inherited.np.zeros((h,w,3),inherited.np.uint8)
        a.screen=Screen();a.cfg['points']={k:[100,100] for k,_ in s.TEMPLATES}
        for rect in (None,[100,100,90,70],[9000,9000,90,70]):
            a.cfg['credit_rect']=rect;a.cfg['watch_rect']=None
            issues=a.preflight(check_templates=False)
            self.assertFalse([msg for level,msg in issues if level=='error'],issues)
            self.assertTrue([msg for level,msg in issues if level=='warn'],issues)
    def test_preflight_explains_sufficient_state_without_inventing_credit(self):
        from vision import ENOUGH_CREDIT
        a=self.a;a.screen=SimpleNamespace(size=(2560,1600),left=0,top=0)
        a.cfg['points']={k:[100,100] for k,_ in s.TEMPLATES}
        a.cfg['credit_rect']=[100,100,90,70]
        with patch.object(s.Engine,'read_refill_credit',return_value=(ENOUGH_CREDIT,'three frames')):
            issues=a.preflight(check_templates=False)
        text=' '.join(msg for level,msg in issues if level=='ok')
        self.assertIn('足够一次下注',text);self.assertNotIn('Credit=enough',text)
    def test_review_controls_available(self):
        a=self.a;a.open_sample_center();labels=[]
        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class()=='TButton':labels.append(c.cget('text'))
                walk(c)
        walk(a._sample_center)
        self.assertIn('打开待审核样本',labels);self.assertIn('审核并填写真实数字',labels);a._sample_center.destroy()
    def test_start_without_digit_region_reaches_engine(self):
        a=self.a;started=[]
        a.cfg.update(credit_rect=None,watch_rect=None)
        a.vars['game_window'].set('')
        for vx,vy in a.pt_vars.values():vx.set('100');vy.set('100')
        a.screen=SimpleNamespace(size=(2560,1600),left=0,top=0)
        engine=SimpleNamespace(start=lambda:started.append(True),is_alive=lambda:False,
                               completed_normally=False,finish_reason='测试',cfg={},request_stop=lambda:None)
        with patch.object(f,'desktop_unlocked',return_value=True),patch.object(s,'list_windows',return_value=[]),patch.object(s,'Engine',return_value=engine),patch.object(a,'save_cfg'):
            a.start();self.pump(lambda:bool(started))
        self.assertEqual(started,[True])
    def test_cancelled_freeze_callback_cannot_clear_new_operation(self):
        a=self.a;timeline=[];jobs=[]
        class Lock:
            def __enter__(self):timeline.append('lock');return self
            def __exit__(self,*args):timeline.append('unlock')
        screen=SimpleNamespace(grab=lambda:inherited.np.zeros((100,100,3),inherited.np.uint8))
        a.screen=screen
        with patch.object(s,'MouseLock',Lock),patch.object(s,'list_windows',return_value=[]),patch.object(a,'_background',side_effect=lambda work,done:jobs.append((work,done))):
            a._freeze_then(lambda frame:timeline.append('overlay'))
            a.request_stop();a._generation+=1;a._operation_busy=True
            newer=object();a._mouse_lock=newer
            jobs[0][1](None,None)
            self.assertTrue(a._operation_busy);self.assertIs(a._mouse_lock,newer);self.assertNotIn('overlay',timeline)
            a._operation_busy=False;a._mouse_lock=None

SELECTED='''real_zero_not_11111 all_standard_digits real_hooked_seven full_real_screens_find_credit_and_chance
dark_obstructed_and_red_button_are_unknown truncated_digit_never_reads_as_one digit_fit_accepts_real_selections
real_samples_all_decode_correctly real_bench_sample_naming_formats decode_best_recovers_stray_red_via_panel
consensus_rejects_flicker_and_keeps_stable stability_and_reference relative_credit_ref_resolves_via_app_dir
region_pattern_diff blank_and_repeated_templates wait_frames_stable_returns_on_quiet_screen
picked_coords_are_physical_pixels preflight_blocks_out_of_screen_coords
scene_shift_separates_animation_from_rotation narrow_credit_region_is_flagged
digit_width_uses_median_not_span pause_key_not_bound_by_default stop_keys_are_customizable stop_prevents_input
fused_weak_template_match_does_not_veto_segment fused_agrees_and_without_bank_falls_back_to_segment
template_bank_never_misreads_real_samples stale_bank_is_served_without_blocking red_button_boxes_ignores_titles_and_decor
red_button_boxes_splits_triple import_button_samples_whole_grouped_and_skip load_template_variants_merges_main_and_subdir
locate_uses_all_variants_and_picks_best load_templates_pulls_main_and_variants truth_gate_stops_on_bad_readings
next_sample_name_never_overwrites pick_credit_candidate_prefers_reading_over_distance rotate_plan_symmetric_and_dedup
wizard_settle_accepts_fraction'''.split()
def main():
    suite=unittest.TestSuite()
    for cls in (Policies,EnginePolicy,RefillRecognition,Interface):suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
    suite.addTests(inherited.Regression('test_'+name) for name in SELECTED)
    stream=io.StringIO();result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite);report=stream.getvalue()
    (ROOT/'验收结果.txt').write_text(report,encoding='utf-8');print(report)
    return 0 if result.wasSuccessful() else 1
if __name__=='__main__':sys.exit(main())
