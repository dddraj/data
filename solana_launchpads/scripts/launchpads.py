#!/usr/bin/env python3
"""CLI for the Solana launchpad decoder.

    # no node needed
    python scripts/launchpads.py list
    python scripts/launchpads.py math pumpfun

    # against your own node
    export SOLANA_RPC=https://your-node:8899
    python scripts/launchpads.py params pumpfun
    python scripts/launchpads.py decode <curve or pool address>
    python scripts/launchpads.py platforms meteora_dbc
    python scripts/launchpads.py idl-status
    python scripts/launchpads.py watch --interval 60
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_decoder import (  # noqa: E402
    LAUNCHPADS,
    JsonRpcAccountSource,
    LaunchpadDecoder,
)
from launchpad_decoder.adapters.pumpfun import GLOBAL_SNAPSHOT  # noqa: E402
from launchpad_decoder.registry import VERIFIED_PLATFORMS, get as get_spec  # noqa: E402


def make_decoder(args) -> LaunchpadDecoder:
    endpoint = args.rpc or os.environ.get("SOLANA_RPC")
    source = JsonRpcAccountSource(endpoint, commitment=args.commitment) if endpoint else None
    if source is None and args.needs_rpc:
        raise SystemExit(
            "this command needs a node: pass --rpc or set SOLANA_RPC "
            "(any JSON-RPC endpoint; your own validator works best)"
        )
    return LaunchpadDecoder(source, prefer_onchain_idl=not args.no_onchain_idl)


def fmt(value, places: int = 10) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value and (abs(value) < 1e-4 or abs(value) >= 1e9):
            return f"{value:.{places}e}"
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return str(value)


# --------------------------------------------------------------------------


def cmd_list(args) -> None:
    print(f"{'key':20} {'curve family':26} {'state account':20} program id")
    print("-" * 110)
    for spec in LAUNCHPADS:
        print(
            f"{spec.key:20} {spec.curve_family.value:26} "
            f"{(spec.state_account or '(from on-chain IDL)'):20} {spec.program_id}"
        )
    print()
    print("Multi-tenant programs -- the platforms below are config accounts, not programs:")
    for platform in VERIFIED_PLATFORMS:
        print(f"  {platform.name:20} {platform.launchpad_key:20} {platform.config_address}")
    print("  (run `platforms <launchpad>` against a node for the full, current list)")


def cmd_math(args) -> None:
    """Show the launch numbers a launchpad applies, with no node involved."""
    spec = get_spec(args.launchpad)
    print(f"{spec.display_name}  [{spec.program_id}]")
    print(f"  curve family : {spec.curve_family.value}")
    print(f"  state account: {spec.state_account or '(discovered from the on-chain IDL)'}")
    print(f"  graduates to : {spec.graduates_to or '-'}")
    if spec.notes:
        print(f"  notes        : {spec.notes}")
    if spec.key != "pumpfun":
        print("\n  Launch parameters are per-pool or per-config; use `params` with a node.")
        return

    from launchpad_decoder import curves
    from launchpad_decoder.types import price_raw_to_ui, ui_amount

    g = GLOBAL_SNAPSHOT
    ivt, ivs, irt, tts = (
        g["initial_virtual_token_reserves"],
        g["initial_virtual_sol_reserves"],
        g["initial_real_token_reserves"],
        g["token_total_supply"],
    )
    supply = ui_amount(tts, 6)
    launch = price_raw_to_ui(curves.cp_price_raw(ivs, ivt), 6, 9)
    target = curves.cp_raise_for_supply(ivs, ivt, irt)
    final = price_raw_to_ui(curves.cp_final_price_raw(ivs, ivt, irt), 6, 9)
    print("\n  From the bundled Global snapshot (run `params pumpfun` to use live values):")
    print(f"    total supply        : {supply:,.0f}")
    print(f"    launch price        : {launch:.10e} SOL")
    print(f"    launch market cap   : {launch * supply:,.3f} SOL")
    print(f"    raise target        : {ui_amount(target, 9):,.6f} SOL")
    print(f"    graduation price    : {final:.10e} SOL")
    print(f"    graduation mkt cap  : {final * supply:,.2f} SOL")


def cmd_params(args) -> None:
    decoder = make_decoder(args)
    keys = [args.launchpad] if args.launchpad else list(decoder.by_key)
    print(json.dumps(decoder.snapshot_static_params(keys), indent=2, default=str))


def cmd_decode(args) -> None:
    decoder = make_decoder(args)
    metrics = decoder.decode_address(args.address)
    if metrics is None:
        raise SystemExit(f"{args.address} is not a curve account of any known launchpad")
    if args.json:
        print(json.dumps(metrics.as_dict(include_raw=args.raw), indent=2, default=str))
        return

    print(f"{metrics.launchpad}  {metrics.curve_account_type}  {metrics.curve_address}")
    print(f"  base mint    : {metrics.base_mint}  ({metrics.base_decimals} dp)")
    print(f"  quote mint   : {metrics.quote_mint}  ({metrics.quote_decimals} dp)")
    print(f"  curve        : {metrics.curve_family.value} / {metrics.curve_type}")
    print(f"  schema       : {metrics.schema_source}")
    print()
    rows = [
        ("total supply", metrics.total_supply),
        ("tokens for sale", metrics.tokens_for_sale),
        ("tokens sold", metrics.tokens_sold),
        ("launch price", metrics.launch_price_quote),
        ("current price", metrics.current_price_quote),
        ("graduation price", metrics.graduation_price_quote),
        ("launch mcap", metrics.launch_mcap_quote),
        ("current mcap", metrics.current_mcap_quote),
        ("graduation mcap", metrics.graduation_mcap_quote),
        ("raise target", metrics.raise_target_quote),
        ("raised", metrics.raised_quote),
        ("progress", metrics.progress),
        ("complete", metrics.complete),
        ("migrated", metrics.migrated),
        ("fee (bps)", metrics.fee_bps),
    ]
    for label, param in rows:
        print(f"  {label:18} {fmt(param.value):>24}   [{param.source.value}] {param.note}")
    for warning in metrics.warnings:
        print(f"  ! {warning}")


def cmd_platforms(args) -> None:
    decoder = make_decoder(args)
    platforms = decoder.discover_platforms(args.launchpad, limit=args.limit)
    if args.json:
        print(json.dumps([p.as_dict() for p in platforms], indent=2, default=str))
        return
    print(f"{len(platforms)} platform config account(s) on {args.launchpad}:")
    for platform in platforms:
        label = platform.name or "(unnamed)"
        print(f"  {label:28} {platform.config_address}  {platform.config_account_type}")
        for key, value in platform.fields.items():
            print(f"      {key:32} {value}")


def cmd_scan(args) -> None:
    decoder = make_decoder(args)
    for metrics in decoder.scan_program(args.launchpad, limit=args.limit):
        print(
            json.dumps(
                {
                    "curve": metrics.curve_address,
                    "mint": metrics.base_mint,
                    "price": metrics.current_price_quote.value,
                    "mcap": metrics.current_mcap_quote.value,
                    "target": metrics.raise_target_quote.value,
                    "progress": metrics.progress.value,
                },
                default=str,
            )
        )


def cmd_idl_status(args) -> None:
    decoder = make_decoder(args)
    for key, status in sorted(decoder.onchain_idl_status().items()):
        print(f"  {key:22} {status}")


def cmd_deployments(args) -> None:
    decoder = make_decoder(args)
    from launchpad_decoder.program_state import summarize_deployment

    for program_id, deployment in decoder.deployments().items():
        spec = decoder.specs[program_id]
        print(f"{spec.key}:")
        for key, value in summarize_deployment(deployment).items():
            print(f"    {key:22} {value}")


def cmd_watch(args) -> None:
    decoder = make_decoder(args)
    decoder.snapshot_static_params()
    print(f"baseline taken for {len(decoder.by_key)} programs; polling every {args.interval}s")
    while True:
        time.sleep(args.interval)
        report = decoder.refresh()
        if report:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            print(f"[{stamp}] {report.describe()}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--rpc", help="JSON-RPC endpoint (default: $SOLANA_RPC)")
    parser.add_argument("--commitment", default="confirmed")
    parser.add_argument(
        "--no-onchain-idl",
        action="store_true",
        help="use only the bundled IDL snapshots, never the program's own IDL account",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", help="show the launchpad registry")
    p.set_defaults(func=cmd_list, needs_rpc=False)

    p = sub.add_parser("math", help="show a launchpad's curve parameters offline")
    p.add_argument("launchpad")
    p.set_defaults(func=cmd_math, needs_rpc=False)

    p = sub.add_parser("params", help="read live launch parameters, with provenance")
    p.add_argument("launchpad", nargs="?")
    p.set_defaults(func=cmd_params, needs_rpc=True)

    p = sub.add_parser("decode", help="decode one curve / pool account")
    p.add_argument("address")
    p.add_argument("--json", action="store_true")
    p.add_argument("--raw", action="store_true", help="include the decoded account state")
    p.set_defaults(func=cmd_decode, needs_rpc=True)

    p = sub.add_parser("platforms", help="list the tenants of a multi-tenant program")
    p.add_argument("launchpad")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_platforms, needs_rpc=True)

    p = sub.add_parser("scan", help="decode curve accounts of one program")
    p.add_argument("launchpad")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_scan, needs_rpc=True)

    p = sub.add_parser("idl-status", help="which programs publish an IDL on chain")
    p.set_defaults(func=cmd_idl_status, needs_rpc=True)

    p = sub.add_parser("deployments", help="deployment slot / upgrade authority per program")
    p.set_defaults(func=cmd_deployments, needs_rpc=True)

    p = sub.add_parser("watch", help="poll for program upgrades and parameter changes")
    p.add_argument("--interval", type=int, default=60)
    p.set_defaults(func=cmd_watch, needs_rpc=True)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
