#!/usr/bin/env python3
"""Generate cross-language golden vectors.

The Go package is a port, and a port is only worth anything if it agrees with
the reference to the last decimal. This writes account bytes plus the numbers
the Python decoder produces from them; the Go test suite reads the same file
and asserts it computes the same. Any divergence fails the Go build.

    python scripts/gen_golden.py && (cd go && go test ./...)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from fixtures import encode_account  # noqa: E402
from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder.anchor_idl import compile_idl  # noqa: E402
from launchpad_decoder.registry import get as get_spec  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
OUT = ROOT / "go" / "launchpad" / "testdata" / "golden.json"

PUMP = get_spec("pumpfun")
IVT = 1_073_000_000_000_000
IVS = 30_000_000_000
IRT = 793_100_000_000_000
SUPPLY = 1_000_000_000_000_000

#: the invariant names the decoder can emit, so prose warnings are not
#: mistaken for violations
VIOLATION_NAMES = ("k_violation", "quote_below_open", "base_above_open")

GLOBAL_FIELDS = {
    "initial_virtual_token_reserves": IVT,
    "initial_virtual_sol_reserves": IVS,
    "initial_real_token_reserves": IRT,
    "token_total_supply": SUPPLY,
    "fee_basis_points": 100,
}


def case(name: str, note: str, curve_fields: dict, truncate_to: int | None = None) -> dict:
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)

    global_blob = encode_account(idl, schema, "Global", GLOBAL_FIELDS)
    curve_blob = encode_account(idl, schema, "BondingCurve", curve_fields)
    if truncate_to is not None:
        curve_blob = curve_blob[:truncate_to]

    source = StaticAccountSource(slot=1)
    source.add_raw(pdas.pumpfun_global(), PUMP.program_id, global_blob)
    address = "So11111111111111111111111111111111111111112"
    source.add_raw(address, PUMP.program_id, curve_blob)

    m = LaunchpadDecoder(source, resolve_mints=False).decode_address(address)
    assert m is not None, name

    return {
        "name": name,
        "note": note,
        "global_hex": global_blob.hex(),
        "curve_hex": curve_blob.hex(),
        "expect": {
            "total_supply": m.total_supply.value,
            "tokens_for_sale": m.tokens_for_sale.value,
            "tokens_sold": m.tokens_sold.value,
            "launch_price": m.launch_price_quote.value,
            "current_price": m.current_price_quote.value,
            "graduation_price": m.graduation_price_quote.value,
            "launch_mcap": m.launch_mcap_quote.value,
            "current_mcap": m.current_mcap_quote.value,
            "graduation_mcap": m.graduation_mcap_quote.value,
            "raise_target": m.raise_target_quote.value,
            "raised": m.raised_quote.value,
            "progress": m.progress.value,
            "complete": bool(m.complete.value),
            "fee_bps": m.fee_bps.value,
            "violations": sorted(
                name for name in VIOLATION_NAMES
                if any(w.startswith(name + ":") for w in m.warnings)
            ),
            "truncated": any("predates the current program layout" in w for w in m.warnings),
            "suspect_field": next(
                (w.split("suspect field: ")[1].split(" --")[0] for w in m.warnings if "suspect field:" in w),
                "",
            ),
        },
    }


def main() -> None:
    paid = 42_500_000_000
    mid_quote = IVS + paid
    mid_base = (IVT * IVS) // mid_quote
    sold = IVT - mid_base

    cases = [
        case(
            "fresh_curve",
            "an untouched pump.fun curve: the canonical 28 SOL open, 85.005 SOL to graduate",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": IVS,
                "real_token_reserves": IRT,
                "real_quote_reserves": 0,
                "token_total_supply": SUPPLY,
            },
        ),
        case(
            "mid_curve",
            "half the raise paid in",
            {
                "virtual_token_reserves": mid_base,
                "virtual_quote_reserves": mid_quote,
                "real_token_reserves": IRT - sold,
                "real_quote_reserves": paid,
                "token_total_supply": SUPPLY,
            },
        ),
        case(
            "completed_curve",
            "curve drained, awaiting migration to PumpSwap",
            {
                "virtual_token_reserves": IVT - IRT,
                "virtual_quote_reserves": IVS + 85_005_359_057,
                "real_token_reserves": 0,
                "real_quote_reserves": 85_005_359_057,
                "token_total_supply": SUPPLY,
                "complete": True,
            },
        ),
        case(
            "field_mixing_anomaly",
            "the row a peer session measured in production: vBase*vQuote is 61% of k, "
            "so the two reserves did not come from one read",
            {
                "virtual_token_reserves": 1_077_887_039_606_396,
                "virtual_quote_reserves": 18_204_928_211,
                "real_token_reserves": IRT,
                "real_quote_reserves": 1,
                "token_total_supply": SUPPLY,
            },
        ),
        case(
            "legacy_truncated_account",
            "an account written before `creator` was appended; the prefix still decodes",
            {
                "virtual_token_reserves": IVT,
                "virtual_quote_reserves": IVS,
                "real_token_reserves": IRT,
                "real_quote_reserves": 0,
                "token_total_supply": SUPPLY,
            },
            truncate_to=8 + 8 * 5 + 1,
        ),
    ]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"pumpfun": cases}, indent=2) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)}: {len(cases)} pump.fun vectors")
    for c in cases:
        e = c["expect"]
        print(
            f"  {c['name']:26} launch={e['launch_price']:.6e} "
            f"mcap={e['current_mcap']:>10.2f} target={e['raise_target']} "
            f"violations={e['violations']}"
        )


if __name__ == "__main__":
    main()
