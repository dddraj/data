"""Program upgrades, on-chain IDLs, and the self-healing parameter path.

This is the part that matters when a launchpad redeploys: the decoder must
notice, throw away what it cached, relearn the layout, and say what changed.
"""

from __future__ import annotations

import json
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder.anchor_idl import account_discriminator, compile_idl  # noqa: E402
from launchpad_decoder.base58 import b58decode  # noqa: E402
from launchpad_decoder.program_state import (  # noqa: E402
    BPF_LOADER_2,
    BPF_LOADER_UPGRADEABLE,
    PROGRAMDATA_HEADER_LEN,
    ProgramWatcher,
    fetch_onchain_idl,
    idl_address,
    parse_program_account,
    parse_programdata_account,
    programdata_address,
    read_deployment,
)
from launchpad_decoder.registry import get as get_spec  # noqa: E402
from launchpad_decoder.types import ValueSource  # noqa: E402
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
AUTHORITY = "2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN"


def program_account_blob(pd_address: str) -> bytes:
    return (2).to_bytes(4, "little") + b58decode(pd_address)


def programdata_blob(slot: int, authority: str | None, elf: bytes = b"\x7fELF") -> bytes:
    blob = (3).to_bytes(4, "little") + slot.to_bytes(8, "little")
    blob += b"\x01" + b58decode(authority) if authority else b"\x00" + bytes(32)
    assert len(blob) == PROGRAMDATA_HEADER_LEN
    return blob + elf


def deployed_source(slot: int = 100, elf: bytes = b"\x7fELF", authority=AUTHORITY):
    pd = programdata_address(PUMP)
    source = StaticAccountSource(slot=slot)
    source.add_raw(PUMP, BPF_LOADER_UPGRADEABLE, program_account_blob(pd))
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(slot, authority, elf))
    return source, pd


# --------------------------------------------------------------------------
# upgradeable loader parsing
# --------------------------------------------------------------------------


def test_parse_program_and_programdata_accounts():
    pd = programdata_address(PUMP)
    assert parse_program_account(program_account_blob(pd)) == pd
    assert parse_programdata_account(programdata_blob(4242, AUTHORITY)) == (4242, AUTHORITY)
    assert parse_programdata_account(programdata_blob(7, None)) == (7, None)
    # wrong enum tag / truncated data must not be mistaken for a deployment
    assert parse_program_account(b"\x00" * 64) is None
    assert parse_programdata_account(b"\x00" * 64) is None
    assert parse_programdata_account(b"\x03") is None


def test_read_deployment_reports_slot_authority_and_hash():
    source, pd = deployed_source(slot=250, elf=b"\x7fELFcafe")
    deployment = read_deployment(source, PUMP, hash_executable=True)
    assert deployment.upgradeable is True
    assert deployment.programdata_address == pd
    assert deployment.last_deploy_slot == 250
    assert deployment.upgrade_authority == AUTHORITY
    assert deployment.executable_len == len(b"\x7fELFcafe")
    assert len(deployment.executable_hash) == 64
    assert deployment.immutable is False


def test_revoked_authority_is_reported_as_immutable():
    source, _ = deployed_source(authority=None)
    assert read_deployment(source, PUMP).immutable is True


def test_non_upgradeable_loader_is_immutable():
    source = StaticAccountSource()
    source.add_raw(PUMP, BPF_LOADER_2, b"\x7fELF")
    deployment = read_deployment(source, PUMP)
    assert deployment.upgradeable is False
    assert deployment.immutable is True
    assert deployment.last_deploy_slot is None


# --------------------------------------------------------------------------
# the watcher
# --------------------------------------------------------------------------


def test_watcher_reports_a_redeploy_and_nothing_else():
    source, pd = deployed_source(slot=100)
    watcher = ProgramWatcher(source, [PUMP], hash_executable=True)

    assert watcher.poll() == []  # first poll only establishes a baseline
    assert watcher.poll() == []  # unchanged

    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(180, AUTHORITY, b"\x7fELFv2"))
    upgrades = watcher.poll()
    assert len(upgrades) == 1
    assert upgrades[0].previous.last_deploy_slot == 100
    assert upgrades[0].current.last_deploy_slot == 180
    assert "redeployed" in upgrades[0].describe()
    assert watcher.poll() == []  # the new state is now the baseline


def test_watcher_detects_a_rebuild_at_the_same_slot_via_the_elf_hash():
    source, pd = deployed_source(slot=100, elf=b"\x7fELFaaa")
    watcher = ProgramWatcher(source, [PUMP], hash_executable=True)
    watcher.poll()
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(100, AUTHORITY, b"\x7fELFbbb"))
    assert len(watcher.poll()) == 1


def test_watcher_survives_an_unreachable_program():
    watcher = ProgramWatcher(StaticAccountSource(), [PUMP])
    assert watcher.poll() == []
    assert PUMP in watcher.unreachable


def test_programdata_addresses_are_what_you_subscribe_to():
    source, pd = deployed_source()
    watcher = ProgramWatcher(source, [PUMP])
    assert watcher.programdata_addresses()[PUMP] == pd


