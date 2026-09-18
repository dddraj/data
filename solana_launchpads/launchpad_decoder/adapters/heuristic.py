"""The fallback adapter: decode a launchpad nobody wrote an adapter for.

Given any account decoded through any Anchor IDL -- typically one fetched from
the program's own on-chain IDL account -- this adapter tries to recognise the
bonding-curve shape from field *names* and produce the same metrics as a
hand-written adapter.

It exists because the launchpad list is not static.  A new program deployed
tomorrow that calls its fields `virtual_token_reserves` / `virtual_sol_reserves`
(as most pump.fun-shaped forks do) is priced correctly with no code change, and
one that does something genuinely novel is reported as unrecognised rather than
silently mis-priced.

Confidence is reported so a consumer can decide whether to trust it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import curves
from ..base58 import is_pubkey
from ..types import CurveFamily, LaunchMetrics, ValueSource, price_raw_to_ui, ui_amount
from .base import Adapter, DecodeContext

#: (role, ordered regex alternatives -- earlier patterns win)
FIELD_PATTERNS: Tuple[Tuple[str, Sequence[str]], ...] = (
    ("virtual_base", (r"^virtual_(token|base)_reserves?$", r"^virtual_(token|base)$", r"^v_?(token|base)_reserves?$")),
    ("virtual_quote", (r"^virtual_(sol|quote|collateral|native)_reserves?$", r"^virtual_(quote|sol)$", r"^v_?(sol|quote)_reserves?$", r"^shift$")),
    ("real_base", (r"^real_(token|base)_reserves?$", r"^real_(token|base)$", r"^(token|base)_reserves?$")),
    ("real_quote", (r"^real_(sol|quote|collateral)_reserves?$", r"^real_(quote|sol)$", r"^(sol|quote)_reserves?$")),
    ("total_supply", (r"^token_total_supply$", r"^total_supply$", r"^supply$", r"^pre_migration_token_supply$")),
    ("tokens_for_sale", (r"^initial_real_(token|base)_reserves?$", r"^total_(base_)?sell(_amount)?$", r"^swap_base_amount$", r"^initial_tokens$", r"^curve_amount$")),
    ("raise_target", (r"^migration_quote_threshold$", r"^total_quote_fund_raising$", r"^target_raise$", r"^(graduation|migration|curve)_threshold$", r"^threshold$")),
    ("raised", (r"^real_(sol|quote)_reserves?$", r"^quote_reserve$", r"^current_sol$", r"^total_raised$", r"^collateral_collected$")),
    ("sqrt_price", (r"^sqrt_price$", r"^current_sqrt_price$")),
    ("base_mint", (r"^base_mint$", r"^token_mint$", r"^mint$", r"^base_token_mint$", r"^token_a_mint$")),
    ("quote_mint", (r"^quote_mint$", r"^quote_token_mint$", r"^collateral_mint$", r"^token_b_mint$")),
    ("base_decimals", (r"^base_decimals$", r"^decimals$", r"^token_decimals?$", r"^base_token_mint_decimals$")),
    ("quote_decimals", (r"^quote_decimals$", r"^quote_token_mint_decimals$")),
    ("creator", (r"^creator$", r"^coin_creator$", r"^owner$", r"^admin$")),
    ("complete", (r"^complete(d)?$", r"^is_completed?$", r"^graduated$", r"^is_graduated$")),
    ("migrated", (r"^is_migrated$", r"^migrated$", r"^migration_progress$")),
)

_INT_ROLES = {
    "virtual_base",
    "virtual_quote",
    "real_base",
    "real_quote",
    "total_supply",
    "tokens_for_sale",
    "raise_target",
    "raised",
    "sqrt_price",
}


@dataclass
class FieldMap:
    roles: Dict[str, str]
    confidence: float
    family: CurveFamily

    def field(self, role: str) -> Optional[str]:
        return self.roles.get(role)


def map_fields(state: Dict[str, Any]) -> FieldMap:
    """Best-effort mapping from account field names to curve roles."""
    roles: Dict[str, str] = {}
    taken: set = set()
    for role, patterns in FIELD_PATTERNS:
        for pattern in patterns:
            matches = [
                name
                for name in state
                if name not in taken and re.match(pattern, name)
            ]
            if not matches:
                continue
            chosen = _best_match(state, role, matches)
            if chosen is None:
                continue
            roles[role] = chosen
            taken.add(chosen)
            break

    family = CurveFamily.UNKNOWN
    score = 0.0
    if "virtual_base" in roles and "virtual_quote" in roles:
        family = CurveFamily.CONSTANT_PRODUCT_VIRTUAL
        score += 0.55
    elif "sqrt_price" in roles:
        family = CurveFamily.SQRT_PIECEWISE
        score += 0.35
    elif "real_base" in roles and "real_quote" in roles:
        family = CurveFamily.AMM_REAL_RESERVES
        score += 0.3
    for role, weight in (
        ("total_supply", 0.15),
        ("raise_target", 0.15),
        ("raised", 0.05),
        ("base_mint", 0.05),
        ("complete", 0.05),
    ):
        if role in roles:
            score += weight
    return FieldMap(roles=roles, confidence=min(1.0, score), family=family)


def _best_match(state: Dict[str, Any], role: str, matches: List[str]) -> Optional[str]:
    """Prefer a candidate whose value has the right shape for the role."""
    def ok(name: str) -> bool:
        value = state.get(name)
        if role in _INT_ROLES:
            return isinstance(value, int) and not isinstance(value, bool)
        if role in ("base_mint", "quote_mint", "creator"):
            return isinstance(value, str) and is_pubkey(value)
        if role in ("base_decimals", "quote_decimals"):
            return isinstance(value, int) and 0 <= value <= 18
        if role in ("complete", "migrated"):
            return isinstance(value, (bool, int))
        return True

    viable = [name for name in matches if ok(name)]
    if not viable:
        return None
    # Shortest name wins ties: `supply` beats `supply_something_else`.
    return sorted(viable, key=len)[0]


class HeuristicAdapter(Adapter):
    """Price an unknown launchpad from field names alone."""

    key = "heuristic"
    #: below this, metrics are still returned but flagged as low confidence
    min_confidence = 0.5

    def state_account_names(self, ctx: DecodeContext) -> Tuple[str, ...]:
        if ctx.spec.state_account:
            return (ctx.spec.state_account,)
        # Guess which of the program's accounts holds per-launch curve state.
        ranked: List[Tuple[float, str]] = []
        for name, account in ctx.schema.accounts.items():
            fields = {field_name for field_name, _ in account.layout.fields}
            mapping = map_fields({f: 0 for f in fields})
            if mapping.confidence > 0:
                ranked.append((mapping.confidence, name))
        ranked.sort(reverse=True)
        return tuple(name for _score, name in ranked)

    def decode(
        self,
        ctx: DecodeContext,
        account_name: str,
        state: Dict[str, Any],
        address: Optional[str] = None,
    ) -> LaunchMetrics:
        m = self.new_metrics(ctx, account_name, address, state)
        mapping = map_fields(state)
        m.curve_family = (
            mapping.family if mapping.family is not CurveFamily.UNKNOWN else ctx.spec.curve_family
        )
        m.curve_type = f"heuristic:{m.curve_family.value}"

        def value(role: str) -> Optional[Any]:
            name = mapping.field(role)
            return state.get(name) if name else None

        m.base_mint = value("base_mint")
        m.quote_mint = value("quote_mint") or ctx.spec.default_quote_mint
        m.creator = value("creator")
        m.base_decimals = value("base_decimals")
        if m.base_decimals is None:
            m.base_decimals = ctx.decimals_for(m.base_mint) or ctx.spec.default_base_decimals
        m.quote_decimals = value("quote_decimals") or ctx.decimals_for(m.quote_mint) or 9

        total_supply = value("total_supply")
        m.total_supply = self.param(
            ui_amount(total_supply, m.base_decimals),
            ValueSource.ONCHAIN_STATE if total_supply is not None else ValueSource.UNAVAILABLE,
            ctx.slot,
            f"field {mapping.field('total_supply')}" if mapping.field("total_supply") else "",
        )
        for role, attr in (("tokens_for_sale", "tokens_for_sale"),):
            raw = value(role)
            if raw is not None:
                setattr(
                    m,
                    attr,
                    self.param(
                        ui_amount(raw, m.base_decimals),
                        ValueSource.ONCHAIN_STATE,
                        ctx.slot,
                        f"field {mapping.field(role)}",
                    ),
                )

        v_base, v_quote = value("virtual_base"), value("virtual_quote")
        r_base, r_quote = value("real_base"), value("real_quote")

        if m.curve_family is CurveFamily.CONSTANT_PRODUCT_VIRTUAL and v_base and v_quote is not None:
            m.current_price_quote = self.param(
                price_raw_to_ui(
                    curves.cp_price_raw(v_quote, v_base), m.base_decimals, m.quote_decimals
                ),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                f"{mapping.field('virtual_quote')} / {mapping.field('virtual_base')}",
            )
            for_sale = value("tokens_for_sale")
            if for_sale and r_base is not None:
                # Reconstruct the seeded reserves from the untouched part of the
                # curve so the launch price is available even mid-curve.
                initial_v_base = v_base + (for_sale - r_base) if r_base <= for_sale else v_base
                k = v_base * v_quote
                if initial_v_base:
                    initial_v_quote = k // initial_v_base
                    m.launch_price_quote = self.param(
                        price_raw_to_ui(
                            curves.cp_price_raw(initial_v_quote, initial_v_base),
                            m.base_decimals,
                            m.quote_decimals,
                        ),
                        ValueSource.DERIVED,
                        ctx.slot,
                        "reserves rewound to zero tokens sold",
                    )
                    target = curves.cp_raise_for_supply(
                        initial_v_quote, initial_v_base, for_sale
                    )
                    if target is not None:
                        m.raise_target_quote = self.param(
                            ui_amount(target, m.quote_decimals),
                            ValueSource.DERIVED,
                            ctx.slot,
                            "quote needed to drain the sellable supply",
                        )
                        m.graduation_price_quote = self.param(
                            price_raw_to_ui(
                                curves.cp_final_price_raw(
                                    initial_v_quote, initial_v_base, for_sale
                                ),
                                m.base_decimals,
                                m.quote_decimals,
                            ),
                            ValueSource.DERIVED,
                            ctx.slot,
                        )
        elif m.curve_family is CurveFamily.SQRT_PIECEWISE:
            sqrt_price = value("sqrt_price")
            m.current_price_quote = self.param(
                price_raw_to_ui(
                    curves.sqrt_price_to_raw_price(sqrt_price) if sqrt_price else None,
                    m.base_decimals,
                    m.quote_decimals,
                ),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                "(sqrt_price / 2^64)^2",
            )
        elif r_base and r_quote is not None:
            m.current_price_quote = self.param(
                price_raw_to_ui(
                    curves.cp_price_raw(r_quote, r_base), m.base_decimals, m.quote_decimals
                ),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                f"{mapping.field('real_quote')} / {mapping.field('real_base')}",
            )

        explicit_target = value("raise_target")
        if explicit_target is not None:
            m.raise_target_quote = self.param(
                ui_amount(explicit_target, m.quote_decimals),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                f"field {mapping.field('raise_target')}",
            )
        raised = value("raised")
        if raised is not None:
            m.raised_quote = self.param(
                ui_amount(raised, m.quote_decimals),
                ValueSource.ONCHAIN_STATE,
                ctx.slot,
                f"field {mapping.field('raised')}",
            )

        complete = value("complete")
        if complete is not None:
            m.complete = self.param(bool(complete), ValueSource.ONCHAIN_STATE, ctx.slot)
        migrated = value("migrated")
        if migrated is not None:
            m.migrated = self.param(bool(migrated), ValueSource.ONCHAIN_STATE, ctx.slot)

        m.raw_state = {
            **state,
            "_heuristic_field_map": mapping.roles,
            "_heuristic_confidence": round(mapping.confidence, 3),
        }
        if mapping.confidence < self.min_confidence:
            m.warn(
                f"low-confidence heuristic decode ({mapping.confidence:.2f}): "
                f"mapped {sorted(mapping.roles)}. Write a dedicated adapter "
                f"before trusting these numbers."
            )
        else:
            m.warn(
                f"decoded heuristically from field names "
                f"(confidence {mapping.confidence:.2f})"
            )
        return self.finish(m)
