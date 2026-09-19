"""The subscription half of a hot reload.

Hot-swapping a layout is only half the job. Most streams compose their
subscription once, at connect time -- a Geyser SubscribeRequest, a websocket
accountSubscribe -- so an account whose owner is not already in that request
never arrives however good the decoder is. Swapping the layout under a stream
that has gone quiet fixes nothing, and it fails silently, which is the worst
way for it to fail.

So these check that the filter set is recomputed and the movement reported,
for the two things that move it: a program nobody had catalogued being learned,
and a redeploy that renames an account struct.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource  # noqa: E402
from launchpad_decoder.anchor_idl import account_discriminator  # noqa: E402
from launchpad_decoder.base58 import b58decode  # noqa: E402
from launchpad_decoder.program_state import (  # noqa: E402
    BPF_LOADER_UPGRADEABLE,
    idl_address,
    programdata_address,
)
from tests.test_program_state import (  # noqa: E402
    AUTHORITY,
    program_account_blob,
    programdata_blob,
)


def load_example():
    spec = importlib.util.spec_from_file_location(
        "live_node_example", ROOT / "scripts" / "live_node_example.py"
    )
    module = importlib.util.module_from_spec(spec)
    # Register before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which is absent for a module exec'd in
    # place.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


live_node = load_example()

#: a deterministic, valid pubkey that is certainly not in the registry --
#: using a registered id here would test nothing, since its filter is
#: already in the subscription
NEW_PROGRAM = "7eCh8pZejtgEyKzHPCob3zj2Q93hVDHiBHnwgTR53VN6"


def idl_blob(document: dict) -> bytes:
    payload = zlib.compress(json.dumps(document).encode())
    return (
        account_discriminator("IdlAccount")
        + b58decode(AUTHORITY)
        + len(payload).to_bytes(4, "little")
        + payload
    )


def idl_document(account_name: str) -> dict:
    return {
        "address": NEW_PROGRAM,
        "metadata": {"name": "new_pad", "version": "0.3.0"},
        "accounts": [{"name": account_name}],
        "types": [
            {
                "name": account_name,
                "type": {
                    "kind": "struct",
                    "fields": [
                        {"name": "virtual_token_reserves", "type": "u64"},
                        {"name": "virtual_sol_reserves", "type": "u64"},
                        {"name": "real_token_reserves", "type": "u64"},
                        {"name": "real_sol_reserves", "type": "u64"},
                        {"name": "token_total_supply", "type": "u64"},
                        {"name": "initial_real_token_reserves", "type": "u64"},
                        {"name": "mint", "type": "pubkey"},
                        {"name": "complete", "type": "bool"},
                    ],
                },
            }
        ],
    }


def curve_blob(account_name: str) -> bytes:
    body = b"".join(
        value.to_bytes(8, "little")
        for value in (
            1_073_000_000_000_000,
            30_000_000_000,
            793_100_000_000_000,
            0,
            1_000_000_000_000_000,
            793_100_000_000_000,
        )
    )
    return account_discriminator(account_name) + body + bytes(32) + b"\x00"


def deployed(source: StaticAccountSource, program: str, slot: int, elf: bytes = b"\x7fELF"):
    pd = programdata_address(program)
    source.add_raw(program, BPF_LOADER_UPGRADEABLE, program_account_blob(pd))
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(slot, AUTHORITY, elf))
    return pd


def test_learning_a_program_moves_the_subscription():
    """The case that matters most: a launchpad nobody catalogued.

    Its accounts reach us only because one happened to be in the stream
    already. Adopting its layout without widening the subscription means the
    rest of its curves never arrive.
    """
    source = StaticAccountSource(slot=5)
    deployed(source, NEW_PROGRAM, slot=5)
    source.add_raw(idl_address(NEW_PROGRAM), NEW_PROGRAM, idl_blob(idl_document("BondingCurve")))

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    livedec = live_node.LiveDecoder(decoder)

    before = livedec.filters()
    assert not any(entry["owner"] == NEW_PROGRAM for entry in before)
    assert livedec.pending_resubscribe is None

    address = "So11111111111111111111111111111111111111112"
    metrics = livedec.on_account_update(NEW_PROGRAM, address, curve_blob("BondingCurve"), 6)
    assert metrics is not None, "the unknown program should have been learned"

    change = livedec.pending_resubscribe
    assert change is not None, "learning a program must move the subscription"
    assert any(entry["owner"] == NEW_PROGRAM for entry in change.added)
    assert any(entry["owner"] == NEW_PROGRAM for entry in livedec.filters())
    assert NEW_PROGRAM in change.reason


def test_a_renamed_account_moves_the_discriminator_and_the_filter():
    """A redeploy that renames an account struct.

    The discriminator is sha256("account:<Name>")[:8], so a rename moves it.
    The old memcmp then matches nothing and the stream goes quiet without any
    error at all -- the filter set has to be re-diffed on every upgrade.
    """
    source = StaticAccountSource(slot=5)
    pd = deployed(source, NEW_PROGRAM, slot=5)
    source.add_raw(idl_address(NEW_PROGRAM), NEW_PROGRAM, idl_blob(idl_document("BondingCurve")))

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder.learn_program(NEW_PROGRAM)
    livedec = live_node.LiveDecoder(decoder)
    old = [e for e in livedec.filters() if e["owner"] == NEW_PROGRAM]
    assert len(old) == 1

    # redeploy, renaming BondingCurve -> Curve
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(99, AUTHORITY, b"\x7fELFv2"))
    source.add_raw(idl_address(NEW_PROGRAM), NEW_PROGRAM, idl_blob(idl_document("Curve")))

    livedec.on_program_upgrade(NEW_PROGRAM)
    change = livedec.pending_resubscribe
    assert change is not None, "a renamed account must move the subscription"
    assert [e["account_type"] for e in change.added] == ["Curve"]
    assert [e["account_type"] for e in change.removed] == ["BondingCurve"]
    # and the new discriminator really is different
    assert change.added[0]["memcmp_base58"] != old[0]["memcmp_base58"]


def test_a_quiet_upgrade_does_not_churn_the_subscription():
    """A redeploy that does not touch the layout must not ask for a resubscribe."""
    source = StaticAccountSource(slot=5)
    pd = deployed(source, NEW_PROGRAM, slot=5)
    source.add_raw(idl_address(NEW_PROGRAM), NEW_PROGRAM, idl_blob(idl_document("BondingCurve")))

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder.learn_program(NEW_PROGRAM)
    livedec = live_node.LiveDecoder(decoder)

    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(99, AUTHORITY, b"\x7fELFv2"))
    livedec.on_program_upgrade(NEW_PROGRAM)
    assert livedec.pending_resubscribe is None


def test_filters_carry_what_a_subscription_needs():
    decoder = LaunchpadDecoder(None, resolve_mints=False)
    for entry in live_node.curve_account_filters(decoder):
        assert entry["owner"]
        if entry.get("memcmp_base58"):
            assert entry["memcmp_offset"] == 0
            assert entry["account_type"]
