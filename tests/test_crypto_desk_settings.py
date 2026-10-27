# tests/test_crypto_desk_settings.py
# Пороги стола: диск, пределы, живое чтение политикой.

import os
import tempfile
import unittest

from skills.crypto import desk_policy as policy
from skills.crypto.settings import store as settings_store
from skills.crypto.settings.schema import DEFAULTS, from_payload, merge_settings


class TestDeskSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "settings.json")
        self._old = settings_store._PATH
        settings_store._PATH = self.path
        settings_store.reset_cache()
        self.addCleanup(self._restore)

    def _restore(self):
        settings_store._PATH = self._old
        settings_store.reset_cache()

    def test_defaults_without_file(self):
        data = settings_store.current()
        self.assertEqual(data.pocket_pct, DEFAULTS.pocket_pct)
        self.assertEqual(data.sleeve_max_pct, DEFAULTS.sleeve_max_pct)
        self.assertTrue(data.probe_on)
        self.assertFalse(data.second_on)
        self.assertEqual(data.second_ticker, "AVA")
        self.assertEqual(policy.btc_dip_quote(1000.0), 250.0)
        self.assertEqual(policy.probe_quote(), 30.0)

    def test_update_links_pocket_and_sleeve(self):
        data = settings_store.update(pocket_pct=30)
        self.assertEqual(data.pocket_pct, 30.0)
        self.assertEqual(data.sleeve_max_pct, 50.0)
        self.assertTrue(os.path.isfile(self.path))
        settings_store.reset_cache()
        loaded = settings_store.current()
        self.assertEqual(loaded.pocket_pct, 30.0)
        self.assertEqual(policy.btc_dip_quote(1000.0), 300.0)
        self.assertEqual(policy.cash_floor_pct(btc_chg_7=1.0, btc_chg_day=0.5), 30.0)

    def test_clamp_and_reset(self):
        settings_store.update(probe_usd=500, pocket_bounce_pct=0.01)
        data = settings_store.current()
        self.assertEqual(data.probe_usd, 100.0)
        self.assertEqual(data.pocket_bounce_pct, 0.3)
        settings_store.reset()
        self.assertEqual(settings_store.current().probe_usd, DEFAULTS.probe_usd)

    def test_live_pocket_day_and_probe_off(self):
        settings_store.update(pocket_day_pct=-5, probe_on=False)
        self.assertTrue(policy.btc_dip_should_buy(day_chg=-5.0, qty=0))
        self.assertFalse(policy.btc_dip_should_buy(day_chg=-3.0, qty=0))
        self.assertFalse(settings_store.current().probe_on)

    def test_payload_roundtrip(self):
        raw = {
            "pocket": {"pct": 20, "day_pct": -4},
            "sleeve": {"max_names": 2, "entry_z": -3},
            "probe": {"usd": 40, "on": True},
        }
        data = from_payload(raw)
        self.assertEqual(data.pocket_pct, 20.0)
        self.assertEqual(data.sleeve_max_pct, 50.0)
        self.assertEqual(data.sleeve_max_names, 2)
        self.assertEqual(data.sleeve_entry_z, -3.0)
        self.assertEqual(data.probe_usd, 40.0)
        self.assertFalse(data.second_on)
        self.assertEqual(from_payload({"sui": {"ticker": "DOGE", "on": True}}).second_ticker, "AVA")
        self.assertTrue(from_payload({"sui": {"ticker": "SUI", "on": True}}).second_on)

    def test_merge_sleeve_updates_pocket(self):
        data = merge_settings(DEFAULTS, sleeve_max_pct=80)
        self.assertEqual(data.sleeve_max_pct, 80.0)
        self.assertEqual(data.pocket_pct, 20.0)
