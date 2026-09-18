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
from launchpad_decoder.discovery import derived_curve_address  # noqa: E402

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

        metrics = decoder.decode(account)
        if metrics and any("k_violation" in w for w in metrics.warnings):
            invariant_failures += 1

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
    examples = report["examples"]

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return

    print(f"checked {report['rows_checked']} rows ({unreadable} unreadable)")
    print(
        f"rows whose ON-CHAIN state fails the curve invariant: {invariant_failures} "
        "(if this is ~0, the chain is fine and the disagreement is in the pipeline)\n"
    )
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
