"""`verify_against_node` -- the first-contact checklist for a real cluster.

The interesting cases are the ones where a bundled snapshot has drifted from
the program, since that is the failure a snapshot always risks and the one
that silently mis-decodes.
"""

from __future__ import annotations

import copy
import json
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import LaunchpadDecoder, StaticAccountSource, pdas  # noqa: E402
from launchpad_decoder.anchor_idl import account_discriminator  # noqa: E402
from launchpad_decoder.base58 import b58decode  # noqa: E402
from launchpad_decoder.discovery import verify_against_node  # noqa: E402
from launchpad_decoder.program_state import (  # noqa: E402
    BPF_LOADER_UPGRADEABLE,
    PROGRAMDATA_HEADER_LEN,
    idl_address,
    programdata_address,
)
from launchpad_decoder.registry import LaunchpadSpec, get as get_spec  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"
PUMP = get_spec("pumpfun")
AUTHORITY = "2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN"


def idl_blob(document: dict) -> bytes:
    payload = zlib.compress(json.dumps(document).encode())
    return (
        account_discriminator("IdlAccount")
        + b58decode(AUTHORITY)
        + len(payload).to_bytes(4, "little")
        + payload
    )


def deployed(program_id: str, slot: int = 100) -> StaticAccountSource:
    source = StaticAccountSource(slot=slot)
    source.add_raw(
        program_id,
        BPF_LOADER_UPGRADEABLE,
        (2).to_bytes(4, "little") + b58decode(programdata_address(program_id)),
        executable=True,
    )
    header = (
        (3).to_bytes(4, "little") + slot.to_bytes(8, "little") + b"\x01" + b58decode(AUTHORITY)
    )
    assert len(header) == PROGRAMDATA_HEADER_LEN
    source.add_raw(programdata_address(program_id), BPF_LOADER_UPGRADEABLE, header + b"\x7fELF")
    return source


def pump_only(source) -> LaunchpadDecoder:
    """A decoder narrowed to pump.fun, so checks stay readable."""
    return LaunchpadDecoder(source, launchpads=[PUMP], resolve_mints=False)


def results(checks):
    return {c.name: c for c in checks}


def test_missing_program_is_reported_not_raised():
    checks = verify_against_node(pump_only(StaticAccountSource()))
    assert results(checks)["program exists"].ok is False
    assert "not found" in results(checks)["program exists"].detail


def test_non_executable_account_stops_the_checks_early():
    source = StaticAccountSource()
    source.add_raw(PUMP.program_id, BPF_LOADER_UPGRADEABLE, b"\x00" * 36, executable=False)
    checks = results(verify_against_node(pump_only(source)))
    assert checks["program exists"].ok
    assert checks["program is executable"].ok is False
    assert "deployment readable" not in checks


def test_a_healthy_program_passes_every_check():
    source = deployed(PUMP.program_id)
    bundled = json.loads((IDL_DIR / "pump.json").read_text())
    source.add_raw(idl_address(PUMP.program_id), PUMP.program_id, idl_blob(bundled))

    # the Global config has to decode too
    from launchpad_decoder.anchor_idl import compile_idl
    from tests.fixtures import encode_account

    schema = compile_idl(bundled)
    source.add_raw(
        pdas.pumpfun_global(),
        PUMP.program_id,
        encode_account(bundled, schema, "Global", {"token_total_supply": 1}),
    )

    checks = results(verify_against_node(pump_only(source)))
    assert all(c.ok for c in checks.values()), {k: v.detail for k, v in checks.items() if not v.ok}
    assert "no breaking drift" in checks["bundled layout still decodes this program"].detail
    assert "authority" in checks["deployment readable"].detail


def test_an_appended_field_is_reported_as_safe():
    """Anchor programs append fields; the bundled prefix still decodes."""
    source = deployed(PUMP.program_id)
    document = copy.deepcopy(json.loads((IDL_DIR / "pump.json").read_text()))
    for node in document["types"]:
        if node["name"] == "BondingCurve":
            node["type"]["fields"].append({"name": "brand_new_flag", "type": "bool"})
    source.add_raw(idl_address(PUMP.program_id), PUMP.program_id, idl_blob(document))

    checks = results(verify_against_node(pump_only(source)))
    # the layout check still passes: the bundled prefix decodes correctly
    assert checks["bundled layout still decodes this program"].ok is True
    stale = checks["bundled snapshot is current"]
    assert stale.ok is False
    assert stale.severity == "warning"
    assert stale.blocking is False
    assert "appended" in stale.detail


