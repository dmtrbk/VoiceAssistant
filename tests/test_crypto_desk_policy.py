# tests/test_crypto_desk_policy.py
# Правила автостола крипты: режим BTC, скор, коридор, анти-чёрн.

import os
import tempfile
import time
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from skills.crypto import desk_policy as policy
from skills.crypto import desk_policy_legacy as policy_legacy
from skills.crypto import journal as crypto_journal
from skills.crypto.settings import store as crypto_settings_store
from skills.crypto.skill import CryptoSkill


_MSK = ZoneInfo("Europe/Moscow")
_SETTINGS_DIR = tempfile.TemporaryDirectory()
_SETTINGS_PATH = os.path.join(_SETTINGS_DIR.name, "settings.json")
crypto_settings_store._PATH = _SETTINGS_PATH
crypto_settings_store.reset_cache()


class TestDeskPolicy(unittest.TestCase):
    def test_risk_off_on_weak_btc_week(self):
        self.assertTrue(policy_legacy.is_risk_off(btc_chg_7=-6.0, btc_chg_day=1.0))
        self.assertTrue(policy_legacy.is_risk_off(btc_chg_7=-1.0, btc_chg_day=-4.0))
        self.assertFalse(policy_legacy.is_risk_off(btc_chg_7=2.0, btc_chg_day=-1.0))

    def test_cash_floor_bull_vs_normal(self):
        self.assertEqual(
            policy_legacy.cash_floor_pct(btc_chg_7=6.0, btc_chg_day=1.0),
            policy.DESK_CASH_FLOOR_BULL_PCT,
        )
        self.assertEqual(
            policy_legacy.cash_floor_pct(btc_chg_7=1.0, btc_chg_day=0.5),
            policy.DESK_CASH_FLOOR_PCT,
        )
        self.assertEqual(
            policy_legacy.cash_floor_pct(btc_chg_7=-6.0, btc_chg_day=0.0),
            100.0,
        )

    def test_band_is_alt_corridor(self):
        self.assertEqual(policy_legacy.band_pct_for("BTC"), policy.REBALANCE_BAND_ALT_PCT)
        self.assertEqual(policy_legacy.band_pct_for("DOGE"), policy.REBALANCE_BAND_ALT_PCT)

    def test_filter_keeps_pump_drops_cooldown(self):
        rows = [
            {"ticker": "BTC", "turnover": 1e9, "chg": 1.0, "chg_7": 2.0},
            {"ticker": "DOGE", "turnover": 1e9, "chg": 40.0, "chg_7": 5.0},
            {"ticker": "SOL", "turnover": 1e9, "chg": 1.0, "chg_7": 3.0},
        ]
        filtered = policy_legacy.filter_auto_candidates(
            rows,
            held=set(),
            watchlist=["BTC", "ETH", "SOL", "DOGE"],
            cooldown={"SOL"},
        )
        tickers = {r["ticker"] for r in filtered}
        self.assertIn("BTC", tickers)
        self.assertIn("DOGE", tickers)
        self.assertNotIn("SOL", tickers)

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_score_alloc_keeps_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, why = policy_legacy.score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("кэш", why.lower())

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_score_alloc_bull_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        rows.append({"ticker": "MNT", "chg": -6.0, "chg_7": -4.0, "turnover": 1e9, "bb_z": -3.1})
        alloc, why = policy_legacy.score_alloc(rows, cash_floor_pct=policy.DESK_CASH_FLOOR_BULL_PCT)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("ETH", alloc)
        self.assertEqual(alloc.get("MNT"), policy_legacy.alt_slot_pct())
        self.assertIn("8%", why)

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_core_split_equally_despite_scores(self):
        rows = [
            {"ticker": "BTC", "chg": -0.4, "chg_7": -0.5, "turnover": 1e9},
            {"ticker": "ETH", "chg": 1.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, why = policy_legacy.score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("ETH", alloc)
        self.assertIn("кэш", why.lower())

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_score_empty_when_core_below_floor(self):
        rows = [
            {"ticker": "BTC", "chg": -2.0, "chg_7": -9.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": -1.0, "chg_7": -10.0, "turnover": 1e9},
        ]
        alloc, why = policy_legacy.score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("кэш", why.lower())

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_score_without_sleeve_signal_stays_cash(self):
        rows = [
            {"ticker": "BTC", "chg": 0.5, "chg_7": -4.0, "turnover": 1e9},
            {"ticker": "DOGE", "chg": 2.0, "chg_7": -1.0, "turnover": 1e9},
        ]
        alloc, why = policy_legacy.score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("DOGE", alloc)
        self.assertIn("кэш", why.lower())

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_alt_sleeve_caps_and_leaves_unused_as_cash(self):
        # В рукав идут альты у нижней полосы 4h: самые глубокие, по равному слоту.
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.8, "chg_7": 2.5, "turnover": 1e9},
            {"ticker": "SOL", "chg": 1.2, "chg_7": 4.0, "turnover": 1e9, "bb_z": -1.5},
            {"ticker": "MNT", "chg": -6.0, "chg_7": -4.0, "turnover": 1e9, "bb_z": -3.1},
            {"ticker": "LINK", "chg": -4.0, "chg_7": -3.0, "turnover": 1e9, "bb_z": -2.6},
            {"ticker": "AVAX", "chg": -3.0, "chg_7": -2.0, "turnover": 1e9, "bb_z": -2.9},
            {"ticker": "NEAR", "chg": -5.0, "chg_7": -3.5, "turnover": 1e9, "bb_z": -2.7},
        ]
        alloc, why = policy_legacy.score_alloc(rows)
        alts = {k: v for k, v in alloc.items() if k in policy.DESK_ALT_SLEEVE}
        self.assertNotIn("SOL", alloc)
        self.assertEqual(set(alts), {"MNT", "AVAX"})
        self.assertTrue(all(v == policy_legacy.alt_slot_pct() for v in alts.values()))
        self.assertLessEqual(sum(alts.values()), policy.ALT_SLEEVE_MAX_PCT + 0.5)
        self.assertLessEqual(sum(alloc.values()), 100.0 - policy.DESK_CASH_FLOOR_PCT + 0.5)
        self.assertIn("альты", why)

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_btc_eth_enter_sleeve_at_lower_band(self):
        self.assertIn("BTC", policy.DESK_ALT_SLEEVE)
        self.assertIn("ETH", policy.DESK_ALT_SLEEVE)
        rows = [
            {"ticker": "BTC", "turnover": 1e9, "bb_z": -2.8},
            {"ticker": "ETH", "turnover": 1e9, "bb_z": -2.6},
            {"ticker": "SOL", "turnover": 1e9, "bb_z": -1.0},
        ]
        alloc, _why = policy_legacy.score_alloc(rows)
        self.assertEqual(alloc.get("BTC"), policy_legacy.alt_slot_pct())
        self.assertEqual(alloc.get("ETH"), policy_legacy.alt_slot_pct())
        self.assertNotIn("SOL", alloc)

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_sol_in_sleeve(self):
        self.assertIn("SOL", policy.DESK_ALT_SLEEVE)
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.8, "chg_7": 2.5, "turnover": 1e9},
            {"ticker": "SOL", "chg": 5.0, "chg_7": 10.0, "turnover": 1e9, "bb_z": 1.0},
        ]
        alloc, _why = policy_legacy.score_alloc(rows)
        self.assertNotIn("SOL", alloc)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("ETH", alloc)
        rows[2]["bb_z"] = -2.8
        alloc, _why = policy_legacy.score_alloc(rows)
        self.assertEqual(alloc.get("SOL"), policy_legacy.alt_slot_pct())

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_alt_enters_only_at_lower_band(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "MNT", "chg": -5.0, "chg_7": -3.0, "turnover": 1e9, "bb_z": -2.8},
            {"ticker": "LINK", "chg": -6.0, "chg_7": -9.0, "turnover": 1e9, "bb_z": -1.2},
            {"ticker": "NEAR", "chg": -9.0, "chg_7": -20.0, "turnover": 1e9},
        ]
        alloc, why = policy_legacy.score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertIn("MNT", alloc)
        self.assertNotIn("LINK", alloc)
        self.assertNotIn("NEAR", alloc)  # без полос не входим
        self.assertIn("альты", why)

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_held_alt_kept_until_exit_dust_dropped(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "XRP", "chg": 2.0, "chg_7": 1.0, "turnover": 1e9, "bb_z": 0.8, "held_value": 30.0},
            {"ticker": "ADA", "chg": 1.0, "chg_7": 1.0, "turnover": 1e9, "bb_z": 0.5, "held_value": 0.4},
        ]
        alloc, _why = policy_legacy.score_alloc(rows)
        self.assertEqual(alloc.get("XRP"), policy_legacy.alt_slot_pct())
        self.assertNotIn("ADA", alloc)

    def test_held_alts_take_slots_first(self):
        rows = [
            {"ticker": t, "turnover": 1e9, "bb_z": 0.0, "held_value": 20.0}
            for t in ("XRP", "DOGE", "ADA")
        ] + [{"ticker": "LINK", "turnover": 1e9, "bb_z": -3.5}]
        alts = policy_legacy.pick_alts(rows)
        self.assertEqual(set(alts), {"XRP", "DOGE"})

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_blacklist_occupies_slot_until_sold(self):
        rows = [
            {"ticker": "DOGE", "turnover": 1e9, "bb_z": 0.2, "held_value": 40.0},
            {"ticker": "XRP", "turnover": 1e9, "bb_z": 0.1, "held_value": 40.0},
            {"ticker": "LINK", "turnover": 1e9, "bb_z": -3.2},
        ]
        weekly = policy_legacy.pick_alts(rows, excluded={"DOGE"})
        self.assertIn("DOGE", weekly)
        tight = policy_legacy.pick_alts(rows, blacklist={"DOGE"})
        self.assertNotIn("DOGE", tight)
        self.assertEqual(set(tight), {"XRP"})
        room = policy_legacy.pick_alts(rows, blacklist={"DOGE"}, max_names=3)
        self.assertNotIn("DOGE", room)
        self.assertEqual(set(room), {"XRP", "LINK"})
        _alloc, why = policy_legacy.score_alloc(rows, blacklist={"DOGE"})
        self.assertIn("чёрный список DOGE", why)

    def test_alt_exit_reason(self):
        self.assertEqual(policy_legacy.alt_exit_reason(price=95.0, entry=100.0), "стоп")
        self.assertIsNone(policy_legacy.alt_exit_reason(price=97.0, entry=100.0))
        self.assertEqual(policy_legacy.alt_exit_reason(price=97.0, entry=100.0, swing_low=98.0), "дно")
        self.assertIsNone(policy_legacy.alt_exit_reason(price=99.0, entry=100.0, swing_low=98.0))
        self.assertIsNone(policy_legacy.alt_exit_reason(price=102.0, entry=100.0, ma=101.0))
        # На $100 взвод ≈ комиссия×2 + $0.50 ≈ +0.86%, не старые +3%.
        self.assertIsNone(policy_legacy.update_alt_exit_arm(None, price=100.5, entry=100.0, now=1.0, qty=1.0))
        armed = policy_legacy.update_alt_exit_arm(None, price=103.0, entry=100.0, now=10.0, qty=1.0)
        self.assertAlmostEqual(armed["high"], 103.0)
        # Откат ещё не −0,5% от вершины (нужно ≤ 102.485).
        self.assertFalse(
            policy_legacy.alt_exit_ready(
                armed, price=102.7, entry=100.0, now=10.0 + policy.ALT_EXIT_HOLD_SEC, qty=1.0
            )
        )
        higher = policy_legacy.update_alt_exit_arm(armed, price=108.0, entry=100.0, now=20.0, qty=1.0)
        self.assertEqual(higher["ts"], 20.0)
        need = 108.0 * (1.0 - policy.ALT_EXIT_PULLBACK_PCT / 100.0)
        self.assertGreaterEqual(
            (need / 100.0 - 1.0) * 100.0, policy.exit_trap_arm_pct(entry=100.0, qty=1.0)
        )
        self.assertFalse(
            policy_legacy.alt_exit_ready(higher, price=need, entry=100.0, now=20.0 + 10, qty=1.0)
        )
        self.assertTrue(
            policy_legacy.alt_exit_ready(
                higher, price=need, entry=100.0, now=20.0 + policy.ALT_EXIT_HOLD_SEC, qty=1.0
            )
        )
        self.assertEqual(
            policy_legacy.alt_exit_reason(
                price=need,
                entry=100.0,
                exit_arm=higher,
                now=20.0 + policy.ALT_EXIT_HOLD_SEC,
                qty=1.0,
            ),
            "вершина",
        )
        # ENA: ловушка была, откат есть, уже под входом — не ждать стоп −4%.
        self.assertTrue(
            policy_legacy.alt_exit_ready(
                higher, price=99.5, entry=100.0, now=20.0 + policy.ALT_EXIT_HOLD_SEC, qty=1.0
            )
        )
        self.assertEqual(
            policy_legacy.alt_exit_reason(
                price=99.5,
                entry=100.0,
                exit_arm=higher,
                now=20.0 + policy.ALT_EXIT_HOLD_SEC,
                qty=1.0,
            ),
            "вершина",
        )
        self.assertEqual(policy.position_basis(2.0, None), 2.0)
        self.assertEqual(policy.position_basis(2.0, 1.9), 1.9)
        line = policy_legacy.watch_snapshot_line("XRP", price=1.85, entry=2.0, kind="alt", basis=1.9)
        self.assertIn("avg=2", line)
        self.assertIn("отсчёт 1.9", line)
        self.assertIn("до стопа 1.4пп", line)

    def test_alt_sleeve_qty_for_exit_reserves_pocket(self):
        sol = policy.BTC_DIP_TICKER
        self.assertAlmostEqual(
            policy_legacy.alt_sleeve_qty_for_exit(
                ticker=sol,
                held_qty=1.834,
                pocket_qty=0.872,
                price=111.0,
                min_trade_usd=5.0,
            ),
            0.962,
            places=3,
        )
        self.assertEqual(
            policy_legacy.alt_sleeve_qty_for_exit(
                ticker=sol,
                held_qty=0.872,
                pocket_qty=0.872,
                price=111.0,
                min_trade_usd=5.0,
            ),
            0.0,
        )
        self.assertEqual(
            policy_legacy.alt_sleeve_qty_for_exit(
                ticker="STRK",
                held_qty=477.0,
                pocket_qty=477.0,
                price=0.06,
                min_trade_usd=5.0,
            ),
            0.0,
        )
        self.assertAlmostEqual(
            policy_legacy.alt_sleeve_qty_for_exit(
                ticker="BNB",
                held_qty=0.5,
                pocket_qty=0.0,
                price=750.0,
                min_trade_usd=5.0,
            ),
            0.5,
        )

    def test_pick_watch_alt_entries_respects_room_and_blocks(self):
        bands = {
            "LINK": {"z": -3.0, "ma": 1.0},
            "NEAR": {"z": -2.6, "ma": 1.0},
            "DOT": {"z": -3.4, "ma": 1.0},
            "ADA": {"z": -1.0, "ma": 1.0},
            "PEPE": {"z": -4.0, "ma": 1.0},
        }
        picked = policy_legacy.pick_watch_alt_entries(
            bands,
            target_alloc={"BTC": 40.0, "XRP": 11.7},
            held_alts=set(),
            blocked={"DOT"},
            max_names=3,
        )
        self.assertEqual(picked, ["LINK"])
        none = policy_legacy.pick_watch_alt_entries(
            bands, target_alloc={"XRP": 11.7}, held_alts={"DOGE", "MNT"}, blocked=set()
        )
        self.assertEqual(none, [])

    def test_rebalance_band(self):
        self.assertFalse(
            policy_legacy.should_rebalance_leg(
                current_value=100.0,
                target_value=103.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy_legacy.should_rebalance_leg(
                current_value=100.0,
                target_value=200.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
            )
        )

    def test_take_profit_on_day_pump(self):
        # Внутри коридора 6% equity, но дневной памп → фиксируем избыток.
        self.assertTrue(
            policy_legacy.should_rebalance_leg(
                current_value=130.0,
                target_value=100.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
                day_chg=policy.TAKE_PROFIT_DAY_PCT,
            )
        )
        self.assertFalse(
            policy_legacy.should_rebalance_leg(
                current_value=130.0,
                target_value=100.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
                day_chg=5.0,
            )
        )

    def test_watch_take_profit_and_dip(self):
        self.assertTrue(
            policy_legacy.should_watch_take_profit(
                day_chg=policy.WATCH_TP_DAY_PCT,
                current_value=140.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertFalse(
            policy_legacy.should_watch_take_profit(
                day_chg=5.0,
                current_value=140.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertFalse(
            policy_legacy.should_watch_dip_buy(
                ticker="BTC",
                day_chg=-5.0,
                current_value=50.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy_legacy.should_watch_dip_buy(
                ticker="DOGE",
                day_chg=1.0,
                current_value=0.0,
                target_value=100.0,
                min_trade_usd=5.0,
                bb_z=-3.0,
            )
        )
        # Держанный альт не усредняем, а выше нижней полосы не входим.
        self.assertFalse(
            policy_legacy.should_watch_dip_buy(
                ticker="DOGE",
                day_chg=-10.0,
                current_value=50.0,
                target_value=100.0,
                min_trade_usd=5.0,
                bb_z=-3.0,
            )
        )
        self.assertFalse(
            policy_legacy.should_watch_dip_buy(
                ticker="DOGE",
                day_chg=-10.0,
                current_value=0.0,
                target_value=100.0,
                min_trade_usd=5.0,
                bb_z=-1.0,
            )
        )
        self.assertFalse(
            policy_legacy.should_watch_dip_buy(
                ticker="FAKE",
                day_chg=-10.0,
                current_value=50.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy_legacy.should_watch_take_profit(
                day_chg=policy.WATCH_TP_ALT_DAY_PCT,
                current_value=140.0,
                target_value=100.0,
                min_trade_usd=5.0,
                tp_pct=policy.WATCH_TP_ALT_DAY_PCT,
            )
        )

    def test_exclusion_follows_week(self):
        entered = policy_legacy.update_exclusions(
            set(),
            [
                {"ticker": "DOGE", "chg_7": -6.0},
                {"ticker": "SOL", "chg_7": -1.0},
                {"ticker": "BTC", "chg_7": -20.0},
            ],
        )
        self.assertEqual(entered, {"DOGE", "BTC"})
        stayed = policy_legacy.update_exclusions(entered, [{"ticker": "DOGE", "chg_7": -2.0}])
        self.assertEqual(stayed, {"DOGE", "BTC"})
        left = policy_legacy.update_exclusions(
            stayed,
            [{"ticker": "DOGE", "chg_7": 0.2}, {"ticker": "BTC", "chg_7": 0.2}],
        )
        self.assertEqual(left, set())

    def test_probe_shelf_trend_and_exit(self):
        shelf = [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(60)]
        prev = {"high": 1.02, "low": 0.99, "close": 1.0, "volume": 10.0}
        live = {"high": 1.08, "low": 1.0, "close": 1.06, "volume": 40.0}
        setup = policy.probe_shelf_setup(shelf + [prev, live])
        self.assertIsNotNone(setup)
        assert setup is not None
        self.assertAlmostEqual(setup["shelf"], 1.01)
        self.assertIsNone(policy.probe_shelf_setup(shelf + [prev, {**live, "close": 1.20, "volume": 80}]))
        wide = [{"high": 1.20, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(60)]
        self.assertIsNone(policy.probe_shelf_setup(wide + [prev, live]))
        self.assertIsNone(policy.probe_shelf_setup(shelf + [prev, {**live, "volume": 10.0}]))
        late = {**prev, "high": 1.06}
        self.assertIsNone(policy.probe_shelf_setup(shelf + [late, live]))
        drifted = [dict(bar) for bar in shelf]
        for index, bar in enumerate(drifted[-8:]):
            close = 1.0 + index * 0.004
            bar["close"] = close
            bar["high"] = close
            bar["low"] = close * 0.99
        self.assertIsNone(policy.probe_shelf_setup(drifted + [prev, {**live, "close": 1.10, "high": 1.11}]))
        loud = [dict(bar) for bar in shelf]
        loud[-1]["volume"] = 40.0
        loud[-2]["volume"] = 40.0
        self.assertIsNone(policy.probe_shelf_setup(loud + [prev, live]))
        wide_then_quiet = (
            [{"high": 1.25, "low": 0.90, "close": 1.0, "volume": 10.0} for _ in range(50)]
            + [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(8)]
        )
        local = policy.probe_shelf_setup(
            wide_then_quiet + [prev, {"high": 1.08, "low": 1.0, "close": 1.07, "volume": 22.0}]
        )
        self.assertIsNotNone(local)
        assert local is not None
        self.assertAlmostEqual(local["shelf"], 1.01)
        self.assertIsNone(
            policy.probe_shelf_setup(
                wide_then_quiet + [prev, {"high": 1.08, "low": 1.0, "close": 1.07, "volume": 15.0}]
            )
        )

        up = [float(i) for i in range(1, 80)]
        self.assertEqual(policy.ma_trend(up), "bull")
        self.assertEqual(policy.ma_trend(list(reversed(up))), "bear")
        self.assertEqual(policy.ma_trend([5.0] * 80), "neutral")
        self.assertTrue(policy.probe_trends_ok("bull", "neutral", 8.0))
        self.assertFalse(policy.probe_trends_ok("bull", "bear", 8.0))
        self.assertFalse(policy.probe_trends_ok("neutral", "neutral", 8.0))
        self.assertTrue(policy.probe_trends_ok("bull", "bull", 30.0))
        self.assertFalse(policy.probe_trends_ok("bull", "neutral", 8.0, week_chg=-6.0))
        self.assertTrue(policy.probe_trends_ok("bull", "neutral", 8.0, week_chg=-4.0))
        self.assertFalse(
            policy.pocket_entry_trends_ok(trend15="bear", trend1h="bull", week_chg=0.0)
        )
        self.assertFalse(
            policy.pocket_entry_trends_ok(trend15="bull", trend1h="bear", week_chg=0.0)
        )
        self.assertFalse(
            policy.pocket_entry_trends_ok(trend15="bull", trend1h="neutral", week_chg=-6.0)
        )
        self.assertTrue(
            policy.pocket_entry_trends_ok(trend15="bull", trend1h="neutral", week_chg=-4.0)
        )
        self.assertFalse(
            policy.probe_trends_ok("bull", "neutral", 8.0, algo=policy.PROBE_ALGO_GRIND)
        )
        self.assertTrue(
            policy.probe_trends_ok("bull", "bull", 8.0, algo=policy.PROBE_ALGO_GRIND)
        )
        self.assertLess(
            policy.probe_scan_rank({
                "chg": -18.0, "price": 0.108, "low": 0.071, "high": 0.111, "turnover": 5_000_000,
            }),
            0,
        )
        self.assertGreater(
            policy.probe_scan_rank({
                "chg": 7.6, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 4_000_000,
            }),
            0,
        )
        # День/ночь ≥ $500k; тоньше — отсекаем.
        self.assertGreater(
            policy.probe_scan_rank({
                "chg": 2.0, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 600_000,
            }),
            0,
        )
        self.assertLess(
            policy.probe_scan_rank({
                "chg": 2.0, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 300_000,
            }),
            0,
        )
        self.assertLess(
            policy.probe_scan_rank({
                "chg": 30.0, "price": 0.0045, "low": 0.003, "high": 0.005, "turnover": 46_000,
            }),
            0,
        )
        self.assertLess(
            policy.probe_scan_rank({
                "chg": 2.0, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 10_000,
            }),
            0,
        )
        # Проверка дневного и ночного порога оборота (YEKT) + защиты от падающего рынка.
        from datetime import datetime, timedelta, timezone
        yekt = timezone(timedelta(hours=5))
        day_ts = datetime(2026, 10, 31, 14, 0, tzinfo=yekt).timestamp()
        night_ts = datetime(2026, 10, 31, 23, 0, tzinfo=yekt).timestamp()
        self.assertEqual(policy.probe_min_turnover_usd(day_ts), 500_000.0)
        self.assertEqual(policy.probe_min_turnover_usd(night_ts), 500_000.0)
        self.assertFalse(policy.probe_night_halted(day_ts))
        self.assertTrue(policy.probe_night_halted(night_ts))
        self.assertFalse(
            policy.probe_night_halted(datetime(2026, 10, 31, 21, 0, tzinfo=yekt).timestamp())
        )
        self.assertFalse(
            policy.probe_night_halted(datetime(2026, 10, 31, 22, 59, tzinfo=yekt).timestamp())
        )
        self.assertTrue(
            policy.probe_night_halted(datetime(2026, 10, 31, 23, 0, tzinfo=yekt).timestamp())
        )
        self.assertFalse(
            policy.probe_night_halted(datetime(2026, 10, 31, 20, 59, tzinfo=yekt).timestamp())
        )
        self.assertTrue(
            policy.probe_night_halted(datetime(2026, 11, 1, 5, 59, tzinfo=yekt).timestamp())
        )
        self.assertFalse(
            policy.probe_night_halted(datetime(2026, 11, 1, 6, 0, tzinfo=yekt).timestamp())
        )
        self.assertIsNone(policy.probe_night_halt_reason(night_ts))
        self.assertIsNone(policy.probe_night_halt_reason(day_ts))
        self.assertEqual(policy.PROBE_MAX_LEGS_BTC_DOWN, 2)
        self.assertEqual(policy.probe_max_legs(now=day_ts), 3)
        self.assertEqual(policy.probe_max_legs(btc_day_chg=0.4, now=day_ts), 3)
        self.assertEqual(policy.probe_max_legs(btc_day_chg=0.0, now=day_ts), 3)
        self.assertEqual(policy.probe_max_legs(btc_day_chg=-0.01, now=day_ts), 3)
        self.assertEqual(policy.probe_max_legs(now=night_ts), 2)
        self.assertEqual(policy.tape_btc_day_chg([{"ticker": "ETH", "chg": 1.0}]), None)
        self.assertEqual(
            policy.tape_btc_day_chg([{"ticker": "BTC", "chg": -1.2}, {"ticker": "ETH", "chg": 2.0}]),
            -1.2,
        )
        night_stair = {"shelf": 1.0, "price": 1.01, "stair": 1.0, "algo": "candle"}
        night_candle = {"shelf": 1.0, "price": 1.01, "algo": "candle"}
        self.assertTrue(policy.probe_setup_ok_now(night_stair, now=day_ts, turnover=600_000))
        self.assertTrue(
            policy.probe_setup_ok_now(night_stair, now=night_ts, turnover=600_000)
        )
        self.assertFalse(
            policy.probe_setup_ok_now(night_stair, now=night_ts, turnover=200_000)
        )
        self.assertFalse(
            policy.probe_setup_ok_now(night_candle, now=night_ts, turnover=5_000_000)
        )
        night_spark = {"shelf": 1.0, "price": 1.01, "spark": 1.0, "algo": "spark"}
        self.assertTrue(
            policy.probe_setup_ok_now(night_spark, now=night_ts, turnover=600_000)
        )
        self.assertFalse(
            policy.probe_setup_ok_now(night_spark, now=night_ts, turnover=200_000)
        )
        self.assertFalse(
            policy.probe_setup_ok_now(night_stair, now=day_ts, turnover=200_000, strict=True)
        )
        self.assertFalse(
            policy.probe_setup_ok_now(night_stair, now=day_ts, turnover=400_000, strict=True)
        )
        self.assertTrue(
            policy.probe_setup_ok_now(night_stair, now=day_ts, turnover=1_500_000, strict=True)
        )
        self.assertEqual(policy.probe_min_turnover_usd(strict=True), 800_000.0)
        self.assertEqual(policy.PROBE_SHELF_COOLDOWN_SEC, 25 * 60)
        self.assertEqual(policy.PROBE_SHELF_COOLDOWN_LOSS_USD, 0.25)
        self.assertTrue(policy.PROBE_MARKET_FALL_ON)
        red_market = [{"ticker": f"C{i}", "chg": -2.0, "turnover": 500_000} for i in range(25)]
        thin_green = [
            {"ticker": f"C{i}", "chg": 2.0 if i < 5 else -1.0, "turnover": 500_000}
            for i in range(25)
        ]
        halt_breadth = [
            {"ticker": f"C{i}", "chg": 2.0 if i < 3 else -1.0, "turnover": 500_000}
            for i in range(25)
        ]
        normal_market = [
            {"ticker": f"C{i}", "chg": 2.0 if i < 10 else -1.0, "turnover": 500_000}
            for i in range(25)
        ]
        self.assertFalse(
            policy.probe_market_regime_ok([{"ticker": "BTC", "chg": -3.1, "turnover": 50_000_000}])[0]
        )
        self.assertFalse(policy.probe_market_regime_ok(red_market)[0])
        self.assertTrue(policy.probe_market_regime_ok(thin_green)[0])
        self.assertFalse(policy.probe_market_regime_ok(halt_breadth)[0])
        self.assertTrue(policy.probe_market_regime_ok(normal_market)[0])
        self.assertFalse(
            policy.probe_market_regime_ok(
                [{"ticker": "BTC", "chg": 2.0, "turnover": 50_000_000}],
                btc_flash_pct=-1.2,
            )[0]
        )
        self.assertTrue(
            policy.probe_market_regime_ok(
                [{"ticker": "BTC", "chg": 2.0, "turnover": 50_000_000}],
                btc_flash_pct=-0.4,
            )[0]
        )
        flash_bars = [{"close": 100.0}] * 3 + [{"close": 98.5}]
        self.assertAlmostEqual(policy.probe_btc_flash_pct(flash_bars), -1.5)
        self.assertEqual(policy.PROBE_MIN_MARKET_BREADTH_PCT, 16.0)
        rising_btc = [{"ticker": "BTC", "chg": 1.2, "turnover": 50_000_000}]
        self.assertTrue(policy.probe_market_rising(rising_btc))
        falling = [{"ticker": "BTC", "chg": -1.0, "turnover": 50_000_000}] + [
            {"ticker": f"C{i}", "chg": -1.0 if i >= 5 else 0.5, "turnover": 500_000}
            for i in range(25)
        ]
        self.assertFalse(policy.probe_market_rising(falling))
        alt_up = [{"ticker": "BTC", "chg": -0.4, "turnover": 50_000_000}] + [
            {"ticker": f"C{i}", "chg": 2.0 if i < 15 else -0.5, "turnover": 500_000}
            for i in range(25)
        ]
        self.assertTrue(policy.probe_market_rising(alt_up))
        self.assertTrue(policy.PROBE_STRICT_MODE)
        self.assertEqual(
            policy.probe_strict_reason("после полки пауза 20м", rising_btc),
            "после полки пауза 20м",
        )
        # Падающий рынок глушит локальный strict — там уже полный стоп входов.
        self.assertIsNone(
            policy.probe_strict_reason("после полки пауза 20м", red_market)
        )
        self.assertIsNone(
            policy.probe_strict_reason("дневной лимит убытка (-6.20$)", rising_btc)
        )
        self.assertEqual(
            policy.probe_strict_reason("серия из 3 убытков (пауза 60м)", normal_market),
            "серия из 3 убытков (пауза 60м)",
        )
        self.assertEqual(policy.PROBE_MARKET_FALL_PAUSE_SEC, 3600.0)
        t0 = 1_700_000_000.0
        reason_on, until_on = policy.probe_market_regime_pause_reason(
            [{"ticker": "BTC", "chg": -4.0, "turnover": 50_000_000}],
            now=t0,
            pause_until=0.0,
        )
        self.assertIn("падающий рынок", reason_on or "")
        self.assertAlmostEqual(until_on, t0 + 3600.0)
        reason_w, until_w = policy.probe_market_regime_pause_reason(
            halt_breadth, now=t0, pause_until=0.0,
        )
        reason_solo, _until_solo = policy.probe_market_regime_pause_reason(
            thin_green, now=t0, pause_until=0.0,
        )
        self.assertIsNone(reason_solo)
        self.assertIn("ширина", reason_w or "")
        self.assertEqual(until_w, 0.0)
        self.assertAlmostEqual(
            policy.probe_scan_rank({
                "chg": -2.0, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 4_000_000,
            }),
            -2.0,
        )
        # −4% суток видно; отсечка ниже −15%.
        self.assertAlmostEqual(
            policy.probe_scan_rank({
                "chg": -4.0, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 4_000_000,
            }),
            -4.0,
        )
        self.assertLess(
            policy.probe_scan_rank({
                "chg": -16.0, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 4_000_000,
            }),
            policy.PROBE_DAY_MIN_PCT,
        )
        self.assertEqual(policy.PROBE_SCAN_LIMIT_LIVE, 25)
        self.assertEqual(policy.PROBE_SCAN_CLASSIC_LIMIT_LIVE, 25)
        self.assertEqual(policy.PROBE_SCAN_LIMIT, 25)
        self.assertEqual(
            policy.probe_scan_limits(paper=False),
            (25, 25, 25),
        )
        self.assertEqual(
            policy.probe_scan_limits(paper=True),
            (
                policy.PROBE_SCAN_LIMIT_PAPER,
                policy.PROBE_SCAN_CLASSIC_LIMIT_PAPER,
                policy.PROBE_SCAN_MANDATORY_TOP_PAPER,
            ),
        )
        self.assertEqual(
            policy.probe_scan_rank({
                "chg": 40.0, "price": 0.0265, "low": 0.017, "high": 0.027, "turnover": 2_000_000,
            }),
            40.0,
        )
        self.assertGreater(
            policy.probe_scan_rank({
                "chg": 36.0, "price": 0.066, "low": 0.048, "high": 0.084, "turnover": 2_000_000,
            }),
            policy.probe_scan_rank({
                "chg": 7.6, "price": 0.050, "low": 0.046, "high": 0.057, "turnover": 4_000_000,
            }),
        )
        ranked = policy.probe_scan_ranked_rows([
            {"ticker": "A", "chg": 3.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "B", "chg": 12.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "C", "chg": 8.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "D", "chg": 20.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "E", "chg": 1.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "F", "chg": 15.0, "price": 1.0, "turnover": 500_000},
        ], limit=6)
        self.assertEqual(
            [str(r[1]["ticker"]) for r in ranked[:5]],
            ["D", "F", "B", "C", "A"],
        )
        self.assertEqual(policy.PROBE_SCAN_MANDATORY_TOP_LIVE, 25)
        # Боевой скан — Топ-25 суток; ракеты со дна подмешиваются по bounce от low.
        crowd = [
            {"ticker": f"G{i}", "chg": 40.0 - i * 0.4, "price": 1.0, "turnover": 600_000}
            for i in range(50)
        ] + [
            {"ticker": "LIQ", "chg": 4.0, "price": 1.0, "turnover": 8_000_000},
        ]
        crowd_ranked = policy.probe_scan_ranked_rows(crowd, paper=False)
        # 15 слотов лидерам (cap 25 − 10 ракетных); LIQ +4% в «дно» (≤+2%) не входит.
        self.assertEqual(
            [str(r[1]["ticker"]) for r in crowd_ranked],
            [f"G{i}" for i in range(15)],
        )
        # Спящий −4% суток, но уже +5% от low — попадает в скан как ракета.
        rocket_rows = [
            {"ticker": f"L{i}", "chg": 10.0 - i, "price": 1.0, "low": 0.99, "turnover": 600_000}
            for i in range(12)
        ] + [
            {
                "ticker": "ICNT",
                "chg": -4.0,
                "price": 0.305,
                "low": 0.2844,
                "turnover": 800_000,
            },
        ]
        rocket_ranked = policy.probe_scan_ranked_rows(rocket_rows, paper=False)
        self.assertIn("ICNT", [str(r[1]["ticker"]) for r in rocket_ranked])
        # Имитация: широкий скан; после mandatory-top высоколиквидный разгон раньше хвоста.
        paper_ranked = policy.probe_scan_ranked_rows(crowd, paper=True)
        self.assertGreaterEqual(len(paper_ranked), 50)
        self.assertEqual(str(paper_ranked[0][1]["ticker"]), "G0")
        self.assertEqual(str(paper_ranked[49][1]["ticker"]), "G49")
        self.assertEqual(str(paper_ranked[50][1]["ticker"]), "LIQ")
        wide = policy.probe_scan_ranked_rows(crowd, limit=60, paper=True)
        # limit 60 = 50 лидеров + 10 слотов дна; LIQ +4% ни туда, ни в ≤+2%.
        self.assertEqual(str(wide[0][1]["ticker"]), "G0")
        self.assertEqual(str(wide[49][1]["ticker"]), "G49")
        self.assertEqual(len(wide), 50)
        self.assertNotIn("LIQ", [str(r[1]["ticker"]) for r in wide])
        mild_red = policy.probe_scan_ranked_rows([
            {"ticker": "RED", "chg": -2.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "DEAD", "chg": -4.0, "price": 1.0, "turnover": 500_000},
            {"ticker": "UP", "chg": 1.0, "price": 1.0, "turnover": 500_000},
        ])
        self.assertEqual([str(r[1]["ticker"]) for r in mild_red], ["UP", "RED", "DEAD"])

        base = dict(armed_ts=100.0, price=1.06, shelf=1.01, trend15="bull", day_chg=8.0, arm_px=1.05)
        self.assertEqual(policy.probe_arm_action(now=160.0, **base), "wait")
        self.assertEqual(
            policy.probe_arm_action(now=100.0 + policy.PROBE_ENTRY_HOLD_SEC, **base),
            "buy",
        )
        self.assertEqual(
            policy.probe_arm_action(now=100.0 + policy.PROBE_ENTRY_HOLD_SEC, **{**base, "price": 1.0}),
            "drop",
        )
        self.assertEqual(
            policy.probe_arm_action(now=100.0 + policy.PROBE_ENTRY_HOLD_SEC, **{**base, "price": 1.10}),
            "drop",
        )
        self.assertEqual(
            policy.probe_arm_action(now=100.0 + policy.PROBE_ENTRY_HOLD_SEC, **base, week_chg=-6.0),
            "drop",
        )
        sprint = dict(base, algo=policy.PROBE_ALGO_SPRINT)
        self.assertEqual(policy.probe_arm_action(now=120.0, **sprint), "wait")
        self.assertEqual(
            policy.probe_arm_action(now=100.0 + policy.PROBE_SPRINT_HOLD_SEC, **sprint),
            "buy",
        )
        # Спринт терпит больший chase, чем классика.
        self.assertEqual(
            policy.probe_arm_action(
                now=100.0 + policy.PROBE_SPRINT_HOLD_SEC,
                **{**sprint, "price": 1.05 * 1.04},
            ),
            "buy",
        )

        quiet5 = [{"high": 1.0, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(14)]
        boom = {"high": 1.08, "low": 1.0, "close": 1.06, "volume": 40.0}
        live5 = {"high": 1.07, "low": 1.04, "close": 1.055, "volume": 30.0}
        impulse = policy.probe_impulse_setup(quiet5 + [boom, live5])
        self.assertIsNotNone(impulse)
        assert impulse is not None
        self.assertAlmostEqual(impulse["shelf"], 1.0)
        self.assertIsNone(
            policy.probe_impulse_setup(
                quiet5 + [boom, {**live5, "close": 1.40, "high": 1.41}]
            )
        )
        # Живой стол: ступенька, иначе выстрел с базы (impulse).
        picked_impulse = policy.probe_pick_setup(
            [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(60)]
            + [{"high": 1.02, "low": 0.99, "close": 1.0, "volume": 10.0},
               {"high": 1.08, "low": 1.0, "close": 1.06, "volume": 40.0}],
            quiet5 + [boom, live5],
        )
        self.assertIsNotNone(picked_impulse)
        assert picked_impulse is not None
        self.assertEqual(picked_impulse["algo"], policy.PROBE_ALGO_IMPULSE)
        picked = policy.probe_pick_setup(
            [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(60)]
            + [{"high": 1.02, "low": 0.99, "close": 1.0, "volume": 10.0},
               {"high": 1.08, "low": 1.0, "close": 1.06, "volume": 40.0}],
            quiet5 + [boom, live5],
            order=(policy.PROBE_ALGO_IMPULSE,),
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["algo"], policy.PROBE_ALGO_IMPULSE)
        # NAVX-подобный выстрел с базы: +22% от часового хая, цена у хая суток.
        navx_base = [
            {"open": 0.00870, "high": 0.00872, "low": 0.00866, "close": 0.00870, "volume": 10.0}
            for _ in range(14)
        ]
        navx_boom = {
            "open": 0.00870,
            "high": 0.01040,
            "low": 0.00868,
            "close": 0.01020,
            "volume": 40.0,
        }
        navx_live = {
            "open": 0.01020,
            "high": 0.01123,
            "low": 0.01018,
            "close": 0.01115,
            "volume": 36.0,
        }
        navx = policy.probe_impulse_setup(navx_base + [navx_boom, navx_live])
        self.assertIsNotNone(navx)
        assert navx is not None
        self.assertEqual(navx.get("impulse"), 1.0)
        self.assertTrue(
            policy.probe_scan_day_ok(price=0.01122, day_high=0.01123, day_chg=22.7)
        )
        self.assertFalse(
            policy.probe_candle_day_ok(
                price=0.01122, day_high=0.01123, day_chg=22.7, stair=True
            )
        )

        # Candle: старт восходящей 5м свечи от open (pos 0.35..0.85), без верхнего фитиля.
        base5 = [
            {"open": 1.0, "high": 1.002, "low": 0.998, "close": 1.0, "volume": 10.0}
            for _ in range(14)
        ]
        rising = {
            "open": 1.0,
            "high": 1.014,
            "low": 0.999,
            "close": 1.008,  # ~0.8% тело, pos=0.60 в зоне 0.35..0.85
            "volume": 25.0,
        }
        self.assertTrue(
            policy.probe_candle_live_ok(
                rising["close"], rising["high"], rising["low"], rising["open"]
            )
        )
        # Тело 0.8% — candle. Пробой полки (4%) к телу свечи больше не привязан.
        self.assertIsNotNone(policy.probe_candle_setup(base5 + [rising]))
        spark_early = {
            **rising,
            "ts": 1000.0,
            "close": 1.012,
            "high": 1.015,
            "low": 0.998,
            "volume": 25.0,
        }
        self.assertIsNotNone(policy.probe_spark_setup(base5 + [spark_early], now=1060.0))
        self.assertIsNone(policy.probe_spark_setup(base5 + [spark_early], now=1130.0))
        self.assertIsNone(policy.probe_spark_setup(base5 + [spark_early]))
        spark_pick = policy.probe_pick_setup(
            [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
            base5 + [spark_early],
            day_chg=8.0,
            day_high=1.08,
            now=1060.0,
        )
        self.assertIsNotNone(spark_pick)
        assert spark_pick is not None
        # Порядок: impulse/pop/spark/candle — ранняя свеча берётся как spark.
        self.assertEqual(spark_pick["algo"], policy.PROBE_ALGO_SPARK)
        self.assertEqual(policy.probe_algo_params(policy.PROBE_ALGO_SPARK)["hold_sec"], 0.0)
        self.assertEqual(policy.probe_setup_hold_sec({"algo": "spark", "hold_sec": 0.0}), 0.0)
        self.assertEqual(policy.probe_setup_hold_sec({"algo": "spark"}), 90.0)
        # Подскок ICNT: тело 2.77% после окна spark, объём 4×, фитиль короткий.
        pop_bar = {
            "ts": 1000.0,
            "open": 1.0,
            "high": 1.0285,
            "low": 0.998,
            "close": 1.0277,
            "volume": 40.0,
        }
        self.assertIsNone(policy.probe_spark_setup(base5 + [pop_bar], now=1180.0))
        pop = policy.probe_pop_setup(base5 + [pop_bar], now=1180.0)
        self.assertIsNotNone(pop)
        # Дно: тело 2.77% и 4× объёма проходит; 2× и тело 1.6% — нет.
        self.assertIsNotNone(
            policy.probe_pop_setup(base5 + [pop_bar], now=1180.0, day_chg=0.5)
        )
        self.assertIsNone(
            policy.probe_pop_setup(
                base5 + [{**pop_bar, "volume": 12.0}], now=1180.0, day_chg=0.5
            )
        )
        self.assertIsNone(
            policy.probe_pop_setup(
                base5 + [{**pop_bar, "close": 1.016, "high": 1.017}],
                now=1180.0,
                day_chg=-1.0,
            )
        )
        self.assertEqual(policy.PROBE_NO_TRAP_TIMEOUT_SEC, 10 * 60)
        assert pop is not None
        self.assertAlmostEqual(pop["shelf"], 1.0)
        self.assertEqual(pop.get("pop"), 1.0)
        picked_pop = policy.probe_pick_setup(
            [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
            base5 + [pop_bar],
            day_chg=8.0,
            day_high=1.05,
            now=1180.0,
        )
        self.assertIsNotNone(picked_pop)
        assert picked_pop is not None
        self.assertEqual(picked_pop["algo"], policy.PROBE_ALGO_POP)
        self.assertEqual(picked_pop.get("hold_sec"), 0.0)
        self.assertIsNone(policy.probe_pop_setup(base5 + [spark_early], now=1180.0))
        fat = {**pop_bar, "close": 1.095, "high": 1.096}
        self.assertIsNone(policy.probe_pop_setup(base5 + [fat], now=1180.0))
        wide_wick = {**pop_bar, "high": 1.055}
        self.assertIsNone(policy.probe_pop_setup(base5 + [wide_wick], now=1180.0))
        thin = {**pop_bar, "volume": 11.0}
        self.assertIsNone(policy.probe_pop_setup(base5 + [thin], now=1180.0))
        climax_base = list(base5)
        climax_base[-6] = {**climax_base[-6], "open": 0.85, "close": 0.85}
        self.assertIsNone(policy.probe_pop_setup(climax_base + [pop_bar], now=1180.0))
        picked_at_high = policy.probe_pick_setup(
            [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
            base5 + [pop_bar],
            day_chg=8.0,
            day_high=1.028,
            now=1180.0,
        )
        self.assertIsNotNone(picked_at_high)
        assert picked_at_high is not None
        self.assertEqual(picked_at_high["algo"], policy.PROBE_ALGO_POP)
        self.assertFalse(policy.PROBE_SHELF_EXIT_ON)
        self.assertTrue(policy.PROBE_STRICT_MODE)
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.02,
                entry=1.0277,
                shelf=1.0,
                trend15="bear",
                peak=1.0277,
                algo=policy.PROBE_ALGO_POP,
                qty=40.0 / 1.0277,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_FAIL_SCRATCH_SEC + 1.0,
            )
        )
        # −1.8% на soft-полке (не пробой open) — работает при PROBE_SHELF_EXIT_ON=False.
        self.assertEqual(
            policy.probe_exit_reason(
                price=0.995,
                entry=1.0277,
                shelf=1.0,
                trend15="bull",
                peak=1.0277,
                algo=policy.PROBE_ALGO_POP,
                qty=40.0 / 1.0277,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_FAIL_SCRATCH_SEC + 1.0,
            ),
            "полка",
        )
        self.assertTrue(
            policy.probe_setup_ok_now(
                {"pop": 1.0}, now=1_700_000_000.0, turnover=1_600_000, strict=True
            )
        )
        at_high = {**spark_early, "close": 1.016, "high": 1.017, "low": 1.000}
        self.assertTrue(
            policy.probe_spark_live_ok(
                at_high["close"], at_high["high"], at_high["low"], at_high["open"]
            )
        )
        # Перегрев 30м не режет тело внутри 0.6–2%. Выше 2% — уже не candle и не spark.
        heat = [
            {"open": 1.0, "high": 1.002, "low": 0.998, "close": 1.0, "volume": 10.0}
            for _ in range(8)
        ] + [
            {"open": 0.980, "high": 0.982, "low": 0.978, "close": 0.980, "volume": 10.0}
            for _ in range(6)
        ]
        heat_bar = {
            "open": 1.0,
            "high": 1.012,
            "low": 0.999,
            "close": 1.010,
            "volume": 25.0,
            "ts": 1000.0,
        }
        self.assertIsNotNone(policy.probe_candle_setup(heat + [heat_bar], now=1060.0))
        self.assertIsNotNone(policy.probe_spark_setup(heat + [heat_bar], now=1060.0))
        heat_break = {**heat_bar, "close": 1.045, "high": 1.050}
        self.assertIsNone(policy.probe_candle_setup(heat + [heat_break], now=1060.0))
        self.assertIsNone(policy.probe_spark_setup(heat + [heat_break], now=1060.0))
        collapsed_wick = {**rising, "close": 1.006, "high": 1.020}
        self.assertIsNone(policy.probe_candle_setup(base5 + [collapsed_wick]))
        tip_candle = {**rising, "close": 1.011, "high": 1.012}
        self.assertIsNotNone(policy.probe_candle_setup(base5 + [tip_candle]))
        late_body = {**rising, "close": 1.022, "high": 1.023}
        self.assertIsNone(policy.probe_candle_setup(base5 + [late_body]))
        early_bar = {**rising, "ts": 1000.0, "volume": 4.0}
        self.assertIsNone(policy.probe_candle_setup(base5 + [early_bar]))
        self.assertIsNone(policy.probe_candle_setup(base5 + [early_bar], now=1045.0))
        # buy_ok: тело 0.6–2% идёт через spark (фитиль и пролив), выше потолка — нет.
        self.assertTrue(
            policy.probe_candle_buy_ok(1.008, 1.010, 0.999, 1.0)
        )
        self.assertFalse(
            policy.probe_candle_buy_ok(1.025, 1.026, 0.999, 1.0)
        )
        self.assertFalse(
            policy.probe_candle_buy_ok(1.003, 1.02, 0.999, 1.0)
        )
        # Плотная ступенька (body 0.5%, wick 0.05%, pos 0.90) проходит buy_ok.
        self.assertTrue(
            policy.probe_candle_buy_ok(1.005, 1.0055, 1.000, 1.000)
        )
        # PIEVERSE: px == hi, фитиль 0. tip_cap = hi*0.9995 всегда ниже цены.
        self.assertTrue(
            policy.probe_candle_buy_ok(1.490, 1.490, 1.489, 1.489)
        )
        # Фитиль 0.02% тоже у хая: порог пропуска 0.15%.
        self.assertTrue(
            policy.probe_candle_buy_ok(1.4897, 1.490, 1.489, 1.489)
        )
        # Контроль спреда и проскальзывания ask (GROVE: 0.007281 -> ask 0.007409).
        self.assertTrue(
            policy.probe_book_buy_ok(bid=0.00727, ask=0.00729, arm_px=0.007281)
        )
        self.assertFalse(
            policy.probe_book_buy_ok(bid=0.00720, ask=0.00729, arm_px=0.007281)
        )
        self.assertFalse(
            policy.probe_book_buy_ok(bid=0.00739, ask=0.007409, arm_px=0.007281)
        )
        self.assertEqual(policy.PROBE_MAX_SPREAD_PCT, 0.55)
        self.assertEqual(policy.PROBE_MAX_ASK_SLIP_PCT, 0.65)
        self.assertTrue(
            policy.probe_book_buy_ok(bid=0.04051, ask=0.04071, arm_px=0.0405)
        )
        self.assertEqual(policy.PROBE_MIN_ENTRY_OBI, 0.55)
        self.assertTrue(policy.probe_entry_obi_ok(None))
        self.assertFalse(policy.probe_entry_obi_ok(0.50))
        self.assertFalse(policy.probe_entry_obi_ok(0.51))
        self.assertTrue(policy.probe_entry_obi_ok(0.55))
        self.assertTrue(policy.probe_entry_obi_ok(0.80))
        self.assertTrue(policy.probe_entry_obi_ok(0.70, strict=True))
        self.assertTrue(policy.probe_entry_obi_ok(0.60, strict=True))
        self.assertFalse(policy.probe_entry_obi_ok(0.59, strict=True))
        self.assertFalse(policy.probe_entry_obi_ok(0.40, strict=True))
        self.assertFalse(
            policy.probe_book_buy_ok(bid=0.00727, ask=0.00729, arm_px=0.007281, strict=True)
        )
        # Ступенька мельче probe_break_min_pct — не вход.
        stair_bars = base5[:-2] + [
            {"open": 1.000, "high": 1.004, "low": 1.000, "close": 1.003, "volume": 12.0},
            {"open": 1.003, "high": 1.008, "low": 1.003, "close": 1.007, "volume": 20.0},
            {"open": 1.007, "high": 1.011, "low": 1.007, "close": 1.0105, "volume": 18.0},
        ]
        self.assertIsNone(policy.probe_candle_setup(stair_bars))
        self.assertIsNone(
            policy.probe_pick_setup(
                [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
                stair_bars,
                day_chg=7.2,
                day_high=1.015,
            )
        )
        self.assertLessEqual(policy.PROBE_CANDLE_HOLD_SEC, 2)
        stair2_bars = base5[:-2] + [
            {"open": 1.001, "high": 1.001, "low": 0.999, "close": 1.000, "volume": 10.0},
            {"open": 1.000, "high": 1.005, "low": 1.000, "close": 1.0045, "volume": 20.0},
            {"open": 1.0045, "high": 1.0095, "low": 1.0045, "close": 1.009, "volume": 16.0},
        ]
        self.assertIsNone(policy.probe_candle_setup(stair2_bars))
        self.assertTrue(policy.PROBE_DAILY_LOSS_ON)
        self.assertFalse(policy.probe_daily_loss_blocked(-5.5))
        self.assertTrue(policy.probe_daily_loss_blocked(-6.0))
        with patch.object(policy, "PROBE_DAILY_LOSS_ON", False):
            self.assertFalse(policy.probe_daily_loss_blocked(-6.0))
        self.assertEqual(
            policy.PROBE_ALGO_ORDER,
            (
                policy.PROBE_ALGO_IMPULSE,
                policy.PROBE_ALGO_POP,
                policy.PROBE_ALGO_SPARK,
                policy.PROBE_ALGO_CANDLE,
            ),
        )
        self.assertNotIn(policy.PROBE_ALGO_GRIND, policy.PROBE_ALGO_ORDER)
        self.assertGreaterEqual(policy.PROBE_CANDLE_MIN_BODY_PCT, 0.6)
        self.assertEqual(policy.probe_candle_min_body_pct(), policy.PROBE_CANDLE_MIN_BODY_PCT)
        self.assertEqual(policy.probe_candle_max_body_pct(), policy.PROBE_CANDLE_MAX_BODY_PCT)
        self.assertEqual(policy.PROBE_POP_MIN_BODY_PCT, 1.5)
        self.assertEqual(policy.PROBE_POP_MAX_BODY_PCT, 8.5)
        self.assertEqual(policy.PROBE_POP_MAX_WICK_PCT, 1.80)
        self.assertEqual(policy.PROBE_POP_MAX_30M_CHG_PCT, 16.0)
        self.assertEqual(policy.PROBE_CANDLE_MAX_BAR_POS, 0.98)
        weak = {**rising, "close": 1.004, "high": 1.012}
        self.assertIsNone(policy.probe_candle_setup(base5 + [weak]))
        # Ступенька мельче пробоя — не вход. +48% и пик суток — тоже нет.
        self.assertIsNone(
            policy.probe_pick_setup(
                [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
                stair_bars,
                day_chg=8.0,
                day_high=1.02,
            )
        )
        self.assertIsNone(
            policy.probe_pick_setup(
                [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
                stair_bars,
                day_chg=48.0,
                day_high=1.12,
            )
        )
        # У самого пика суток — нет; глубокий слив с хая (>28%) — нет.
        self.assertIsNone(
            policy.probe_pick_setup(
                [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
                stair_bars,
                day_chg=8.0,
                day_high=1.01,
            )
        )
        self.assertIsNone(
            policy.probe_pick_setup(
                [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(80)],
                stair_bars,
                day_chg=8.0,
                day_high=1.50,
            )
        )
        self.assertTrue(
            policy.probe_trends_ok("neutral", "bull", 8.0, algo=policy.PROBE_ALGO_CANDLE)
        )
        self.assertFalse(
            policy.probe_trends_ok("neutral", "neutral", 8.0, algo=policy.PROBE_ALGO_CANDLE)
        )
        self.assertFalse(
            policy.probe_trends_ok("bear", "neutral", 8.0, algo=policy.PROBE_ALGO_CANDLE)
        )
        self.assertTrue(
            policy.probe_trends_ok("neutral", "bear", 8.0, algo=policy.PROBE_ALGO_SPARK)
        )
        self.assertTrue(
            policy.probe_trends_ok("neutral", "neutral", 8.0, algo=policy.PROBE_ALGO_POP)
        )
        self.assertTrue(
            policy.probe_trends_ok("bull", "bear", 8.0, algo=policy.PROBE_ALGO_POP)
        )
        self.assertTrue(
            policy.probe_trends_ok("bear", "bull", 8.0, algo=policy.PROBE_ALGO_SPARK)
        )
        self.assertTrue(
            policy.probe_trends_ok("bear", "bear", 8.0, algo=policy.PROBE_ALGO_POP)
        )
        # Impulse: ракета со дна при bear 15м/1ч.
        self.assertTrue(
            policy.probe_trends_ok("bear", "bear", 8.0, algo=policy.PROBE_ALGO_IMPULSE)
        )
        self.assertTrue(
            policy.probe_candle_day_ok(
                price=1.0, day_high=1.0, day_chg=3.0, allow_breakout=True
            )
        )
        self.assertTrue(
            policy.probe_candle_day_ok(
                price=1.004, day_high=1.0, day_chg=3.0, allow_breakout=True
            )
        )
        self.assertFalse(
            policy.probe_candle_day_ok(
                price=1.008, day_high=1.0, day_chg=3.0, allow_breakout=True
            )
        )
        self.assertFalse(
            policy.probe_candle_day_ok(price=1.0, day_high=1.0, day_chg=3.0)
        )
        # Arm не должен снимать candle на neutral 15м (иначе INIT вечный wait→drop).
        self.assertEqual(
            policy.probe_arm_action(
                armed_ts=100.0,
                now=100.0 + policy.PROBE_CANDLE_HOLD_SEC,
                price=1.006,
                shelf=1.0,
                trend15="neutral",
                day_chg=10.0,
                arm_px=1.005,
                algo=policy.PROBE_ALGO_CANDLE,
            ),
            "buy",
        )
        self.assertEqual(
            policy.probe_arm_action(
                armed_ts=100.0,
                now=100.0 + policy.PROBE_CANDLE_HOLD_SEC,
                price=1.006,
                shelf=1.0,
                trend15="bear",
                day_chg=10.0,
                arm_px=1.005,
                algo=policy.PROBE_ALGO_CANDLE,
            ),
            "drop",
        )
        late_day = policy.probe_pick_setup(
            [],
            quiet5 + [boom, live5],
            order=(policy.PROBE_ALGO_IMPULSE,),
            day_chg=40.0,
        )
        self.assertIsNone(late_day)
        grind_bars = (
            [{"high": 1.0, "low": 0.98, "close": 0.99, "volume": 10.0} for _ in range(20)]
            + [
                {"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0},
                {"high": 1.02, "low": 1.0, "close": 1.015, "volume": 12.0},
                {"high": 1.03, "low": 1.01, "close": 1.025, "volume": 12.0},
                {"high": 1.04, "low": 1.02, "close": 1.035, "volume": 12.0},
            ]
        )
        late_grind = policy.probe_pick_setup(
            grind_bars,
            None,
            order=(policy.PROBE_ALGO_GRIND,),
            day_chg=22.0,
        )
        self.assertIsNone(late_grind)
        self.assertEqual(
            policy.probe_arm_action(
                now=100.0 + policy.PROBE_IMPULSE_HOLD_SEC,
                armed_ts=100.0,
                price=1.06,
                shelf=1.01,
                trend15="bull",
                day_chg=40.0,
                arm_px=1.05,
                algo=policy.PROBE_ALGO_IMPULSE,
            ),
            "drop",
        )
        # CAP: impulse на neutral 15м после холда покупаем, не снимаем.
        self.assertEqual(
            policy.probe_arm_action(
                now=100.0 + policy.PROBE_IMPULSE_HOLD_SEC,
                armed_ts=100.0,
                price=1.06,
                shelf=1.01,
                trend15="neutral",
                day_chg=8.0,
                arm_px=1.05,
                algo=policy.PROBE_ALGO_IMPULSE,
            ),
            "buy",
        )
        pin_bars = [
            {"open": 1.00, "high": 1.01, "low": 0.99, "close": 1.00, "volume": 10.0},
            {"open": 1.00, "high": 1.02, "low": 1.00, "close": 1.015, "volume": 12.0},
            {"open": 1.015, "high": 1.04, "low": 1.01, "close": 1.035, "volume": 14.0},
            {"open": 1.035, "high": 1.05, "low": 1.03, "close": 1.048, "volume": 16.0},
            {"open": 1.048, "high": 1.06, "low": 1.04, "close": 1.055, "volume": 18.0},
            {"open": 1.055, "high": 1.07, "low": 1.05, "close": 1.062, "volume": 20.0},
            {"open": 1.055, "high": 1.062, "low": 1.052, "close": 1.058, "volume": 24.0},
        ]
        pin_setup = policy.probe_pin_setup(pin_bars)
        self.assertIsNotNone(pin_setup)
        assert pin_setup is not None
        self.assertAlmostEqual(pin_setup["shelf"], 1.05)
        picked = policy.probe_pick_setup(
            [],
            pin_bars,
            ticker="CAP",
            pin_ticker="CAP",
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["algo"], policy.PROBE_ALGO_PIN)
        self.assertIsNone(
            policy.probe_pick_setup([], pin_bars, ticker="CAP", pin_ticker="TA")
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.017,
                entry=1.0,
                shelf=0.99,
                trend15="bull",
                peak=1.03,
                algo=policy.PROBE_ALGO_PIN,
                qty=40.0,
            ),
            "вершина",
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.025,
                entry=1.0,
                shelf=0.99,
                trend15="bull",
                peak=1.03,
                algo=policy.PROBE_ALGO_PIN,
                qty=40.0,
            )
        )

        stair = []
        px = 1.0
        for i in range(16):
            px = 1.0 + i * 0.004
            stair.append({
                "high": px * 1.002,
                "low": px * 0.998,
                "close": px,
                "volume": 10.0,
            })
        # Откат от хая живой свечи ≥ tip_gap и не верхняя доля бара.
        live_grind = {
            "high": px * 1.013,
            "low": px * 0.999,
            "close": px * 1.000,  # ≥1.2% от хая, не верх бара
            "volume": 12.0,
        }
        self.assertTrue(
            policy.probe_grind_live_ok(
                live_grind["close"], live_grind["high"], live_grind["low"]
            )
        )
        grind = policy.probe_grind_setup(stair + [live_grind])
        self.assertIsNotNone(grind)
        assert grind is not None
        self.assertLess(grind["shelf"], grind["price"])
        tip = {
            "high": px * 1.012,
            "low": px * 0.999,
            "close": px * 1.011,  # у хая свечи — поздно
            "volume": 12.0,
        }
        self.assertFalse(
            policy.probe_grind_live_ok(tip["close"], tip["high"], tip["low"])
        )
        self.assertIsNone(policy.probe_grind_setup(stair + [tip]))
        # Верхняя доля бара при формальном tip_gap — тоже поздно.
        upper = {
            "high": px * 1.02,
            "low": px * 1.0,
            "close": px * 1.015,  # 75% высоты свечи
            "volume": 12.0,
        }
        self.assertFalse(
            policy.probe_grind_live_ok(upper["close"], upper["high"], upper["low"])
        )
        spike = {
            "high": px * 1.03,
            "low": px * 0.999,
            "close": px * 1.003,
            "volume": 20.0,
        }
        self.assertIsNone(policy.probe_grind_setup(stair + [spike]))
        steep = []
        level = 1.0
        for _ in range(16):
            level *= 1.012
            steep.append({
                "high": level * 1.002,
                "low": level * 0.998,
                "close": level,
                "volume": 10.0,
            })
        steep_live = {
            "high": level * 1.01,
            "low": level * 0.999,
            "close": level * 1.002,
            "volume": 12.0,
        }
        self.assertIsNone(policy.probe_grind_setup(steep + [steep_live]))
        ripped = {**live_grind, "close": px * 1.05, "high": px * 1.05}
        self.assertIsNone(policy.probe_grind_setup(stair + [ripped]))
        flat = [{"high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0} for _ in range(16)]
        self.assertIsNone(policy.probe_grind_setup(flat + [live_grind]))
        self.assertEqual(policy.PROBE_GRIND_TIP_GAP_PCT, 1.2)
        self.assertEqual(policy.PROBE_GRIND_TIP_FRAC, 0.45)
        self.assertEqual(policy.PROBE_GRIND_CHASE_PCT, 0.5)
        self.assertEqual(policy.PROBE_GRIND_HOLD_SEC, 10)
        self.assertEqual(policy.PROBE_GRIND_MAX_DAY_PCT, 18.0)
        # grind/candle не режем soft «тренд» в мелком минусе (BLEND).
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.0502,
                entry=0.05085,
                shelf=0.04921,
                trend15="bear",
                peak=0.05094,
                algo=policy.PROBE_ALGO_GRIND,
            )
        )
        # INIT: касание open свечи — не полка; пробой open −0,4% — полка.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.2205,
                entry=0.22309,
                shelf=0.2205,
                trend15="bull",
                peak=0.2234,
                algo=policy.PROBE_ALGO_CANDLE,
            )
        )
        with patch.object(policy, "PROBE_SHELF_EXIT_ON", True):
            self.assertEqual(
                policy.probe_exit_reason(
                    price=0.2195,
                    entry=0.22309,
                    shelf=0.2205,
                    trend15="bull",
                    peak=0.2234,
                    algo=policy.PROBE_ALGO_CANDLE,
                ),
                "полка",
            )
        # −2.86% уже короткий стоп (−1.8%), даже у grind.
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.02,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                arm_px=1.07,
                peak=1.06,
                algo=policy.PROBE_ALGO_GRIND,
            ),
            "полка",
        )
        # Мягкий минус < −1.8% — ещё «ожидание».
        self.assertEqual(
            policy.probe_exit_reason(price=1.04, entry=1.05, shelf=1.0, trend15="bull"),
            "ожидание",
        )
        self.assertEqual(
            policy.probe_exit_reason(price=1.04, entry=1.05, shelf=1.0, trend15="neutral"),
            "ожидание",
        )
        with patch.object(policy, "PROBE_SHELF_EXIT_ON", True):
            self.assertEqual(
                policy.probe_exit_reason(price=1.0, entry=1.05, shelf=1.0, trend15="bull"),
                "полка",
            )
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.04, entry=1.05, shelf=1.0, trend15="bull", entry_low=1.04
            ),
            "дно",
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.04, entry=1.0, shelf=0.5, trend15="bull", entry_low=1.05, peak=1.07
            ),
            "вершина",
        )
        self.assertEqual(
            policy.probe_exit_reason(price=0.96, entry=1.05, shelf=0.5, trend15="bull"),
            "стоп",
        )
        self.assertIsNone(policy.probe_exit_reason(price=1.20, entry=1.05, shelf=1.0, trend15="bull"))
        self.assertIsNone(policy.probe_exit_reason(price=1.19, entry=1.05, shelf=1.0, trend15="neutral"))
        self.assertEqual(
            policy.probe_exit_reason(price=1.19, entry=1.05, shelf=1.0, trend15="bear"),
            "тренд",
        )
        # Ловушка: пик покрыл комиссию+$0.50, откат −0,5% при ещё живом плюсе.
        qty100 = 100.0 / 1.05
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.10,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                arm_px=1.08,
                peak=1.20,
                qty=qty100,
            ),
            "вершина",
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.06, entry=1.05, shelf=1.0, trend15="bull", arm_px=1.07, peak=1.08,
            ),
            "вершина",
        )
        # Ещё растём — ловушка молчит.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.12,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                peak=1.12,
                qty=qty100,
                algo=policy.PROBE_ALGO_GRIND,
            )
        )
        # Подножка: −2.86% уже короткий стоп; лёгкий минус без soft-среза.
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.02,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                arm_px=1.07,
                peak=1.06,
                algo=policy.PROBE_ALGO_SPRINT,
            ),
            "полка",
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.04,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                arm_px=1.07,
                peak=1.06,
                algo=policy.PROBE_ALGO_SPRINT,
            )
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.04,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                entry_low=1.04,
                algo=policy.PROBE_ALGO_IMPULSE,
            )
        )
        # Минус к входу за 90 с при живой полке — держим (ICNT 0.2306→0.2295 над 0.2279).
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.04,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                peak=1.05,
                algo=policy.PROBE_ALGO_IMPULSE,
                qty=40.0 / 1.05,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_FAIL_SCRATCH_SEC + 1.0,
            )
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.2295,
                entry=0.2306,
                shelf=0.2279,
                trend15="bull",
                peak=0.2306,
                algo=policy.PROBE_ALGO_SPARK,
                qty=40.0 / 0.2306,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_FAIL_SCRATCH_SEC + 1.0,
            )
        )
        with patch.object(policy, "PROBE_SHELF_EXIT_ON", True):
            self.assertEqual(
                policy.probe_exit_reason(
                    price=0.99,
                    entry=1.05,
                    shelf=1.0,
                    trend15="bull",
                    peak=1.05,
                    algo=policy.PROBE_ALGO_IMPULSE,
                    qty=40.0 / 1.05,
                    opened_ts=1000.0,
                    now=1000.0 + policy.PROBE_FAIL_SCRATCH_SEC + 1.0,
                ),
                "полка",
            )
        self.assertFalse(policy.probe_should_handoff("срез"))
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.04,
                entry=1.05,
                shelf=1.0,
                trend15="bull",
                peak=1.05,
                algo=policy.PROBE_ALGO_IMPULSE,
                qty=40.0 / 1.05,
                opened_ts=1000.0,
                now=1000.0 + 30.0,
            )
        )
        with patch.object(policy, "PROBE_SHELF_EXIT_ON", True):
            self.assertEqual(
                policy.probe_exit_reason(
                    price=1.0,
                    entry=1.05,
                    shelf=1.0,
                    trend15="bull",
                    algo=policy.PROBE_ALGO_SPRINT,
                ),
                "полка",
            )
        self.assertEqual(policy.probe_quote(), 40.0)
        self.assertTrue(policy.probe_should_handoff("ожидание"))
        self.assertTrue(policy.probe_should_handoff("тренд"))
        self.assertTrue(policy.probe_should_handoff("застой"))
        self.assertFalse(policy.probe_should_handoff("дно"))
        self.assertFalse(policy.probe_should_handoff("полка"))
        self.assertFalse(policy.probe_should_handoff("стоп"))
        self.assertFalse(policy.probe_should_handoff("вершина"))
        self.assertEqual(policy.pocket_from_probe_max(), 3)
        self.assertEqual(policy.PROBE_MAX_LEGS, 3)
        self.assertEqual(policy.PROBE_HUNTER_MAX_LEGS, 2)
        self.assertEqual(policy.PROBE_SNIPER_MAX_LEGS, 1)
        best_leg = policy.pick_second_pocket_leg_for_funding(
            [
                {"ticker": "AGLD", "qty": 160.0, "entry": 0.1872},
                {"ticker": "SQD", "qty": 1533.0, "entry": 0.01957},
            ],
            {"AGLD": 0.1840, "SQD": 0.01945},
        )
        self.assertIsNotNone(best_leg)
        assert best_leg is not None
        self.assertEqual(best_leg["ticker"], "SQD")
        now = 1_700_000_000.0
        room = policy.pick_probe_leg_for_room(
            [
                {"ticker": "SKY", "qty": 600.0, "entry": 0.066, "ts": now - 3600},
                {"ticker": "LTC", "qty": 0.7, "entry": 55.0, "ts": now - 1200},
                {"ticker": "NAVX", "qty": 4000.0, "entry": 0.01, "ts": now - 60},
            ],
            {"SKY": 0.065, "LTC": 55.2, "NAVX": 0.011},
            now=now,
        )
        self.assertIsNotNone(room)
        assert room is not None
        self.assertEqual(room["ticker"], "SKY")
        self.assertIsNone(
            policy.pick_probe_leg_for_room(
                [{"ticker": "NAVX", "qty": 1.0, "entry": 0.01, "ts": now - 60}],
                {"NAVX": 0.011},
                now=now,
            )
        )
        # Candle +2%: откат 0.2% держим, 0.5% продаём. Широкий 1.5% остался у impulse.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.05477 * (1.0 - 0.002),
                entry=0.053678,
                shelf=0.05279,
                trend15="bull",
                peak=0.05477,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=558.89,
            )
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=0.05477 * (1.0 - 0.005),
                entry=0.053678,
                shelf=0.05279,
                trend15="bull",
                peak=0.05477,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=558.89,
            ),
            "вершина",
        )
        self.assertAlmostEqual(
            policy.exit_trap_arm_pct(cost_usd=40.0, extra_usd=policy.PROBE_TRAP_EXTRA_USD),
            1.56,
            places=2,
        )
        self.assertAlmostEqual(
            policy.exit_trap_arm_pct(cost_usd=40.0, extra_usd=policy.PROBE_FAST_TRAP_EXTRA_USD),
            0.99,
            places=2,
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=0.982,
                entry=1.0,
                shelf=0.94,
                trend15="bull",
                peak=1.0,
                algo=policy.PROBE_ALGO_SPARK,
                qty=30.0,
            ),
            "полка",
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.990,
                entry=1.0,
                shelf=0.94,
                trend15="bull",
                peak=1.0,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=30.0,
            )
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=0.997,
                entry=1.0,
                shelf=0.97,
                trend15="bull",
                peak=1.013,
                algo=policy.PROBE_ALGO_POP,
                qty=30.0,
            ),
            "полка",
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.9995,
                entry=1.0,
                shelf=0.97,
                trend15="bull",
                peak=1.013,
                algo=policy.PROBE_ALGO_POP,
                qty=30.0,
            )
        )
        # Пик +1.7% и откат 0.5% ещё выше круга комиссии. Откат 1.5% уже ниже — не продаём.
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.017 * (1.0 - 0.005),
                entry=1.0,
                shelf=0.99,
                trend15="neutral",
                peak=1.017,
                algo=policy.PROBE_ALGO_POP,
                qty=40.0,
            ),
            "вершина",
        )
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.017 * (1.0 - 0.015),
                entry=1.0,
                shelf=0.99,
                trend15="neutral",
                peak=1.017,
                algo=policy.PROBE_ALGO_POP,
                qty=40.0,
            )
        )
        # Пик +5% дышит до отката 1%.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.05 * (1.0 - 0.006),
                entry=1.0,
                shelf=0.99,
                trend15="bull",
                peak=1.05,
                algo=policy.PROBE_ALGO_SPARK,
                qty=40.0,
            )
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.05 * (1.0 - 0.011),
                entry=1.0,
                shelf=0.99,
                trend15="bull",
                peak=1.05,
                algo=policy.PROBE_ALGO_SPARK,
                qty=40.0,
            ),
            "вершина",
        )
        # Шум +0.8% больше не взводит ловушку щупа.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.006,
                entry=1.0,
                shelf=0.99,
                trend15="bull",
                peak=1.008,
                algo=policy.PROBE_ALGO_SPARK,
                qty=40.0,
            )
        )
        # Нет трапа и цена ≤ входа ≥ 20м → «срез» (не ждём 2ч).
        self.assertEqual(
            policy.probe_exit_reason(
                price=0.3143,
                entry=0.3174,
                shelf=0.314,
                trend15="neutral",
                peak=0.3199,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=94.5,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_NO_TRAP_TIMEOUT_SEC + 1.0,
            ),
            "срез",
        )
        # До таймаута при живой полке candle не режем.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.3143,
                entry=0.3174,
                shelf=0.314,
                trend15="neutral",
                peak=0.3199,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=94.5,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_NO_TRAP_TIMEOUT_SEC - 1.0,
            )
        )
        # Трап взвёлся, лёгкий минус (выше BE−0.1%), ≥2ч → «застой», не полка-BE.
        self.assertEqual(
            policy.probe_exit_reason(
                price=0.3172,
                entry=0.3174,
                shelf=0.314,
                trend15="neutral",
                peak=0.3230,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=94.5,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_CANDLE_STALE_SEC + 1.0,
            ),
            "застой",
        )
        # Плюс над входом не режем scratch: ждём ловушку или полку.
        self.assertIsNone(
            policy.probe_exit_reason(
                price=0.3180,
                entry=0.3174,
                shelf=0.314,
                trend15="neutral",
                peak=0.3199,
                algo=policy.PROBE_ALGO_CANDLE,
                qty=94.5,
                opened_ts=1000.0,
                now=1000.0 + policy.PROBE_CANDLE_STALE_SEC + 1.0,
            )
        )
        # В кармане2 нога со щупа (scratch=True) закрывается в безубыток при -0.20% и выше.
        self.assertIsNone(
            policy.btc_dip_exit_reason(price=0.995, entry=1.0, scratch=True)
        )
        self.assertEqual(
            policy.btc_dip_exit_reason(price=0.9985, entry=1.0, scratch=True),
            "безубыток",
        )
        self.assertTrue(policy.pocket_unwind_ready(price=0.2803, bid=0.2795))
        self.assertFalse(policy.pocket_unwind_ready(price=0.2803, bid=0.2780))
        self.assertTrue(policy.pocket_unwind_ready(price=0.2803, bid=0.0))
        self.assertFalse(policy.pocket_unwind_ready(price=0.0, bid=0.2795))

    def test_btc_dip_rules(self):
        self.assertTrue(policy.btc_dip_should_buy(day_chg=-3.0, qty=0))
        self.assertFalse(policy.btc_dip_should_buy(day_chg=-3.0, qty=0.01))
        self.assertFalse(policy.btc_dip_should_buy(day_chg=-2.0, qty=0))
        bars = [{"high": 1.05, "low": 1.00, "close": 1.02, "volume": 1.0} for _ in range(16)]
        dip = bars + [{"high": 1.01, "low": 0.99, "close": 1.00, "volume": 1.0}]
        self.assertAlmostEqual(policy.pocket_swing(dip), 0.99)
        shallow = [{"high": 1.05, "low": 1.03, "close": 1.04, "volume": 1.0} for _ in range(16)]
        shallow.append({"high": 1.05, "low": 1.03, "close": 1.04, "volume": 1.0})
        self.assertIsNone(policy.pocket_swing(shallow))
        armed = policy.update_pocket_swing_arm(None, price=1.0, swing_low=0.99, now=1.0)
        self.assertAlmostEqual(armed["low"], 0.99)
        kept = policy.update_pocket_swing_arm(armed, price=1.0, swing_low=None, now=2.0)
        self.assertAlmostEqual(kept["low"], 0.99)
        self.assertIsNone(policy.update_pocket_swing_arm(armed, price=1.04, swing_low=None, now=2.0))
        self.assertEqual(
            policy.btc_dip_exit_reason(price=0.98, entry=1.0, swing_low=0.99),
            "дно",
        )
        self.assertIsNone(policy.btc_dip_exit_reason(price=1.02, entry=1.0, swing_low=0.99))
        fresh = [
            {"high": 1.05, "low": 0.99, "close": 1.00, "volume": 1.0},
            {"high": 1.02, "low": 1.00, "close": 1.01, "volume": 1.0},
        ]
        self.assertFalse(policy.pocket_dip_held(fresh, 0.99))
        waiting = [
            {"high": 1.08, "low": 1.05, "close": 1.06, "volume": 1.0},
            {"high": 1.05, "low": 0.99, "close": 1.00, "volume": 1.0},
            {"high": 1.03, "low": 1.01, "close": 1.02, "volume": 1.0},
        ]
        self.assertFalse(policy.pocket_dip_held(waiting, 0.99))
        held = [
            {"high": 1.05, "low": 0.99, "close": 1.00, "volume": 1.0},
            {"high": 1.03, "low": 1.00, "close": 1.01, "volume": 1.0},
            {"high": 1.04, "low": 1.01, "close": 1.02, "volume": 1.0},
        ]
        self.assertTrue(policy.pocket_dip_held(held, 0.99))
        stale = held + [{"high": 1.06, "low": 1.03, "close": 1.04, "volume": 1.0}]
        self.assertFalse(policy.pocket_dip_held(stale, 0.99))
        undercut = [
            {"high": 1.05, "low": 0.99, "close": 1.00, "volume": 1.0},
            {"high": 1.02, "low": 0.98, "close": 1.00, "volume": 1.0},
            {"high": 1.03, "low": 1.00, "close": 1.01, "volume": 1.0},
        ]
        self.assertFalse(policy.pocket_dip_held(undercut, 0.99))
        # Тик ленты чуть ниже low свечи — это та же свеча.
        tape = [
            {"high": 1.05, "low": 0.994, "close": 1.00, "volume": 1.0},
            {"high": 1.04, "low": 1.00, "close": 1.02, "volume": 1.0},
            {"high": 1.05, "low": 1.02, "close": 1.04, "volume": 1.0},
        ]
        self.assertTrue(policy.pocket_dip_held(tape, 0.99))
        self.assertFalse(policy.pocket_dip_pending(tape, 0.99))
        waiting_tape = [
            {"high": 1.08, "low": 1.05, "close": 1.06, "volume": 1.0},
            {"high": 1.05, "low": 0.994, "close": 1.00, "volume": 1.0},
            {"high": 1.04, "low": 1.01, "close": 1.03, "volume": 1.0},
        ]
        self.assertTrue(policy.pocket_dip_pending(waiting_tape, 0.99))
        self.assertFalse(policy.pocket_dip_held(waiting_tape, 0.99))
        far = [
            {"high": 1.10, "low": 1.05, "close": 1.06, "volume": 1.0},
            {"high": 1.08, "low": 1.04, "close": 1.05, "volume": 1.0},
            {"high": 1.07, "low": 1.04, "close": 1.05, "volume": 1.0},
        ]
        self.assertFalse(policy.pocket_dip_held(far, 0.99))
        self.assertIsNotNone(
            policy.update_pocket_swing_arm(armed, price=1.04, swing_low=None, now=2.0, keep=True)
        )
        pocket_usd = policy.btc_dip_quote(1000.0)
        self.assertAlmostEqual(pocket_usd, 1000.0 * policy.cfg().pocket_pct / 100.0)
        self.assertEqual(policy.sui_dip_quote(), 50.0)
        self.assertEqual(policy.pocket_cash_reserve(1000.0, pocket_open=False), pocket_usd)
        self.assertEqual(policy.pocket_cash_reserve(1000.0, pocket_open=True), 0.0)
        self.assertIsNone(policy.btc_dip_exit_reason(price=101.1, entry=100.0))
        self.assertIsNone(policy.btc_dip_exit_reason(price=101.0, entry=100.0))
        self.assertEqual(policy.btc_dip_exit_reason(price=85.0, entry=100.0), "стоп")
        self.assertIsNone(policy.btc_dip_exit_reason(price=100.4, entry=100.0))
        self.assertIsNone(
            policy.update_pocket_exit_arm(None, price=100.3, entry=100.0, now=1.0, qty=1.0)
        )
        armed = policy.update_pocket_exit_arm(None, price=101.6, entry=100.0, now=10.0, qty=1.0)
        self.assertAlmostEqual(armed["high"], 101.6)
        self.assertFalse(policy.pocket_exit_ready(armed, price=100.9, now=10.0 + policy.POCKET_EXIT_HOLD_SEC))
        higher = policy.update_pocket_exit_arm(armed, price=104.0, entry=100.0, now=20.0, qty=1.0)
        self.assertEqual(higher["ts"], 20.0)
        held = policy.update_pocket_exit_arm(
            higher, price=103.5, entry=100.0, now=20.0 + policy.POCKET_EXIT_HOLD_SEC, qty=1.0
        )
        self.assertEqual(held["high"], 104.0)
        self.assertEqual(held["ts"], 20.0)
        need = 104.0 * (1.0 - policy.POCKET_EXIT_PULLBACK_PCT / 100.0)
        self.assertFalse(policy.pocket_exit_ready(held, price=need + 0.05, now=20.0 + policy.POCKET_EXIT_HOLD_SEC))
        self.assertTrue(policy.pocket_exit_ready(held, price=need, now=20.0 + policy.POCKET_EXIT_HOLD_SEC))
        self.assertEqual(
            policy.btc_dip_exit_reason(price=need, entry=100.0, exit_arm=held, now=20.0 + policy.POCKET_EXIT_HOLD_SEC),
            "вершина",
        )
        # Откат меньше 1.2% от пика ещё держим; 1.2% и уход под вход — сразу.
        shallow = armed["high"] * (1.0 - 0.4 / 100.0)
        self.assertGreater(shallow, 100.0)
        self.assertFalse(
            policy.pocket_exit_ready(armed, price=shallow, now=10.0 + 30.0, entry=100.0)
        )
        deep_drop = armed["high"] * (1.0 - policy.POCKET_EXIT_DEEP_PULLBACK_PCT / 100.0)
        self.assertTrue(
            policy.pocket_exit_ready(armed, price=deep_drop, now=10.0 + 15.0, entry=100.0)
        )
        self.assertTrue(
            policy.pocket_exit_ready(armed, price=99.5, now=10.0 + 30.0, entry=100.0)
        )
        self.assertEqual(
            policy.btc_dip_exit_reason(price=99.5, entry=100.0, exit_arm=armed, now=10.0 + 30.0),
            "вершина",
        )
        kept = policy.update_pocket_exit_arm(held, price=100.2, entry=100.0, now=80.0, qty=1.0)
        self.assertEqual(kept["high"], 104.0)
        # $40 нога: порог ~0.66%, $100 — 0.48%, $200 — 0.42%.
        self.assertAlmostEqual(policy.exit_trap_arm_pct(cost_usd=100.0), 0.48, places=2)
        self.assertAlmostEqual(policy.exit_trap_arm_pct(cost_usd=40.0), 0.66, places=2)
        self.assertLess(policy.exit_trap_arm_pct(cost_usd=200.0), 0.5)
        # Дисбаланс стакана L2 (OBI) и срабатывание ловушки по bid / медвежьей плите:
        obi_bear = policy.orderbook_obi([["1.0", "100"]], [["1.0", "400"]], depth=5)
        self.assertAlmostEqual(obi_bear or 0.0, 0.20, places=2)
        # При медвежьей плите (obi <= 0.28) откат -0.25% режет сразу без ожидания hold 60с:
        self.assertTrue(
            policy.pocket_exit_ready(
                armed,
                price=armed["high"],
                bid=armed["high"] * (1.0 - 0.0025),
                now=10.0 + 5.0,
                entry=100.0,
                obi=obi_bear,
            )
        )
        # Если lastPrice застыл на пике 104.0, но bid в стакане уже упал до need — выход готов:
        self.assertTrue(
            policy.pocket_exit_ready(held, price=104.0, bid=need, now=20.0 + policy.POCKET_EXIT_HOLD_SEC)
        )

    def test_watch_stream_tickers_skips_dust(self):
        self.assertEqual(
            policy_legacy.watch_stream_tickers(
                held_values={"BNB": 80.0, "MNT": 0.3, "BTC": 10.0},
                pocket_open=False,
            ),
            {"BNB", "BTC"},
        )
        self.assertEqual(
            policy_legacy.watch_stream_tickers(held_values={"BNB": 80.0}, pocket_open=True),
            {"BNB", "SOL"},
        )

    def test_entry_arm_waits_for_bounce_then_cancels(self):
        armed = policy_legacy.update_alt_arm(None, price=100.0, z=-2.8, now=1.0)
        self.assertAlmostEqual(armed["low"], 100.0)
        later = 1.0 + policy.ALT_ENTRY_HOLD_SEC
        self.assertFalse(policy_legacy.alt_bounce_ready(armed, price=101.6, z=-2.2, now=1.0 + 60))
        self.assertTrue(policy_legacy.alt_bounce_ready(armed, price=101.6, z=-1.8, now=later))
        deeper = policy_legacy.update_alt_arm(armed, price=98.0, z=-3.1, now=later)
        self.assertAlmostEqual(deeper["low"], 98.0)
        self.assertEqual(deeper["ts"], later)
        held = later + policy.ALT_ENTRY_HOLD_SEC
        self.assertFalse(policy_legacy.alt_bounce_ready(deeper, price=98.5, z=-2.4, now=held))
        self.assertTrue(policy_legacy.alt_bounce_ready(deeper, price=99.5, z=-1.6, now=held))
        self.assertIsNone(policy_legacy.update_alt_arm(deeper, price=102.0, z=-0.4, now=held))
        # Догон выше chase% от дна — сетап сбрасываем, не покупаем.
        chased = policy_legacy.update_alt_arm(None, price=100.0, z=-2.8, now=1.0)
        self.assertIsNotNone(chased)
        self.assertTrue(policy_legacy.alt_chase_too_far(chased, 100.0 * 1.06))
        self.assertIsNone(
            policy_legacy.update_alt_arm(chased, price=100.0 * 1.06, z=-2.0, now=held)
        )
        self.assertFalse(
            policy_legacy.alt_bounce_ready(chased, price=100.0 * 1.06, z=-2.0, now=held)
        )
        pocket = policy.update_pocket_arm(None, price=100.0, day_chg=-4.0, now=1.0)
        self.assertFalse(policy.pocket_bounce_ready(pocket, price=101.2, now=1.0 + 60))
        self.assertTrue(
            policy.pocket_bounce_ready(pocket, price=101.2, now=1.0 + policy.POCKET_ENTRY_HOLD_SEC)
        )
        self.assertIsNone(policy.update_pocket_arm(pocket, price=101.0, day_chg=-2.0, now=2.0))

    def test_watch_rate_limit(self):
        now = time.time()
        self.assertTrue(policy_legacy.watch_rate_ok([], now=now))
        self.assertTrue(policy_legacy.watch_rate_ok([now - 10, now - 20], now=now, max_per_hour=3))
        self.assertFalse(policy_legacy.watch_rate_ok([now - 10, now - 20], now=now, max_per_hour=2))

    def test_watch_period_sec_and_snapshot(self):
        self.assertEqual(policy_legacy.watch_period_sec(legs=[]), policy.WATCH_PERIOD_CALM)
        self.assertEqual(
            policy_legacy.watch_period_sec(
                legs=[{"price": 100.8, "entry": 100.0, "kind": "pocket", "qty": 1.0}]
            ),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy_legacy.watch_period_sec(legs=[{"price": 87.0, "entry": 100.0, "kind": "alt"}]),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy_legacy.watch_period_sec(
                legs=[{"price": 100.3, "entry": 100.0, "ma": 101.3, "kind": "alt", "qty": 1.0}]
            ),
            policy.WATCH_PERIOD_CALM,
        )
        self.assertEqual(
            policy_legacy.watch_period_sec(
                legs=[{"price": 102.7, "entry": 100.0, "kind": "alt", "qty": 1.0}]
            ),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy_legacy.watch_period_sec(
                legs=[{"price": 106.0, "entry": 100.0, "kind": "alt", "exit_high": 108.0}]
            ),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy_legacy.watch_period_sec(
                legs=[{"price": 100.2, "entry": 100.0, "kind": "pocket", "exit_high": 102.0}]
            ),
            policy.WATCH_PERIOD_HOT,
        )
        line = policy_legacy.watch_snapshot_line("SOL", price=101.0, entry=100.0, kind="pocket")
        self.assertIn("avg=100", line)
        self.assertIn("до ловушки", line)
        armed_line = policy_legacy.watch_snapshot_line(
            "SOL", price=103.0, entry=100.0, kind="pocket", exit_high=104.0
        )
        self.assertIn("верх 104", armed_line)

    @unittest.skip("Рукав/alloc — desk_policy_legacy, не рантайм")
    def test_next_watch_at_aligns_to_wall(self):
        from skills.crypto.desk import next_watch_at

        self.assertEqual(next_watch_at(720, 1000.0), 1440.0)
        self.assertEqual(next_watch_at(60, 1445.0), 1500.0)

    def test_trail_arm_and_stop(self):
        # +10% от входа → armed; high 110, trail 6% → stop 103.4; цена 103 → hit
        leg = policy_legacy.update_trail_leg(
            price=110.0,
            entry=100.0,
            high=None,
            armed=False,
            arm_pct=policy.TRAIL_ARM_PCT,
            trail_pct=6.0,
        )
        self.assertTrue(leg["armed"])
        self.assertAlmostEqual(leg["high"], 110.0)
        self.assertFalse(leg["hit"])
        hit = policy_legacy.update_trail_leg(
            price=103.0,
            entry=100.0,
            high=110.0,
            armed=True,
            arm_pct=policy.TRAIL_ARM_PCT,
            trail_pct=6.0,
        )
        self.assertTrue(hit["hit"])
        self.assertAlmostEqual(hit["stop"], 110.0 * 0.94, places=4)
        # Без вооружения не бьём
        cold = policy_legacy.update_trail_leg(
            price=95.0,
            entry=100.0,
            high=100.0,
            armed=False,
            arm_pct=policy.TRAIL_ARM_PCT,
            trail_pct=6.0,
        )
        self.assertFalse(cold["armed"])
        self.assertFalse(cold["hit"])
        self.assertEqual(policy.TRAIL_ARM_PCT, 5.0)
        self.assertEqual(policy_legacy.trail_pct_for("BTC"), policy.TRAIL_ALT_PCT)
        self.assertEqual(policy_legacy.trail_pct_for("DOGE"), policy.TRAIL_ALT_PCT)

    def test_calculate_trailing_stop_freqtrade_examples(self):
        # Docs: entry 100, stoploss −10%, peak 102 → stop 91.8
        step = policy_legacy.calculate_trailing_stop(
            price=102.0, entry=100.0, stoploss_pct=10.0
        )
        self.assertTrue(step["trailing"])
        self.assertAlmostEqual(step["peak"], 102.0)
        self.assertAlmostEqual(step["stop"], 91.8, places=4)
        self.assertFalse(step["hit"])

        # Docs: positive 2%, offset 3%, peak 103.5 → stop 101.43
        tight = policy_legacy.calculate_trailing_stop(
            price=103.5,
            entry=100.0,
            peak=103.5,
            stoploss_pct=10.0,
            positive_trail_pct=2.0,
            offset_pct=3.0,
        )
        self.assertAlmostEqual(tight["stop"], 101.43, places=2)
        self.assertAlmostEqual(tight["trail_pct"], 2.0)

        # only_offset: до +3% стоп статичен от входа (90), не от пика 102
        static = policy_legacy.calculate_trailing_stop(
            price=102.0,
            entry=100.0,
            stoploss_pct=10.0,
            offset_pct=3.0,
            only_offset=True,
        )
        self.assertFalse(static["trailing"])
        self.assertAlmostEqual(static["stop"], 90.0, places=4)

        # Стоп не ослабевает при откате цены
        held = policy_legacy.calculate_trailing_stop(
            price=101.0,
            entry=100.0,
            peak=103.5,
            prev_stop=101.43,
            stoploss_pct=10.0,
            positive_trail_pct=2.0,
            offset_pct=3.0,
        )
        self.assertAlmostEqual(held["stop"], 101.43, places=2)
        self.assertTrue(held["hit"])

    def test_probe_reserve_and_boost_quote(self):
        q = policy.probe_quote()
        # 3 слота: пусто — резерв одной ноги; есть нога — место под остальные.
        self.assertEqual(policy.probe_reserve_quote(open_legs=0), q)
        self.assertEqual(policy.probe_reserve_quote(open_legs=1), q * 2)
        self.assertEqual(policy.probe_reserve_quote(open_legs=2), max(q, policy.PROBE_BOOST_USD))
        self.assertEqual(policy.probe_reserve_quote(open_legs=3), 0.0)
        legs = [
            {"ticker": "AAA", "entry": 100.0, "qty": 1.0},
            {"ticker": "BBB", "entry": 50.0, "qty": 1.0},
        ]
        self.assertEqual(
            policy.probe_quote_for_buy(legs=legs, prices={"AAA": 103.5, "BBB": 51.6}),
            policy.PROBE_BOOST_USD,
        )
        self.assertEqual(
            policy.probe_quote_for_buy(legs=legs, prices={"AAA": 102.0, "BBB": 51.6}),
            q,
        )