# --------------------------------------------------------------------------
# on-chain IDL
# --------------------------------------------------------------------------


def idl_account_blob(document: dict, authority: str = AUTHORITY) -> bytes:
    payload = zlib.compress(json.dumps(document).encode())
    return (
        account_discriminator("IdlAccount")
        + b58decode(authority)
        + len(payload).to_bytes(4, "little")
        + payload
    )


NEW_PAD_IDL = {
    "address": "boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4",
    "metadata": {"name": "new_pad", "version": "0.3.0"},
    "accounts": [{"name": "BondingCurve"}],
    "types": [
        {
            "name": "BondingCurve",
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


def test_fetch_onchain_idl_inflates_and_compiles():
    program = NEW_PAD_IDL["address"]
    source = StaticAccountSource(slot=5)
    source.add_raw(idl_address(program), program, idl_account_blob(NEW_PAD_IDL))

    onchain = fetch_onchain_idl(source, program)
    assert onchain is not None
    assert onchain.authority == AUTHORITY
    schema = onchain.compile()
    assert schema.name == "new_pad"
    assert "BondingCurve" in schema.accounts
    assert schema.source.startswith("onchain:")


def test_missing_idl_account_is_not_an_error():
    assert fetch_onchain_idl(StaticAccountSource(), PUMP) is None


def test_uncompressed_idl_payload_is_still_readable():
    program = NEW_PAD_IDL["address"]
    payload = json.dumps(NEW_PAD_IDL).encode()
    blob = (
        account_discriminator("IdlAccount")
        + b58decode(AUTHORITY)
        + len(payload).to_bytes(4, "little")
        + payload
    )
    source = StaticAccountSource()
    source.add_raw(idl_address(program), program, blob)
    assert fetch_onchain_idl(source, program).document["metadata"]["name"] == "new_pad"


def test_onchain_idl_beats_the_bundled_snapshot():
    """A layout published on chain is the one that is used."""
    patched = json.loads((IDL_DIR / "pump.json").read_text())
    patched["metadata"] = {"name": "pump", "version": "9.9.9"}
    source, _ = deployed_source()
    source.add_raw(idl_address(PUMP), PUMP, idl_account_blob(patched))

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    schema = decoder.schema(PUMP)
    assert schema.version == "9.9.9"
    assert schema.source.startswith("onchain:")
    # ...and the bundled one is still there as a fallback
    assert decoder.bundled_schema(PUMP).source.startswith("bundled:")


def test_heuristic_adapter_prices_a_launchpad_it_has_never_seen():
    """A brand-new program, decoded purely from its on-chain IDL."""
    spec = get_spec("boop")
    assert spec.requires_onchain_idl

    source = StaticAccountSource(slot=9)
    source.add_raw(idl_address(spec.program_id), spec.program_id, idl_account_blob(NEW_PAD_IDL))

    schema = compile_idl(NEW_PAD_IDL)
    curve = encode_account(
        NEW_PAD_IDL,
        schema,
        "BondingCurve",
        {
            "virtual_token_reserves": 1_073_000_000_000_000,
            "virtual_sol_reserves": 30_000_000_000,
            "real_token_reserves": 793_100_000_000_000,
            "real_sol_reserves": 0,
            "token_total_supply": 1_000_000_000_000_000,
            "initial_real_token_reserves": 793_100_000_000_000,
            "mint": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG",
            "complete": False,
        },
    )
    address = "So11111111111111111111111111111111111111112"
    source.add_raw(address, spec.program_id, curve)

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder._mint_decimals["MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG"] = 6
    m = decoder.decode_address(address)

    assert m is not None
    assert m.launchpad == "boop"
    # same shape as pump.fun, so the same numbers fall out with no adapter
    assert m.current_price_quote.value == pytest.approx(2.7958993e-8, rel=1e-6)
    assert m.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)
    assert m.total_supply.value == pytest.approx(1_000_000_000)
    # virtual reserves + supply + mint + a completion flag, but no explicit
    # raise-target field (it is reconstructed), so short of a perfect score
    assert m.raw_state["_heuristic_confidence"] == pytest.approx(0.8)
    assert m.raw_state["_heuristic_field_map"]["virtual_quote"] == "virtual_sol_reserves"
    assert any("heuristically" in w for w in m.warnings)


def test_heuristic_adapter_admits_when_it_cannot_recognise_a_curve():
    from launchpad_decoder.adapters import map_fields

    mapping = map_fields({"foo": 1, "bar": "baz", "some_counter": 7})
    assert mapping.confidence == 0.0


# --------------------------------------------------------------------------
# self-healing: caches drop and parameters get re-read on an upgrade
# --------------------------------------------------------------------------


def pump_global_blob(initial_virtual_sol: int) -> bytes:
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    return encode_account(
        idl,
        schema,
        "Global",
        {
            "initial_virtual_token_reserves": 1_073_000_000_000_000,
            "initial_virtual_sol_reserves": initial_virtual_sol,
            "initial_real_token_reserves": 793_100_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
            "fee_basis_points": 100,
        },
    )


def test_refresh_reports_a_launch_parameter_change_after_an_upgrade():
    source, pd = deployed_source(slot=100)
    global_address = pdas.pumpfun_global()
    source.add_raw(global_address, PUMP, pump_global_blob(30_000_000_000))

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    before = decoder.static_params("pumpfun")
    assert (
        before["configs"]["global"]["values"]["initial_virtual_sol_reserves"]
        == 30_000_000_000
    )
    decoder.snapshot_static_params(["pumpfun"])
    assert not decoder.refresh()  # nothing has happened yet

    # the program is redeployed and the launch parameters change with it
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(500, AUTHORITY, b"\x7fELFv2"))
    source.add_raw(global_address, PUMP, pump_global_blob(42_000_000_000))

    report = decoder.refresh()
    assert len(report.upgrades) == 1
    paths = {c.path: (c.before, c.after) for c in report.changes}
    assert (
        paths["configs.global.values.initial_virtual_sol_reserves"]
        == (30_000_000_000, 42_000_000_000)
    )
    assert "initial_virtual_sol_reserves" in report.describe()


def test_an_upgrade_changes_the_prices_the_decoder_reports():
    source, pd = deployed_source(slot=100)
    source.add_raw(pdas.pumpfun_global(), PUMP, pump_global_blob(30_000_000_000))

    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    curve = encode_account(
        idl,
        schema,
        "BondingCurve",
        {
            "virtual_token_reserves": 1_073_000_000_000_000,
            "virtual_quote_reserves": 30_000_000_000,
            "real_token_reserves": 793_100_000_000_000,
            "token_total_supply": 1_000_000_000_000_000,
        },
    )
    address = "So11111111111111111111111111111111111111112"
    source.add_raw(address, PUMP, curve)

    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder.snapshot_static_params(["pumpfun"])
    first = decoder.decode_address(address)
    assert first.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)

    # pump.fun doubles the seeded SOL reserve and redeploys
    source.add_raw(pd, BPF_LOADER_UPGRADEABLE, programdata_blob(500, AUTHORITY, b"\x7fELFv2"))
    source.add_raw(pdas.pumpfun_global(), PUMP, pump_global_blob(60_000_000_000))

    assert decoder.refresh()

    # A curve created AFTER the upgrade is seeded with the new reserve, and
    # that is the one whose numbers double.
    fresh_address = "So11111111111111111111111111111111111111113"
    source.add_raw(
        fresh_address,
        PUMP,
        encode_account(
            idl,
            schema,
            "BondingCurve",
            {
                "virtual_token_reserves": 1_073_000_000_000_000,
                "virtual_quote_reserves": 60_000_000_000,
                "real_token_reserves": 793_100_000_000_000,
                "token_total_supply": 1_000_000_000_000_000,
            },
        ),
    )
    second = decoder.decode_address(fresh_address)
    assert second.raise_target_quote.value == pytest.approx(170.010718, rel=1e-6)
    assert second.launch_price_quote.value == pytest.approx(
        2 * first.launch_price_quote.value, rel=1e-9
    )
    assert second.launch_price_quote.source is ValueSource.ONCHAIN_CONFIG

    # But the curve that already existed keeps the opening it was seeded with.
    # A curve's opening is baked in at creation, so repricing it against the
    # program's new default would be wrong -- and reporting it as broken for
    # not matching would be worse.
    unchanged = decoder.decode_address(address)
    assert unchanged.raise_target_quote.value == pytest.approx(85.005359, rel=1e-6)
    assert unchanged.launch_price_quote.value == pytest.approx(
        first.launch_price_quote.value, rel=1e-9
    )
    assert not [w for w in unchanged.warnings if "k_violation" in w]


