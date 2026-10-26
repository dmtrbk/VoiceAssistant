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
        self.assertTrue(policy.is_risk_off(btc_chg_7=-6.0, btc_chg_day=1.0))
        self.assertTrue(policy.is_risk_off(btc_chg_7=-1.0, btc_chg_day=-4.0))
        self.assertFalse(policy.is_risk_off(btc_chg_7=2.0, btc_chg_day=-1.0))

    def test_cash_floor_bull_vs_normal(self):
        self.assertEqual(
            policy.cash_floor_pct(btc_chg_7=6.0, btc_chg_day=1.0),
            policy.DESK_CASH_FLOOR_BULL_PCT,
        )
        self.assertEqual(
            policy.cash_floor_pct(btc_chg_7=1.0, btc_chg_day=0.5),
            policy.DESK_CASH_FLOOR_PCT,
        )
        self.assertEqual(
            policy.cash_floor_pct(btc_chg_7=-6.0, btc_chg_day=0.0),
            100.0,
        )

    def test_band_is_alt_corridor(self):
        self.assertEqual(policy.band_pct_for("BTC"), policy.REBALANCE_BAND_ALT_PCT)
        self.assertEqual(policy.band_pct_for("DOGE"), policy.REBALANCE_BAND_ALT_PCT)

    def test_filter_drops_pump_and_cooldown(self):
        rows = [
            {"ticker": "BTC", "turnover": 1e9, "chg": 1.0, "chg_7": 2.0},
            {"ticker": "DOGE", "turnover": 1e9, "chg": 40.0, "chg_7": 5.0},
            {"ticker": "SOL", "turnover": 1e9, "chg": 1.0, "chg_7": 3.0},
        ]
        filtered = policy.filter_auto_candidates(
            rows,
            held=set(),
            watchlist=["BTC", "ETH", "SOL", "DOGE"],
            cooldown={"SOL"},
        )
        tickers = {r["ticker"] for r in filtered}
        self.assertIn("BTC", tickers)
        self.assertNotIn("DOGE", tickers)
        self.assertNotIn("SOL", tickers)

    def test_score_alloc_keeps_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("кэш", why.lower())

    def test_score_alloc_bull_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        rows.append({"ticker": "MNT", "chg": -6.0, "chg_7": -4.0, "turnover": 1e9, "bb_z": -3.1})
        alloc, why = policy.score_alloc(rows, cash_floor_pct=policy.DESK_CASH_FLOOR_BULL_PCT)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("ETH", alloc)
        self.assertEqual(alloc.get("MNT"), policy.alt_slot_pct())
        self.assertIn("8%", why)

    def test_core_split_equally_despite_scores(self):
        rows = [
            {"ticker": "BTC", "chg": -0.4, "chg_7": -0.5, "turnover": 1e9},
            {"ticker": "ETH", "chg": 1.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("ETH", alloc)
        self.assertIn("кэш", why.lower())

    def test_score_empty_when_core_below_floor(self):
        rows = [
            {"ticker": "BTC", "chg": -2.0, "chg_7": -9.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": -1.0, "chg_7": -10.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("кэш", why.lower())

    def test_score_without_sleeve_signal_stays_cash(self):
        rows = [
            {"ticker": "BTC", "chg": 0.5, "chg_7": -4.0, "turnover": 1e9},
            {"ticker": "DOGE", "chg": 2.0, "chg_7": -1.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("DOGE", alloc)
        self.assertIn("кэш", why.lower())

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
        alloc, why = policy.score_alloc(rows)
        alts = {k: v for k, v in alloc.items() if k in policy.DESK_ALT_SLEEVE}
        self.assertNotIn("SOL", alloc)
        self.assertEqual(set(alts), {"MNT", "AVAX", "NEAR"})
        self.assertTrue(all(v == policy.alt_slot_pct() for v in alts.values()))
        self.assertLessEqual(sum(alts.values()), policy.ALT_SLEEVE_MAX_PCT + 0.5)
        self.assertLessEqual(sum(alloc.values()), 100.0 - policy.DESK_CASH_FLOOR_PCT + 0.5)
        self.assertIn("альты", why)

    def test_btc_eth_enter_sleeve_at_lower_band(self):
        self.assertIn("BTC", policy.DESK_ALT_SLEEVE)
        self.assertIn("ETH", policy.DESK_ALT_SLEEVE)
        rows = [
            {"ticker": "BTC", "turnover": 1e9, "bb_z": -2.8},
            {"ticker": "ETH", "turnover": 1e9, "bb_z": -2.6},
            {"ticker": "SOL", "turnover": 1e9, "bb_z": -1.0},
        ]
        alloc, _why = policy.score_alloc(rows)
        self.assertEqual(alloc.get("BTC"), policy.alt_slot_pct())
        self.assertEqual(alloc.get("ETH"), policy.alt_slot_pct())
        self.assertNotIn("SOL", alloc)

    def test_sol_in_sleeve(self):
        self.assertIn("SOL", policy.DESK_ALT_SLEEVE)
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.8, "chg_7": 2.5, "turnover": 1e9},
            {"ticker": "SOL", "chg": 5.0, "chg_7": 10.0, "turnover": 1e9, "bb_z": 1.0},
        ]
        alloc, _why = policy.score_alloc(rows)
        self.assertNotIn("SOL", alloc)
        self.assertNotIn("BTC", alloc)
        self.assertNotIn("ETH", alloc)
        rows[2]["bb_z"] = -2.8
        alloc, _why = policy.score_alloc(rows)
        self.assertEqual(alloc.get("SOL"), policy.alt_slot_pct())

    def test_alt_enters_only_at_lower_band(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "MNT", "chg": -5.0, "chg_7": -3.0, "turnover": 1e9, "bb_z": -2.8},
            {"ticker": "LINK", "chg": -6.0, "chg_7": -9.0, "turnover": 1e9, "bb_z": -1.2},
            {"ticker": "NEAR", "chg": -9.0, "chg_7": -20.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertIn("MNT", alloc)
        self.assertNotIn("LINK", alloc)
        self.assertNotIn("NEAR", alloc)  # без полос не входим
        self.assertIn("альты", why)

    def test_held_alt_kept_until_exit_dust_dropped(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "XRP", "chg": 2.0, "chg_7": 1.0, "turnover": 1e9, "bb_z": 0.8, "held_value": 30.0},
            {"ticker": "ADA", "chg": 1.0, "chg_7": 1.0, "turnover": 1e9, "bb_z": 0.5, "held_value": 0.4},
        ]
        alloc, _why = policy.score_alloc(rows)
        self.assertEqual(alloc.get("XRP"), policy.alt_slot_pct())
        self.assertNotIn("ADA", alloc)

    def test_held_alts_take_slots_first(self):
        rows = [
            {"ticker": t, "turnover": 1e9, "bb_z": 0.0, "held_value": 20.0}
            for t in ("XRP", "DOGE", "ADA")
        ] + [{"ticker": "LINK", "turnover": 1e9, "bb_z": -3.5}]
        alts = policy.pick_alts(rows)
        self.assertEqual(set(alts), {"XRP", "DOGE", "ADA"})

    def test_alt_exit_reason(self):
        self.assertEqual(policy.alt_exit_reason(price=93.0, entry=100.0), "стоп")
        self.assertIsNone(policy.alt_exit_reason(price=95.0, entry=100.0))
        self.assertEqual(policy.alt_exit_reason(price=97.0, entry=100.0, swing_low=98.0), "дно")
        self.assertIsNone(policy.alt_exit_reason(price=99.0, entry=100.0, swing_low=98.0))
        self.assertIsNone(policy.alt_exit_reason(price=102.0, entry=100.0, ma=101.0))
        armed = policy.update_alt_exit_arm(None, price=103.0, entry=100.0, now=10.0)
        self.assertAlmostEqual(armed["high"], 103.0)
        self.assertFalse(
            policy.alt_exit_ready(armed, price=101.9, entry=100.0, now=10.0 + policy.ALT_EXIT_HOLD_SEC)
        )
        higher = policy.update_alt_exit_arm(armed, price=108.0, entry=100.0, now=20.0)
        self.assertEqual(higher["ts"], 20.0)
        need = 108.0 * (1.0 - policy.ALT_EXIT_PULLBACK_PCT / 100.0)
        self.assertGreaterEqual((need / 100.0 - 1.0) * 100.0, policy.ALT_EXIT_ARM_PCT)
        self.assertFalse(policy.alt_exit_ready(higher, price=need, entry=100.0, now=20.0 + 10))
        self.assertTrue(
            policy.alt_exit_ready(higher, price=need, entry=100.0, now=20.0 + policy.ALT_EXIT_HOLD_SEC)
        )
        self.assertEqual(
            policy.alt_exit_reason(
                price=need, entry=100.0, exit_arm=higher, now=20.0 + policy.ALT_EXIT_HOLD_SEC
            ),
            "вершина",
        )
        self.assertEqual(policy.position_basis(2.0, None), 2.0)
        self.assertEqual(policy.position_basis(2.0, 1.9), 1.9)
        line = policy.watch_snapshot_line("XRP", price=1.85, entry=2.0, kind="alt", basis=1.9)
        self.assertIn("avg=2", line)
        self.assertIn("отсчёт 1.9", line)
        self.assertIn("до стопа 4.4пп", line)

    def test_alt_sleeve_qty_for_exit_reserves_pocket(self):
        sol = policy.BTC_DIP_TICKER
        self.assertAlmostEqual(
            policy.alt_sleeve_qty_for_exit(
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
            policy.alt_sleeve_qty_for_exit(
                ticker=sol,
                held_qty=0.872,
                pocket_qty=0.872,
                price=111.0,
                min_trade_usd=5.0,
            ),
            0.0,
        )
        self.assertEqual(
            policy.alt_sleeve_qty_for_exit(
                ticker="STRK",
                held_qty=477.0,
                pocket_qty=477.0,
                price=0.06,
                min_trade_usd=5.0,
            ),
            0.0,
        )
        self.assertAlmostEqual(
            policy.alt_sleeve_qty_for_exit(
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
        picked = policy.pick_watch_alt_entries(
            bands, target_alloc={"BTC": 40.0, "XRP": 11.7}, held_alts=set(), blocked={"DOT"}
        )
        self.assertEqual(picked, ["LINK"])
        none = policy.pick_watch_alt_entries(
            bands, target_alloc={"XRP": 11.7}, held_alts={"DOGE", "MNT"}, blocked=set()
        )
        self.assertEqual(none, [])

    def test_rebalance_band(self):
        self.assertFalse(
            policy.should_rebalance_leg(
                current_value=100.0,
                target_value=103.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy.should_rebalance_leg(
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
            policy.should_rebalance_leg(
                current_value=130.0,
                target_value=100.0,
                equity=1000.0,
                band_pct=6.0,
                min_trade_usd=5.0,
                day_chg=policy.TAKE_PROFIT_DAY_PCT,
            )
        )
        self.assertFalse(
            policy.should_rebalance_leg(
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
            policy.should_watch_take_profit(
                day_chg=policy.WATCH_TP_DAY_PCT,
                current_value=140.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertFalse(
            policy.should_watch_take_profit(
                day_chg=5.0,
                current_value=140.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertFalse(
            policy.should_watch_dip_buy(
                ticker="BTC",
                day_chg=-5.0,
                current_value=50.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy.should_watch_dip_buy(
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
            policy.should_watch_dip_buy(
                ticker="DOGE",
                day_chg=-10.0,
                current_value=50.0,
                target_value=100.0,
                min_trade_usd=5.0,
                bb_z=-3.0,
            )
        )
        self.assertFalse(
            policy.should_watch_dip_buy(
                ticker="DOGE",
                day_chg=-10.0,
                current_value=0.0,
                target_value=100.0,
                min_trade_usd=5.0,
                bb_z=-1.0,
            )
        )
        self.assertFalse(
            policy.should_watch_dip_buy(
                ticker="FAKE",
                day_chg=-10.0,
                current_value=50.0,
                target_value=100.0,
                min_trade_usd=5.0,
            )
        )
        self.assertTrue(
            policy.should_watch_take_profit(
                day_chg=policy.WATCH_TP_ALT_DAY_PCT,
                current_value=140.0,
                target_value=100.0,
                min_trade_usd=5.0,
                tp_pct=policy.WATCH_TP_ALT_DAY_PCT,
            )
        )

    def test_exclusion_follows_week(self):
        entered = policy.update_exclusions(
            set(),
            [
                {"ticker": "DOGE", "chg_7": -6.0},
                {"ticker": "SOL", "chg_7": -1.0},
                {"ticker": "BTC", "chg_7": -20.0},
            ],
        )
        self.assertEqual(entered, {"DOGE", "BTC"})
        stayed = policy.update_exclusions(entered, [{"ticker": "DOGE", "chg_7": -2.0}])
        self.assertEqual(stayed, {"DOGE", "BTC"})
        left = policy.update_exclusions(
            stayed,
            [{"ticker": "DOGE", "chg_7": 0.2}, {"ticker": "BTC", "chg_7": 0.2}],
        )
        self.assertEqual(left, set())

    def test_liquid_sleeve_keeps_core_and_adds_turnover(self):
        names = policy.liquid_sleeve_names([
            {"ticker": "HYPE", "turnover": 10_000_000},
            {"ticker": "DOOD", "turnover": 14_000_000, "chg": 200.0},
            {"ticker": "TINY", "turnover": 100_000},
            {"ticker": "RLUSD", "turnover": 20_000_000},
            {"ticker": "BTCUP", "turnover": 20_000_000},
            {"ticker": "PUMP", "turnover": 8_000_000},
            {"ticker": "SUI", "turnover": 20_000_000},
            {"ticker": "SOL", "turnover": 1_000_000},
        ])
        self.assertIn("HYPE", names)
        self.assertIn("SUI", names)
        self.assertIn("AVA", policy.DESK_ALT_SLEEVE)
        self.assertIn("SUI", policy.DESK_ALT_SLEEVE)
        self.assertIn("DOOD", names)
        self.assertIn("PUMP", names)
        self.assertIn("SOL", names)
        self.assertIn("DOT", names)
        self.assertNotIn("TINY", names)
        self.assertNotIn("RLUSD", names)
        self.assertNotIn("BTCUP", names)
        policy.set_live_sleeve(names)
        try:
            alloc = policy.pick_alts([
                {"ticker": "HYPE", "bb_z": -3.2, "held_value": 0},
            ])
            self.assertEqual(alloc.get("HYPE"), policy.alt_slot_pct())
            self.assertFalse(policy.should_watch_dip_buy(
                ticker="DOOD",
                day_chg=200.0,
                current_value=0,
                target_value=100,
                min_trade_usd=5,
                bb_z=-3.0,
            ))
        finally:
            policy.set_live_sleeve(set())

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

        up = [float(i) for i in range(1, 80)]
        self.assertEqual(policy.ma_trend(up), "bull")
        self.assertEqual(policy.ma_trend(list(reversed(up))), "bear")
        self.assertEqual(policy.ma_trend([5.0] * 80), "neutral")
        self.assertTrue(policy.probe_trends_ok("bull", "neutral", 8.0))
        self.assertFalse(policy.probe_trends_ok("bull", "bear", 8.0))
        self.assertFalse(policy.probe_trends_ok("neutral", "neutral", 8.0))
        self.assertFalse(policy.probe_trends_ok("bull", "bull", 30.0))
        self.assertFalse(policy.probe_trends_ok("bull", "neutral", 8.0, week_chg=-6.0))
        self.assertTrue(policy.probe_trends_ok("bull", "neutral", 8.0, week_chg=-4.0))

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
            policy.probe_arm_action(now=100.0 + policy.PROBE_ENTRY_HOLD_SEC, **{**base, "price": 1.08}),
            "drop",
        )
        self.assertEqual(
            policy.probe_arm_action(now=100.0 + policy.PROBE_ENTRY_HOLD_SEC, **base, week_chg=-6.0),
            "drop",
        )
        self.assertEqual(
            policy.probe_exit_reason(price=1.02, entry=1.05, shelf=1.0, trend15="bull"),
            "ожидание",
        )
        self.assertEqual(
            policy.probe_exit_reason(price=1.02, entry=1.05, shelf=1.0, trend15="neutral"),
            "ожидание",
        )
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
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.04, entry=1.0, shelf=0.5, trend15="bull", entry_low=1.05, peak=1.07
            )
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
        self.assertIsNone(
            policy.probe_exit_reason(
                price=1.10, entry=1.05, shelf=1.0, trend15="bull", arm_px=1.08, peak=1.20,
            )
        )
        self.assertEqual(
            policy.probe_exit_reason(
                price=1.06, entry=1.05, shelf=1.0, trend15="bull", arm_px=1.07, peak=1.08,
            ),
            "ожидание",
        )
        self.assertEqual(policy.probe_quote(), 30.0)

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
        self.assertEqual(policy.alt_slot_pct(), 25.0)
        self.assertAlmostEqual(policy.btc_dip_quote(1000.0), 250.0)
        self.assertEqual(policy.sui_dip_quote(), 50.0)
        self.assertEqual(policy.pocket_cash_reserve(1000.0, pocket_open=False), 250.0)
        self.assertEqual(policy.pocket_cash_reserve(1000.0, pocket_open=True), 0.0)
        self.assertIsNone(policy.btc_dip_exit_reason(price=101.1, entry=100.0))
        self.assertIsNone(policy.btc_dip_exit_reason(price=101.0, entry=100.0))
        self.assertEqual(policy.btc_dip_exit_reason(price=85.0, entry=100.0), "стоп")
        self.assertIsNone(policy.btc_dip_exit_reason(price=100.4, entry=100.0))
        armed = policy.update_pocket_exit_arm(None, price=101.1, entry=100.0, now=10.0)
        self.assertAlmostEqual(armed["high"], 101.1)
        self.assertFalse(policy.pocket_exit_ready(armed, price=100.9, now=10.0 + policy.POCKET_EXIT_HOLD_SEC))
        higher = policy.update_pocket_exit_arm(armed, price=104.0, entry=100.0, now=20.0)
        self.assertEqual(higher["ts"], 20.0)
        held = policy.update_pocket_exit_arm(higher, price=103.5, entry=100.0, now=20.0 + policy.POCKET_EXIT_HOLD_SEC)
        self.assertEqual(held["high"], 104.0)
        self.assertEqual(held["ts"], 20.0)
        need = 104.0 * (1.0 - policy.POCKET_EXIT_PULLBACK_PCT / 100.0)
        self.assertFalse(policy.pocket_exit_ready(held, price=need + 0.05, now=20.0 + policy.POCKET_EXIT_HOLD_SEC))
        self.assertTrue(policy.pocket_exit_ready(held, price=need, now=20.0 + policy.POCKET_EXIT_HOLD_SEC))
        self.assertEqual(
            policy.btc_dip_exit_reason(price=need, entry=100.0, exit_arm=held, now=20.0 + policy.POCKET_EXIT_HOLD_SEC),
            "вершина",
        )
        kept = policy.update_pocket_exit_arm(held, price=100.2, entry=100.0, now=80.0)
        self.assertEqual(kept["high"], 104.0)

    def test_watch_stream_tickers_skips_dust(self):
        self.assertEqual(
            policy.watch_stream_tickers(
                held_values={"BNB": 80.0, "MNT": 0.3, "BTC": 10.0},
                pocket_open=False,
            ),
            {"BNB", "BTC"},
        )
        self.assertEqual(
            policy.watch_stream_tickers(held_values={"BNB": 80.0}, pocket_open=True),
            {"BNB", "SOL"},
        )

    def test_entry_arm_waits_for_bounce_then_cancels(self):
        armed = policy.update_alt_arm(None, price=100.0, z=-2.8, now=1.0)
        self.assertAlmostEqual(armed["low"], 100.0)
        later = 1.0 + policy.ALT_ENTRY_HOLD_SEC
        self.assertFalse(policy.alt_bounce_ready(armed, price=101.6, z=-2.2, now=1.0 + 60))
        self.assertTrue(policy.alt_bounce_ready(armed, price=101.6, z=-1.8, now=later))
        deeper = policy.update_alt_arm(armed, price=98.0, z=-3.1, now=later)
        self.assertAlmostEqual(deeper["low"], 98.0)
        self.assertEqual(deeper["ts"], later)
        held = later + policy.ALT_ENTRY_HOLD_SEC
        self.assertFalse(policy.alt_bounce_ready(deeper, price=98.5, z=-2.4, now=held))
        self.assertTrue(policy.alt_bounce_ready(deeper, price=99.5, z=-1.6, now=held))
        self.assertIsNone(policy.update_alt_arm(deeper, price=102.0, z=-0.4, now=held))
        pocket = policy.update_pocket_arm(None, price=100.0, day_chg=-4.0, now=1.0)
        self.assertFalse(policy.pocket_bounce_ready(pocket, price=101.2, now=1.0 + 60))
        self.assertTrue(
            policy.pocket_bounce_ready(pocket, price=101.2, now=1.0 + policy.POCKET_ENTRY_HOLD_SEC)
        )
        self.assertIsNone(policy.update_pocket_arm(pocket, price=101.0, day_chg=-2.0, now=2.0))

    def test_watch_rate_limit(self):
        now = time.time()
        self.assertTrue(policy.watch_rate_ok([], now=now))
        self.assertTrue(policy.watch_rate_ok([now - 10, now - 20], now=now, max_per_hour=3))
        self.assertFalse(policy.watch_rate_ok([now - 10, now - 20], now=now, max_per_hour=2))

    def test_watch_period_sec_and_snapshot(self):
        self.assertEqual(policy.watch_period_sec(legs=[]), policy.WATCH_PERIOD_CALM)
        self.assertEqual(
            policy.watch_period_sec(legs=[{"price": 100.8, "entry": 100.0, "kind": "pocket"}]),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy.watch_period_sec(legs=[{"price": 87.0, "entry": 100.0, "kind": "alt"}]),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy.watch_period_sec(
                legs=[{"price": 101.2, "entry": 100.0, "ma": 101.3, "kind": "alt"}]
            ),
            policy.WATCH_PERIOD_CALM,
        )
        self.assertEqual(
            policy.watch_period_sec(legs=[{"price": 102.7, "entry": 100.0, "kind": "alt"}]),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy.watch_period_sec(
                legs=[{"price": 106.0, "entry": 100.0, "kind": "alt", "exit_high": 108.0}]
            ),
            policy.WATCH_PERIOD_HOT,
        )
        self.assertEqual(
            policy.watch_period_sec(
                legs=[{"price": 100.2, "entry": 100.0, "kind": "pocket", "exit_high": 102.0}]
            ),
            policy.WATCH_PERIOD_HOT,
        )
        line = policy.watch_snapshot_line("SOL", price=101.0, entry=100.0, kind="pocket")
        self.assertIn("avg=100", line)
        self.assertIn("до ловушки", line)
        armed_line = policy.watch_snapshot_line(
            "SOL", price=103.0, entry=100.0, kind="pocket", exit_high=104.0
        )
        self.assertIn("верх 104", armed_line)

    def test_next_watch_at_aligns_to_wall(self):
        from skills.crypto.desk import next_watch_at

        self.assertEqual(next_watch_at(720, 1000.0), 1440.0)
        self.assertEqual(next_watch_at(60, 1445.0), 1500.0)

    def test_trail_arm_and_stop(self):
        # +10% от входа → armed; high 110, trail 6% → stop 103.4; цена 103 → hit
        leg = policy.update_trail_leg(
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
        hit = policy.update_trail_leg(
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
        cold = policy.update_trail_leg(
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
        self.assertIsNone(policy.trail_pct_for("BTC"))
        self.assertIsNone(policy.trail_pct_for("DOGE"))


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

    def test_no_cooldown_on_winning_sell(self):
        now = time.time()
        trades = [
            {"ticker": "BTC", "side": "buy", "quote": 100.0, "price": 50.0, "ts_epoch": now - 3600},
            {"ticker": "BTC", "side": "sell", "quote": 120.0, "price": 60.0, "ts_epoch": now - 100},
        ]
        cool = crypto_journal.cooldown_tickers(12.0, trades, now=now)
        self.assertNotIn("BTC", cool)

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


class TestDeskAutoUsesScore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, attr in (
            ("holds.json", "_HOLD_PATH"),
            ("bought.json", "_BOUGHT_PATH"),
            ("trades.json", "_TRADE_PATH"),
            ("daily.json", "_DAILY_PATH"),
            ("alloc.json", "_ALLOC_PATH"),
            ("trail.json", "_TRAIL_PATH"),
            ("btc_dip.json", "_BTC_DIP_PATH"),
            ("sui_dip.json", "_SUI_DIP_PATH"),
            ("probe.json", "_PROBE_PATH"),
            ("entry_arm.json", "_ENTRY_ARM_PATH"),
            ("excluded.json", "_EXCLUDED_PATH"),
            ("anchor.json", "_ANCHOR_PATH"),
        ):
            path = os.path.join(self.tmp.name, name)
            patcher = patch(f"skills.crypto.common.{attr}", path)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.skill = CryptoSkill()
        self.bands: dict[str, dict] = {}
        bands_patch = patch.object(self.skill, "_alt_bands", side_effect=lambda t: self.bands.get(t))
        bands_patch.start()
        self.addCleanup(bands_patch.stop)
        bars_patch = patch.object(self.skill, "_probe_bars", return_value=[])
        bars_patch.start()
        self.addCleanup(bars_patch.stop)
        cool_patch = patch("skills.crypto.journal.cooldown_tickers", return_value=set())
        cool_patch.start()
        self.addCleanup(cool_patch.stop)

    def test_trade_auto_uses_score_not_groq(self):
        with (
            patch.object(self.skill, "desk_score_alloc", return_value=({}, "кэш")) as score,
            patch.object(self.skill, "ai_pick_alloc") as ai,
            patch.object(self.skill, "desk_auto_candidates", return_value=[]),
            patch.object(self.skill, "_btc_regime", return_value=(1.0, 0.5)),
        ):
            result = self.skill._trade_auto_locked(silent=True)
        score.assert_called_once()
        ai.assert_not_called()
        self.assertIn("кэш", result.lower())

    def test_trade_auto_saves_alloc(self):
        from skills.crypto import common as crypto_common

        with (
            patch.object(self.skill, "desk_score_alloc", return_value=({"BTC": 50.0}, "скор")),
            patch.object(self.skill, "desk_auto_candidates", return_value=[]),
            patch.object(self.skill, "_api_key", ""),
        ):
            self.skill._trade_auto_locked(silent=True)
        state = crypto_common.read_alloc_state()
        self.assertIsNotNone(state)
        self.assertEqual(state["alloc"].get("BTC"), 50.0)

    def test_desk_schedule_counts_from_last_desk(self):
        from skills.crypto import common as crypto_common
        from skills.crypto.desk import next_desk_at

        now = 1_000_000.0
        period = crypto_common._DESK_PERIOD_SEC
        self.assertEqual(next_desk_at(None, now), now)
        self.assertEqual(next_desk_at({"desk_ts": now - period - 60}, now), now)
        self.assertEqual(next_desk_at({"desk_ts": now - 3600}, now), now - 3600 + period)

    def test_watch_write_keeps_desk_ts(self):
        from skills.crypto import common as crypto_common

        crypto_common.write_alloc_state({"BTC": 40.0}, why="стол", desk_ts=123.0)
        crypto_common.write_alloc_state({"BTC": 40.0}, why="дозор", watch_trades=[1.0])
        state = crypto_common.read_alloc_state()
        self.assertEqual(state["desk_ts"], 123.0)
        self.assertGreater(state["ts"], 123.0)

    def test_watch_without_alloc(self):
        result = self.skill._desk_watch_locked(silent=True)
        self.assertIn("цели", result.lower())

    def test_watch_take_profit_sells(self):
        from skills.crypto import common as crypto_common

        crypto_common.write_alloc_state({"BTC": 40.0}, why="test")
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty, quote_usdt))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    20.0,
                    [{"ticker": "BTC", "qty": 2.0, "value": 200.0, "price": 100.0, "name": "Биткоин"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(20.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_btc_regime", return_value=(2.0, 1.0)),
            patch.object(self.skill, "_ticker", return_value={"price": 100.0, "chg": 15.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_api_key", "x"),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"BTC"}
            result = self.skill._desk_watch_locked(silent=True)
        self.assertFalse(any(t == "BTC" and side == "Sell" for t, side, *_ in placed), result)

    def test_watch_trailing_stop_sells_full_leg(self):
        from skills.crypto import common as crypto_common

        crypto_common.write_alloc_state({"BTC": 40.0}, why="test")
        crypto_common.write_trail_state(
            {"BTC": {"entry": 100.0, "high": 120.0, "armed": True}}
        )
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty, quote_usdt))
            return f"trail {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    10.0,
                    [{"ticker": "BTC", "qty": 1.0, "value": 112.0, "price": 112.0, "name": "Биткоин"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(10.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_btc_regime", return_value=(2.0, 1.0)),
            # 112 / 120 high, trail 6% → stop 112.8; 112 <= 112.8 → hit
            patch.object(self.skill, "_ticker", return_value={"price": 112.0, "chg": 1.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_api_key", "x"),
            patch("skills.crypto.journal.open_avg_costs", return_value={"BTC": 100.0}),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"BTC"}
            result = self.skill._desk_watch_locked(silent=True)
        self.assertFalse(placed, result)
        self.assertNotIn("BTC", crypto_common.read_trail_state())

    def test_risk_off_keeps_held_alts(self):
        rows = [{"ticker": "BTC", "chg": -4.0, "chg_7": -8.0, "turnover": 1e9, "held": 0}]
        held = [
            {"ticker": "DOGE", "qty": 300.0, "value": 30.0, "price": 0.1, "name": "Дож"},
            {"ticker": "BTC", "qty": 1.0, "value": 80.0, "price": 80.0, "name": "Биткоин"},
        ]
        with (
            patch.object(self.skill, "_btc_regime", return_value=(-8.0, -4.0)),
            patch.object(self.skill, "desk_auto_candidates", return_value=rows),
            patch.object(self.skill, "_wallet", return_value=(100.0, held)),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"DOGE", "BTC"}
            alloc, why = self.skill.desk_score_alloc(rows)
        self.assertEqual(alloc, {"BTC": policy.alt_slot_pct(), "DOGE": policy.alt_slot_pct()})
        self.assertIn("новые альты не беру", why)

    def test_desk_score_uses_bull_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9, "held": 0},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9, "held": 0},
            {"ticker": "MNT", "chg": -6.0, "chg_7": 1.0, "turnover": 1e9, "bb_z": -3.1, "held": 0},
        ]
        with (
            patch.object(self.skill, "_btc_regime", return_value=(6.0, 1.0)),
            patch.object(self.skill, "desk_auto_candidates", return_value=rows),
        ):
            alloc, why = self.skill.desk_score_alloc(rows)
        self.assertNotIn("BTC", alloc)
        self.assertEqual(alloc.get("MNT"), policy.alt_slot_pct())
        self.assertIn("8%", why)

    def test_missing_bought_file_does_not_claim_held(self):
        self.skill._desk_bought_ready = False
        self.skill._desk_bought = set()
        self.skill._ensure_desk_bought({"BTC", "ETH"})
        self.assertTrue(self.skill._desk_bought_ready)
        self.assertEqual(self.skill._desk_bought, set())
        self.assertTrue(self.skill._is_owner_position("BTC"))
        self.assertTrue(self.skill._is_owner_position("ETH"))

    def test_rebalance_splits_cash_proportionally(self):
        # Кэша мало на обе дыры → делим пропорционально need.
        placed: list[tuple[str, float]] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, float(quote_usdt or 0)))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(800.0, []),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(800.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 1.0, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.should_rebalance_leg", return_value=True),
            patch.object(self.skill, "_alt_entry_ready", return_value=True),
            patch("skills.crypto.desk.time.sleep"),
        ):
            # цели: LINK 60, AVAX 40 при equity≈800. Резерв SOL 25%, второй карман если включён, щуп 30$.
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"LINK", "AVAX"}
            self.skill._rebalance({"LINK": 60.0, "AVAX": 40.0})
        by_ticker = {t: q for t, q in placed}
        self.assertIn("LINK", by_ticker)
        self.assertIn("AVAX", by_ticker)
        self.assertGreater(by_ticker["LINK"], by_ticker["AVAX"])
        second = policy.cfg().sui_usd if policy.cfg().second_on else 0.0
        probe = policy.cfg().probe_usd if policy.cfg().probe_on else 0.0
        self.assertAlmostEqual(
            sum(by_ticker.values()),
            800.0 * 0.998 - 200.0 - second - probe,
            places=1,
        )

    def test_buy_resets_stale_trail_leg(self):
        from skills.crypto import common as crypto_common

        crypto_common.write_trail_state(
            {"MNT": {"entry": 0.67, "high": 0.7069, "armed": True}}
        )
        with (
            patch.object(self.skill, "_filters", return_value={"step": 0.01, "min_amt": 5.0, "min_qty": 0.0}),
            patch.object(self.skill, "_ticker", return_value={"price": 0.65, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_signed", return_value={"result": {"orderId": "1"}}),
            patch.object(
                self.skill,
                "_order_state",
                return_value={"orderStatus": "Filled", "cumExecQty": "7.69", "cumExecValue": "5.0"},
            ),
            patch.object(self.skill, "_journal_trade"),
            patch.object(self.skill, "_mark_desk_bought"),
        ):
            self.skill._place_order("MNT", "Buy", quote_usdt=5.0)
        self.assertNotIn("MNT", crypto_common.read_trail_state())

    def test_watch_trail_ignores_dust(self):
        from skills.crypto import common as crypto_common

        crypto_common.write_alloc_state({"BTC": 40.0}, why="test")
        crypto_common.write_trail_state(
            {"MNT": {"entry": 0.67, "high": 0.7069, "armed": True}}
        )
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    60.0,
                    [{"ticker": "MNT", "qty": 0.5, "value": 0.32, "price": 0.64, "name": "MNT"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(60.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_btc_regime", return_value=(2.0, 1.0)),
            patch.object(self.skill, "_ticker", return_value={"price": 0.64, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_api_key", "x"),
            patch("skills.crypto.journal.open_avg_costs", return_value={}),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"MNT"}
            self.skill._desk_watch_locked(silent=True)
        self.assertFalse(any(t == "MNT" and s == "Sell" for t, s in placed))
        self.assertNotIn("MNT", crypto_common.read_trail_state())

    def _rebalance_with_fresh_alt(self, *, respect_hold: bool, target: dict | None = None) -> list[tuple[str, str]]:
        placed: list[tuple[str, str]] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    200.0,
                    [{"ticker": "DOGE", "qty": 300.0, "value": 30.0, "price": 0.1, "name": "Дож"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(200.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 0.1, "chg": 1.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.journal.recently_bought", return_value={"DOGE"}),
            patch.object(self.skill, "_alt_entry_ready", return_value=True),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"DOGE"}
            self.skill._rebalance(target or {"LINK": 40.0}, respect_hold=respect_hold)
        return placed

    def test_rebalance_keeps_fresh_alt(self):
        placed = self._rebalance_with_fresh_alt(respect_hold=True)
        self.assertNotIn(("DOGE", "Sell"), placed)
        self.assertIn(("LINK", "Buy"), placed)

    def test_rebalance_keeps_sol_dip_pocket(self):
        from skills.crypto.common import write_btc_dip

        write_btc_dip(1.0, 100.0)
        placed: list[tuple[str, str]] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    252.0,
                    [{"ticker": "SOL", "qty": 1.0, "value": 100.0, "price": 100.0, "name": "Солана"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(252.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 100.0, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"SOL"}
            self.skill._rebalance({"BTC": 20.0, "ETH": 20.0})
        self.assertNotIn(("SOL", "Sell"), placed)

    def test_risk_off_rebalance_sells_sleeve_keeps_pocket(self):
        from skills.crypto.common import read_btc_dip, write_btc_dip

        write_btc_dip(0.9, 100.0)
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    100.0,
                    [{"ticker": "SOL", "qty": 1.8, "value": 180.0, "price": 100.0, "name": "Солана"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(100.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 100.0, "chg": -4.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"SOL"}
            self.skill._rebalance({}, respect_hold=False)
        self.assertEqual(placed, [("SOL", "Sell", 0.9)])
        self.assertAlmostEqual(read_btc_dip()["qty"], 0.9)

    def test_btc_dip_sells_only_pocket_on_bounce(self):
        from skills.crypto.common import read_btc_dip, write_btc_dip

        write_btc_dip(0.01, 100.0)
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty))
            return f"ok {ticker}"

        phrase = None
        with patch.object(self.skill, "_place_order", side_effect=place):
            phrase = self.skill._trade_btc_dip(
                day_chg=-6.0,
                positions={"SOL": {"qty": 1.0, "value": 101.5, "price": 101.5}},
                price=101.5,
                blocked=False,
            )
        self.assertIsNone(phrase)
        self.assertEqual(placed, [])
        self.assertAlmostEqual(read_btc_dip()["exit_high"], 101.5)
        higher = None
        with (
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.time.time", return_value=time.time() + policy.POCKET_EXIT_HOLD_SEC + 5),
        ):
            higher = self.skill._trade_btc_dip(
                day_chg=-1.0,
                positions={"SOL": {"qty": 1.0, "value": 100.4, "price": 100.4}},
                price=101.5 * (1.0 - policy.POCKET_EXIT_PULLBACK_PCT / 100.0),
                blocked=False,
            )
        self.assertIn("ok", higher or "")
        self.assertEqual(placed[0][0], "SOL")
        self.assertEqual(placed[0][2], 0.01)
        self.assertEqual(read_btc_dip()["qty"], 0.0)

    def test_pocket_keeps_coins_when_sell_does_not_fill(self):
        from skills.crypto.common import read_btc_dip, write_btc_dip

        write_btc_dip(1.0, 100.0, low=90.0)

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            raise RuntimeError("заявка не исполнилась")

        with patch.object(self.skill, "_place_order", side_effect=place):
            phrase = self.skill._trade_btc_dip(
                day_chg=-20.0,
                positions={"SOL": {"qty": 1.0, "value": 80.0, "price": 80.0}},
                price=80.0,
                blocked=False,
            )
        self.assertIsNone(phrase)
        self.assertAlmostEqual(read_btc_dip()["qty"], 1.0)
        self.assertAlmostEqual(read_btc_dip()["low"], 90.0)

    def test_sui_pocket_buys_fifty_and_keeps_sol(self):
        from skills.crypto.common import read_btc_dip, read_entry_arms, read_sui_dip, write_btc_dip, write_entry_arms

        write_btc_dip(0.2, 100.0)
        now = time.time()
        write_entry_arms(
            {},
            {"low": 100.0, "ts": now},
            sui={"low": 1.0, "ts": now - policy.POCKET_ENTRY_HOLD_SEC - 5},
        )
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, quote_usdt))
            return f"ok {ticker}"

        held_bars = [
            {"high": 1.2, "low": 1.05, "close": 1.1, "volume": 1.0},
            {"high": 1.1, "low": 1.0, "close": 1.02, "volume": 1.0},
            {"high": 1.08, "low": 1.01, "close": 1.04, "volume": 1.0},
            {"high": 1.05, "low": 1.012, "close": 1.01, "volume": 1.0},
        ]
        with (
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_probe_bars", return_value=held_bars),
            patch.object(
                self.skill,
                "_cash_and_held",
                return_value=(80.0, {"AVA": {"qty": 40.0, "value": 50.0, "price": 1.01}}),
            ),
        ):
            phrase = self.skill._trade_btc_dip(
                day_chg=-4.0,
                positions={},
                price=1.01,
                blocked=False,
                equity=400.0,
                ticker=policy.SUI_DIP_TICKER,
            )
        self.assertIn("ok", phrase or "")
        self.assertEqual(placed, [("AVA", "Buy", 50.0)])
        self.assertAlmostEqual(read_sui_dip()["qty"], 40.0)
        self.assertAlmostEqual(read_btc_dip()["qty"], 0.2)
        arms = read_entry_arms()
        self.assertIsNone(arms.get("ava"))
        self.assertIsNotNone(arms.get("pocket"))

    def test_rebalance_sells_sui_sleeve_keeps_pocket(self):
        from skills.crypto.common import read_sui_dip, write_sui_dip

        write_sui_dip(40.0, 1.0)
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    200.0,
                    [{"ticker": "AVA", "qty": 90.0, "value": 90.0, "price": 1.0, "name": "Ава"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(200.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 1.0, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"AVA"}
            self.skill._rebalance({}, respect_hold=False)
        self.assertEqual(placed, [("AVA", "Sell", 50.0)])
        self.assertAlmostEqual(read_sui_dip()["qty"], 40.0)

    def test_probe_buys_thirty_then_exits_under_shelf(self):
        from skills.crypto.common import read_probe, write_probe

        write_probe(arm_ticker="DOOD", arm_shelf=1.0, arm_ts=time.time() - policy.PROBE_ENTRY_HOLD_SEC - 5)
        bars = [{"high": float(i), "low": float(i), "close": float(i), "volume": 1.0} for i in range(1, 70)]
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, quote_usdt, base_qty))
            return f"ok {ticker}"

        cash = iter((
            (100.0, {}),
            (70.0, {"DOOD": {"qty": 20.0, "value": 30.0, "price": 1.5}}),
        ))
        with (
            patch.object(self.skill, "_ticker", return_value={"price": 1.5, "chg": 8.0, "turnover": 2_000_000}),
            patch.object(self.skill, "_probe_bars", return_value=bars),
            patch.object(self.skill, "_momentum", return_value={"chg_7": 1.0}),
            patch.object(self.skill, "_cash_and_held", side_effect=lambda: next(cash)),
            patch.object(self.skill, "_place_order", side_effect=place),
        ):
            self.skill._probe_locked()
        self.assertEqual(placed, [("DOOD", "Buy", 30.0, None)])
        self.assertAlmostEqual(read_probe()["qty"], 20.0)
        self.assertAlmostEqual(read_probe()["shelf"], 1.0)

        placed.clear()
        with (
            patch.object(self.skill, "_ticker", return_value={"price": 0.9, "chg": 1.0, "turnover": 1}),
            patch.object(self.skill, "_probe_bars", return_value=bars),
            patch.object(self.skill, "_cash_and_held", return_value=(70.0, {"DOOD": {"qty": 20.0}})),
            patch.object(self.skill, "_place_order", side_effect=place),
        ):
            self.skill._probe_locked()
        self.assertEqual(placed, [("DOOD", "Sell", None, 20.0)])
        self.assertEqual(read_probe()["qty"], 0.0)
        self.assertEqual(read_probe()["ticker"], "")

    def test_risk_off_sells_core_keeps_alt(self):
        placed: list[tuple[str, str]] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side))
            return f"ok {ticker}"

        with (
            patch.object(
                self.skill,
                "_wallet",
                return_value=(
                    10.0,
                    [
                        {"ticker": "DOGE", "qty": 300.0, "value": 12.0, "price": 0.04, "name": "Дож"},
                        {"ticker": "BTC", "qty": 1.0, "value": 60.0, "price": 60.0, "name": "Биткоин"},
                    ],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(10.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 0.1, "chg": 1.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"DOGE", "BTC"}
            self.skill._rebalance({"DOGE": policy.alt_slot_pct()}, respect_hold=False)
        self.assertIn(("BTC", "Sell"), placed)
        self.assertNotIn(("DOGE", "Sell"), placed)

    def _watch_alt(self, *, price: float, entry: float, alloc: dict, positions: list, cash: float = 60.0):
        from skills.crypto import common as crypto_common

        crypto_common.write_alloc_state(alloc, why="test")
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty, quote_usdt))
            return f"ok {ticker}"

        with (
            patch.object(self.skill, "_wallet", return_value=(cash, positions)),
            patch.object(self.skill, "_cash_and_held", return_value=(cash, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_btc_regime", return_value=(2.0, 1.0)),
            patch.object(self.skill, "_ticker", return_value={"price": price, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_api_key", "x"),
            patch("skills.crypto.journal.open_avg_costs", return_value={"XRP": entry}),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"XRP", "BTC"}
            result = self.skill._desk_watch_locked(silent=True)
        return placed, result, crypto_common.read_alloc_state()

    def test_watch_sells_alt_at_middle_band(self):
        self.bands = {"XRP": {"z": 0.1, "ma": 2.0}}
        pos = [{"ticker": "XRP", "qty": 20.0, "value": 41.0, "price": 2.05, "name": "XRP"}]
        placed, result, state = self._watch_alt(
            price=2.05, entry=2.0, alloc={"BTC": 40.0, "XRP": 11.7}, positions=pos
        )
        self.assertFalse(any(t == "XRP" and side == "Sell" for t, side, *_ in placed), result)
        self.assertIn("XRP", state["alloc"])

    def test_watch_sells_alt_on_pullback_from_high(self):
        from skills.crypto.common import read_sleeve_exits

        self.bands = {"XRP": {"z": 0.4, "ma": 2.0}}
        pos = [{"ticker": "XRP", "qty": 20.0, "value": 42.0, "price": 2.10, "name": "XRP"}]
        placed, _result, state = self._watch_alt(
            price=2.10, entry=2.0, alloc={"BTC": 40.0, "XRP": 11.7}, positions=pos
        )
        self.assertFalse(any(side == "Sell" for _t, side, *_ in placed))
        self.assertIn("XRP", state["alloc"])
        self.assertAlmostEqual(read_sleeve_exits()["XRP"]["high"], 2.10)
        pull = 2.10 * (1.0 - policy.ALT_EXIT_PULLBACK_PCT / 100.0)
        pos[0]["price"] = pull
        with patch("skills.crypto.desk.time.time", return_value=time.time() + policy.ALT_EXIT_HOLD_SEC + 5):
            placed, result, state = self._watch_alt(
                price=pull, entry=2.0, alloc={"BTC": 40.0, "XRP": 11.7}, positions=pos
            )
        self.assertIn(("XRP", "Sell", 20.0, None), placed, result)
        self.assertNotIn("XRP", state["alloc"])
        self.assertNotIn("XRP", read_sleeve_exits())

    def test_anchor_measures_stop_from_current_price(self):
        from skills.crypto.common import write_anchors

        write_anchors({"XRP": 1.90})
        self.bands = {"XRP": {"z": -2.0, "ma": 2.2}}
        pos = [{"ticker": "XRP", "qty": 20.0, "value": 37.0, "price": 1.85, "name": "XRP"}]
        placed, result, _state = self._watch_alt(
            price=1.85, entry=2.0, alloc={"XRP": 11.7}, positions=pos
        )
        self.assertFalse(any(side == "Sell" for _t, side, *_ in placed), result)

    def test_watch_stop_loss_alt(self):
        self.bands = {"XRP": {"z": -3.0, "ma": 2.4}}
        pos = [{"ticker": "XRP", "qty": 20.0, "value": 33.0, "price": 1.65, "name": "XRP"}]
        placed, result, _state = self._watch_alt(
            price=1.65, entry=2.0, alloc={"BTC": 40.0, "XRP": 11.7}, positions=pos
        )
        self.assertIn(("XRP", "Sell", 20.0, None), placed, result)

    def test_watch_holds_alt_between_bands(self):
        self.bands = {"XRP": {"z": -1.0, "ma": 2.2}}
        pos = [{"ticker": "XRP", "qty": 20.0, "value": 38.0, "price": 1.9, "name": "XRP"}]
        placed, _result, _state = self._watch_alt(
            price=1.9, entry=2.0, alloc={"BTC": 40.0, "XRP": 11.7}, positions=pos
        )
        self.assertFalse(any(t == "XRP" for t, *_ in placed))

    def test_watch_enters_alt_at_lower_band(self):
        from skills.crypto.common import read_entry_arms

        self.bands = {"LINK": {"z": -2.9, "ma": 20.0}, "NEAR": {"z": -1.0, "ma": 3.0}}
        placed, _result, state = self._watch_alt(
            price=18.0, entry=0.0, alloc={"BTC": 40.0}, positions=[], cash=100.0
        )
        self.assertFalse([t for t, side, *_ in placed if side == "Buy"])
        self.assertNotIn("LINK", state["alloc"])
        self.assertAlmostEqual(read_entry_arms()["alts"]["LINK"]["low"], 18.0)
        from skills.crypto.common import write_entry_arms

        aged = read_entry_arms()
        aged["alts"]["LINK"]["ts"] = time.time() - policy.ALT_ENTRY_HOLD_SEC - 5
        write_entry_arms(aged["alts"], aged.get("pocket"))
        held_bars = [
            {"high": 19.0, "low": 18.0, "close": 18.2, "volume": 1.0},
            {"high": 18.8, "low": 18.2, "close": 18.4, "volume": 1.0},
            {"high": 18.6, "low": 18.25, "close": 18.3, "volume": 1.0},
        ]
        with patch.object(self.skill, "_probe_bars", return_value=held_bars):
            placed, result, state = self._watch_alt(
                price=18.3, entry=0.0, alloc={"BTC": 40.0}, positions=[], cash=400.0
            )
        buys = [(t, q) for t, side, _b, q in placed if side == "Buy"]
        self.assertEqual([t for t, _q in buys], ["LINK"], result)
        self.assertAlmostEqual(buys[0][1], 100.0, places=1)
        self.assertEqual(state["alloc"].get("LINK"), policy.alt_slot_pct())

    def test_watch_rate_limit_still_exits_alt(self):
        from skills.crypto import common as crypto_common

        now = time.time()
        crypto_common.write_alloc_state(
            {"XRP": 11.7}, why="test", watch_trades=[now, now - 10]
        )
        self.bands = {"XRP": {"z": -3.0, "ma": 2.4}}
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side, base_qty))
            return f"ok {ticker}"

        pos = [{"ticker": "XRP", "qty": 20.0, "value": 33.0, "price": 1.65, "name": "XRP"}]
        with (
            patch.object(self.skill, "_wallet", return_value=(60.0, pos)),
            patch.object(self.skill, "_cash_and_held", return_value=(60.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_btc_regime", return_value=(2.0, 1.0)),
            patch.object(self.skill, "_ticker", return_value={"price": 1.65, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_api_key", "x"),
            patch("skills.crypto.journal.open_avg_costs", return_value={"XRP": 2.0}),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"XRP"}
            result = self.skill._desk_watch_locked(silent=True)
        self.assertIn(("XRP", "Sell", 20.0), placed, result)

    def test_watch_rate_limit_blocks_new_entry(self):
        from skills.crypto import common as crypto_common

        now = time.time()
        crypto_common.write_alloc_state(
            {"BTC": 40.0}, why="test", watch_trades=[now, now - 10]
        )
        self.bands = {"LINK": {"z": -2.9, "ma": 20.0}}
        placed: list[tuple] = []

        def place(ticker, side, quote_usdt=None, base_qty=None, price=0.0, maker=False):
            placed.append((ticker, side))
            return f"ok {ticker}"

        with (
            patch.object(self.skill, "_wallet", return_value=(100.0, [])),
            patch.object(self.skill, "_cash_and_held", return_value=(100.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_btc_regime", return_value=(2.0, 1.0)),
            patch.object(self.skill, "_ticker", return_value={"price": 18.0, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch.object(self.skill, "_api_key", "x"),
            patch("skills.crypto.journal.open_avg_costs", return_value={}),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = set()
            result = self.skill._desk_watch_locked(silent=True)
        self.assertFalse(placed, result)
        self.assertNotIn("LINK", crypto_common.read_alloc_state()["alloc"])

    def test_watch_sets_hot_period_near_stop(self):
        # −5% ещё не стоп (−7%), но уже горячая зона.
        self.bands = {"XRP": {"z": -2.0, "ma": 2.2}}
        pos = [{"ticker": "XRP", "qty": 20.0, "value": 38.0, "price": 1.90, "name": "XRP"}]
        placed, _result, _state = self._watch_alt(
            price=1.90, entry=2.0, alloc={"XRP": 11.7}, positions=pos
        )
        self.assertFalse(any(t == "XRP" and side == "Sell" for t, side, *_ in placed))
        self.assertEqual(self.skill._watch_period_sec, policy.WATCH_PERIOD_HOT)
        self.assertEqual(self.skill._quote_cache_sec, 0.0)


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
        self.assertAlmostEqual(self.orders[0][1], 26.0)  # дыра 16$ + буфер 10$
        self.assertAlmostEqual(self.spot, 36.0)

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
        self.assertAlmostEqual(self.orders[0][1], 70.0)
        self.assertAlmostEqual(self.spot, 10.0)

    def test_park_tops_up_spot_buffer(self):
        self.spot = 0.2
        with self._patched():
            self.skill._earn_park_idle()
        self.assertEqual(self.orders[0][0], "Redeem")
        self.assertAlmostEqual(self.orders[0][1], 9.8)
        self.assertAlmostEqual(self.spot, 10.0)

    def test_park_skips_small_leftover(self):
        self.spot = 11.0
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


if __name__ == "__main__":
    unittest.main()