class TestJournalChurnAndDaily(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "trades.json")
        self.daily = os.path.join(self.tmp.name, "daily.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cooldown_after_losing_sell(self):
        now = time.time()
        trades = [
            {"ticker": "ETH", "side": "buy", "quote": 100.0, "price": 10.0, "ts_epoch": now - 3600},
            {"ticker": "ETH", "side": "sell", "quote": 40.0, "price": 8.0, "ts_epoch": now - 100},
        ]
        cool = crypto_journal.cooldown_tickers(12.0, trades, now=now)
        self.assertIn("ETH", cool)
        self.assertNotIn(
            "ETH",
            crypto_journal.cooldown_tickers(12.0, trades, now=now, reset_ts=now - 50),
        )

    def test_no_cooldown_on_winning_sell(self):
        now = time.time()
        trades = [
            {"ticker": "BTC", "side": "buy", "quote": 100.0, "price": 50.0, "ts_epoch": now - 3600},
            {"ticker": "BTC", "side": "sell", "quote": 120.0, "price": 60.0, "ts_epoch": now - 100},
        ]
        cool = crypto_journal.cooldown_tickers(12.0, trades, now=now)
        self.assertNotIn("BTC", cool)

    def test_probe_auto_pin_picks_continuation_not_parabolic(self):
        now = 1_800_000_000.0
        rows = [
            {"ticker": "KOR", "chg": 448.0, "turnover": 1_860_000, "price": 0.4, "high": 0.95},
            {"ticker": "CAP", "chg": 26.1, "turnover": 6_050_000, "price": 0.093, "high": 0.0956},
            {"ticker": "ARX", "chg": 25.5, "turnover": 3_590_000, "price": 0.28, "high": 0.2966},
            {"ticker": "ELSA", "chg": 24.6, "turnover": 1_410_000, "price": 0.077, "high": 0.0771},
            {"ticker": "PYBOBO", "chg": 24.0, "turnover": 1_450_000, "price": 0.0005, "high": 0.000605},
        ]
        self.assertTrue(policy.probe_pin_row_ok(rows[2]))
        self.assertTrue(policy.probe_pin_row_ok(rows[1]))
        self.assertFalse(policy.probe_pin_row_ok(rows[0]))
        # ELSA у самого хая суток: перехай больше не брак.
        self.assertTrue(policy.probe_pin_row_ok(rows[3]))
        self.assertTrue(
            policy.probe_pin_row_ok(
                {"ticker": "TOP", "chg": 10.0, "turnover": 2_000_000, "price": 1.004, "high": 1.0}
            )
        )
        self.assertFalse(
            policy.probe_pin_row_ok(
                {"ticker": "TOP", "chg": 10.0, "turnover": 2_000_000, "price": 1.012, "high": 1.0}
            )
        )
        picked = policy.probe_choose_pin(
            rows, None, now=now, lost=set(), moves={"CAP": 2.4, "ARX": 1.6}
        )
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(picked["ticker"], "CAP")
        lost_cap = policy.probe_choose_pin(
            rows, picked, now=now + 60, lost={"CAP"}, moves={"CAP": 2.4, "ARX": 1.6}
        )
        self.assertEqual(lost_cap["ticker"] if lost_cap else "", "ARX")
        held = policy.probe_choose_pin(
            rows,
            {"ticker": "ARX", "ts": now, "score": 1.8},
            now=now + 30,
            lost=set(),
            moves={"ARX": 1.8, "CAP": 1.9},
        )
        self.assertEqual(held["ticker"] if held else "", "ARX")
        switched = policy.probe_choose_pin(
            rows,
            {"ticker": "ARX", "ts": now, "score": 1.6},
            now=now + 30,
            lost=set(),
            moves={"ARX": 1.6, "CAP": 2.4},
        )
        self.assertEqual(switched["ticker"] if switched else "", "CAP")
        dropped = policy.probe_choose_pin(
            rows,
            {"ticker": "ELSA", "ts": now, "score": 2.0},
            now=now + 60,
            lost=set(),
            moves={"ELSA": 2.0, "CAP": 2.4},
        )
        self.assertNotEqual(dropped["ticker"] if dropped else "", "ELSA")
        faded = policy.probe_choose_pin(
            rows,
            {"ticker": "FLUID", "ts": now, "score": 62.0},
            now=now + 60,
            lost=set(),
            moves={"FLUID": 0.2, "CAP": 2.4},
        )
        self.assertEqual(faded["ticker"] if faded else "", "CAP")
        forced = policy.probe_choose_pin(
            rows,
            {"ticker": "KOR", "ts": now, "score": 0.0, "forced": True},
            now=now + 20 * 60,
            lost=set(),
            moves={"CAP": 2.4},
        )
        self.assertEqual(forced["ticker"] if forced else "", "KOR")
        after = policy.probe_choose_pin(
            rows,
            {"ticker": "KOR", "ts": now, "score": 0.0, "forced": True},
            now=now + 46 * 60,
            lost=set(),
            moves={"CAP": 2.4},
        )
        self.assertEqual(after["ticker"] if after else "", "CAP")
        quiet = [
            {"open": 1.0, "high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0}
            for _ in range(16)
        ]
        self.assertIsNone(policy.probe_pin_move(quiet))
        cap_bars = quiet[:-4] + [
            {"open": 1.0, "high": 1.01, "low": 0.99, "close": 1.0, "volume": 10.0},
            {"open": 1.0, "high": 1.002, "low": 0.998, "close": 1.003, "volume": 10.0},
            {"open": 1.003, "high": 1.006, "low": 1.0, "close": 1.004, "volume": 12.0},
            {"open": 1.004, "high": 1.028, "low": 1.004, "close": 1.024, "volume": 40.0},
        ]
        move = policy.probe_pin_move(cap_bars)
        self.assertIsNotNone(move)
        assert move is not None
        self.assertGreaterEqual(move, policy.PROBE_PIN_MOVE_PCT)
        self.assertIsNone(policy.probe_choose_pin(rows, None, now=now, lost=set()))
        weak = [
            {"ticker": "MET", "chg": 8.2, "turnover": 1_600_000, "price": 0.31, "high": 0.315},
        ]
        self.assertIsNone(policy.probe_choose_pin(weak, None, now=now, lost=set()))
        self.assertEqual(policy.PROBE_PIN_LOSS_LOOKBACK_SEC, 45 * 60)
        self.assertEqual(policy.PROBE_PIN_SCRATCH_IGNORE_USD, 1.00)
        self.assertEqual(policy.PROBE_FAIL_SCRATCH_SEC, 180.0)
        self.assertEqual(policy.PROBE_PIN_MIN_SCORE, 45.0)
        self.assertEqual(policy.PROBE_NIGHT_PIN_MAX_LEGS, 2)
        falling = rows + [
            {"ticker": "MET", "chg": 9.0, "turnover": 1_600_000, "price": 0.25, "high": 0.32},
        ]
        dumped = policy.probe_choose_pin(
            falling,
            {"ticker": "MET", "ts": now, "score": 32.8},
            now=now + 20 * 60,
            lost=set(),
        )
        self.assertNotEqual(dumped["ticker"] if dumped else "", "MET")

    def test_probe_recent_loss_tickers_for_pin(self):
        now = 1_800_000_000.0
        trades = [
            {"ticker": "BEAM", "side": "buy", "quote": 40.0, "price": 0.005, "ts_epoch": now - 8000},
            {"ticker": "BEAM", "side": "sell", "quote": 38.8, "price": 0.00485, "ts_epoch": now - 100},
            {"ticker": "CAP", "side": "buy", "quote": 40.0, "price": 0.08, "ts_epoch": now - 8000},
            {"ticker": "CAP", "side": "sell", "quote": 41.7, "price": 0.0834, "ts_epoch": now - 90},
        ]
        lost = crypto_journal.probe_recent_loss_tickers(now=now, trades=trades)
        self.assertIn("BEAM", lost)
        self.assertNotIn("CAP", lost)
        icnt = [
            {"ticker": "ICNT", "side": "buy", "quote": 39.99, "price": 0.2306, "ts_epoch": now - 200},
            {
                "ticker": "ICNT",
                "side": "sell",
                "quote": 39.70,
                "price": 0.2295,
                "pnl_usd": -0.30,
                "ts_epoch": now - 80,
            },
        ]
        tape_hot = [{"ticker": "ICNT", "chg": 13.5, "turnover": 1_700_000, "price": 0.236, "high": 0.243}]
        tape_cold = [{"ticker": "ICNT", "chg": 3.0, "turnover": 1_700_000, "price": 0.210, "high": 0.243}]
        self.assertNotIn(
            "ICNT",
            crypto_journal.probe_recent_loss_tickers(now=now, trades=icnt, tape=tape_hot),
        )
        self.assertIn(
            "ICNT",
            crypto_journal.probe_recent_loss_tickers(now=now, trades=icnt, tape=tape_cold),
        )
        self.assertIn(
            "ICNT",
            crypto_journal.probe_recent_loss_tickers(now=now, trades=icnt),
        )
        deep = [
            {"ticker": "ICNT", "side": "buy", "quote": 40.0, "price": 0.3228, "ts_epoch": now - 400},
            {
                "ticker": "ICNT",
                "side": "sell",
                "quote": 39.16,
                "price": 0.317,
                "pnl_usd": -0.84,
                "ts_epoch": now - 60,
            },
        ]
        day_hot = [{"ticker": "ICNT", "chg": 13.5, "price": 0.320, "turnover": 1_700_000}]
        day_flat = [{"ticker": "ICNT", "chg": 3.0, "price": 0.320, "turnover": 1_700_000}]
        day_break = [{"ticker": "ICNT", "chg": 3.0, "price": 0.326, "turnover": 1_700_000}]
        self.assertNotIn(
            "ICNT",
            crypto_journal.probe_recent_loss_tickers(now=now, trades=deep, tape=day_hot),
        )
        self.assertIn(
            "ICNT",
            crypto_journal.probe_recent_loss_tickers(now=now, trades=deep, tape=day_flat),
        )
        self.assertNotIn(
            "ICNT",
            crypto_journal.probe_recent_loss_tickers(now=now, trades=deep, tape=day_break),
        )
        self.assertTrue(policy.probe_pin_loss_breakout(0.317, 0.326))
        self.assertFalse(policy.probe_pin_loss_breakout(0.317, 0.320))

    def test_probe_reentry_blocks_recent_sell(self):
        now = time.time()
        trades = [
            {"ticker": "KAS", "side": "buy", "quote": 60.0, "price": 0.036, "ts_epoch": now - 3600},
            {"ticker": "KAS", "side": "sell", "quote": 61.0, "price": 0.037, "ts_epoch": now - 60},
        ]
        # Плюсовой sell — обычный cooldown молчит, щуп всё равно ждёт reentry.
        self.assertNotIn("KAS", crypto_journal.cooldown_tickers(12.0, trades, now=now))
        self.assertIn("KAS", crypto_journal.recently_sold(policy.PROBE_REENTRY_HOURS, trades, now=now))
        self.assertNotIn(
            "KAS",
            crypto_journal.recently_sold(policy.PROBE_REENTRY_HOURS, trades, now=now + 3 * 3600),
        )
        shelf = [
            {"ticker": "FOGO", "side": "sell", "reason": "полка", "ts_epoch": now - 60 * 60},
            {"ticker": "CAP", "side": "sell", "reason": "вершина", "ts_epoch": now - 60 * 60},
        ]
        scratch = [
            {"ticker": "TA", "side": "sell", "reason": "срез", "ts_epoch": now - 5 * 60},
            {"ticker": "BLEND", "side": "sell", "reason": "вершина", "ts_epoch": now - 5 * 60},
        ]
        self.assertIn("TA", crypto_journal.scratch_reentry_tickers(scratch, now=now))
        self.assertNotIn("BLEND", crypto_journal.scratch_reentry_tickers(scratch, now=now))
        self.assertNotIn(
            "TA",
            crypto_journal.scratch_reentry_tickers(scratch, now=now + 21 * 60),
        )
        self.assertEqual(policy.PROBE_SPARK_VOL_MULT, 2.2)
        self.assertEqual(policy.PROBE_POP_VOL_MULT, 2.2)
        self.assertEqual(policy.PROBE_PIN_MIN_TURNOVER_USD, 600_000.0)
        self.assertTrue(
            policy.probe_pin_row_ok(
                {"ticker": "TA", "chg": 18.0, "turnover": 600_000, "price": 0.10, "high": 0.101}
            )
        )
        self.assertFalse(
            policy.probe_pin_row_ok(
                {"ticker": "TA", "chg": 18.0, "turnover": 599_000, "price": 0.10, "high": 0.101}
            )
        )
        self.assertEqual(policy.PROBE_SCRATCH_COOLDOWN_SEC, 20 * 60)
        self.assertIn("FOGO", crypto_journal.shelf_reentry_tickers(shelf, now=now))
        self.assertNotIn("CAP", crypto_journal.shelf_reentry_tickers(shelf, now=now))
        self.assertNotIn(
            "FOGO",
            crypto_journal.shelf_reentry_tickers(shelf, now=now + 40 * 60),
        )

    def test_probe_loss_guard_night_and_shelf_cooldown(self):
        from datetime import datetime, timedelta, timezone

        yekt = timezone(timedelta(hours=5))
        night = datetime(2026, 11, 2, 3, 0, tzinfo=yekt).timestamp()
        day = datetime(2026, 11, 2, 10, 0, tzinfo=yekt).timestamp()
        self.assertIsNone(crypto_journal.probe_loss_guard_reason(now=night, trades=[]))
        self.assertIsNone(crypto_journal.probe_loss_guard_reason(now=day, trades=[]))
        shelf_trades = [
            {"ticker": "DOOD", "side": "buy", "quote": 40.0, "price": 0.002, "ts_epoch": day - 1800},
            {"ticker": "DOOD", "side": "sell", "quote": 39.22, "price": 0.001961, "ts_epoch": day - 120},
        ]
        reason = crypto_journal.probe_loss_guard_reason(
            now=day, trades=shelf_trades, enabled=True
        )
        self.assertIsNotNone(reason)
        self.assertTrue(str(reason).startswith("после полки пауза"))
        later = crypto_journal.probe_loss_guard_reason(
            now=day + policy.PROBE_SHELF_COOLDOWN_SEC,
            trades=shelf_trades,
            enabled=True,
        )
        self.assertIsNone(later)
        scratch = [
            {"ticker": "PUMP", "side": "buy", "quote": 40.0, "price": 0.004, "ts_epoch": day - 1800},
            {"ticker": "PUMP", "side": "sell", "quote": 39.90, "price": 0.00399, "ts_epoch": day - 60},
        ]
        self.assertIsNone(
            crypto_journal.probe_loss_guard_reason(now=day, trades=scratch, enabled=True)
        )
        self.assertTrue(policy.PROBE_STRICT_MODE)
        self.assertIsNotNone(
            crypto_journal.probe_loss_guard_reason(now=day, trades=shelf_trades)
        )

    def test_probe_daily_loss_reset_ignores_old_sells(self):
        now = datetime(2026, 11, 2, 18, 0, tzinfo=_MSK).timestamp()
        trades = [
            {"ticker": "UNI", "side": "buy", "quote": 40.0, "price": 1.0, "ts_epoch": now - 8000},
            {"ticker": "UNI", "side": "sell", "quote": 34.0, "price": 0.85, "ts_epoch": now - 7000},
        ]
        reason = crypto_journal.probe_loss_guard_reason(
            now=now, trades=trades, reset_ts=0.0, enabled=True
        )
        self.assertIsNotNone(reason)
        self.assertTrue(str(reason).startswith("дневной лимит"))
        self.assertIsNone(
            crypto_journal.probe_loss_guard_reason(
                now=now, trades=trades, reset_ts=now - 60, enabled=True
            )
        )
        with patch.object(policy, "PROBE_DAILY_LOSS_ON", False):
            self.assertIsNone(
                crypto_journal.probe_loss_guard_reason(
                    now=now, trades=trades, reset_ts=0.0, enabled=True
                )
            )

    def test_daily_report_gate(self):
        morning = datetime(2026, 9, 18, 10, 0, tzinfo=_MSK)
        evening = datetime(2026, 9, 18, 21, 30, tzinfo=_MSK)
        self.assertFalse(crypto_journal.should_send_daily_report(now=morning, path=self.daily))
        self.assertTrue(crypto_journal.should_send_daily_report(now=evening, path=self.daily))
        crypto_journal.mark_daily_report_sent(now=evening, path=self.daily)
        self.assertFalse(crypto_journal.should_send_daily_report(now=evening, path=self.daily))

    def test_format_daily_report(self):
        now = datetime.now(_MSK)
        trades = [
            {
                "ticker": "BTC",
                "side": "buy",
                "quote": 20.0,
                "price": 100.0,
                "name": "Биткоин",
                "ts_epoch": now.timestamp(),
                "ts": now.strftime("%d.%m %H:%M"),
            }
        ]
        text = crypto_journal.format_daily_pnl_report(trades, lifetime=1.5, day=now)
        self.assertIn("сделок 1", text)
        self.assertIn("BTC", text)


class TestMakerOrders(unittest.TestCase):
    """Лимитные заявки стола: maker 0.1% вместо taker 0.18%, остаток добираем рыночной."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, attr in (
            ("holds.json", "_HOLD_PATH"),
            ("bought.json", "_BOUGHT_PATH"),
            ("trades.json", "_TRADE_PATH"),
            ("trail.json", "_TRAIL_PATH"),
        ):
            patcher = patch(f"skills.crypto.common.{attr}", os.path.join(self.tmp.name, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.skill = CryptoSkill()
        self.bodies: list[dict] = []
        self.states: list[dict] = []
        for target, kwargs in (
            ("_filters", {"return_value": {"min_qty": 0.0, "min_amt": 5.0, "step": 0.01, "tick": 0.01}}),
            ("_ticker", {"return_value": {"price": 100.0, "chg": 0.0, "turnover": 1}}),
            ("_book", {"return_value": {"bid": 99.99, "ask": 100.01}}),
            ("_journal_trade", {}),
            ("_bust_private_cache", {}),
        ):
            p = patch.object(self.skill, target, **kwargs)
            p.start()
            self.addCleanup(p.stop)
        p = patch("skills.crypto.trades.time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def _signed(self, method, path, params=None):
        if path == "/v5/order/create":
            self.bodies.append(dict(params or {}))
            return {"result": {"orderId": f"id{len(self.bodies)}"}}
        if path in {"/v5/order/realtime", "/v5/order/history"}:
            return {"result": {"list": [self.states.pop(0)] if self.states else []}}
        return {"result": {}}

    def _run(self, side, **kwargs):
        with patch.object(self.skill, "_signed", side_effect=self._signed):
            return self.skill._place_order("DOGE", side, maker=True, **kwargs)

    def test_limit_fill_skips_market(self):
        self.states = [{"orderStatus": "Filled", "cumExecQty": "0.26", "cumExecValue": "26.0"}]
        phrase = self._run("Buy", quote_usdt=26.0)
        self.assertEqual(len(self.bodies), 1)
        self.assertEqual(self.bodies[0]["orderType"], "Limit")
        self.assertEqual(self.bodies[0]["timeInForce"], "PostOnly")
        self.assertEqual(self.bodies[0]["price"], "99.99")
        self.assertIn("26", phrase)

    def test_partial_fill_tops_up_with_market(self):
        self.states = [
            {"orderStatus": "PartiallyFilled", "cumExecQty": "0.10", "cumExecValue": "10.0"},
            {"orderStatus": "Cancelled", "cumExecQty": "0.10", "cumExecValue": "10.0"},
            {"orderStatus": "Filled", "cumExecQty": "0.16", "cumExecValue": "16.0"},
        ]
        self._run("Buy", quote_usdt=26.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Limit", "Market"])
        self.assertAlmostEqual(float(self.bodies[1]["qty"]), 16.0, places=2)

    def test_unfilled_limit_falls_back_to_market(self):
        self.states = [
            {"orderStatus": "Cancelled", "cumExecQty": "0", "cumExecValue": "0"},
            {"orderStatus": "Filled", "cumExecQty": "0.26", "cumExecValue": "26.0"},
        ]
        self._run("Sell", base_qty=0.26, price=100.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Limit", "Market"])
        self.assertEqual(self.bodies[0]["price"], "100.01")  # продажа округляется вверх
        self.assertAlmostEqual(float(self.bodies[1]["qty"]), 0.26, places=4)

    def test_voice_order_stays_market(self):
        self.states = [{"orderStatus": "Filled", "cumExecQty": "0.26", "cumExecValue": "26.0"}]
        with patch.object(self.skill, "_signed", side_effect=self._signed):
            self.skill._place_order("DOGE", "Buy", quote_usdt=26.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Market"])

    def test_unfilled_market_is_not_a_trade(self):
        self.states = [{"orderStatus": "Cancelled", "cumExecQty": "0", "cumExecValue": "0"}]
        with patch.object(self.skill, "_signed", side_effect=self._signed):
            with self.assertRaises(RuntimeError):
                self.skill._place_order("DOGE", "Buy", quote_usdt=26.0)
        self.skill._journal_trade.assert_not_called()

    def test_maker_disabled_by_env(self):
        self.states = [{"orderStatus": "Filled", "cumExecQty": "0.26", "cumExecValue": "26.0"}]
        with patch.dict(os.environ, {"CRYPTO_MAKER_ORDERS": "off"}):
            self._run("Buy", quote_usdt=26.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Market"])

    def test_price_step_rounding(self):
        from skills.crypto.common import _price_str

        self.assertEqual(_price_str(0.094999, 0.0001), "0.0949")
        self.assertEqual(_price_str(0.094999, 0.0001, round_up=True), "0.095")
        self.assertEqual(_price_str(123.456, 0.0), "123.456")


class TestEarn(unittest.TestCase):
    """Простаивающий кэш под проценты: капитал не теряется, перед покупкой деньги возвращаются."""

    def setUp(self):
        self.skill = CryptoSkill()
        self.skill._api_key = "x"
        self.skill._api_secret = "y"
        self.staked = 100.0
        self.spot = 10.0
        self.orders: list[tuple[str, float]] = []
        env = patch.dict(os.environ, {"CRYPTO_EARN": "on"})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        earn_path = patch(
            "skills.crypto.common._EARN_PATH",
            os.path.join(self.tmp.name, "earn.json"),
        )
        earn_path.start()
        self.addCleanup(earn_path.stop)
        p = patch("skills.crypto.earn.time.sleep")
        p.start()
        self.addCleanup(p.stop)

    def _signed(self, method, path, params=None):
        if path == "/v5/earn/position":
            return {"result": {"list": [{"amount": str(self.staked)}]}}
        if path == "/v5/earn/place-order":
            amount = float((params or {}).get("amount") or 0)
            kind = str((params or {}).get("orderType") or "")
            self.orders.append((kind, amount))
            if kind == "Redeem":
                self.staked -= amount
                self.spot += amount
            else:
                self.staked += amount
                self.spot -= amount
            return {"result": {"orderId": "1"}}
        if path == "/v5/account/wallet-balance":
            return {"result": {"list": [{"coin": [
                {"coin": "USDT", "walletBalance": str(self.spot), "usdValue": str(self.spot)},
            ]}]}}
        return {"result": {}}

    def _patched(self):
        return patch.object(self.skill, "_signed", side_effect=self._signed)

    def test_wallet_counts_staked_as_cash(self):
        with self._patched():
            cash, _pos = self.skill._wallet()
            spot_only, _pos = self.skill._wallet(with_earn=False)
        self.assertAlmostEqual(cash, 110.0)
        self.assertAlmostEqual(spot_only, 10.0)

    def test_redeem_before_buy(self):
        with self._patched():
            self.skill._earn_free_cash(26.0)
        self.assertEqual(self.orders[0][0], "Redeem")
        self.assertAlmostEqual(self.orders[0][1], 28.0)  # дыра 16$ + буфер 12$
        self.assertAlmostEqual(self.spot, 38.0)

    def test_no_redeem_when_spot_enough(self):
        self.spot = 40.0
        with self._patched():
            self.skill._earn_free_cash(26.0)
        self.assertEqual(self.orders, [])

    def test_park_idle_cash(self):
        self.spot = 80.0
        with self._patched():
            self.skill._earn_park_idle()
        self.assertEqual(self.orders[0][0], "Stake")
        self.assertAlmostEqual(self.orders[0][1], 68.0)
        self.assertAlmostEqual(self.spot, 12.0)

    def test_park_tops_up_spot_buffer(self):
        self.spot = 0.2
        with self._patched():
            self.skill._earn_park_idle()
        self.assertEqual(self.orders[0][0], "Redeem")
        self.assertAlmostEqual(self.orders[0][1], 11.8)
        self.assertAlmostEqual(self.spot, 12.0)

    def test_park_skips_small_leftover(self):
        self.spot = 13.0
        with self._patched():
            self.skill._earn_park_idle()
        self.assertEqual(self.orders, [])

    def test_interest_from_transaction_log(self):
        log = [
            {"type": "FLEXIBLE_STAKING_SUBSCRIPTION", "cashFlow": "-129.13"},
            {"type": "FLEXIBLE_STAKING_REDEMPTION", "cashFlow": "6"},
            {"type": "FLEXIBLE_STAKING_SUBSCRIPTION", "cashFlow": "-6"},
            {"type": "FLEXIBLE_STAKING_INTEREST", "cashFlow": "0.42"},
            {"type": "TRADE", "cashFlow": "17.6"},
        ]

        def signed(method, path, params=None):
            if path == "/v5/account/transaction-log":
                return {"result": {"list": log}}
            return self._signed(method, path, params)

        self.staked = 129.13
        with patch.object(self.skill, "_signed", side_effect=signed):
            self.assertAlmostEqual(self.skill._earn_interest(), 0.42, places=4)

    def test_interest_zero_before_first_payout(self):
        log = [{"type": "FLEXIBLE_STAKING_SUBSCRIPTION", "cashFlow": "-129.13"}]

        def signed(method, path, params=None):
            if path == "/v5/account/transaction-log":
                return {"result": {"list": log}}
            return self._signed(method, path, params)

        self.staked = 129.13
        with patch.object(self.skill, "_signed", side_effect=signed):
            self.assertAlmostEqual(self.skill._earn_interest(), 0.0, places=4)

    def test_interest_keeps_old_stake_after_it_leaves_the_log(self):
        full = [
            {"id": "sub", "type": "FLEXIBLE_STAKING_SUBSCRIPTION", "cashFlow": "-129.13"},
            {"id": "fee", "type": "FLEXIBLE_STAKING_INTEREST", "cashFlow": "0.42"},
        ]
        later = [{"id": "trade-new", "type": "TRADE", "cashFlow": "20"}]
        calls = {"n": 0}

        def signed(method, path, params=None):
            if path == "/v5/account/transaction-log":
                calls["n"] += 1
                rows = full if calls["n"] == 1 else later
                return {"result": {"list": rows, "nextPageCursor": ""}}
            return self._signed(method, path, params)

        self.staked = 129.13
        with patch.object(self.skill, "_signed", side_effect=signed):
            self.assertAlmostEqual(self.skill._earn_interest(), 0.42, places=4)
            self.staked = 129.55
            self.assertAlmostEqual(self.skill._earn_interest(), 0.84, places=4)

    def test_disabled_without_env(self):
        with patch.dict(os.environ, {"CRYPTO_EARN": "off"}), self._patched():
            self.assertEqual(self.skill._earn_staked(), 0.0)
            self.skill._earn_park_idle()
            cash, _pos = self.skill._wallet()
        self.assertEqual(self.orders, [])
        self.assertAlmostEqual(cash, 10.0)


class TestBollinger(unittest.TestCase):
    def test_bands_and_z(self):
        from skills.crypto.indicators import bollinger

        closes = [10.0] * 19 + [12.0]
        b = bollinger(closes)
        self.assertAlmostEqual(b["ma"], 10.1)
        self.assertGreater(b["z"], 2.0)
        self.assertAlmostEqual(b["upper"] - b["ma"], b["ma"] - b["lower"])
        self.assertIsNone(bollinger(closes[:10]))
        flat = bollinger([5.0] * 20)
        self.assertEqual(flat["z"], 0.0)

    def test_closes_from_kline_oldest_first(self):
        from skills.crypto.indicators import closes_from_kline

        rows = [["3", "0", "0", "0", "3.0"], ["2", "0", "0", "0", "2.0"], ["1", "0", "0", "0", "1.0"]]
        self.assertEqual(closes_from_kline(rows), [1.0, 2.0, 3.0])


class TestBybitTape(unittest.TestCase):
    def test_topics_and_hot_edge(self):
        from skills.crypto.stream import BybitPublicTape, stream_topics

        self.assertEqual(
            stream_topics({"SOL", "BNB"}),
            ["tickers.BNBUSDT", "kline.240.BNBUSDT", "tickers.SOLUSDT", "kline.240.SOLUSDT"],
        )
        tape = BybitPublicTape()
        tape.set_legs([{"ticker": "SOL", "entry": 100.0, "price": 100.0, "kind": "pocket"}])
        tape.apply_message(
            {
                "topic": "tickers.SOLUSDT",
                "data": {"symbol": "SOLUSDT", "lastPrice": "100.80", "price24hPcnt": "-0.016"},
            }
        )
        q = tape.quote("SOL")
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q["price"], 100.8)
        self.assertAlmostEqual(q["chg"], -1.6)
        self.assertEqual(tape.consume_signals(), "зона")
        tape.apply_message(
            {
                "topic": "tickers.SOLUSDT",
                "data": {"symbol": "SOLUSDT", "lastPrice": "100.85"},
            }
        )
        self.assertEqual(tape.consume_signals(), "")

    def test_entry_arm_wakes_on_bounce(self):
        from skills.crypto.stream import BybitPublicTape

        tape = BybitPublicTape()
        old = time.time() - 21 * 60
        tape.set_entry_arms({"SOL": {"low": 100.0, "bounce": 1.5, "hold": 20 * 60, "ts": old}})
        tape.apply_message({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "99.5"}})
        self.assertEqual(tape.consume_signals(), "")
        self.assertAlmostEqual(tape.entry_lows()["SOL"], 99.5)
        tape.apply_message({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "101.2"}})
        self.assertEqual(tape.consume_signals(), "")
        tape._entry["SOL"]["low_ts"] = time.time() - 21 * 60
        tape.apply_message({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "101.2"}})
        self.assertEqual(tape.consume_signals(), "отскок")

    def test_exit_arm_wakes_on_pullback(self):
        from skills.crypto.stream import BybitPublicTape

        tape = BybitPublicTape()
        old = time.time() - 6 * 60
        tape.set_exit_arms({"SOL": {"high": 110.0, "pullback": 1.0, "hold": 5 * 60, "ts": old}})
        tape.apply_message({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "111"}})
        self.assertEqual(tape.consume_signals(), "")
        self.assertAlmostEqual(tape.exit_highs()["SOL"], 111.0)
        tape.apply_message({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "109.5"}})
        self.assertEqual(tape.consume_signals(), "")
        tape._exit["SOL"]["high_ts"] = time.time() - 6 * 60
        tape.apply_message({"topic": "tickers.SOLUSDT", "data": {"symbol": "SOLUSDT", "lastPrice": "109.5"}})
        self.assertEqual(tape.consume_signals(), "откат")

    def test_kline_confirm_wakes(self):
        from skills.crypto.stream import BybitPublicTape

        tape = BybitPublicTape()
        tape.apply_message(
            {"topic": "kline.240.BNBUSDT", "data": [{"confirm": False, "close": "1"}]}
        )
        self.assertEqual(tape.consume_signals(), "")
        tape.apply_message(
            {"topic": "kline.240.BNBUSDT", "data": [{"confirm": True, "close": "1"}]}
        )
        self.assertEqual(tape.consume_signals(), "свеча")


class TestManualTrap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, attr in (
            ("holds.json", "_HOLD_PATH"),
            ("bought.json", "_BOUGHT_PATH"),
            ("second_pocket.json", "_SECOND_POCKET_PATH"),
            ("probe.json", "_PROBE_PATH"),
            ("btc_dip.json", "_BTC_DIP_PATH"),
            ("trades.json", "_TRADE_PATH"),
        ):
            path = os.path.join(self.tmp.name, name)
            patcher = patch(f"skills.crypto.common.{attr}", path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.skill = CryptoSkill()
        self.skill._desk_bought_ready = True
        self.skill._desk_bought = {"DOOD"}
        self.skill._manual_holds = {"BTC", "ETH"}
        self.skill._api_key = "x"

    def test_probe_market_stop_waits_until_limit_is_cancelled(self):
        from skills.crypto.common import probe_legs, write_probe

        write_probe(
            ticker="SPX",
            qty=100.0,
            entry=0.41,
            shelf=0.402,
            arm_px=0.409,
            peak=0.409,
            low=0.408,
            algo=policy.PROBE_ALGO_GRIND,
            tp_order="lim-1",
            tp_px=0.428,
        )
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, maker))
            return f"ok {ticker}"

        bars = [{"high": 0.41, "low": 0.40, "close": 0.401, "volume": 1.0} for _ in range(80)]
        with (
            patch.object(self.skill, "_ticker", return_value={"price": 0.401, "chg": -1.0}),
            patch.object(self.skill, "_probe_bars", return_value=bars),
            patch.object(self.skill, "_order_state", return_value={}),
            patch.object(
                self.skill, "_cancel_limit_confirmed", return_value=(False, 0.0, 0.0, 0.0),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(100.0, {"SPX": {"qty": 100.0}})),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.ma_trend", return_value="bear"),
        ):
            self.skill._probe_exit(probe_legs()[0])
        self.assertEqual(placed, [])
        self.assertEqual(probe_legs()[0]["ticker"], "SPX")

    def test_free_qty_subtracts_managed(self):
        self.assertAlmostEqual(
            policy.manual_trap_free_qty(100.0, probe_qty=30.0, pocket_qty=10.0, dip_qty=5.0),
            55.0,
        )
        self.assertEqual(policy.manual_trap_free_qty(10.0, probe_qty=10.0), 0.0)
        self.assertIn("BTC", policy.MANUAL_TRAP_SKIP)
        self.assertNotIn("ETH", policy.MANUAL_TRAP_SKIP)

    def test_claim_manual_skips_btc_takes_diam(self):
        from skills.crypto.common import read_second_pocket_legs, write_probe

        write_probe(ticker="PEPE", qty=50.0, entry=0.1, shelf=0.09, peak=0.1)
        with (
            patch.object(self.skill, "_manual_entry_px", return_value=0.005),
            patch.object(self.skill, "_spot_buy_vwap", return_value=0.005),
        ):
            notes = self.skill._claim_manual_trap_legs(
                {
                    "BTC": {"ticker": "BTC", "qty": 0.01, "price": 100_000.0, "value": 1000.0},
                    "DIAM": {"ticker": "DIAM", "qty": 2000.0, "price": 0.005, "value": 10.0},
                    "DOOD": {"ticker": "DOOD", "qty": 100.0, "price": 0.1, "value": 10.0},
                    "PEPE": {"ticker": "PEPE", "qty": 50.0, "price": 0.1, "value": 5.0},
                }
            )
        legs = {leg["ticker"]: leg for leg in read_second_pocket_legs()}
        self.assertIn("DIAM", legs)
        self.assertIn("DOOD", legs)  # даже если была в bought.json — свободна от щупа, берём под ловушку
        self.assertNotIn("BTC", legs)
        self.assertNotIn("PEPE", legs)  # занята активной ногой щупа
        self.assertTrue(any("DIAM" in n for n in notes))

    def test_claim_does_not_duplicate(self):
        from skills.crypto.common import read_second_pocket_legs, upsert_second_pocket_leg

        upsert_second_pocket_leg(ticker="DIAM", qty=2000.0, entry=0.005, max_legs=32)
        with patch.object(self.skill, "_manual_entry_px", return_value=0.006):
            self.skill._claim_manual_trap_legs(
                {"DIAM": {"ticker": "DIAM", "qty": 2000.0, "price": 0.005, "value": 10.0}}
            )
        legs = read_second_pocket_legs()
        self.assertEqual(len(legs), 1)
        self.assertAlmostEqual(legs[0]["entry"], 0.005)

    def test_claim_journals_manual_buy_once(self):
        from skills.crypto import journal as crypto_journal
        from skills.crypto.common import read_second_pocket_legs

        with patch.object(self.skill, "_manual_entry_px", return_value=0.01323):
            self.skill._claim_manual_trap_legs(
                {"DRIFT": {"ticker": "DRIFT", "qty": 3766.79, "price": 0.01323, "value": 49.83}}
            )
        trades = crypto_journal._read_trades()
        buys = [row for row in trades if row.get("ticker") == "DRIFT" and row.get("side") == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertAlmostEqual(float(buys[0]["price"]), 0.01323, places=5)
        self.assertAlmostEqual(float(buys[0]["quote"]), 49.83, places=1)
        with patch.object(self.skill, "_manual_entry_px", return_value=0.01323):
            self.skill._claim_manual_trap_legs(
                {"DRIFT": {"ticker": "DRIFT", "qty": 3766.79, "price": 0.01323, "value": 49.83}}
            )
        trades = crypto_journal._read_trades()
        buys = [row for row in trades if row.get("ticker") == "DRIFT" and row.get("side") == "buy"]
        self.assertEqual(len(buys), 1)
        self.assertEqual(len(read_second_pocket_legs()), 1)
        pnl = crypto_journal.realized_pnl_for_sell(
            trades, ticker="DRIFT", quote=50.14, price=0.01331
        )
        self.assertIsNotNone(pnl)
        self.assertGreater(float(pnl), 0.2)

    def test_probe_make_room_parks_worst_leg(self):
        from skills.crypto.common import probe_legs, read_second_pocket_legs, write_probe
        from skills.crypto.settings import store as settings_store

        settings_store.update(second_on=True, pocket_from_probe_max=3)
        self.addCleanup(lambda: settings_store.update(second_on=False))
        now = time.time()
        write_probe(
            ticker="SKY",
            qty=600.0,
            entry=0.066,
            shelf=0.065,
            peak=0.066,
            ts=now - 3600,
        )
        write_probe(
            ticker="LTC",
            qty=0.7,
            entry=55.0,
            shelf=54.5,
            peak=55.2,
            ts=now - 1200,
        )
        with (
            patch.object(self.skill, "_probe_cap", return_value=2),
            patch.object(
                self.skill,
                "_ticker",
                side_effect=lambda t: {"SKY": {"price": 0.065}, "LTC": {"price": 55.2}}[t],
            ),
        ):
            ok = self.skill._probe_make_room()
        self.assertTrue(ok)
        self.assertEqual({leg["ticker"] for leg in probe_legs()}, {"LTC"})
        pocket = read_second_pocket_legs()
        self.assertEqual(len(pocket), 1)
        self.assertEqual(pocket[0]["ticker"], "SKY")
        self.assertTrue(pocket[0].get("scratch"))


if __name__ == "__main__":
    unittest.main()
