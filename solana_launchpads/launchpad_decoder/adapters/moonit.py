"""Moonit (formerly Moonshot / DEX Screener).

Program: MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG

Moonit is the one launchpad in this set that sizes its curve from a *market cap
threshold* rather than a quote-token target: `CurveAccount.marketcap_threshold`
is the trigger, and how much SOL that takes has to be solved for.

The reserve constants per curve type live in the program binary rather than in
an account, so they are reproduced here from `@heliofi/launchpad-common` and
tagged BUNDLED_SNAPSHOT.  Everything else -- supply, threshold, coefficient b,
curve type -- is read from the curve account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext


@dataclass(frozen=True)
class MoonitCurveConstants:
    """Compiled-in curve parameters, from `@heliofi/launchpad-common`."""

    family: CurveFamily
    dynamic_threshold_pct: float
    initial_virtual_token_reserves: int = 0
    initial_virtual_collateral_reserves: int = 0
    collateral_decimals: int = 9


CURVE_CONSTANTS: Dict[str, MoonitCurveConstants] = {
    "ConstantProductV1": MoonitCurveConstants(
        CurveFamily.CONSTANT_PRODUCT_VIRTUAL, 80.0, 1_073_000_000_000_000_000, 30_000_000_000
    ),
    "ConstantProductV2": MoonitCurveConstants(
        CurveFamily.CONSTANT_PRODUCT_VIRTUAL, 80.0, 1_060_000_000_000_000_000, 14_000_000_000
    ),
    "LinearV1": MoonitCurveConstants(CurveFamily.LINEAR, 55.0),
    "FlatCurveV1": MoonitCurveConstants(CurveFamily.FIXED_PRICE, 49.0),
    "FlatCurveV1AntiSnipe": MoonitCurveConstants(CurveFamily.FIXED_PRICE, 49.0),
}

CURRENCY_DECIMALS = {"Sol": 9}


def _variant(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("variant", "")
    return ""


class MoonitAdapter(Adapter):
    key = "moonit"

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        m.base_mint = state.get("mint")
        m.base_decimals = state.get("decimals")

        collateral = _variant(state.get("collateral_currency")) or "Sol"
        m.quote_mint = ctx.spec.default_quote_mint
        m.quote_decimals = CURRENCY_DECIMALS.get(collateral, 9)

        curve_type = _variant(state.get("curve_type")) or "ConstantProductV1"
        m.curve_type = curve_type
        constants = CURVE_CONSTANTS.get(curve_type)
        if constants is None:
            m.warn(f"unknown Moonit curve_type {curve_type!r}; prices not computed")
            return self.finish(m)
        m.curve_family = constants.family

        supply_raw = state.get("total_supply")
        curve_amount = state.get("curve_amount")
        position = None
        if supply_raw is not None and curve_amount is not None:
            position = max(0, supply_raw - curve_amount)

        m.total_supply = self.param(
            ui_amount(supply_raw, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        m.tokens_sold = self.param(
            ui_amount(position, m.base_decimals),
            ValueSource.DERIVED,
            ctx.slot,
            "total_supply - curve_amount",
        )

        mc_currency = _variant(state.get("marketcap_currency")) or "Sol"
        mc_decimals = CURRENCY_DECIMALS.get(mc_currency, 9)
        threshold_raw = state.get("marketcap_threshold")

        if constants.family is CurveFamily.CONSTANT_PRODUCT_VIRTUAL:
            self._constant_product(m, ctx, state, constants, position, supply_raw, threshold_raw)
        elif constants.family is CurveFamily.LINEAR:
            self._linear(m, state, constants, position, supply_raw, threshold_raw, mc_decimals)
        else:
            self._flat(m, constants, supply_raw, threshold_raw, mc_decimals)

        m.complete = self.param(
            None if curve_amount is None else curve_amount == 0,
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
        )
        m.raw_state = {
            **state,
            "_marketcap_threshold_ui": ui_amount(threshold_raw, mc_decimals),
            "_marketcap_currency": mc_currency,
            "_note": (
                "Moonit's own 'market cap' is price x tokens sold; the "
                "*_mcap_quote fields here are price x total supply"
            ),
        }
        return self.finish(m)

    # -- curve families -------------------------------------------------
    def _constant_product(
        self,
        m: LaunchMetrics,
        ctx: DecodeContext,
        state: Dict[str, Any],
        constants: MoonitCurveConstants,
        position: Optional[int],
        supply_raw: Optional[int],
        threshold_raw: Optional[int],
    ) -> None:
        ivt = constants.initial_virtual_token_reserves
        ivc = constants.initial_virtual_collateral_reserves
        k = ivt * ivc

        m.launch_price_quote = self.param(
            price_raw_to_ui(curves.cp_price_raw(ivc, ivt), m.base_decimals, m.quote_decimals),
            ValueSource.BUNDLED_SNAPSHOT,
            0,
            "initialVirtualCollateralReserves / initialVirtualTokenReserves",
        )

        if position is not None and position < ivt:
            vt = ivt - position
            vc = k // vt
            m.current_price_quote = self.param(
                price_raw_to_ui(curves.cp_price_raw(vc, vt), m.base_decimals, m.quote_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "k / (ivt - sold) over (ivt - sold)",
            )
            m.raised_quote = self.param(
                ui_amount(max(0, vc - ivc), m.quote_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "virtual collateral above the seeded reserve",
            )

        if threshold_raw and supply_raw:
            graduation_position = curves.solve_position_for_marketcap(
                threshold_raw, ivt, ivc, 1, min(supply_raw, ivt - 1)
            )
            if graduation_position:
                vt = ivt - graduation_position
                vc = k // vt
                m.graduation_price_quote = self.param(
                    price_raw_to_ui(
                        curves.cp_price_raw(vc, vt), m.base_decimals, m.quote_decimals
                    ),
                    ValueSource.DERIVED,
                    0,
                    "price where price x tokens_sold reaches marketcap_threshold",
                )
                m.raise_target_quote = self.param(
                    ui_amount(max(0, vc - ivc), m.quote_decimals),
                    ValueSource.DERIVED,
                    0,
                    "collateral required to reach marketcap_threshold",
                )
                m.tokens_for_sale = self.param(
                    ui_amount(graduation_position, m.base_decimals), ValueSource.DERIVED, 0
                )
            else:
                m.warn("marketcap_threshold is unreachable on this curve")

    def _linear(
        self,
        m: LaunchMetrics,
        state: Dict[str, Any],
        constants: MoonitCurveConstants,
        position: Optional[int],
        supply_raw: Optional[int],
        threshold_raw: Optional[int],
        mc_decimals: int,
    ) -> None:
        coef_b_raw = state.get("coef_b")
        if coef_b_raw is None or supply_raw is None or not threshold_raw:
            m.warn("LinearV1 curve needs coef_b, total_supply and marketcap_threshold")
            return
        coef_b = coef_b_raw / (10**m.quote_decimals)
        coef_a = curves.moonit_linear_coef_a(
            coef_b,
            supply_raw,
            m.base_decimals or 9,
            threshold_raw,
            mc_decimals,
            constants.dynamic_threshold_pct,
        )
        if coef_a is None:
            m.warn("could not solve the LinearV1 slope")
            return

        supply_ui = supply_raw / (10 ** (m.base_decimals or 9))
        sale_ui = supply_ui * constants.dynamic_threshold_pct / 100.0

        m.launch_price_quote = self.param(
            curves.moonit_linear_price_ui(coef_a, coef_b, 0.0),
            ValueSource.DERIVED,
            0,
            "coef_b (price at zero tokens sold)",
        )
        if position is not None:
            position_ui = position / (10 ** (m.base_decimals or 9))
            m.current_price_quote = self.param(
                curves.moonit_linear_price_ui(coef_a, coef_b, position_ui),
                ValueSource.DERIVED,
                m.slot,
                "coef_a * sold + coef_b",
            )
            m.raised_quote = self.param(
                curves.moonit_linear_cost_ui(coef_a, coef_b, 0.0, position_ui),
                ValueSource.DERIVED,
                m.slot,
                "integral of the linear price up to tokens sold",
            )
        m.graduation_price_quote = self.param(
            curves.moonit_linear_price_ui(coef_a, coef_b, sale_ui), ValueSource.DERIVED, 0
        )
        m.raise_target_quote = self.param(
            curves.moonit_linear_cost_ui(coef_a, coef_b, 0.0, sale_ui),
            ValueSource.DERIVED,
            0,
            "integral of the linear price over the sellable supply",
        )
        m.tokens_for_sale = self.param(sale_ui, ValueSource.DERIVED, 0)

    def _flat(
        self,
        m: LaunchMetrics,
        constants: MoonitCurveConstants,
        supply_raw: Optional[int],
        threshold_raw: Optional[int],
        mc_decimals: int,
    ) -> None:
        if not supply_raw:
            return
        supply_ui = supply_raw / (10 ** (m.base_decimals or 9))
        sale_ui = supply_ui * constants.dynamic_threshold_pct / 100.0
        m.tokens_for_sale = self.param(sale_ui, ValueSource.BUNDLED_SNAPSHOT, 0)
        if threshold_raw:
            threshold_ui = threshold_raw / (10**mc_decimals)
            m.graduation_price_quote = self.param(
                threshold_ui / sale_ui if sale_ui else None,
                ValueSource.DERIVED,
                0,
                "marketcap_threshold spread over the sellable supply",
            )
            m.raise_target_quote = self.param(
                threshold_ui, ValueSource.ONCHAIN_STATE, 0, "marketcap_threshold"
            )
        m.warn(
            "flat curves price off collateral collected, which lives in the "
            "curve's token vault rather than the curve account -- current price "
            "needs that balance"
        )
