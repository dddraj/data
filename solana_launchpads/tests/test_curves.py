"""Curve mathematics, checked against values published by the programs' own SDKs."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_decoder import curves  # noqa: E402
from launchpad_decoder.curves import LiquiditySegment  # noqa: E402
from launchpad_decoder.types import price_raw_to_ui, ui_amount  # noqa: E402

# pump.fun mainnet `Global`, per pump-fun/pump-public-docs
PUMP_IVT = 1_073_000_000_000_000
PUMP_IVS = 30_000_000_000
PUMP_IRT = 793_100_000_000_000
PUMP_SUPPLY = 1_000_000_000_000_000


class TestConstantProduct:
    def test_pumpfun_launch_price_and_market_cap(self):
        price = price_raw_to_ui(curves.cp_price_raw(PUMP_IVS, PUMP_IVT), 6, 9)
        assert price == pytest.approx(2.7958993e-8, rel=1e-6)
        mcap = price * ui_amount(PUMP_SUPPLY, 6)
        # the ~28 SOL starting market cap every pump.fun coin opens at
        assert mcap == pytest.approx(27.959, rel=1e-4)

    def test_pumpfun_raise_target_is_the_famous_85_sol(self):
        target = curves.cp_raise_for_supply(PUMP_IVS, PUMP_IVT, PUMP_IRT)
        assert target == 85_005_359_057
        assert ui_amount(target, 9) == pytest.approx(85.0054, rel=1e-6)

    def test_pumpfun_graduation_price_and_market_cap(self):
        price = price_raw_to_ui(
            curves.cp_final_price_raw(PUMP_IVS, PUMP_IVT, PUMP_IRT), 6, 9
        )
        assert price == pytest.approx(4.1088017e-7, rel=1e-6)
        assert price * ui_amount(PUMP_SUPPLY, 6) == pytest.approx(410.88, rel=1e-4)

    def test_raise_target_actually_drains_the_curve(self):
        target = curves.cp_raise_for_supply(PUMP_IVS, PUMP_IVT, PUMP_IRT)
        out = curves.cp_base_out(PUMP_IVS, PUMP_IVT, target)
        assert out >= PUMP_IRT  # rounding is in the curve's favour, never short

    def test_quote_in_rounds_up_so_the_buyer_always_gets_enough(self):
        wanted = 10_000_000_000_000  # 10M tokens
        quote = curves.cp_quote_in(PUMP_IVS, PUMP_IVT, wanted)
        assert curves.cp_base_out(PUMP_IVS, PUMP_IVT, quote) >= wanted
        # ...and it is not over-charging by more than the rounding step
        assert curves.cp_base_out(PUMP_IVS, PUMP_IVT, quote - 1) < wanted

    def test_price_rises_monotonically_along_the_curve(self):
        k = PUMP_IVT * PUMP_IVS
        previous = 0.0
        for sold in range(0, PUMP_IRT, PUMP_IRT // 20):
            base = PUMP_IVT - sold
            price = curves.cp_price_raw(k // base, base)
            assert price > previous
            previous = price

    def test_buying_the_whole_virtual_reserve_is_rejected(self):
        with pytest.raises(ValueError):
            curves.cp_quote_in(PUMP_IVS, PUMP_IVT, PUMP_IVT)
        assert curves.cp_raise_for_supply(PUMP_IVS, PUMP_IVT, 0) is None


class TestMoonitMarketCapSolver:
    """`marketCapToMinimalTokens` from `@heliofi/launchpad-common`.

    Those SDK constants are computed in floating-point BigNumber; the integer
    solver here is exact, so two of the four vectors match bit for bit and the
    rest agree to ~1e-13 relative.
    """

    V1 = (1_073_000_000_000_000_000, 30_000_000_000)
    V2 = (1_060_000_000_000_000_000, 14_000_000_000)

    @pytest.mark.parametrize(
        "constants,threshold,expected",
        [
            (V1, 345_000_000_000, 799_820_983_207_404_442),
            (V2, 10_000_000_000, 344_740_929_008_610_006),
            (V2, 200_000_000_000, 814_207_069_725_281_919),
            (V2, 20_000_000_000, 469_667_071_199_732_347),
        ],
    )
    def test_solved_position_matches_sdk(self, constants, threshold, expected):
        ivt, ivc = constants
        position = curves.solve_position_for_marketcap(threshold, ivt, ivc, 1, ivt - 1)
        assert position == pytest.approx(expected, rel=1e-12)

    def test_exact_vectors_are_exact(self):
        ivt, ivc = self.V1
        assert (
            curves.solve_position_for_marketcap(345_000_000_000, ivt, ivc, 1, ivt - 1)
            == 799_820_983_207_404_442
        )

    def test_threshold_beyond_the_sellable_supply_returns_none(self):
        # A curve that may only sell 1e15 of its 1.073e18 virtual base can
        # never reach a 345 SOL market cap.
        ivt, ivc = self.V1
        assert (
            curves.solve_position_for_marketcap(345_000_000_000, ivt, ivc, 1, 10**15)
            is None
        )


class TestLinearAndFixed:
    def test_launchlab_linear_price_is_slope_times_sold(self):
        slope_q64 = 3 * curves.Q64
        assert curves.launchlab_linear_price_raw(slope_q64, 0) == 0
        assert curves.launchlab_linear_price_raw(slope_q64, 10) == pytest.approx(30.0)

    def test_moonit_linear_cost_is_the_integral_of_its_price(self):
        a, b = 2.0, 1.0
        # ∫₀^n (a x + b) dx = a n²/2 + b n
        assert curves.moonit_linear_cost_ui(a, b, 0.0, 3.0) == pytest.approx(2 * 9 / 2 + 3)
        assert curves.moonit_linear_price_ui(a, b, 3.0) == pytest.approx(7.0)

    def test_moonit_slope_reproduces_the_target_market_cap(self):
        supply, decimals, threshold, dt = 10**18, 9, 345_000_000_000, 55.0
        coef_a = curves.moonit_linear_coef_a(0.0, supply, decimals, threshold, 9, dt)
        sale = (supply / 10**decimals) * dt / 100
        price_at_graduation = curves.moonit_linear_price_ui(coef_a, 0.0, sale)
        assert price_at_graduation * sale == pytest.approx(threshold / 1e9, rel=1e-9)

    def test_fixed_price_does_not_move(self):
        assert curves.fixed_price_raw(30, 1000) == pytest.approx(0.03)


class TestMeteoraDbc:
    Q64 = curves.Q64

    def test_sqrt_price_squares_to_the_price(self):
        sqrt_price = curves.raw_price_to_sqrt_price(0.25)
        assert curves.sqrt_price_to_raw_price(sqrt_price) == pytest.approx(0.25, rel=1e-9)

    def test_delta_quote_matches_the_program_formula(self):
        lower, upper, liquidity = self.Q64, 2 * self.Q64, 5 * self.Q64
        # Δquote = L (√Pu - √Pl) / 2^128
        expected = (liquidity * (upper - lower)) // (1 << 128)
        assert curves.dbc_delta_quote(lower, upper, liquidity, round_up=False) == expected

    def test_delta_base_matches_the_program_formula(self):
        lower, upper, liquidity = self.Q64, 2 * self.Q64, 5 * self.Q64
        expected = (liquidity * (upper - lower)) // (lower * upper)
        assert curves.dbc_delta_base(lower, upper, liquidity, round_up=False) == expected

    def test_migration_price_round_trips_through_quote_for_range(self):
        start = self.Q64 // 1000
        curve = [
            LiquiditySegment(self.Q64 // 500, 40 * self.Q64 * self.Q64),
            LiquiditySegment(self.Q64 // 100, 40 * self.Q64 * self.Q64),
        ]
        threshold = curves.dbc_quote_for_range(start, curve[0].sqrt_price, curve)
        assert threshold > 0
        reached = curves.dbc_migration_sqrt_price(threshold, start, curve)
        assert reached == pytest.approx(curve[0].sqrt_price, rel=1e-6)

    def test_base_for_swap_accumulates_across_segments(self):
        start = self.Q64 // 1000
        curve = [
            LiquiditySegment(self.Q64 // 500, 40 * self.Q64 * self.Q64),
            LiquiditySegment(self.Q64 // 100, 40 * self.Q64 * self.Q64),
        ]
        one_segment = curves.dbc_base_for_swap(start, curve[0].sqrt_price, curve)
        two_segments = curves.dbc_base_for_swap(start, curve[1].sqrt_price, curve)
        assert 0 < one_segment < two_segments

    def test_active_segments_trims_the_padded_array(self):
        padded = [LiquiditySegment(5, 5), LiquiditySegment(0, 0), LiquiditySegment(9, 9)]
        assert curves.active_segments(padded) == [LiquiditySegment(5, 5)]

    def test_unpack_segments_reads_decoded_dicts(self):
        segments = curves.unpack_segments([{"sqrt_price": 3, "liquidity": 4}])
        assert segments == (LiquiditySegment(3, 4),)


class TestPowerLaw:
    def test_scale_makes_the_curve_issue_exactly_the_tradable_supply(self):
        supply, target, c, e = 800_000_000.0, 85.0, 30.0, 1.0
        scale = curves.power_law_scale(supply, target, c, e)
        issued = scale * curves.power_law_area(0.0, target, c, e)
        assert issued == pytest.approx(supply, rel=1e-12)

    def test_price_rises_with_the_amount_raised(self):
        supply, target, c, e = 800_000_000.0, 85.0, 30.0, 1.3
        scale = curves.power_law_scale(supply, target, c, e)
        launch = curves.power_law_price_ui(0.0, c, e, scale)
        graduation = curves.power_law_price_ui(target, c, e, scale)
        assert 0 < launch < graduation

    def test_exponent_one_uses_the_logarithmic_branch(self):
        import math

        assert curves.power_law_area(0.0, 10.0, 1.0, 1.0) == pytest.approx(math.log(11.0))


def test_progress_ratio_is_clamped():
    assert curves.progress_ratio(50, 100) == 0.5
    assert curves.progress_ratio(500, 100) == 1.0
    assert curves.progress_ratio(-5, 100) == 0.0
    assert curves.progress_ratio(5, 0) is None
    assert curves.progress_ratio(None, 100) is None
