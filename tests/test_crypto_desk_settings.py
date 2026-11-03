# tests/test_crypto_desk_settings.py
# Пороги стола: диск, пределы, живое чтение политикой.

import os
import tempfile
import unittest

from skills.crypto import desk_policy as policy
from skills.crypto import desk_policy_legacy as policy_legacy
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
        self.assertFalse(data.probe_paper)
        self.assertTrue(policy.probe_places_buys())
        self.assertFalse(data.sleeve_on)
        self.assertFalse(data.second_on)
        self.assertEqual(data.second_ticker, "AVA")
        self.assertEqual(data.pocket_from_probe_max, 6)
        self.assertEqual(policy.btc_dip_quote(1000.0), 250.0)
        self.assertEqual(policy.probe_quote(), 40.0)

    def test_update_links_pocket_and_sleeve(self):
        data = settings_store.update(pocket_pct=30)
        self.assertEqual(data.pocket_pct, 30.0)
        self.assertEqual(data.sleeve_max_pct, 50.0)
        self.assertTrue(os.path.isfile(self.path))
        settings_store.reset_cache()
        loaded = settings_store.current()
        self.assertEqual(loaded.pocket_pct, 30.0)
        self.assertEqual(policy.btc_dip_quote(1000.0), 300.0)
        self.assertEqual(policy_legacy.cash_floor_pct(btc_chg_7=1.0, btc_chg_day=0.5), 30.0)

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
        self.assertFalse(policy.probe_places_buys())

    def test_sleeve_off_blocks_score(self):
        settings_store.update(sleeve_on=False)
        self.assertFalse(settings_store.current().sleeve_on)
        self.assertEqual(policy_legacy.alt_slot_pct(), 0.0)
        alloc, why = policy_legacy.score_alloc(
            [{"ticker": "ENA", "bb_z": -2.8, "chg": -5.0, "turnover": 1e7}]
        )
        self.assertEqual(alloc, {})
        self.assertIn("выключен", why.lower())

    def test_payload_roundtrip(self):
        raw = {
            "pocket": {"pct": 20, "day_pct": -4},
            "sleeve": {"max_names": 2, "entry_z": -3, "on": False},
            "probe": {"usd": 40, "on": True, "paper": True},
        }
        data = from_payload(raw)
        self.assertEqual(data.pocket_pct, 20.0)
        self.assertEqual(data.sleeve_max_pct, 50.0)
        self.assertEqual(data.sleeve_max_names, 2)
        self.assertEqual(data.sleeve_entry_z, -3.0)
        self.assertFalse(data.sleeve_on)
        self.assertEqual(data.probe_usd, 40.0)
        self.assertTrue(data.probe_paper)
        self.assertFalse(data.second_on)
        self.assertEqual(from_payload({"sui": {"ticker": "DOGE", "on": True}}).second_ticker, "AVA")
        self.assertTrue(from_payload({"sui": {"ticker": "SUI", "on": True}}).second_on)

    def test_probe_paper_blocks_buys_not_scan_flag(self):
        settings_store.update(probe_paper=True)
        data = settings_store.current()
        self.assertTrue(data.probe_on)
        self.assertTrue(data.probe_paper)
        self.assertTrue(policy.probe_is_paper())
        self.assertFalse(policy.probe_places_buys())
        self.assertEqual(policy.probe_reserve_quote(open_legs=0), 0.0)
        settings_store.update(probe_paper=False)
        self.assertTrue(policy.probe_places_buys())
        self.assertGreater(policy.probe_reserve_quote(open_legs=0), 0.0)

    def test_probe_paper_journal(self):
        from skills.crypto import journal as crypto_journal

        path = os.path.join(self.tmp.name, "paper.json")
        crypto_journal.record_probe_paper(
            kind="buy",
            ticker="SKY",
            algo="candle",
            shelf=0.05,
            price=0.051,
            quote=40.0,
            reason="взял бы",
            extra={"obi": 0.62, "stair": 1.0},
            now=1_700_000_000.0,
            path=path,
        )
        open_legs = crypto_journal.probe_paper_open_legs(path)
        self.assertEqual(len(open_legs), 1)
        self.assertEqual(open_legs[0]["ticker"], "SKY")
        self.assertTrue(
            crypto_journal.probe_paper_recent(
                "SKY", "buy", now=1_700_000_100.0, path=path
            )
        )
        crypto_journal.record_probe_paper(
            kind="exit",
            ticker="SKY",
            algo="candle",
            shelf=0.05,
            price=0.049,
            reason="полка",
            now=1_700_000_200.0,
            path=path,
        )
        self.assertEqual(crypto_journal.probe_paper_open_legs(path), [])

    def test_merge_sleeve_updates_pocket(self):
        data = merge_settings(DEFAULTS, sleeve_max_pct=80)
        self.assertEqual(data.sleeve_max_pct, 80.0)
        self.assertEqual(data.pocket_pct, 20.0)
