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
from skills.crypto.skill import CryptoSkill


_MSK = ZoneInfo("Europe/Moscow")


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

    def test_band_core_tighter_than_alt(self):
        self.assertEqual(policy.band_pct_for("BTC"), policy.REBALANCE_BAND_CORE_PCT)
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
        self.assertTrue(alloc)
        # Ядро ≤ 100 − floor − рукав; без альтов рукав остаётся кэшем.
        core_cap = 100.0 - policy.DESK_CASH_FLOOR_PCT - policy.ALT_SLEEVE_MAX_PCT
        self.assertLessEqual(sum(alloc.values()), core_cap + 0.5)
        self.assertIn("Скор", why)
        self.assertIn("альты 0%", why)

    def test_score_alloc_bull_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows, cash_floor_pct=policy.DESK_CASH_FLOOR_BULL_PCT)
        self.assertTrue(alloc)
        core_cap = 100.0 - policy.DESK_CASH_FLOOR_BULL_PCT - policy.ALT_SLEEVE_MAX_PCT
        self.assertGreaterEqual(sum(alloc.values()), core_cap - 0.5)
        self.assertLessEqual(sum(alloc.values()), core_cap + 0.5)
        self.assertIn("8%", why)

    def test_core_split_equally_despite_scores(self):
        rows = [
            {"ticker": "BTC", "chg": -0.4, "chg_7": -0.5, "turnover": 1e9},
            {"ticker": "ETH", "chg": 1.5, "chg_7": 3.0, "turnover": 1e9},
        ]
        alloc, _why = policy.score_alloc(rows)
        core_budget = 100.0 - policy.DESK_CASH_FLOOR_PCT - policy.ALT_SLEEVE_MAX_PCT
        self.assertEqual(alloc["BTC"], alloc["ETH"])
        self.assertAlmostEqual(alloc["BTC"] + alloc["ETH"], core_budget, delta=0.2)

    def test_score_empty_when_core_below_floor(self):
        rows = [
            {"ticker": "BTC", "chg": -2.0, "chg_7": -9.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": -1.0, "chg_7": -10.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertEqual(alloc, {})
        self.assertIn("кэш", why.lower())

    def test_score_allows_core_mild_dip(self):
        # Mean-reversion: ядро с откатом 7д до -8 ещё в игре.
        rows = [
            {"ticker": "BTC", "chg": 0.5, "chg_7": -4.0, "turnover": 1e9},
            {"ticker": "DOGE", "chg": 2.0, "chg_7": -1.0, "turnover": 1e9},
        ]
        alloc, why = policy.score_alloc(rows)
        self.assertIn("BTC", alloc)
        self.assertNotIn("DOGE", alloc)
        self.assertIn("Скор", why)

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
        core_sum = sum(v for k, v in alloc.items() if k in policy.DESK_CORE)
        self.assertNotIn("SOL", alloc)
        self.assertEqual(set(alts), {"MNT", "AVAX", "NEAR"})
        self.assertTrue(all(v == policy.alt_slot_pct() for v in alts.values()))
        self.assertLessEqual(sum(alts.values()), policy.ALT_SLEEVE_MAX_PCT + 0.5)
        core_cap = 100.0 - policy.DESK_CASH_FLOOR_PCT - policy.ALT_SLEEVE_MAX_PCT
        self.assertLessEqual(core_sum, core_cap + 0.5)
        self.assertLessEqual(sum(alloc.values()), 100.0 - policy.DESK_CASH_FLOOR_PCT + 0.5)
        self.assertIn("альты", why)

    def test_sol_in_sleeve_not_core(self):
        self.assertNotIn("SOL", policy.DESK_CORE)
        self.assertIn("SOL", policy.DESK_ALT_SLEEVE)
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 3.0, "turnover": 1e9},
            {"ticker": "ETH", "chg": 0.8, "chg_7": 2.5, "turnover": 1e9},
            {"ticker": "SOL", "chg": 5.0, "chg_7": 10.0, "turnover": 1e9, "bb_z": 1.0},
        ]
        alloc, _why = policy.score_alloc(rows)
        self.assertNotIn("SOL", alloc)
        self.assertIn("BTC", alloc)
        self.assertIn("ETH", alloc)
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
        self.assertIn("BTC", alloc)
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
        self.assertEqual(policy.alt_exit_reason(price=85.0, entry=100.0, ma=120.0), "стоп")
        self.assertEqual(policy.alt_exit_reason(price=102.0, entry=100.0, ma=101.0), "средняя")
        # У средней, но прибыль не перекрывает комиссии с запасом.
        self.assertIsNone(policy.alt_exit_reason(price=100.5, entry=100.0, ma=100.2))
        self.assertIsNone(policy.alt_exit_reason(price=105.0, entry=100.0, ma=110.0))
        self.assertIsNone(policy.alt_exit_reason(price=90.0, entry=100.0, ma=None))

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
        self.assertEqual(picked, ["LINK", "NEAR"])
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
        self.assertTrue(
            policy.should_watch_dip_buy(
                ticker="BTC",
                day_chg=policy.WATCH_DIP_DAY_PCT,
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

    def test_watch_rate_limit(self):
        now = time.time()
        self.assertTrue(policy.watch_rate_ok([], now=now))
        self.assertTrue(policy.watch_rate_ok([now - 10, now - 20], now=now, max_per_hour=3))
        self.assertFalse(policy.watch_rate_ok([now - 10, now - 20], now=now, max_per_hour=2))

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
        self.assertEqual(policy.trail_pct_for("BTC"), policy.TRAIL_CORE_PCT)
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
            # equity≈220, target BTC 40%≈88, current 200 → excess, day +15% ≥ watch TP
            result = self.skill._desk_watch_locked(silent=True)
        self.assertTrue(any(side == "Sell" for _t, side, *_ in placed), result)
        self.assertIn("дозор", result.lower())

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
        self.assertTrue(placed, result)
        self.assertEqual(placed[0][0], "BTC")
        self.assertEqual(placed[0][1], "Sell")
        self.assertAlmostEqual(float(placed[0][2]), 1.0)
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
        self.assertEqual(alloc, {"DOGE": policy.alt_slot_pct()})
        self.assertIn("альты до выхода", why)

    def test_desk_score_uses_bull_cash_floor(self):
        rows = [
            {"ticker": "BTC", "chg": 1.0, "chg_7": 4.0, "turnover": 1e9, "held": 0},
            {"ticker": "ETH", "chg": 0.5, "chg_7": 3.0, "turnover": 1e9, "held": 0},
        ]
        with (
            patch.object(self.skill, "_btc_regime", return_value=(6.0, 1.0)),
            patch.object(self.skill, "desk_auto_candidates", return_value=rows),
        ):
            alloc, why = self.skill.desk_score_alloc(rows)
        self.assertTrue(alloc)
        core_cap = 100.0 - policy.DESK_CASH_FLOOR_BULL_PCT - policy.ALT_SLEEVE_MAX_PCT
        self.assertGreaterEqual(sum(alloc.values()), core_cap - 0.5)
        self.assertLessEqual(sum(alloc.values()), core_cap + 0.5)
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
                return_value=(100.0, []),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(100.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 1.0, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.desk.should_rebalance_leg", return_value=True),
            patch("skills.crypto.desk.time.sleep"),
        ):
            # цели: BTC 60, ETH 40 при equity≈100 → need 60 и 40, budget≈99.8
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"BTC", "ETH"}
            self.skill._rebalance({"BTC": 60.0, "ETH": 40.0})
        by_ticker = {t: q for t, q in placed}
        self.assertIn("BTC", by_ticker)
        self.assertIn("ETH", by_ticker)
        self.assertGreater(by_ticker["BTC"], by_ticker["ETH"])
        self.assertAlmostEqual(sum(by_ticker.values()), 100.0 * 0.998, places=1)

    def test_buy_resets_stale_trail_leg(self):
        from skills.crypto import common as crypto_common

        crypto_common.write_trail_state(
            {"MNT": {"entry": 0.67, "high": 0.7069, "armed": True}}
        )
        with (
            patch.object(self.skill, "_filters", return_value={"step": 0.01, "min_amt": 5.0, "min_qty": 0.0}),
            patch.object(self.skill, "_ticker", return_value={"price": 0.65, "chg": 0.0, "turnover": 1}),
            patch.object(self.skill, "_signed", return_value={"result": {"orderId": "1"}}),
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
                    70.0,
                    [{"ticker": "DOGE", "qty": 300.0, "value": 30.0, "price": 0.1, "name": "Дож"}],
                ),
            ),
            patch.object(self.skill, "_cash_and_held", return_value=(70.0, {})),
            patch.object(self.skill, "_ensure_desk_bought"),
            patch.object(self.skill, "_ticker", return_value={"price": 0.1, "chg": 1.0, "turnover": 1}),
            patch.object(self.skill, "_place_order", side_effect=place),
            patch("skills.crypto.journal.recently_bought", return_value={"DOGE"}),
            patch("skills.crypto.desk.time.sleep"),
        ):
            self.skill._desk_bought_ready = True
            self.skill._desk_bought = {"DOGE"}
            self.skill._rebalance(target or {"BTC": 40.0}, respect_hold=respect_hold)
        return placed

    def test_rebalance_keeps_fresh_alt(self):
        placed = self._rebalance_with_fresh_alt(respect_hold=True)
        self.assertNotIn(("DOGE", "Sell"), placed)
        self.assertIn(("BTC", "Buy"), placed)

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
        self.assertIn(("XRP", "Sell", 20.0, None), placed, result)
        self.assertNotIn("XRP", state["alloc"])

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
        self.bands = {"LINK": {"z": -2.9, "ma": 20.0}, "NEAR": {"z": -1.0, "ma": 3.0}}
        placed, result, state = self._watch_alt(
            price=18.0, entry=0.0, alloc={"BTC": 40.0}, positions=[], cash=100.0
        )
        buys = [(t, q) for t, side, _b, q in placed if side == "Buy"]
        self.assertEqual([t for t, _q in buys], ["LINK"], result)
        self.assertAlmostEqual(buys[0][1], 100.0 * policy.alt_slot_pct() / 100.0, places=1)
        self.assertEqual(state["alloc"].get("LINK"), policy.alt_slot_pct())


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
        ]
        self._run("Buy", quote_usdt=26.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Limit", "Market"])
        self.assertAlmostEqual(float(self.bodies[1]["qty"]), 16.0, places=2)

    def test_unfilled_limit_falls_back_to_market(self):
        self.states = [{"orderStatus": "Cancelled", "cumExecQty": "0", "cumExecValue": "0"}]
        self._run("Sell", base_qty=0.26, price=100.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Limit", "Market"])
        self.assertEqual(self.bodies[0]["price"], "100.01")  # продажа округляется вверх
        self.assertAlmostEqual(float(self.bodies[1]["qty"]), 0.26, places=4)

    def test_voice_order_stays_market(self):
        with patch.object(self.skill, "_signed", side_effect=self._signed):
            self.skill._place_order("DOGE", "Buy", quote_usdt=26.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Market"])

    def test_maker_disabled_by_env(self):
        with patch.dict(os.environ, {"CRYPTO_MAKER_ORDERS": "off"}):
            self._run("Buy", quote_usdt=26.0)
        self.assertEqual([b["orderType"] for b in self.bodies], ["Market"])

    def test_price_step_rounding(self):
        from skills.crypto.common import _price_str

        self.assertEqual(_price_str(0.094999, 0.0001), "0.0949")
        self.assertEqual(_price_str(0.094999, 0.0001, round_up=True), "0.095")
        self.assertEqual(_price_str(123.456, 0.0), "123.456")


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


if __name__ == "__main__":
    unittest.main()
