"""Self-consistency checks on decoded curve state.

A decoded price can be wrong in two very different ways. The layout can be
wrong, which `verify_against_node` catches. Or the *values* can be individually
plausible but mutually impossible -- a virtual reserve pair that does not lie on
the curve it claims to be on. That second kind survives every schema check and
shows up downstream as a wrong price on a coin that looks fine.

A constant-product curve gives a free test: ``vBase * vQuote`` is invariant.
The programs floor their outputs, so in practice k creeps *up* by parts per
billion over a curve's life and never moves down. A pair whose k differs from
the curve's opening k by a visible margin did not come off one account read.

The usual cause is field mixing rather than corruption -- two fields sourced
from different places and merged into one row. pump.fun makes this easy: its
`TradeEvent` carries `virtual_sol_reserves` (index 6) *and*
`virtual_quote_reserves` (index 30) as separate fields, the second appended
later for non-SOL quote pairs. Read one from the account and the other from an
event and the pair no longer multiplies to k.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

#: k only ever creeps up, by parts per billion. 0.5% is far beyond any
#: legitimate drift while staying clear of floating-point noise.
K_TOLERANCE = 0.005

#: how far a virtual reserve may sit past its opening value before it is
#: treated as impossible rather than as rounding dust
OPEN_TOLERANCE = 1e-6


@dataclass(frozen=True)
class Violation:
    name: str
    detail: str
    severity: str = "error"

    def __str__(self) -> str:
        return f"{self.name}: {self.detail}"


def check_constant_product_pair(
    virtual_base: Optional[int],
    virtual_quote: Optional[int],
    initial_base: Optional[int],
    initial_quote: Optional[int],
    *,
    tolerance: float = K_TOLERANCE,
) -> List[Violation]:
    """Does this virtual reserve pair lie on the curve it claims to be on?

    All four arguments are raw on-chain units. Returns an empty list when the
    pair is consistent, which is the common case.
    """
    violations: List[Violation] = []
    if not virtual_base or not virtual_quote or not initial_base or not initial_quote:
        return violations

    k_open = initial_base * initial_quote
    k_now = virtual_base * virtual_quote
    drift = k_now / k_open - 1.0

    if abs(drift) > tolerance:
        violations.append(
            Violation(
                "k_violation",
                f"vBase*vQuote is {k_now:.6e}, the curve opened at {k_open:.6e} "
                f"({drift:+.1%}). Flooring moves k up by parts per billion and "
                f"never down, so this pair did not come from one account read",
            )
        )

    if virtual_quote < initial_quote * (1 - OPEN_TOLERANCE):
        violations.append(
            Violation(
                "quote_below_open",
                f"virtual quote {virtual_quote} is below the opening "
                f"{initial_quote}; a one-way curve cannot go below its seed",
            )
        )
    if virtual_base > initial_base * (1 + OPEN_TOLERANCE):
        violations.append(
            Violation(
                "base_above_open",
                f"virtual base {virtual_base} is above the opening {initial_base}; "
                f"more tokens would have been sold into the curve than ever left it",
            )
        )
    return violations


def diagnose_pair(
    virtual_base: Optional[int],
    virtual_quote: Optional[int],
    initial_base: Optional[int],
    initial_quote: Optional[int],
    base_for_sale: Optional[int] = None,
) -> Tuple[Optional[str], str]:
    """Given an inconsistent pair, say which side is the likelier culprit.

    Each field has a legal range on a one-way curve: the base reserve only ever
    falls from its opening (and not below what the curve may sell), the quote
    reserve only ever rises. Whichever observed value sits outside its range is
    the one to distrust, and the other implies what it should have been.

    Returns ``(suspect_field, explanation)``; `suspect_field` is None when the
    pair is consistent or cannot be judged.
    """
    if not virtual_base or not virtual_quote or not initial_base or not initial_quote:
        return None, "insufficient data"

    k_open = initial_base * initial_quote
    if abs(virtual_base * virtual_quote / k_open - 1.0) <= K_TOLERANCE:
        return None, "pair is consistent with the opening k"

    floor_base = initial_base - base_for_sale if base_for_sale else 0
    base_ok = floor_base <= virtual_base <= initial_base * (1 + OPEN_TOLERANCE)
    quote_ok = virtual_quote >= initial_quote * (1 - OPEN_TOLERANCE)

    implied_quote = k_open // virtual_base
    implied_base = k_open // virtual_quote

    if base_ok and not quote_ok:
        return (
            "virtual_quote",
            f"virtual base is in range, virtual quote is not; on this base the "
            f"quote should be {implied_quote} ({implied_quote / 1e9:.4f} "
            f"in 9-decimal units), not {virtual_quote}",
        )
    if quote_ok and not base_ok:
        return (
            "virtual_base",
            f"virtual quote is in range, virtual base is not; on this quote the "
            f"base should be {implied_base}, not {virtual_base}",
        )
    if not base_ok and not quote_ok:
        # Both are outside their range, but rarely by the same margin. The one
        # that has travelled furthest past its bound is the one to chase first.
        base_excursion = max(0.0, (virtual_base - initial_base) / initial_base)
        quote_excursion = max(0.0, (initial_quote - virtual_quote) / initial_quote)
        if quote_excursion >= base_excursion:
            return (
                "virtual_quote",
                f"both reserves are out of range, but the quote is further out "
                f"({quote_excursion:.1%} below its opening vs {base_excursion:.1%} "
                f"above for the base); on this base the quote should be "
                f"{implied_quote} ({implied_quote / 1e9:.4f} in 9-decimal units), "
                f"not {virtual_quote}",
            )
        return (
            "virtual_base",
            f"both reserves are out of range, but the base is further out "
            f"({base_excursion:.1%} above its opening vs {quote_excursion:.1%} "
            f"below for the quote); on this quote the base should be "
            f"{implied_base}, not {virtual_base}",
        )
    return None, "both reserves are individually in range but their product is not k"


def check_metrics(metrics, *, tolerance: float = K_TOLERANCE) -> List[Violation]:
    """Run whatever invariants apply to an already-decoded `LaunchMetrics`.

    Price-level rather than reserve-level, so it works for any curve family:
    the launch price is the floor a one-way curve can never trade below, and
    the graduation price is the ceiling it cannot pass while still on the curve.
    """
    from .types import CurveFamily

    violations: List[Violation] = []
    launch = metrics.launch_price_quote.value
    now = metrics.current_price_quote.value
    graduation = metrics.graduation_price_quote.value

    if launch is not None and now is not None and launch > 0:
        if now < launch * (1 - OPEN_TOLERANCE):
            violations.append(
                Violation(
                    "price_below_launch",
                    f"current price {now:.6e} is below the launch price {launch:.6e}; "
                    f"a one-way curve cannot trade below its opening",
                )
            )
    if (
        graduation is not None
        and now is not None
        and graduation > 0
        and metrics.complete.value is False
        and now > graduation * (1 + OPEN_TOLERANCE)
    ):
        violations.append(
            Violation(
                "price_above_graduation",
                f"current price {now:.6e} exceeds the graduation price "
                f"{graduation:.6e} while the curve is still marked incomplete",
            )
        )

    raised = metrics.raised_quote.value
    target = metrics.raise_target_quote.value
    if raised is not None and target and raised > target * 1.02:
        violations.append(
            Violation(
                "raised_above_target",
                f"raised {raised} exceeds the raise target {target}",
                severity="warning",
            )
        )

    if metrics.curve_family is CurveFamily.CONSTANT_PRODUCT_VIRTUAL:
        state = metrics.raw_state or {}
        violations.extend(
            check_constant_product_pair(
                state.get("virtual_token_reserves"),
                state.get("virtual_quote_reserves") or state.get("virtual_sol_reserves"),
                state.get("_initial_virtual_base"),
                state.get("_initial_virtual_quote"),
                tolerance=tolerance,
            )
        )
    return violations
