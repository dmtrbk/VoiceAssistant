import os
import tempfile
import time
import unittest
from datetime import date, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from skills.stocks import StocksSkill
from skills.stocks import journal as sj
from skills.stocks.common import read_alloc_state, read_trail_state, write_alloc_state, write_trail_state
from skills.stocks.desk_policy import (
    CASH_FLOOR_PCT,
    apply_cash_floor,
    cash_floor_pct,
    exit_reason,
    index_changes,
    is_risk_off,
    park_amount_rub,
    park_release_lots,
    should_rebalance_leg,
    update_trail_leg,
    watch_rate_ok,
)

_MSK = ZoneInfo("Europe/Moscow")


class TestStocksPolicy(unittest.TestCase):
    def test_index_changes_week_and_day(self):
        history = [
            ("2026-10-02", 2279.75),
            ("2026-10-01", 2260.0),
            ("2026-09-26", 2400.0),
            ("2026-09-25", 2380.0),
        ]
        week, day = index_changes(history, 2290.02, 0.56, today=date(2026, 10, 4))
        self.assertAlmostEqual(week, (2290.02 / 2400.0 - 1) * 100, places=1)
        self.assertEqual(day, 0.56)

    def test_index_changes_without_current_uses_last_close(self):
        history = [("2026-10-02", 2200.0), ("2026-10-01", 2250.0), ("2026-09-24", 2300.0)]
        week, day = index_changes(history, None, None, today=date(2026, 10, 2))
        self.assertAlmostEqual(day, (2200 / 2250 - 1) * 100, places=1)
        self.assertAlmostEqual(week, (2200 / 2300 - 1) * 100, places=1)

    def test_index_changes_empty(self):
        self.assertEqual(index_changes([], None, None, today=date(2026, 10, 2)), (None, None))

    def test_risk_off_and_cash_floor(self):
        self.assertTrue(is_risk_off(week=-4.5, day=0.5))
        self.assertTrue(is_risk_off(week=-1.0, day=-2.5))
        self.assertFalse(is_risk_off(week=1.0, day=-2.5))
        self.assertFalse(is_risk_off(week=None, day=None))
        self.assertEqual(cash_floor_pct(week=-5.0, day=0.0), 100.0)
        self.assertEqual(cash_floor_pct(week=4.0, day=0.2), 0.0)
        self.assertEqual(cash_floor_pct(week=1.0, day=0.2), CASH_FLOOR_PCT)

    def test_apply_cash_floor_scales_down(self):
        scaled = apply_cash_floor({"SBER": 60.0, "NVTK": 40.0}, 10.0)
        self.assertAlmostEqual(sum(scaled.values()), 90.0, delta=0.2)
        self.assertEqual(apply_cash_floor({"SBER": 50.0}, 10.0), {"SBER": 50.0})
        self.assertEqual(apply_cash_floor({"SBER": 100.0}, 100.0), {})

    def test_band_keeps_small_drift(self):
        kw = {"equity": 100_000, "min_trade_rub": 2000}
        self.assertFalse(should_rebalance_leg(current_value=52_000, target_value=50_000, **kw))
        self.assertTrue(should_rebalance_leg(current_value=56_000, target_value=50_000, **kw))
        self.assertTrue(
            should_rebalance_leg(current_value=53_000, target_value=50_000, day_chg=8.0, **kw)
        )

    def test_watch_rate(self):
        now = 10_000.0
        self.assertTrue(watch_rate_ok([now - 4000, now - 100], now=now))
        self.assertFalse(watch_rate_ok([now - 200, now - 100], now=now))

    def test_trail_and_stop(self):
        leg = update_trail_leg(price=106.0, entry=100.0)
        self.assertTrue(leg["armed"])
        self.assertFalse(leg["hit"])
        leg = update_trail_leg(price=101.0, entry=100.0, high=110.0, armed=True)
        self.assertTrue(leg["hit"])
        self.assertEqual(exit_reason(price=101.0, entry=100.0, trail_hit=True), "трейл")
        self.assertEqual(exit_reason(price=89.0, entry=100.0, trail_hit=False), "стоп")
        self.assertIsNone(exit_reason(price=99.0, entry=100.0, trail_hit=False))
        unarmed = update_trail_leg(price=101.0, entry=100.0, high=104.0)
        self.assertFalse(unarmed["hit"])

    def test_park_amounts(self):
        self.assertEqual(park_amount_rub(cash=1000, equity=100_000), 0.0)
        self.assertAlmostEqual(park_amount_rub(cash=20_000, equity=100_000), 18_000)
        self.assertEqual(park_release_lots(need_rub=5000, cash=6000, lot_cost=1.7, held_lots=100), 0)
        self.assertEqual(park_release_lots(need_rub=5000, cash=4000, lot_cost=100, held_lots=50), 10)
        self.assertEqual(park_release_lots(need_rub=50_000, cash=0, lot_cost=100, held_lots=50), 50)


