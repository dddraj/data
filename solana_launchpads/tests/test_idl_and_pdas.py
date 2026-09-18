"""IDL compilation, discriminators and PDA derivation."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import pdas  # noqa: E402
from launchpad_decoder.anchor_idl import account_discriminator, compile_idl  # noqa: E402
from launchpad_decoder.base58 import b58decode, b58encode  # noqa: E402
from launchpad_decoder.pubkey import find_program_address, is_on_curve  # noqa: E402
from launchpad_decoder.registry import LAUNCHPADS  # noqa: E402
from tests.fixtures import encode_account  # noqa: E402

IDL_DIR = ROOT / "launchpad_decoder" / "idl"


def test_base58_round_trip():
    assert b58encode(bytes(32)) == "11111111111111111111111111111111"
    for text in (
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "So11111111111111111111111111111111111111112",
    ):
        assert b58encode(b58decode(text)) == text
        assert len(b58decode(text)) == 32


def test_registry_program_ids_are_valid_pubkeys():
    for spec in LAUNCHPADS:
        assert len(b58decode(spec.program_id)) == 32, spec.key


@pytest.mark.parametrize("path", sorted(IDL_DIR.glob("*.json")), ids=lambda p: p.name)
def test_every_bundled_idl_compiles(path):
    schema = compile_idl(json.loads(path.read_text()), source="test")
    assert schema.accounts, f"{path.name} produced no decodable accounts"
    for account in schema.accounts.values():
        assert len(account.discriminator) == 8
        assert account.layout.fields


def test_registry_state_accounts_exist_in_their_bundled_idl():
    for spec in LAUNCHPADS:
        if spec.requires_onchain_idl or not spec.idl_file or not spec.state_account:
            continue
        schema = spec.bundled_schema()
        assert schema is not None, spec.key
        assert spec.state_account in schema.accounts, (spec.key, spec.state_account)


def test_anchor_discriminator_matches_published_values():
    # Anchor derives account discriminators as sha256("account:<Name>")[:8];
    # these are the values the programs actually publish in their IDLs.
    assert list(account_discriminator("BondingCurve")) == [23, 183, 248, 55, 96, 216, 172, 96]
    assert list(account_discriminator("Global")) == [167, 232, 232, 177, 200, 108, 114, 127]
    assert list(account_discriminator("PoolState")) == [247, 237, 227, 245, 215, 195, 222, 70]
    assert list(account_discriminator("VirtualPool")) == [213, 224, 5, 209, 98, 69, 119, 92]
    assert list(account_discriminator("PoolConfig")) == [26, 108, 14, 123, 116, 230, 129, 43]
    assert list(account_discriminator("CurveAccount")) == [8, 91, 83, 28, 132, 216, 248, 22]


def test_legacy_idl_accounts_get_derived_discriminators():
    # Heaven ships a pre-0.30 IDL with no discriminators in the document.
    schema = compile_idl(json.loads((IDL_DIR / "heaven_amm.json").read_text()))
    account = schema.account("liquidityPoolState")
    assert account.discriminator == hashlib.sha256(b"account:liquidityPoolState").digest()[:8]
    # ...and camelCase fields are normalised.
    names = [name for name, _ in account.layout.fields]
    assert "base_token_vault_balance" in names
    assert "curr_price" in names


def test_pda_derivation_matches_published_addresses():
    # pump.fun's Global account, documented in pump-fun/pump-public-docs
    assert pdas.pumpfun_global() == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    # LaunchLab's default constant-product config and LetsBonk's platform
    # config, both taken from the letsbonkdotfun-sdk constants
    assert pdas.launchlab_global_config() == "6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX"
    assert (
        pdas.launchlab_platform_config("2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN")
        == "FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1"
    )


def test_find_program_address_returns_off_curve_keys():
    address, bump = find_program_address([b"global"], pdas.PUMPFUN)
    assert 0 <= bump <= 255
    assert not is_on_curve(b58decode(address))


def test_on_curve_detection():
    # A real wallet pubkey is on the curve; a PDA is not.
    assert is_on_curve(b58decode("2P56vRWDrCBGkqYXxgSWAnuZQZrJPySRQGToTJThpmkN"))
    assert not is_on_curve(b58decode(pdas.pumpfun_global()))


def test_encoder_decoder_round_trip_on_every_bundled_account():
    """The fixture encoder and the IDL decoder must agree, field for field."""
    for path in sorted(IDL_DIR.glob("*.json")):
        idl = json.loads(path.read_text())
        schema = compile_idl(idl, source="test")
        for name, account in schema.accounts.items():
            blob = encode_account(idl, schema, name, {})
            decoded = account.decode(blob, strict=True)
            assert set(decoded) == {f for f, _ in account.layout.fields}, (path.name, name)


def test_truncated_accounts_decode_their_prefix():
    """Anchor programs append fields; old accounts must still decode."""
    idl = json.loads((IDL_DIR / "pump.json").read_text())
    schema = compile_idl(idl)
    full = encode_account(idl, schema, "BondingCurve", {"virtual_token_reserves": 7})
    legacy = full[: 8 + 8 * 5 + 1 + 32]  # the original pre-`is_mayhem_mode` layout
    decoded = schema.account("BondingCurve").decode(legacy)
    assert decoded["virtual_token_reserves"] == 7
    assert "is_mayhem_mode" in decoded["_truncated_fields"]


def test_unknown_discriminator_is_not_misidentified():
    schema = compile_idl(json.loads((IDL_DIR / "pump.json").read_text()))
    assert schema.identify(b"\x00" * 64) is None
    assert schema.decode_account(b"\x01" * 64) is None
