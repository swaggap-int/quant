"""The broker is where backtests lie to you, so every rule is pinned down here."""

from __future__ import annotations

import unittest

from quant.broker import Broker, Order
from quant.config import Costs, RiskConfig

TS = 1_600_000_000_000
DAY = 86_400_000


def make_broker(
    cash: float = 10_000.0,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    risk: float = 0.01,
    allow_short: bool = False,
    max_position: float = 1.0,
) -> Broker:
    return Broker(
        initial_cash=cash,
        costs=Costs(fee_bps=fee_bps, slippage_bps=slippage_bps),
        risk=RiskConfig(risk_per_trade=risk, max_position_pct=max_position, allow_short=allow_short),
        bar_ms=DAY,
    )


class TestCostsAndFills(unittest.TestCase):
    def test_buy_pays_slippage_and_fee(self) -> None:
        b = make_broker(fee_bps=10.0, slippage_bps=5.0)
        b.submit(Order("buy", qty=10, reason="test"))
        b.process_open(TS, 100.0, 101.0, 99.0)
        fill = b.fills[0]
        self.assertAlmostEqual(fill["price"], 100.0 * 1.0005)  # slip hurts the buyer
        self.assertAlmostEqual(fill["fee"], fill["price"] * 10 * 0.001)
        self.assertAlmostEqual(b.cash, 10_000.0 - fill["price"] * 10 - fill["fee"])

    def test_sell_receives_less_than_mid(self) -> None:
        b = make_broker(slippage_bps=10.0)
        b.submit(Order("buy", qty=1))
        b.process_open(TS, 100.0, 101.0, 99.0)
        b.submit(Order("sell", reason="exit"))
        b.process_open(TS + DAY, 100.0, 101.0, 99.0)
        self.assertAlmostEqual(b.fills[1]["price"], 99.9)

    def test_zero_cost_round_trip_is_exactly_flat(self) -> None:
        b = make_broker()
        b.submit(Order("buy", qty=3))
        b.process_open(TS, 50.0, 51.0, 49.0)
        b.submit(Order("sell"))
        b.process_open(TS + DAY, 50.0, 51.0, 49.0)
        self.assertAlmostEqual(b.cash, 10_000.0)
        self.assertAlmostEqual(b.trades[0]["pnl"], 0.0)

    def test_cash_never_goes_negative_when_fully_invested(self) -> None:
        b = make_broker(fee_bps=10.0, slippage_bps=5.0)
        b.submit(Order("buy", weight=1.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertGreaterEqual(b.cash, 0.0)
        self.assertLessEqual(b.position_value(100.0), 10_000.0)

    def test_dust_orders_are_rejected_not_filled(self) -> None:
        b = make_broker(cash=100.0)
        b.submit(Order("buy", weight=0.05))  # $5, below the $10 minimum
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertEqual(b.fills, [])
        self.assertIn("order below minimum notional", b.rejections)


class TestSizing(unittest.TestCase):
    def test_risk_sizing_solves_for_the_stop_distance(self) -> None:
        b = make_broker(cash=10_000.0, risk=0.01)
        b.submit(Order("buy", risk_pct=0.01, stop=90.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertAlmostEqual(b.position.qty, 10.0)  # $100 risk / $10 per unit

    def test_stop_out_loses_exactly_the_risked_amount_without_costs(self) -> None:
        b = make_broker(cash=10_000.0, risk=0.01)
        b.submit(Order("buy", risk_pct=0.01, stop=90.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        b.check_stop(TS + DAY, 95.0, 96.0, 89.0)  # trades through the stop
        self.assertEqual(len(b.trades), 1)
        self.assertAlmostEqual(b.trades[0]["pnl"], -100.0)
        self.assertAlmostEqual(b.trades[0]["r_multiple"], -1.0)

    def test_gap_through_the_stop_fills_at_the_open_and_loses_more(self) -> None:
        b = make_broker(cash=10_000.0, risk=0.01)
        b.submit(Order("buy", risk_pct=0.01, stop=90.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        b.check_stop(TS + DAY, 85.0, 86.0, 84.0)  # opens below the stop
        self.assertAlmostEqual(b.fills[-1]["price"], 85.0)
        self.assertAlmostEqual(b.trades[0]["pnl"], -150.0)
        self.assertLess(b.trades[0]["r_multiple"], -1.0)

    def test_position_cap_limits_exposure(self) -> None:
        b = make_broker(cash=10_000.0, max_position=0.25)
        b.submit(Order("buy", weight=1.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertAlmostEqual(b.position_value(100.0), 2_500.0)

    def test_wider_stop_means_smaller_position(self) -> None:
        tight = make_broker(cash=10_000.0, risk=0.01)
        tight.submit(Order("buy", risk_pct=0.01, stop=95.0))
        tight.process_open(TS, 100.0, 101.0, 99.0)

        wide = make_broker(cash=10_000.0, risk=0.01)
        wide.submit(Order("buy", risk_pct=0.01, stop=80.0))
        wide.process_open(TS, 100.0, 101.0, 99.0)

        self.assertGreater(tight.position.qty, wide.position.qty)
        self.assertAlmostEqual(tight.position.qty * 5.0, 100.0)  # both risk $100
        self.assertAlmostEqual(wide.position.qty * 20.0, 100.0)

    def test_stop_above_entry_for_a_long_is_rejected(self) -> None:
        b = make_broker()
        b.submit(Order("buy", risk_pct=0.01, stop=110.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertEqual(b.fills, [])
        self.assertTrue(any("invalid stop" in k for k in b.rejections))


class TestPositionRules(unittest.TestCase):
    def test_pyramiding_is_rejected(self) -> None:
        b = make_broker()
        b.submit(Order("buy", qty=1))
        b.process_open(TS, 100.0, 101.0, 99.0)
        b.submit(Order("buy", qty=1))
        b.process_open(TS + DAY, 100.0, 101.0, 99.0)
        self.assertEqual(len(b.fills), 1)
        self.assertIn("already in a position (no pyramiding)", b.rejections)

    def test_sell_while_flat_is_ignored_on_spot(self) -> None:
        b = make_broker()
        b.submit(Order("sell"))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertEqual(b.fills, [])
        self.assertIn("sell signal while flat (shorting disabled)", b.rejections)

    def test_shorts_work_when_enabled(self) -> None:
        b = make_broker(allow_short=True, risk=0.01)
        b.submit(Order("sell", risk_pct=0.01, stop=110.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertAlmostEqual(b.position.qty, -10.0)
        b.check_stop(TS + DAY, 105.0, 111.0, 104.0)
        self.assertAlmostEqual(b.trades[0]["pnl"], -100.0)

    def test_resting_stop_survives_until_triggered(self) -> None:
        b = make_broker()
        b.submit(Order("buy", qty=1, stop=90.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        self.assertAlmostEqual(b.stop_price, 90.0)
        b.check_stop(TS + DAY, 100.0, 101.0, 95.0)
        self.assertEqual(b.trades, [])  # 95 never reached 90
        b.check_stop(TS + 2 * DAY, 100.0, 101.0, 89.0)
        self.assertEqual(len(b.trades), 1)

    def test_stop_is_cancelled_after_exit(self) -> None:
        b = make_broker()
        b.submit(Order("buy", qty=1, stop=90.0))
        b.process_open(TS, 100.0, 101.0, 99.0)
        b.submit(Order("sell", reason="signal exit"))
        b.process_open(TS + DAY, 100.0, 101.0, 99.0)
        self.assertIsNone(b.stop_price)
        self.assertEqual(b.fills[-1]["reason"], "signal exit")

    def test_excursion_tracks_mfe_and_mae(self) -> None:
        b = make_broker()
        b.submit(Order("buy", qty=1))
        b.process_open(TS, 100.0, 100.0, 100.0)
        b.update_excursion(120.0, 95.0)
        b.update_excursion(110.0, 90.0)
        b.submit(Order("sell"))
        b.process_open(TS + DAY, 100.0, 100.0, 100.0)
        trade = b.trades[0]
        self.assertAlmostEqual(trade["mfe_pct"], 20.0)  # high never lower than 120
        self.assertAlmostEqual(trade["mae_pct"], -10.0)


if __name__ == "__main__":
    unittest.main()