def test_config_cache_is_not_dropped_without_an_upgrade():
    source, _ = deployed_source(slot=100)
    source.add_raw(pdas.pumpfun_global(), PUMP, pump_global_blob(30_000_000_000))
    decoder = LaunchpadDecoder(source, resolve_mints=False)
    decoder.static_params("pumpfun")

    # A config edit with no redeploy is deliberately not picked up by the cache
    # until it is invalidated -- callers who need per-slot freshness should
    # subscribe to the config account itself.
    source.add_raw(pdas.pumpfun_global(), PUMP, pump_global_blob(99_000_000_000))
    again = decoder.static_params("pumpfun")
    assert again["configs"]["global"]["values"]["initial_virtual_sol_reserves"] == 30_000_000_000

    decoder.configs.invalidate(PUMP)
    fresh = decoder.static_params("pumpfun")
    assert fresh["configs"]["global"]["values"]["initial_virtual_sol_reserves"] == 99_000_000_000


def test_onchain_idl_status_lists_which_programs_publish_one():
    source, _ = deployed_source()
    source.add_raw(idl_address(PUMP), PUMP, idl_account_blob(NEW_PAD_IDL))
    status = LaunchpadDecoder(source).onchain_idl_status()
    assert status["pumpfun"].startswith("present")
    assert status["meteora_dbc"] == "absent"
