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

The limit of this, stated plainly, because it was measured rather than guessed
and it is easy to overstate what a curve can tell you about itself.

The opening k is read from the program's config *now*. If a launchpad ever
changed its opening reserves, or seeds a different opening per quote mint,
curves created under another constant sit on a different k. Judged against
today's Global they are flagged, and they are not wrong -- the check is.
Measured against a real node on 60 sampled curves, every stored column held
exactly the on-chain field its name claimed and 59 of 60 still failed this
check. That is the check over-reaching, not the data being broken, and it was
rejecting roughly 9% of pump.fun curves.

So `check_curve_opening` asks the weaker question that survives dropping the
constants: is this opening *structurally possible*? And the honest answer to
"can a single row prove itself wrong without them?" is **no**. A curve's four
reserves determine its own opening exactly (see `CurveOpening`), so k against
that opening holds by construction -- the test is circular. What is left is
positivity, and nothing else.

Information about a curve that matches no known constant has to come from
outside the row, and there are three places it can come from:

* **the population** -- bucket `virtual_quote - real_quote` across all curves.
  A real opening is shared by thousands; a corrupted value is unique to its row.
* **the curve's history** -- k is conserved, so k must not *move* for a given
  curve between slots, whatever its value. This needs no constants at all and
  is the strongest test available.
* **the known constants** -- exact, but only for the openings the program uses
  today, which is where this started.

The first two are for an operator with the whole table, not for a decoder
holding one account, which is why this module reports a nonstandard opening as
a warning and leaves the verdict to whoever can see the population.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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


@dataclass(frozen=True)
class CurveOpening:
    """A pump.fun curve's own opening parameters, recovered from its reserves.

    The program moves each virtual reserve in lockstep with its real
    counterpart -- a buy subtracts the same token amount from
    ``virtual_token_reserves`` and ``real_token_reserves``, and adds the same
    quote amount to ``virtual_quote_reserves`` and ``real_quote_reserves``.
    So two differences are fixed for the curve's whole life, and both of them
    are opening parameters:

        quote_seed = virtual_quote - real_quote  == initial_virtual_quote_reserves
        base_floor = virtual_base  - real_base   == initial_virtual_token_reserves
                                                    - initial_real_token_reserves

    `base_floor` is also the virtual base the curve ends on, which is why the
    graduation price is ``(quote_seed + raise) / base_floor``.

    Given those two, the opening k is ``(base_floor + initial_real_base) *
    quote_seed``, and since k is conserved that inverts to

        initial_real_base = virtual_base * virtual_quote / quote_seed - base_floor

    so a curve states its entire opening without the Global account.
    """

    quote_seed: int
    base_floor: int
    implied_initial_real_base: Optional[int]
    #: False when real_quote is zero -- the curve has not traded, so its
    #: reserves carry no evidence about k and self-consistency is vacuous.
    traded: bool

    @property
    def initial_virtual_base(self) -> Optional[int]:
        if self.implied_initial_real_base is None:
            return None
        return self.base_floor + self.implied_initial_real_base


def curve_opening(
    virtual_base: Optional[int],
    virtual_quote: Optional[int],
    real_base: Optional[int],
    real_quote: Optional[int],
) -> Optional[CurveOpening]:
    """Recover a curve's opening from its four reserve fields alone.

    Returns None when a field is missing. Note this needs all four: the two
    virtual reserves say where the curve is, the two real ones say how far it
    has come, and only together do they say where it started.
    """
    if virtual_base is None or virtual_quote is None:
        return None
    if real_base is None or real_quote is None:
        return None

    quote_seed = virtual_quote - real_quote
    base_floor = virtual_base - real_base
    implied_initial_real_base = None
    if quote_seed > 0:
        implied_initial_real_base = (virtual_base * virtual_quote) // quote_seed - base_floor
    return CurveOpening(
        quote_seed=quote_seed,
        base_floor=base_floor,
        implied_initial_real_base=implied_initial_real_base,
        traded=real_quote > 0,
    )