class TestStocksJournal(unittest.TestCase):
    def test_fill_price_and_commission(self):
        data = {
            "executedOrderPrice": {"units": "130", "nano": 500000000},
            "executedCommission": {"units": "0", "nano": 650000000},
        }
        self.assertAlmostEqual(sj.fill_price(data, lot=10, quote_price=131.0), 130.5)
        self.assertAlmostEqual(sj.order_commission(data), 0.65)
        per_lot = {"executedOrderPrice": {"units": "1305", "nano": 0}}
        self.assertAlmostEqual(sj.fill_price(per_lot, lot=10, quote_price=131.0), 130.5)
        self.assertEqual(sj.fill_price({}, lot=1, quote_price=99.0), 99.0)

    def test_realized_with_commission_and_legacy_rows(self):
        trades = [
            {"ticker": "SBER", "side": "buy", "qty": 10, "lots": 1, "price": 300.0, "commission": 1.0},
            {"ticker": "SBER", "side": "sell", "qty": 10, "lots": 1, "price": 310.0, "commission": 1.0},
            {"ticker": "NVTK", "side": "buy", "lots": 1, "price": 1051.6},
            {"ticker": "NVTK", "side": "sell", "lots": 1, "price": 1038.9},
        ]
        self.assertAlmostEqual(sj.realized_pnl(trades), 98.0 - 12.7, places=2)
        self.assertEqual(sj.open_avg_costs(trades), {})
        self.assertIsNone(sj.realized_pnl([]))

    def test_cooldown_on_loss_and_exit_reason(self):
        now = time.time()
        trades = [
            {"ticker": "NVTK", "side": "buy", "qty": 1, "price": 1000.0, "ts_epoch": now - 7200},
            {"ticker": "NVTK", "side": "sell", "qty": 1, "price": 990.0, "ts_epoch": now - 3600},
            {"ticker": "SBER", "side": "buy", "qty": 1, "price": 300.0, "ts_epoch": now - 7200},
            {"ticker": "SBER", "side": "sell", "qty": 1, "price": 330.0, "ts_epoch": now - 3600, "reason": "трейл"},
            {"ticker": "GAZP", "side": "buy", "qty": 1, "price": 100.0, "ts_epoch": now - 7200},
            {"ticker": "GAZP", "side": "sell", "qty": 1, "price": 120.0, "ts_epoch": now - 3600},
            {"ticker": "LKOH", "side": "sell", "qty": 1, "price": 7000.0, "ts_epoch": now - 90 * 3600},
        ]
        self.assertEqual(sj.cooldown_tickers(24, trades, now=now), {"NVTK", "SBER"})
        self.assertEqual(sj.recently_bought(24, trades, now=now), {"NVTK", "SBER", "GAZP"})

    def test_daily_report(self):
        day = datetime(2026, 10, 5, 23, 55, tzinfo=_MSK)
        epoch = day.replace(hour=12).timestamp()
        trades = [
            {"ts": "05.10 12:00", "ts_epoch": epoch, "ticker": "SBER", "name": "Сбер", "side": "buy",
             "lots": 1, "qty": 10, "price": 300.0, "commission": 1.5},
        ]
        text = sj.format_daily_pnl_report(trades, day=day)
        self.assertIn("Акции за 05.10", text)
        self.assertIn("сделок 1", text)
        self.assertIn("3 000 ₽", text)
        with tempfile.TemporaryDirectory() as tmp:
            meta = os.path.join(tmp, "daily.json")
            with patch.object(sj, "trades_on_msk_day", return_value=trades):
                self.assertTrue(sj.should_send_daily_report(now=day, path=meta))
                sj.mark_daily_report_sent(now=day, path=meta)
                self.assertFalse(sj.should_send_daily_report(now=day, path=meta))
                saturday = datetime(2026, 10, 10, 23, 55, tzinfo=_MSK)
                self.assertFalse(sj.should_send_daily_report(now=saturday, path=meta))


