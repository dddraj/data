"""Curve mathematics, expressed in raw on-chain units.

Everything here works on integers the way the programs do, so the numbers line
up with what a simulated buy would actually return.  Conversion to whole tokens
happens once, at the edge, in `types.price_raw_to_ui` / `types.ui_amount`.

Five families cover every Solana launchpad surveyed:

1. constant product over virtual reserves      -- pump.fun, Raydium LaunchLab
                                                  (Constant), Moonit CP v1/v2,
                                                  Boop, Vertigo (`shift`)
2. linear price in tokens sold                 -- LaunchLab (Linear), Moonit LinearV1
3. fixed price                                 -- LaunchLab (Fixed), Moonit FlatCurveV1
4. piecewise sqrt-price liquidity segments     -- Meteora DBC
5. power law over quote raised                 -- GoFundMeme
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

Q64 = 1 << 64
Q128 = 1 << 128


def _ceil_div(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise ZeroDivisionError("denominator is zero")
    return -(-numerator // denominator)


# --------------------------------------------------------------------------
# 1. constant product over virtual reserves
# --------------------------------------------------------------------------


def cp_price_raw(virtual_quote: int, virtual_base: int) -> Optional[float]:
    """Spot price in quote-raw per base-raw: ``vQuote / vBase``."""
    if not virtual_base:
        return None
    return virtual_quote / virtual_base


def cp_base_out(virtual_quote: int, virtual_base: int, quote_in: int) -> int:
    """Tokens received for `quote_in`, the standard x*y=k, rounded down."""
    if quote_in <= 0:
        return 0
    return (quote_in * virtual_base) // (virtual_quote + quote_in)


def cp_quote_in(virtual_quote: int, virtual_base: int, base_out: int) -> int:
    """Quote needed to buy exactly `base_out` tokens, rounded up."""
    if base_out <= 0:
        return 0
    if base_out >= virtual_base:
        raise ValueError("cannot buy the entire virtual base reserve")
    return _ceil_div(virtual_quote * base_out, virtual_base - base_out)


def cp_raise_for_supply(
    initial_virtual_quote: int, initial_virtual_base: int, base_for_sale: int
) -> Optional[int]:
    """Gross quote (pre-fee) needed to drain `base_for_sale` off the curve.

    This is the bonding-curve raise target for every pump.fun-style launchpad:
    ``k / (vBase - forSale) - vQuote``.
    """
    if base_for_sale <= 0 or base_for_sale >= initial_virtual_base:
        return None
    return cp_quote_in(initial_virtual_quote, initial_virtual_base, base_for_sale)


def cp_final_price_raw(
    initial_virtual_quote: int, initial_virtual_base: int, base_for_sale: int
) -> Optional[float]:
    """Spot price once the curve is exhausted."""
    remaining = initial_virtual_base - base_for_sale
    if remaining <= 0:
        return None
    raised = cp_raise_for_supply(initial_virtual_quote, initial_virtual_base, base_for_sale)
    if raised is None:
        return None
    return (initial_virtual_quote + raised) / remaining


# --------------------------------------------------------------------------
# 2. linear price
# --------------------------------------------------------------------------


def launchlab_linear_price_raw(slope_q64: int, base_sold: int) -> Optional[float]:
    """Raydium LaunchLab linear curve: ``price = a * sold / 2^64``.

    `virtual_base` stores the slope `a` in Q64 and `real_base` is tokens sold.
    """
    if slope_q64 <= 0:
        return None
    return (slope_q64 * base_sold) / Q64


def moonit_linear_price_ui(coef_a: float, coef_b: float, curve_position_ui: float) -> float:
    """Moonit LinearV1: ``price(x) = a*x + b`` in whole collateral per whole token."""
    return coef_a * curve_position_ui + coef_b


def moonit_linear_coef_a(
    coef_b_ui: float,
    total_supply_raw: int,
    token_decimals: int,
    marketcap_threshold_raw: int,
    marketcap_decimals: int,
    dynamic_threshold_pct: float,
) -> Optional[float]:
    """Solve the LinearV1 slope from the configured market-cap threshold.

    Mirrors `LinearCurveV1.getCoefA` in `@heliofi/launchpad-common`: the slope
    is whatever makes ``price(D) * D == marketcapThreshold`` at the dynamic
    threshold ``D`` (the share of supply sold before graduation).
    """
    d = (total_supply_raw / (10**token_decimals)) * (dynamic_threshold_pct / 100.0)
    if d <= 0:
        return None
    mc = marketcap_threshold_raw / (10**marketcap_decimals)
    return (mc / d - coef_b_ui) / d


def moonit_linear_cost_ui(
    coef_a: float, coef_b: float, position_ui: float, amount_ui: float
) -> float:
    """Integral of the linear price from `position` to `position + amount`."""
    return 0.5 * coef_a * amount_ui * (2 * position_ui + amount_ui) + coef_b * amount_ui


# --------------------------------------------------------------------------
# 3. fixed price
# --------------------------------------------------------------------------


def fixed_price_raw(virtual_quote: int, virtual_base: int) -> Optional[float]:
    if not virtual_base:
        return None
    return virtual_quote / virtual_base


# --------------------------------------------------------------------------
# 4. Meteora DBC: piecewise sqrt-price segments (Uniswap-v3 style, Q64.64)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquiditySegment:
    sqrt_price: int  # Q64.64 upper bound of this segment
    liquidity: int  # Q64.64 liquidity active below that bound


def sqrt_price_to_raw_price(sqrt_price_q64: int) -> Optional[float]:
    """``(sqrtP / 2^64)^2`` -- quote-raw per base-raw."""
    if sqrt_price_q64 <= 0:
        return None
    ratio = sqrt_price_q64 / Q64
    return ratio * ratio


def raw_price_to_sqrt_price(price_raw: float) -> int:
    return int(math.sqrt(price_raw) * Q64)


def dbc_delta_base(
    lower_sqrt: int, upper_sqrt: int, liquidity: int, round_up: bool = True
) -> int:
    """``Δbase = L * (√Pu - √Pl) / (√Pu * √Pl)``."""
    if upper_sqrt <= lower_sqrt or liquidity <= 0:
        return 0
    numerator = liquidity * (upper_sqrt - lower_sqrt)
    denominator = lower_sqrt * upper_sqrt
    return _ceil_div(numerator, denominator) if round_up else numerator // denominator


def dbc_delta_quote(
    lower_sqrt: int, upper_sqrt: int, liquidity: int, round_up: bool = True
) -> int:
    """``Δquote = L * (√Pu - √Pl)`` with the Q64.64 scaling removed."""
    if upper_sqrt <= lower_sqrt or liquidity <= 0:
        return 0
    product = liquidity * (upper_sqrt - lower_sqrt)
    return _ceil_div(product, Q128) if round_up else product >> 128


def dbc_next_sqrt_price_from_quote_in(
    sqrt_price: int, liquidity: int, quote_in: int
) -> int:
    """Price after adding `quote_in` to a segment: ``√P + Δquote * 2^128 / L``."""
    if liquidity <= 0:
        raise ValueError("liquidity must be positive")
    return sqrt_price + (quote_in * Q128) // liquidity


def dbc_base_for_swap(
    sqrt_start_price: int,
    sqrt_migration_price: int,
    curve: Sequence[LiquiditySegment],
) -> int:
    """Base tokens the curve will sell between start and migration price.

    Direct port of `get_base_token_for_swap` in the DBC program.
    """
    total = 0
    lower = sqrt_start_price
    for segment in curve:
        if segment.sqrt_price <= 0 or segment.liquidity <= 0:
            break
        if segment.sqrt_price > sqrt_migration_price:
            total += dbc_delta_base(lower, sqrt_migration_price, segment.liquidity, True)
            break
        total += dbc_delta_base(lower, segment.sqrt_price, segment.liquidity, True)
        lower = segment.sqrt_price
    return total


def dbc_migration_sqrt_price(
    migration_threshold: int,
    sqrt_start_price: int,
    curve: Sequence[LiquiditySegment],
) -> Optional[int]:
    """Sqrt price reached once `migration_threshold` quote has been paid in.

    Port of `get_migration_threshold_price`.  Used when a config predates the
    stored `migration_sqrt_price` field or stores it as zero.
    """
    if not curve:
        return None
    remaining = migration_threshold
    current = sqrt_start_price
    for segment in curve:
        if segment.sqrt_price <= 0 or segment.liquidity <= 0:
            break
        capacity = dbc_delta_quote(current, segment.sqrt_price, segment.liquidity, True)
        if capacity > remaining:
            return dbc_next_sqrt_price_from_quote_in(current, segment.liquidity, remaining)
        remaining -= capacity
        current = segment.sqrt_price
        if remaining == 0:
            return current
    return current if remaining == 0 else None


def dbc_quote_for_range(
    sqrt_start_price: int,
    sqrt_end_price: int,
    curve: Sequence[LiquiditySegment],
) -> int:
    """Total quote taken in moving the price from start to end."""
    total = 0
    lower = sqrt_start_price
    for segment in curve:
        if segment.sqrt_price <= 0 or segment.liquidity <= 0:
            break
        upper = min(segment.sqrt_price, sqrt_end_price)
        total += dbc_delta_quote(lower, upper, segment.liquidity, True)
        if segment.sqrt_price >= sqrt_end_price:
            break
        lower = segment.sqrt_price
    return total


def active_segments(curve: Sequence[LiquiditySegment]) -> List[LiquiditySegment]:
    """Trim the fixed-size 20-slot curve array down to its populated prefix."""
    out: List[LiquiditySegment] = []
    for segment in curve:
        if segment.sqrt_price <= 0 or segment.liquidity <= 0:
            break
        out.append(segment)
    return out


# --------------------------------------------------------------------------
# 5. GoFundMeme power law
# --------------------------------------------------------------------------


def power_law_area(x1: float, x2: float, constant: float, exponent: float) -> float:
    """``∫ (x + c)^-e dx`` from x1 to x2 -- the token-issuance integral."""
    if abs(exponent - 1.0) < 1e-12:
        return math.log(x2 + constant) - math.log(x1 + constant)
    one_minus = 1.0 - exponent
    return ((x2 + constant) ** one_minus - (x1 + constant) ** one_minus) / one_minus


def power_law_scale(
    tradable_supply_ui: float, target_raise_ui: float, constant: float, exponent: float
) -> Optional[float]:
    """Tokens issued per unit of curve area."""
    total = power_law_area(0.0, target_raise_ui, constant, exponent)
    if total <= 0:
        return None
    return tradable_supply_ui / total


def power_law_price_ui(
    raised_ui: float, constant: float, exponent: float, scale: float
) -> Optional[float]:
    """Spot price in whole quote per whole token at a given amount raised."""
    if scale <= 0:
        return None
    return ((raised_ui + constant) ** exponent) / scale


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def solve_position_for_marketcap(
    marketcap_target_raw: int,
    initial_virtual_base: int,
    initial_virtual_quote: int,
    low: int,
    high: int,
) -> Optional[int]:
    """Smallest tokens-sold whose ``price * sold`` reaches the target market cap.

    Moonit sizes its curve from a market-cap threshold rather than a SOL
    target, so the raise has to be solved for.  Binary search mirrors
    `ConstantProductCurve.getDynamicThresholdFromMarketCap`.
    """
    k = initial_virtual_base * initial_virtual_quote

    def marketcap(position: int) -> Optional[int]:
        base = initial_virtual_base - position
        if base <= 0:
            return None
        quote = k // base
        return (quote * position) // base

    lo, hi = low, high
    if marketcap(hi) is None or (marketcap(hi) or 0) < marketcap_target_raw:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        value = marketcap(mid)
        if value is None or value < marketcap_target_raw:
            lo = mid + 1
        else:
            hi = mid
    return lo


def progress_ratio(raised: Optional[float], target: Optional[float]) -> Optional[float]:
    if raised is None or not target:
        return None
    return max(0.0, min(1.0, raised / target))


def safe_mul(value: Optional[float], other: Optional[float]) -> Optional[float]:
    if value is None or other is None:
        return None
    return value * other


def unpack_segments(raw_curve: Sequence[dict]) -> Tuple[LiquiditySegment, ...]:
    """Turn decoded `LiquidityDistributionConfig[20]` dicts into segments."""
    return tuple(
        LiquiditySegment(
            sqrt_price=int(item.get("sqrt_price", 0)),
            liquidity=int(item.get("liquidity", 0)),
        )
        for item in raw_curve
    )
