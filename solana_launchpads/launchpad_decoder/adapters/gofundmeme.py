"""GoFundMeme.

Program: GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7

GFM parameterises its curve by quote *raised* rather than tokens sold.  Tokens
issued between two raise levels are the integral of a power law:

    tokens(x1 -> x2) = scale * ∫ (x + c)^-e dx
    scale            = tradable_supply / ∫₀^target (x + c)^-e dx
    price(x)         = (x + c)^e / scale

with `c = curveConstant`, `e = curveExponent` and `target = targetRaise`, all
stored on the pool as f64/u64.  That makes the launch price, the graduation
price and the raise target all closed-form.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .. import curves
from ..types import CurveFamily, LaunchMetrics, ValueSource, ui_amount
from .base import Adapter, DecodeContext


class GoFundMemeAdapter(Adapter):
    key = "gofundmeme"
    curve_family = CurveFamily.POWER_LAW

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        m.base_mint = state.get("token_a_mint")
        m.quote_mint = state.get("token_b_mint") or ctx.spec.default_quote_mint
        m.creator = state.get("admin")
        m.curve_type = "power_law"

        m.base_decimals = ctx.decimals_for(m.base_mint) or 9
        m.quote_decimals = ctx.decimals_for(m.quote_mint) or 9

        total_supply_raw = state.get("total_supply")
        initial_tokens_raw = state.get("initial_tokens")
        target_raise_raw = state.get("target_raise")
        current_sol_raw = state.get("current_sol")
        if current_sol_raw is None:
            current_sol_raw = state.get("total_raised")
        constant = state.get("curve_constant")
        exponent = state.get("curve_exponent")

        m.total_supply = self.param(
            ui_amount(total_supply_raw, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        m.tokens_for_sale = self.param(
            ui_amount(initial_tokens_raw, m.base_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )
        token_balance = state.get("token_balance")
        if token_balance is not None and initial_tokens_raw is not None:
            m.tokens_sold = self.param(
                ui_amount(max(0, initial_tokens_raw - token_balance), m.base_decimals),
                ValueSource.DERIVED,
                ctx.slot,
                "initial_tokens - token_balance",
            )

        m.raise_target_quote = self.param(
            ui_amount(target_raise_raw, m.quote_decimals),
            ValueSource.ONCHAIN_STATE,
            ctx.slot,
            "target_raise",
        )
        m.raised_quote = self.param(
            ui_amount(current_sol_raw, m.quote_decimals), ValueSource.ONCHAIN_STATE, ctx.slot
        )

        if constant is None or exponent is None or not target_raise_raw or not initial_tokens_raw:
            m.warn(
                "curve_constant / curve_exponent / target_raise / initial_tokens "
                "are required to price a GoFundMeme curve"
            )
            return self.finish(m)

        tradable_ui = initial_tokens_raw / (10**m.base_decimals)
        target_ui = target_raise_raw / (10**m.quote_decimals)
        scale = curves.power_law_scale(tradable_ui, target_ui, float(constant), float(exponent))
        if not scale:
            m.warn("degenerate power-law curve: the issuance integral is not positive")
            return self.finish(m)

        m.launch_price_quote = self.param(
            curves.power_law_price_ui(0.0, float(constant), float(exponent), scale),
            ValueSource.DERIVED,
            ctx.slot,
            "c^e / scale",
        )
        m.graduation_price_quote = self.param(
            curves.power_law_price_ui(target_ui, float(constant), float(exponent), scale),
            ValueSource.DERIVED,
            ctx.slot,
            "(target + c)^e / scale",
        )
        if current_sol_raw is not None:
            raised_ui = current_sol_raw / (10**m.quote_decimals)
            m.current_price_quote = self.param(
                curves.power_law_price_ui(raised_ui, float(constant), float(exponent), scale),
                ValueSource.DERIVED,
                ctx.slot,
                "(raised + c)^e / scale",
            )

        status = state.get("pool_status")
        status_name = status.get("variant") if isinstance(status, dict) else status
        if isinstance(status_name, str):
            m.complete = self.param(
                status_name.lower() not in ("open", "active", "funding"),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                f"pool_status = {status_name}",
            )

        crank_bps = state.get("crank_reward_bps")
        if crank_bps is not None:
            m.fee_bps = self.param(
                float(crank_bps), ValueSource.ONCHAIN_STATE, ctx.slot, "crank_reward_bps"
            )
        m.raw_state = {**state, "_issuance_scale_tokens_per_area": scale}
        return self.finish(m)
