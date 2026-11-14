"""v0.4 publisher/transport tests; no network, no brokerage orders."""
from pathlib import Path
import datetime as dt
import json
import tempfile
import unittest
from zoneinfo import ZoneInfo

import cloud_update as c
import tradelab_v03 as engine

class Tests(unittest.TestCase):
    def test_frozen_manifest(self):
        frozen, hashed=engine.manifest()
        self.assertEqual(frozen['first_forward_date'], '2026-11-16')
        self.assertEqual(len(hashed),64)

    def test_no_weekend_run(self):
        x=dt.datetime(2026,11,14,18,0,tzinfo=ZoneInfo('America/New_York'))
        self.assertFalse(c.run_gate(x))

    def test_dst_gating(self):
        east=ZoneInfo('America/New_York')
        self.assertFalse(c.run_gate(dt.datetime(2026,11,16,16,29,tzinfo=east)))
        self.assertTrue(c.run_gate(dt.datetime(2026,11,16,16,35,tzinfo=east)))
        self.assertTrue(c.run_gate(dt.datetime(2027,7,12,16,35,tzinfo=east)))

    def test_zero_session_mode(self):
        with tempfile.TemporaryDirectory() as t:
            archive=Path(t)/'no-state.json'
            result=engine.forward([], 'no market yet', archive)
            self.assertEqual(result['days'],0)
            self.assertEqual(result['new_sessions'],0)
            self.assertFalse(archive.exists())
            payload=c.make_payload(result,{'sessions':{}},demo=False)
            self.assertIsNone(payload['last_session'])
            self.assertEqual(payload['sessions'],0)
            self.assertEqual(payload['strategies'][0]['ending_balance'],500)

    def test_synthetic_preview_is_distinct(self):
        with tempfile.TemporaryDirectory() as t:
            archive=Path(t)/'demo.json'
            result=engine.forward(c.shifted_demo_bars(), 'synthetic', archive)
            self.assertEqual(result['days'],4)
            payload=c.make_payload(result,json.loads(archive.read_text()),demo=True)
            self.assertEqual(payload['mode'],'synthetic_demo')
            self.assertEqual(payload['last_session'],'2026-11-19')
            self.assertIn('SYNTHETIC DEMONSTRATION',c.build_packet(result,json.loads(archive.read_text()),demo=True))

if __name__=='__main__':unittest.main()
