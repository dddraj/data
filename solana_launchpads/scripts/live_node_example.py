#!/usr/bin/env python3
"""Wiring the decoder into a live node feed.

The decoder never fetches on its own during the hot path: you hand it bytes,
it hands you metrics.  That makes it drop-in for whatever account stream your
node already produces -- Yellowstone/Geyser gRPC, a Geyser plugin, a
`accountSubscribe` websocket, or a ledger replay.

Three things have to be wired up:

1. **The account filter.**  Subscribe to accounts owned by the launchpad
   program ids, optionally narrowed to the curve account's 8-byte
   discriminator (`curve_account_filters()` below emits both).
2. **The decode call.**  `decode_account_data(owner, data, address, slot)`.
   Returns `None` for accounts that are not curve state, so it is safe to feed
   it every update from the program.
3. **The upgrade watch.**  Also subscribe to each program's ProgramData
   account.  When one of those updates, call `decoder.refresh()`; cached IDLs
   and config accounts are dropped and the parameter diff is returned so you
   can log exactly which launch constant moved.

Run this file directly for a simulated feed that needs no node:

    python scripts/live_node_example.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from launchpad_decoder import (  # noqa: E402
    LaunchMetrics,
    LaunchpadDecoder,
    StaticAccountSource,
)
from launchpad_decoder.base58 import b58encode  # noqa: E402


def curve_account_filters(decoder: LaunchpadDecoder) -> List[Dict[str, object]]:
    """Subscription filters: one per (program, curve account type).

    `memcmp_base58` is the account discriminator, so the node only sends you
    curve state rather than every account the program owns.
    """
    filters: List[Dict[str, object]] = []
    for program_id, spec in decoder.specs.items():
        schema = decoder.schema(program_id)
        if schema is None:
            filters.append({"launchpad": spec.key, "owner": program_id, "memcmp_base58": None})
            continue
        names = [spec.state_account] if spec.state_account else list(schema.accounts)
        for name in names:
            account = schema.accounts.get(name)
            if account is None:
                continue
            filters.append(
                {
                    "launchpad": spec.key,
                    "owner": program_id,
                    "account_type": name,
                    "memcmp_offset": 0,
                    "memcmp_base58": b58encode(account.discriminator),
                }
            )
    return filters


def programdata_filters(decoder: LaunchpadDecoder) -> Dict[str, str]:
    """ProgramData addresses to subscribe to, keyed by program id.

    An update on any of these means the launchpad redeployed.
    """
    return decoder.watcher.programdata_addresses()


class LiveDecoder:
    """A thin event loop you can drop your stream into."""

    def __init__(self, decoder: LaunchpadDecoder) -> None:
        self.decoder = decoder
        self.programdata = {
            address: program_id
            for program_id, address in programdata_filters(decoder).items()
        }
        if decoder.source is not None:
            decoder.snapshot_static_params()

    def on_account_update(
        self, owner: str, address: str, data: bytes, slot: int
    ) -> Optional[LaunchMetrics]:
        """Feed every account update here. Returns metrics, or None."""
        if address in self.programdata:
            self.on_program_upgrade(self.programdata[address])
            return None
        return self.decoder.decode_account_data(owner, data, address, slot)

    def on_program_upgrade(self, program_id: str) -> None:
        report = self.decoder.refresh()
        if not report:
            return
        for upgrade in report.upgrades:
            print(f"[upgrade] {upgrade.describe()}")
        for change in report.changes:
            print(f"[param]   {change.describe()}")


# --------------------------------------------------------------------------
# a runnable simulation, so the wiring can be checked without a node
# --------------------------------------------------------------------------


def _simulated_feed() -> Iterable[Tuple[str, str, bytes, int]]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
    from fixtures import encode_account  # type: ignore

    from launchpad_decoder.anchor_idl import compile_idl
    from launchpad_decoder.registry import get as get_spec

    spec = get_spec("pumpfun")
    idl = json.loads(
        (Path(__file__).resolve().parents[1] / "launchpad_decoder/idl/pump.json").read_text()
    )
    schema = compile_idl(idl)
    ivt, ivs = 1_073_000_000_000_000, 30_000_000_000
    for step, paid in enumerate([0, 10_000_000_000, 42_500_000_000, 85_005_359_057]):
        v_quote = ivs + paid
        v_base = (ivt * ivs) // v_quote
        sold = ivt - v_base
        blob = encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": v_base,
                "virtual_quote_reserves": v_quote,
                "real_token_reserves": max(0, 793_100_000_000_000 - sold),
                "real_quote_reserves": paid,
                "token_total_supply": 1_000_000_000_000_000,
                "complete": paid >= 85_005_359_057,
            },
        )
        yield spec.program_id, "So11111111111111111111111111111111111111112", blob, 1000 + step


def main() -> None:
    decoder = LaunchpadDecoder(StaticAccountSource(), resolve_mints=False)
    live = LiveDecoder(decoder)

    print("subscription filters your node needs:")
    for entry in curve_account_filters(decoder)[:4]:
        print("   ", json.dumps(entry))
    print(f"    ... {len(curve_account_filters(decoder))} filters in total")
    print("\nProgramData accounts to watch for redeploys:")
    for program_id, address in list(programdata_filters(decoder).items())[:3]:
        print(f"    {program_id} -> {address}")

    print("\nsimulated pump.fun curve updates:")
    print(f"  {'slot':>6} {'price (SOL)':>16} {'mcap (SOL)':>12} {'raised':>10} {'progress':>9}")
    for owner, address, data, slot in _simulated_feed():
        metrics = live.on_account_update(owner, address, data, slot)
        if metrics is None:
            continue
        print(
            f"  {slot:>6} {metrics.current_price_quote.value:>16.10f} "
            f"{metrics.current_mcap_quote.value:>12.2f} "
            f"{metrics.raised_quote.value:>10.3f} "
            f"{metrics.progress.value:>9.1%}"
        )


if __name__ == "__main__":
    main()