def test_a_reordered_field_is_reported_as_a_mis_decode():
    """Field order drift is the dangerous case -- it decodes to wrong numbers."""
    source = deployed(PUMP.program_id)
    document = copy.deepcopy(json.loads((IDL_DIR / "pump.json").read_text()))
    for node in document["types"]:
        if node["name"] == "BondingCurve":
            fields = node["type"]["fields"]
            fields[0], fields[1] = fields[1], fields[0]
    source.add_raw(idl_address(PUMP.program_id), PUMP.program_id, idl_blob(document))

    check = results(verify_against_node(pump_only(source)))[
        "bundled layout still decodes this program"
    ]
    assert check.ok is False
    assert check.blocking is True
    assert "mis-decode" in check.detail


def test_a_changed_discriminator_is_caught():
    source = deployed(PUMP.program_id)
    document = copy.deepcopy(json.loads((IDL_DIR / "pump.json").read_text()))
    for entry in document["accounts"]:
        if entry["name"] == "BondingCurve":
            entry["discriminator"] = [9, 9, 9, 9, 9, 9, 9, 9]
    source.add_raw(idl_address(PUMP.program_id), PUMP.program_id, idl_blob(document))

    check = results(verify_against_node(pump_only(source)))[
        "bundled layout still decodes this program"
    ]
    assert check.ok is False
    assert check.blocking is True
    assert "discriminator changed" in check.detail


def test_absent_onchain_idl_is_ok_when_a_snapshot_exists():
    source = deployed(PUMP.program_id)
    check = results(verify_against_node(pump_only(source)))["on-chain IDL"]
    assert check.ok is True
    assert "only layout source" in check.detail


def test_absent_onchain_idl_fails_when_there_is_no_snapshot_either():
    program = get_spec("boop").program_id
    source = deployed(program)
    decoder = LaunchpadDecoder(source, launchpads=[get_spec("boop")], resolve_mints=False)
    check = results(verify_against_node(decoder))["on-chain IDL"]
    assert check.ok is False
    assert check.blocking is True  # nothing can decode this program at all
    assert "no bundled snapshot" in check.detail


def test_unreadable_config_account_is_flagged():
    source = deployed(PUMP.program_id)
    checks = results(verify_against_node(pump_only(source)))
    assert checks["config global decodes"].ok is False
    assert "unreadable" in checks["config global decodes"].detail


def test_sample_decode_reports_a_node_that_refuses_get_program_accounts():
    class Refusing(StaticAccountSource):
        def get_program_accounts(self, program_id, **kwargs):
            raise RuntimeError("410 Gone: getProgramAccounts is disabled")

    source = Refusing(slot=1)
    for pubkey, owner, data in [
        (PUMP.program_id, BPF_LOADER_UPGRADEABLE, (2).to_bytes(4, "little") + b58decode(programdata_address(PUMP.program_id))),
    ]:
        source.add_raw(pubkey, owner, data, executable=True)
    header = (3).to_bytes(4, "little") + (1).to_bytes(8, "little") + b"\x01" + b58decode(AUTHORITY)
    source.add_raw(programdata_address(PUMP.program_id), BPF_LOADER_UPGRADEABLE, header + b"\x7fELF")

    check = results(verify_against_node(pump_only(source), sample=True))["sample curve decodes"]
    assert check.ok is False
    assert "refused" in check.detail


def test_checks_are_serialisable_for_ci():
    checks = verify_against_node(pump_only(deployed(PUMP.program_id)))
    payload = [c.as_dict() for c in checks]
    assert json.dumps(payload)
    assert {"launchpad", "check", "ok", "severity", "detail"} == set(payload[0])


def test_verify_needs_a_node():
    with pytest.raises(RuntimeError, match="AccountSource"):
        verify_against_node(LaunchpadDecoder(None))


def test_every_registered_launchpad_is_covered():
    """The checklist must not silently skip a launchpad."""
    source = StaticAccountSource()
    checks = verify_against_node(LaunchpadDecoder(source, resolve_mints=False))
    covered = {c.launchpad for c in checks}
    assert covered == {spec.key for spec in LaunchpadDecoder(None).by_key.values()}
    assert all(isinstance(spec, LaunchpadSpec) for spec in LaunchpadDecoder(None).by_key.values())
