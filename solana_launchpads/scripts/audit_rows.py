#!/usr/bin/env python3
"""Find out which on-chain field a stored column actually contains.

When a warehouse column disagrees with the chain, "it's wrong" is not the
useful finding -- *which field it actually holds* is, because that names the
mapping bug directly.

So this does not just diff. For every row it decodes the real account and asks,
for each stored column, which on-chain field that stored value equals. Run it
over a few thousand rows and the answer falls out as a histogram:

    stored column `virtual_quote` actually holds:
        virtual_quote_reserves   133,725   81.0%
        real_quote_reserves       31,004   19.0%   <-- the bug, and its size

Input is CSV with a `mint` column plus whatever stored columns you want
checked; everything else is compared as an integer. A `curve` column is used
as the account address when present, otherwise the address is derived from the
mint for launchpads that support it.

    python scripts/audit_rows.py rows.csv --program 6EF8rrec... --rpc $SOLANA_RPC

It then answers the follow-up question, which is the one that actually decides
whether there is a bug at all: when the columns turn out to be faithful and the
curve still looks odd, is that a real cohort or is it corruption?

Two groupings settle it, and neither needs a second data source.

* **Opening buckets.** A pump.fun curve states its own opening, exactly, as
  `virtual_quote - real_quote` (see docs/CURVE_MATH.md). Bucket it across the
  population: a real opening is shared by thousands of rows however far they
  have traded, while a corrupted value is unique to its own row.
* **Cohorts.** Group the odd curves by `complete` and by the per-curve mode
  flags. `complete` is the one to check first -- a graduated curve freezes at
  its final reserves while trading moves to the AMM, so its state stays
  faithful to the account and stops being the market price. A feature that
  explains the cohort shows a share far above its baseline.

Both are printed, and both are in the `--json` report.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_decoder import JsonRpcAccountSource, LaunchpadDecoder  # noqa: E402
from launchpad_decoder.discovery import derived_curve_address
from launchpad_decoder.invariants import curve_opening  # noqa: E402

#: violation names that mean "no curve this program could create looks like this"
_STRUCTURAL = (
    "quote_seed_not_positive",
    "base_floor_not_positive",
    "implied_opening_impossible",
    "implied_opening_exceeds_supply",
    "k_violation",
)

#: columns that are identifiers rather than values to check
ID_COLUMNS = {"mint", "curve", "curve_address", "address", "pool", "creator_program"}


def load_rows(path: str) -> List[Dict[str, str]]:
    handle = sys.stdin if path == "-" else open(path, newline="", encoding="utf-8")
    try:
        return [row for row in csv.DictReader(handle) if row.get("mint") or row.get("curve")]
    finally:
        if handle is not sys.stdin:
            handle.close()


def as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = value.strip().replace(",", "").replace("_", "")
    if not text or text.lower() in ("null", "none", "nan"):
        return None
    try:
        return int(text)
    except ValueError:
        try:  # tolerate values exported as floats
            return int(float(text))
        except ValueError:
            return None


def first_present(state: Dict, *names: str) -> Optional[int]:
    """First field that is present, absorbing the sol -> quote rename.

    Deliberately not `a or b`: a real reserve of zero is the commonest value
    on a fresh curve, and `0 or None` is None, which would quietly skip the
    check on exactly the curves it should be looking at.
    """
    for name in names:
        value = state.get(name)
        if value is not None:
            return value
    return None


#: per-curve features worth grouping a residual population by. `complete` is
#: the one that costs nothing to check and explains the most: a graduated curve
#: keeps reporting its final reserves while trading has moved to the AMM, so a
#: price derived from it is faithful to the account and no longer the market.
COHORT_FIELDS = (
    "complete",
    "is_mayhem_mode",
    "is_cashback_coin",
    "is_holder_reward",
    "quote_mint",
)


def cohort_labels(state: Dict) -> Dict[str, str]:
    """Group one curve by the per-curve features that could explain a cohort."""
    out: Dict[str, str] = {}
    for field in COHORT_FIELDS:
        value = state.get(field)
        if value is None:
            continue  # the account predates the field
        if isinstance(value, bool):
            out[field] = "true" if value else "false"
        elif isinstance(value, str):
            out[field] = value
        else:
            out[field] = "set" if value else "unset"
    return out


def numeric_fields(state: Dict) -> Dict[str, int]:
    return {
        name: value
        for name, value in state.items()
        if isinstance(value, int) and not isinstance(value, bool) and not name.startswith("_")
    }


def audit(
    decoder: LaunchpadDecoder,
    rows: List[Dict[str, str]],
    program: str,
    *,
    show_mismatches: int = 5,
) -> Dict:
    """Attribute each stored column to the on-chain field that holds its value."""
    attribution: Dict[str, Counter] = defaultdict(Counter)
    invariant_failures = 0
    nonstandard = 0
    # The histogram that decides whether an unfamiliar opening is real. The
    # curve's opening quote reserve is exactly virtual_quote - real_quote, so
    # bucketing it across the population separates the two explanations: a real
    # opening is shared by thousands of curves, a corrupted value is unique to
    # its own row.
    opening_buckets: Counter = Counter()
    base_floor_buckets: Counter = Counter()
    # Two explanations that do not need the pipeline to be at fault, both of
    # which are one grouping away. A graduated curve freezes at its final state
    # while trading moves to the AMM, so its reserves stay faithful and stop
    # being the market price. And pump.fun carries per-curve mode flags that
    # could plausibly carry their own seeds.
    cohorts: Dict[str, Counter] = defaultdict(Counter)
    unreadable = 0
    examples: List[Dict] = []

    for row in rows:
        address = row.get("curve") or row.get("curve_address") or row.get("address")
        if not address:
            address = derived_curve_address(decoder, program, row["mint"])
        if not address:
            unreadable += 1
            continue

        account = decoder.source.get_account(address)
        if account is None or account.owner != program:
            unreadable += 1
            continue
        schema = decoder.schema(program)
        decoded = schema.decode_account(account.data) if schema else None
        if decoded is None:
            unreadable += 1
            continue
        _name, state = decoded
        onchain = numeric_fields(state)

        mismatched = {}
        for column, raw in row.items():
            if column in ID_COLUMNS:
                continue
            stored = as_int(raw)
            if stored is None:
                continue
            matches = sorted(f for f, v in onchain.items() if v == stored)
            if matches:
                # An exact match against the identically-named field is the
                # expected case; anything else names the mapping bug.
                attribution[column][column if column in matches else matches[0]] += 1
            else:
                attribution[column]["<no on-chain field holds this value>"] += 1
                mismatched[column] = stored

        opening = curve_opening(
            first_present(state, "virtual_token_reserves"),
            first_present(state, "virtual_quote_reserves", "virtual_sol_reserves"),
            first_present(state, "real_token_reserves"),
            first_present(state, "real_quote_reserves", "real_sol_reserves"),
        )
        if opening is not None:
            opening_buckets[opening.quote_seed] += 1
            base_floor_buckets[opening.base_floor] += 1

        metrics = decoder.decode(account)
        warnings = metrics.warnings if metrics else []
        # Two different findings, and conflating them is what sends an
        # investigation after the pipeline when the chain is the answer.
        # A structural failure is impossible on any curve the program could
        # have created. A nonstandard opening is merely one this build of the
        # program would not create today, which an older build may well have.
        if any(name in w for w in warnings for name in _STRUCTURAL):
            invariant_failures += 1
        unfamiliar = any("nonstandard_opening" in w for w in warnings)
        if unfamiliar:
            nonstandard += 1

        # Count each cohort twice: over all rows, and over the unfamiliar ones
        # alone. A feature that explains the unfamiliar population shows up as
        # a share far above its baseline; one that does not matches it.
        for label, value in cohort_labels(state).items():
            cohorts[label][("all", value)] += 1
            if unfamiliar:
                cohorts[label][("nonstandard", value)] += 1

        if mismatched and len(examples) < show_mismatches:
            examples.append(
                {
                    "mint": row.get("mint"),
                    "curve": address,
                    "stored_without_a_match": mismatched,
                    "onchain": onchain,
                }
            )

    return {
        "rows_checked": len(rows) - unreadable,
        "rows_unreadable": unreadable,
        "rows_failing_curve_invariant_on_chain": invariant_failures,
        "rows_with_a_nonstandard_opening": nonstandard,
        "opening_buckets": dict(opening_buckets.most_common(20)),
        "base_floor_buckets": dict(base_floor_buckets.most_common(20)),
        "cohorts": {
            label: {f"{scope}:{value}": count for (scope, value), count in counts.most_common()}
            for label, counts in cohorts.items()
        },
        "attribution": {col: dict(counts.most_common()) for col, counts in attribution.items()},
        "examples": examples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rows", help="CSV file, or '-' for stdin")
    parser.add_argument("--program", required=True, help="the curve program id")
    parser.add_argument("--rpc", default=os.environ.get("SOLANA_RPC"))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--show-mismatches",
        type=int,
        default=5,
        help="print this many example rows whose columns do not line up",
    )
    args = parser.parse_args()

    if not args.rpc:
        raise SystemExit("needs a node: pass --rpc or set SOLANA_RPC")

    decoder = LaunchpadDecoder(JsonRpcAccountSource(args.rpc), resolve_mints=False)
    rows = load_rows(args.rows)
    if args.limit:
        rows = rows[: args.limit]

    report = audit(decoder, rows, args.program, show_mismatches=args.show_mismatches)
    unreadable = report["rows_unreadable"]
    invariant_failures = report["rows_failing_curve_invariant_on_chain"]
    nonstandard = report["rows_with_a_nonstandard_opening"]
    examples = report["examples"]

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    print(f"checked {report['rows_checked']} rows ({unreadable} unreadable)")
    print(
        f"rows whose ON-CHAIN state is structurally impossible: {invariant_failures}\n"
        f"rows whose opening matches no constant this program build uses: {nonstandard}\n"
    )
    print(
        "    The first number is a real defect wherever it is not zero. The second\n"
        "    is NOT: an older build, or a per-quote-mint seed, produces curves that\n"
        "    are entirely correct and match nothing in today's config. The buckets\n"
        "    below decide which you have -- a real opening is shared by thousands of\n"
        "    curves, a corrupted one is unique to its row.\n"
    )
    if nonstandard and report["cohorts"]:
        print(
            "what the curves with an unfamiliar opening have in common:\n"
            "    A feature that EXPLAINS them shows a share far above its baseline.\n"
            "    `complete` is the one to look at first -- a graduated curve keeps\n"
            "    reporting its final reserves while trading has moved to the AMM, so\n"
            "    a price derived from it is faithful to the account and no longer the\n"
            "    market price.\n"
        )
        for field, counts in report["cohorts"].items():
            rows_all = {
                k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("all:")
            }
            rows_odd = {
                k.split(":", 1)[1]: v for k, v in counts.items() if k.startswith("nonstandard:")
            }
            total_all = sum(rows_all.values()) or 1
            print(f"  {field}:")
            for value, count in sorted(rows_all.items(), key=lambda kv: -kv[1])[:6]:
                odd = rows_odd.get(value, 0)
                baseline = count / total_all
                share = odd / nonstandard
                flag = "   <-- concentrated here" if share > baseline + 0.2 else ""
                print(
                    f"    {value:<24} all {count:>8,} ({baseline:>5.1%})   "
                    f"unfamiliar {odd:>8,} ({share:>5.1%}){flag}"
                )
            print()

    for label, key in (
        ("opening quote reserve (virtual_quote - real_quote)", "opening_buckets"),
        ("opening base gap (virtual_base - real_base)", "base_floor_buckets"),
    ):
        buckets = report[key]
        if not buckets:
            continue
        total = sum(buckets.values())
        print(f"{label}, most common first:")
        for value, count in buckets.items():
            print(f"    {value:>24,}  {count:>8,}  {count / total:>6.1%}")
        print()
    for column, counts in report["attribution"].items():
        total = sum(counts.values())
        print(f"stored column `{column}` actually holds:")
        for field, count in counts.items():
            flag = "   <-- mapping bug" if field != column and not field.startswith("<") else ""
            print(f"    {field:34} {count:>8,}  {count / total:>6.1%}{flag}")
        print()
    for example in examples:
        print(f"example {example['mint']} ({example['curve']}):")
        for column, value in example["stored_without_a_match"].items():
            print(f"    stored {column} = {value} matches no on-chain field")
        print(f"    on-chain: {json.dumps(example['onchain'])}\n")


if __name__ == "__main__":
    main()