class TestStocksDeskRules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for name, attr in (
            ("holds.json", "_HOLD_PATH"),
            ("bought.json", "_BOUGHT_PATH"),
            ("trades.json", "_TRADE_PATH"),
            ("alloc.json", "_ALLOC_PATH"),
            ("trail.json", "_TRAIL_PATH"),
            ("daily.json", "_DAILY_PATH"),
        ):
            patcher = patch(f"skills.stocks.common.{attr}", os.path.join(self.tmp.name, name))
            patcher.start()
            self.addCleanup(patcher.stop)
        sleep = patch("skills.stocks.desk.time.sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        self.skill = StocksSkill()
        self.skill._token = "tok"

    def test_filter_buy_alloc_keeps_cash_floor(self):
        self.assertEqual(self.skill._filter_buy_alloc({"SBER": 90.0}, []), {"SBER": 90.0})
        self.assertEqual(
            self.skill._filter_buy_alloc({"TMOS": 45.0, "VTBR": 45.0}, []),
            {"VTBR": 90.0},
        )

    def test_first_run_treats_held_as_owner(self):
        self.skill._desk_bought_ready = False
        self.skill._desk_bought = set()
        self.skill._ensure_desk_bought({"GMKN", "SBER"})
        self.assertTrue(self.skill._is_owner_position("GMKN"))
        self.assertTrue(self.skill._is_owner_position("SBER"))

    def test_rebalance_does_not_rotate_fresh_buy(self):
        self.skill._desk_bought = {"EUTR"}
        self.skill._desk_bought_ready = True
        sj._write_trades([
            {"ticker": "EUTR", "side": "buy", "qty": 10, "lots": 10, "price": 100.0, "ts_epoch": time.time() - 3600},
        ])
        positions = ([{"ticker": "EUTR", "qty": 10.0, "price": 100.0}], 0.0, 0.0)
        with (
            patch.object(self.skill, "_positions", return_value=positions),
            patch.object(self.skill, "_broker_cash", return_value=0.0),
            patch.object(self.skill, "_quote", return_value=("ВТБ", 80.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(0, 10)),
            patch.object(self.skill, "_day_changes", return_value={}),
            patch.object(self.skill, "_place_order") as place,
        ):
            self.skill._rebalance_portfolio({"VTBR": 100.0})
        place.assert_not_called()

    def test_rebalance_band_skips_small_drift(self):
        self.skill._desk_bought = {"SBER"}
        self.skill._desk_bought_ready = True
        positions = ([{"ticker": "SBER", "qty": 170.0, "price": 300.0}], 0.0, 0.0)
        with (
            patch.object(self.skill, "_positions", return_value=positions),
            patch.object(self.skill, "_broker_cash", return_value=49_000.0),
            patch.object(self.skill, "_lot_size", return_value=10),
            patch.object(self.skill, "_max_lots", return_value=(100, 17)),
            patch.object(self.skill, "_day_changes", return_value={}),
            patch.object(self.skill, "_place_order") as place,
        ):
            result = self.skill._rebalance_portfolio({"SBER": 50.0})
        place.assert_not_called()
        self.assertIn("сбалансирован", result)

    def test_rebalance_skips_when_portfolio_unavailable(self):
        with (
            patch.object(self.skill, "_positions", side_effect=RuntimeError("http 500")),
            patch.object(self.skill, "_broker_cash", return_value=3400.0),
            patch.object(self.skill, "_place_order") as place,
        ):
            result = self.skill._rebalance_portfolio({"CNRU": 26.0, "PRMD": 16.0})
        place.assert_not_called()
        self.assertIn("недоступен", result)

    def test_rebalance_defers_buys_when_portfolio_lost_after_sells(self):
        self.skill._desk_bought = {"EUTR"}
        self.skill._desk_bought_ready = True
        positions = ([{"ticker": "EUTR", "qty": 10.0, "price": 100.0}], 0.0, 0.0)
        with (
            patch.object(self.skill, "_positions", side_effect=[positions, RuntimeError("timeout")]),
            patch.object(self.skill, "_broker_cash", return_value=0.0),
            patch.object(self.skill, "_quote", return_value=("ВТБ", 80.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(10, 10)),
            patch.object(self.skill, "_day_changes", return_value={}),
            patch.object(self.skill, "_place_order", return_value="Продал 10 лотов: EUTR.") as place,
        ):
            result = self.skill._rebalance_portfolio({"VTBR": 100.0})
        place.assert_called_once_with("EUTR", "ORDER_DIRECTION_SELL", 10)
        self.assertIn("Покупки отложил", result)

    def test_desk_choose_skips_cooldown(self):
        candidates = [
            {"ticker": "SBER", "price": 300.0, "lot": 1},
            {"ticker": "NVTK", "price": 1000.0, "lot": 1},
        ]
        signals = [
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-nvtk", "probability": 90},
            {"direction": "SIGNAL_DIRECTION_BUY", "instrumentUid": "u-sber", "probability": 10},
        ]
        insts = {
            "u-nvtk": {"ticker": "NVTK", "classCode": "TQBR", "instrumentKind": "INSTRUMENT_TYPE_SHARE", "lot": 1},
            "u-sber": {"ticker": "SBER", "classCode": "TQBR", "instrumentKind": "INSTRUMENT_TYPE_SHARE", "lot": 1},
        }
        with (
            patch.object(self.skill, "_fetch_buy_signals", return_value=signals),
            patch.object(self.skill, "_instrument", side_effect=lambda _f, _t, uid=None: insts.get(uid or "", {})),
        ):
            alloc = self.skill._desk_choose(candidates, equity=20_000, cooldown={"NVTK"})
        self.assertEqual(alloc, {"SBER": 100.0})

    def test_full_desk_risk_off_keeps_positions(self):
        with (
            patch.object(self.skill, "_desk_candidates", return_value=[{"ticker": "SBER", "price": 300.0}]),
            patch.object(self.skill, "_imoex_regime", return_value=(-6.0, -1.0)),
            patch.object(self.skill, "_rebalance_portfolio") as reb,
        ):
            out = self.skill._trade_auto_locked(silent=True)
        reb.assert_not_called()
        self.assertIn("Рынок слабый", out)
        state = read_alloc_state()
        self.assertTrue(state["risk_off"])
        self.assertEqual(state["alloc"], {})

    def test_full_desk_applies_cash_floor(self):
        with (
            patch.object(self.skill, "_desk_candidates", return_value=[{"ticker": "SBER", "price": 300.0}]),
            patch.object(self.skill, "_equity_estimate", return_value=100_000.0),
            patch.object(self.skill, "_imoex_regime", return_value=(0.5, 0.1)),
            patch.object(self.skill, "_desk_choose", return_value={"SBER": 100.0}),
            patch.object(self.skill, "_market_open", return_value=True),
            patch.object(self.skill, "_park_idle"),
            patch.object(self.skill, "_rebalance_portfolio", return_value="ок") as reb,
        ):
            self.skill._trade_auto_locked(silent=True)
        reb.assert_called_once()
        self.assertEqual(reb.call_args[0][0], {"SBER": 90.0})

    def test_exits_stop_sells_desk_position_and_skips_owner(self):
        self.skill._desk_bought = {"SBER"}
        self.skill._desk_bought_ready = True
        self.skill._manual_holds = {"GMKN"}
        positions = ([
            {"ticker": "SBER", "qty": 10.0, "price": 260.0, "avg": 300.0},
            {"ticker": "GMKN", "qty": 10.0, "price": 50.0, "avg": 150.0},
        ], 0.0, 0.0)
        with (
            patch.object(self.skill, "_safe_positions", return_value=positions),
            patch.object(self.skill, "_broker_cash", return_value=0.0),
            patch.object(self.skill, "_max_lots", return_value=(0, 1)),
            patch.object(self.skill, "_place_order", return_value="Продал 1 лот: Сбер.") as place,
        ):
            parts, sold = self.skill._desk_exits()
        place.assert_called_once_with("SBER", "ORDER_DIRECTION_SELL", 1, reason="стоп")
        self.assertEqual(sold, {"SBER"})
        self.assertEqual(parts, ["Продал 1 лот: Сбер."])

    def test_exits_trail_keeps_high_between_runs(self):
        self.skill._desk_bought = {"SBER"}
        self.skill._desk_bought_ready = True
        common_patches = (
            patch.object(self.skill, "_broker_cash", return_value=0.0),
            patch.object(self.skill, "_max_lots", return_value=(0, 10)),
        )
        for p in common_patches:
            p.start()
            self.addCleanup(p.stop)
        up = ([{"ticker": "SBER", "qty": 100.0, "price": 330.0, "avg": 300.0}], 0.0, 0.0)
        with (
            patch.object(self.skill, "_safe_positions", return_value=up),
            patch.object(self.skill, "_place_order") as place,
        ):
            self.skill._desk_exits()
        place.assert_not_called()
        self.assertTrue(read_trail_state()["SBER"]["armed"])
        down = ([{"ticker": "SBER", "qty": 100.0, "price": 315.0, "avg": 300.0}], 0.0, 0.0)
        with (
            patch.object(self.skill, "_safe_positions", return_value=down),
            patch.object(self.skill, "_place_order", return_value="Продал") as place,
        ):
            _parts, sold = self.skill._desk_exits()
        place.assert_called_once_with("SBER", "ORDER_DIRECTION_SELL", 10, reason="трейл")
        self.assertEqual(sold, {"SBER"})
        self.assertNotIn("SBER", read_trail_state())

    def test_watch_drops_exited_from_target_and_respects_rate(self):
        now = time.time()
        write_alloc_state({"SBER": 50.0, "NVTK": 40.0}, why="цель", watch_trades=[now - 100, now - 50])
        with (
            patch.object(self.skill, "_market_open", return_value=True),
            patch.object(self.skill, "_desk_exits", return_value=(["Продал SBER."], {"SBER"})),
            patch.object(self.skill, "_imoex_regime", return_value=(None, None)),
            patch.object(self.skill, "_rebalance_portfolio") as reb,
        ):
            out = self.skill._desk_watch_locked(silent=True)
        reb.assert_not_called()
        self.assertIn("Продал SBER.", out)
        self.assertEqual(read_alloc_state()["alloc"], {"NVTK": 40.0})

    def test_park_release_and_idle(self):
        positions = ([{"ticker": "LQDT", "qty": 10_000.0, "price": 2.0}], 0.0, 0.0)
        with (
            patch.dict(os.environ, {"STOCKS_PARK": "true", "STOCKS_PARK_TICKER": "LQDT"}),
            patch.object(self.skill, "_safe_positions", return_value=positions),
            patch.object(self.skill, "_broker_cash", return_value=1000.0),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(0, 10_000)),
            patch.object(self.skill, "_place_order", return_value="ok") as place,
        ):
            self.assertTrue(self.skill._park_release(4600.0))
        place.assert_called_once_with("LQDT", "ORDER_DIRECTION_SELL", 1800, reason="паркинг", notify=False)

        with (
            patch.dict(os.environ, {"STOCKS_PARK": "true"}),
            patch.object(self.skill, "_safe_positions", return_value=([], 0.0, 0.0)),
            patch.object(self.skill, "_broker_cash", return_value=20_000.0),
            patch.object(self.skill, "_quote", return_value=("LQDT", 2.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(100_000, 0)),
            patch.object(self.skill, "_place_order", return_value="ok") as place,
        ):
            self.skill._park_idle()
        place.assert_called_once_with("LQDT", "ORDER_DIRECTION_BUY", 9800, reason="паркинг", notify=False)

    def test_park_off_by_default(self):
        with (
            patch.dict(os.environ, {"STOCKS_PARK": ""}),
            patch.object(self.skill, "_place_order") as place,
        ):
            self.skill._park_idle()
            self.assertFalse(self.skill._park_release(10_000))
        place.assert_not_called()

    def test_park_fund_is_not_rebalanced(self):
        self.skill._desk_bought = {"LQDT"}
        self.skill._desk_bought_ready = True
        positions = ([{"ticker": "LQDT", "qty": 1000.0, "price": 1.8}], 0.0, 0.0)
        with (
            patch.dict(os.environ, {"STOCKS_PARK": ""}),
            patch.object(self.skill, "_positions", return_value=positions),
            patch.object(self.skill, "_broker_cash", return_value=0.0),
            patch.object(self.skill, "_quote", return_value=("ВТБ", 80.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=1),
            patch.object(self.skill, "_max_lots", return_value=(0, 1000)),
            patch.object(self.skill, "_day_changes", return_value={}),
            patch.object(self.skill, "_place_order") as place,
        ):
            self.skill._rebalance_portfolio({"VTBR": 100.0})
        for call in place.call_args_list:
            self.assertNotEqual(call.args[0], "LQDT")

    def test_place_order_journals_fill(self):
        data = {
            "executionReportStatus": "EXECUTION_REPORT_STATUS_FILL",
            "lotsExecuted": "2",
            "executedOrderPrice": {"units": "300", "nano": 0},
            "executedCommission": {"units": "1", "nano": 200000000},
        }
        with (
            patch.object(self.skill, "_instrument_ids", return_value=("uid", "figi", "Сбер")),
            patch.object(self.skill, "_pick_account_id", return_value="acc"),
            patch.object(self.skill, "_post", return_value=data),
            patch.object(self.skill, "_quote", return_value=("Сбер", 301.0, 0.0)),
            patch.object(self.skill, "_lot_size", return_value=10),
            patch("skills.stocks.common.telegram_configured", return_value=False),
        ):
            self.skill._place_order("SBER", "ORDER_DIRECTION_BUY", 2, reason="")
        entry = sj._read_trades()[-1]
        self.assertEqual(entry["qty"], 20)
        self.assertEqual(entry["price"], 300.0)
        self.assertEqual(entry["commission"], 1.2)
        self.assertIn("ts_epoch", entry)
        self.assertIn("SBER", self.skill._desk_bought)


if __name__ == "__main__":
    unittest.main()
