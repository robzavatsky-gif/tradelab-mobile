"""Regression, no-lookahead, replay and freeze/integrity checks."""
import csv
import datetime as dt
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tradelab_v03 as lab
import tradelab_v02 as old
import v01_engine as market


class TradeLab03Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = market.generate_synthetic(50, seed=29013)
        cls.days, _, _ = old.assess_sessions(cls.bars)

    def test_freeze_config_matches_code(self):
        manifest, digest = lab.manifest()
        self.assertEqual(manifest['first_forward_date'], '2026-11-16')
        self.assertEqual(len(digest), 64)

    def test_opening_regime_uses_first_hour_only(self):
        daily = [dict(x) for x in next(iter(self.days.values()))]
        oldresult = lab.opening_regime(daily)
        for b in daily[12:]:
            b['high'] += 600
            b['close'] += 100
            b['volume'] = 100000000
        self.assertEqual(lab.opening_regime(daily), oldresult)

    def test_four_expected_regimes(self):
        base = [dict(x) for x in next(iter(self.days.values()))]
        # Uniform small range: sideways
        side = [dict(x) for x in base]
        for b in side[:12]: b.update(open=100, high=100.1, low=99.9, close=100)
        self.assertEqual(lab.opening_regime(side)['regime'], 'sideways')
        rising = [dict(x) for x in base]
        for i,b in enumerate(rising[:12]):
            price = 100 + .04*i
            b.update(open=price, close=price+.02, high=price+.06, low=price-.02)
        self.assertEqual(lab.opening_regime(rising)['regime'], 'uptrend')
        falling = [dict(x) for x in base]
        for i,b in enumerate(falling[:12]):
            price = 100-.04*i
            b.update(open=price, close=price-.02, high=price+.02, low=price-.06)
        self.assertEqual(lab.opening_regime(falling)['regime'], 'downtrend')
        fast = [dict(x) for x in base]
        for b in fast[:12]: b.update(open=100, close=100, high=101, low=99)
        self.assertEqual(lab.opening_regime(fast)['regime'], 'high_volatility')

    def test_gated_trades_only_uptrend_after_1030(self):
        for date, daily in self.days.items():
            for strategy in ('ORB filtered','EMA filtered',
                             'ORB filtered 90m exit', 'EMA filtered adaptive exit'):
                t, diag = lab.simulate(daily,date,strategy,500)
                if t:
                    self.assertEqual(t['regime'], 'uptrend')
                    self.assertGreaterEqual(dt.datetime.fromisoformat(t['signal_time_et']).time(),dt.time(10,30))
                    self.assertEqual(dt.datetime.fromisoformat(t['entry_time_et']) -
                                     dt.datetime.fromisoformat(t['signal_time_et']),dt.timedelta(minutes=5))
                self.assertEqual(diag['raw_signals'] >= 0, True)

    def test_one_trade_per_day_and_no_leverage(self):
        for strategy in lab.STRATEGIES:
            r=lab.evaluate(self.days,strategy)
            self.assertEqual(r['stats']['trade_count'],len(r['trades']))
            self.assertAlmostEqual(r['stats']['pnl'],sum(t['pnl'] for t in r['trades']))
            self.assertEqual(len(set(t['date'] for t in r['trades'])),len(r['trades']))
            for t in r['trades']:
                self.assertLessEqual(t['notional'],250.000001)
                self.assertGreater(t['quantity'],0)
                self.assertEqual(t['date'],t['entry_time_et'][:10])
                self.assertEqual(t['date'],t['exit_time_et'][:10])

    def test_explicit_hard_stops_not_promised(self):
        for s in lab.STRATEGIES:
            t=lab.evaluate(self.days,s)
            for tr in t['trades']:
                self.assertIn(tr['exit_reason'],('stop','stop_gap','target','end_of_day','time_90m'))

    def test_early_signals_blocked_by_filter(self):
        # Baseline creates 10:00 ORB signal; filter cannot act until 10:30.
        day=[dict(x) for x in next(iter(self.days.values()))]
        for i in range(78):
            day[i].update(open=100.0,close=100.0, high=100.1,low=99.9,volume=1000)
        day[6].update(close=100.3,high=100.4)
        sig,diag,_=lab.choose_signal(day,'ORB filtered')
        self.assertIsNone(sig)
        self.assertGreater(diag['skipped_early'],0)

    def test_gated_orb_can_trade_and_90min_exit(self):
        daily=[dict(x) for x in next(iter(self.days.values()))]
        for i,b in enumerate(daily):
            if i < 6:
                c = 100 + .01*i
            elif i < 12:
                c = 100.06 + .02*(i-6)
            else:
                c = 100.35
            b.update(open=c, close=c, high=c+.01, low=c-.01,volume=1000)
        daily[3]['high']=100.28  # first-half hour opening range
        daily[11].update(close=100.17,high=100.19)
        daily[12].update(open=100.17,close=100.35,high=100.36,low=100.16)
        signal,diag,_=lab.choose_signal(daily,'ORB filtered')
        self.assertEqual(diag['regime'],'uptrend')
        self.assertEqual(signal,12)
        t,_=lab.simulate(daily,'2026-11-16','ORB filtered 90m exit',500,3)
        self.assertIsNotNone(t)
        self.assertEqual(t['exit_reason'],'time_90m')
        tstart=dt.datetime.fromisoformat(t['entry_time_et'])
        tend=dt.datetime.fromisoformat(t['exit_time_et'])
        self.assertEqual(tend-tstart,dt.timedelta(minutes=90))

    def test_forward_does_not_include_known_past(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'forward.json'
            past = [b for b in self.bars if b['et'].date().isoformat() < lab.START_DATE]
            dates, added, reject, freeze_id=lab.archive_forward(past,path)
            self.assertEqual(len(dates),0)
            self.assertEqual(added,0)
            self.assertFalse(path.exists())

    def _future_day(self):
        original=next(iter(self.days.values()))
        day=dt.date(2026,11,16)
        result=[]
        for b in original:
            utc=dt.datetime.combine(day,b['et'].time(),tzinfo=dt.timezone(dt.timedelta(hours=-5))).astimezone(dt.timezone.utc)
            row=dict(b)
            row['timestamp']=utc.isoformat()
            row['t']=utc
            row['et']=market.us_eastern_from_utc(utc)
            result.append(row)
        return result

    def test_forward_idempotency_and_revision_guard(self):
        future=self._future_day()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'forward.json'
            a, added, _, _=lab.archive_forward(future,path)
            self.assertEqual(added,1)
            self.assertEqual(len(a),1)
            first_content=path.read_bytes()
            _, added, _, _=lab.archive_forward(future,path)
            self.assertEqual(added,0)
            self.assertEqual(first_content,path.read_bytes())
            changed=[dict(x) for x in future]
            changed[23]['volume']+=10
            with self.assertRaisesRegex(RuntimeError,'Data revision'):
                lab.archive_forward(changed,path)
            self.assertEqual(first_content,path.read_bytes())

    def test_forward_freeze_mismatch(self):
        future=self._future_day()
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'forward.json'
            path.write_text(json.dumps({'schema':1,'freeze_sha256':'wrong','start_date':lab.START_DATE,'sessions':{}}))
            with self.assertRaisesRegex(RuntimeError,'different frozen ruleset'):
                lab.archive_forward(future,path)


    def test_v02_baseline_math_is_unchanged(self):
        mapping = {'ORB baseline':old.STRATEGIES[0], 'VWAP baseline':old.STRATEGIES[1],
                   'EMA baseline':old.STRATEGIES[2]}
        for name, old_name in mapping.items():
            a=lab.evaluate(self.days,name)['stats']
            b=old.evaluate(self.days,old_name)['stats']
            self.assertEqual(a['trade_count'],b['trade_count'])
            self.assertAlmostEqual(a['pnl'],b['pnl'],places=8)

    def test_regime_attribution_reconciles(self):
        r=lab.research(self.bars,'SYNTHETIC')
        rollup=lab.strat_regime_stats(r['periods'])
        for strategy in lab.STRATEGIES:
            observed=[x for x in rollup if x['period']=='all' and x['strategy']==strategy]
            self.assertEqual(sum(x['sessions'] for x in observed),50)
            self.assertEqual(sum(x['trade_count'] for x in observed),r['periods']['all'][strategy]['stats']['trade_count'])
            self.assertAlmostEqual(sum(x['pnl'] for x in observed),r['periods']['all'][strategy]['stats']['pnl'])

    def test_paper_only_no_order_submission(self):
        for name in ('tradelab_v03.py','tradelab_v02.py','v01_engine.py'):
            src=(lab.ROOT/name).read_text(encoding='utf8').lower()
            for forbidden in ('submit_order(', '/v2/orders', 'tradingclient(', 'post('):
                self.assertNotIn(forbidden,src)

    def test_report_and_csv_export(self):
        with tempfile.TemporaryDirectory() as folder:
            original=lab.REPORTS
            lab.REPORTS=Path(folder)
            try:
                result=lab.research(self.bars,'SYNTHETIC CHECK')
                lab.export(result,'test',False)
                txt=(Path(folder)/'test_report.html').read_text()
                self.assertIn('No-trade comparator',txt)
                self.assertIn('SYNTHETIC EXAMPLE',txt)
                with (Path(folder)/'test_comparison.csv').open() as f:
                    rows=list(csv.DictReader(f))
                self.assertEqual(len(rows),7*3)
                self.assertIn('blocked_regime',rows[0])
                with (Path(folder)/'test_diagnostics.csv').open() as f:
                    diag=list(csv.DictReader(f))
                self.assertEqual(len(diag),7*(50+35+15))
            finally: lab.REPORTS=original


if __name__=='__main__':
    unittest.main()