def check_curve_opening(
    opening: Optional[CurveOpening],
    token_total_supply: Optional[int] = None,
) -> List[Violation]:
    """Is a curve's recovered opening structurally possible?

    This is what survives when the program's *current* opening constants are
    not assumed. It asks only what must be true of any constant-product curve
    the program could have created, so it cannot reject a curve merely for
    having been seeded differently from today's default.

    That matters: a launchpad that changes its opening reserves, or seeds a
    different opening per quote mint, leaves a large population of curves that
    are entirely correct and match no current constant. Judging those against
    today's Global rejects real data.
    """
    violations: List[Violation] = []
    if opening is None:
        return violations

    if opening.quote_seed <= 0:
        violations.append(
            Violation(
                "quote_seed_not_positive",
                f"virtual_quote - real_quote is {opening.quote_seed}, but the "
                f"virtual quote reserve is seeded above zero and then tracks the "
                f"real one exactly, so the difference is positive for life. A "
                f"value at or below zero is what a virtual column carrying the "
                f"REAL reserve looks like",
            )
        )
    if opening.base_floor <= 0:
        violations.append(
            Violation(
                "base_floor_not_positive",
                f"virtual_base - real_base is {opening.base_floor}; the curve "
                f"keeps virtual base above real base for life, so this pair did "
                f"not come from one account read",
            )
        )

    implied = opening.implied_initial_real_base
    if implied is not None and opening.quote_seed > 0 and opening.base_floor > 0:
        if implied <= 0:
            violations.append(
                Violation(
                    "implied_opening_impossible",
                    f"the reserves imply the curve opened with {implied} sellable "
                    f"tokens, which cannot be",
                )
            )
        elif token_total_supply and implied > token_total_supply:
            violations.append(
                Violation(
                    "implied_opening_exceeds_supply",
                    f"the reserves imply the curve opened with {implied} sellable "
                    f"tokens against a total supply of {token_total_supply}",
                )
            )
    return violations


def classify_variant(
    virtual_quote: Optional[int],
    real_quote: Optional[int],
    candidates: Dict[str, int],
) -> Tuple[Optional[str], Optional[int]]:
    """Which opening constant a curve was seeded with, read off its reserves.

    pump.fun's buy and sell add the post-fee amount to the virtual *and* the
    real quote reserve together, so their difference never moves:

        virtual_quote - real_quote == initial_virtual_quote_reserves

    for the whole life of the curve. That makes the difference an exact
    classifier. It beats matching k against each candidate, because it needs no
    tolerance and -- unlike counting curves that still sit near their opening
    value -- it keeps working after the curve has traded away from it.

    Returns ``(variant name, its opening constant)``, or ``(None, observed
    offset)`` when the difference matches nothing, which means the pair did not
    come from one read.
    """
    if virtual_quote is None or real_quote is None:
        return None, None
    offset = virtual_quote - real_quote
    for name, initial in candidates.items():
        if initial and offset == initial:
            return name, initial
    return None, offset


def check_reserve_offset(
    virtual_quote: Optional[int],
    real_quote: Optional[int],
    candidates: Dict[str, int],
) -> List[Violation]:
    """Report a curve whose virtual/real quote difference matches no opening.

    This is a *warning*, not an error, and the distinction was learned the hard
    way. The difference is exactly the curve's opening quote reserve, so it is
    a reliable classifier -- but only against the openings the program is using
    today. A launchpad that has changed its seed, or that seeds per quote mint,
    leaves a large population of curves whose opening is real and simply not in
    the candidate list. Treating those as corrupt rejects correct data, and at
    scale it rejects a lot of it.

    An offset at or below zero is different in kind: no opening can be zero or
    negative, so that one is structurally impossible rather than merely
    unfamiliar. `check_curve_opening` raises it as an error.
    """
    variant, offset = classify_variant(virtual_quote, real_quote, candidates)
    if variant is not None or offset is None or offset <= 0:
        # offset <= 0 is check_curve_opening's to report, as an error.
        return []
    known = ", ".join(f"{name}={value}" for name, value in candidates.items() if value)
    return [
        Violation(
            "nonstandard_opening",
            f"virtual_quote - real_quote is {offset}, which matches no opening "
            f"constant the program uses today ({known}). That difference is the "
            f"curve's own opening quote reserve, fixed for its whole life, so "
            f"the curve is priced against {offset} here rather than against the "
            f"default. Whether that opening is real or the row is corrupt cannot "
            f"be told from this row: bucket the value across the population, "
            f"because a real opening is shared by many curves and a corrupt one "
            f"is unique to its row",
            severity="warning",
        )
    ]


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
        quote = state.get("virtual_quote_reserves")
        if quote is None:
            quote = state.get("virtual_sol_reserves")
        violations.extend(
            check_constant_product_pair(
                state.get("virtual_token_reserves"),
                quote,
                state.get("_initial_virtual_base"),
                state.get("_initial_virtual_quote"),
                tolerance=tolerance,
            )
        )
    return violations
