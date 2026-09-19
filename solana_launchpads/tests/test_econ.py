"""The curve-economics map, checked against the real layouts.

A table of field names is worthless if the names are wrong, and prose cannot
be tested. So every field `econ.py` names is looked up in the bundled IDL for
that program: if a launchpad renames a field, or the map is written from
memory, this fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import econ  # noqa: E402
from launchpad_decoder.registry import LAUNCHPADS, get as get_spec  # noqa: E402

FIELD_ATTRS = (
    "raised_field",
    "target_field",
    "tokens_for_sale_field",
    "initial_quote_field",
    "initial_base_field",
)


def field_names(schema, account_name):
    """Every field name on one account of a compiled schema."""
    return {name for name, _decoder in schema.account(account_name).layout.fields}


def test_every_named_field_exists_on_the_account_it_names():
    for row in econ.ECON:
        if row.scope == "none":
            continue
        spec = get_spec(row.launchpad)
        schema = spec.bundled_schema()
        assert schema is not None, f"{row.launchpad} has no bundled IDL"

        # The raise target lives on `source_account`; what the curve has raised
        # is always on the per-launch state account.
        state_fields = field_names(schema, spec.state_account)
        config_fields = field_names(schema, row.source_account)

        for attr in FIELD_ATTRS:
            name = getattr(row, attr)
            if name is None:
                continue
            where = state_fields if attr == "raised_field" else config_fields | state_fields
            assert name in where, (
                f"{row.launchpad}.{attr} = {name!r} is not a field of "
                f"{row.source_account} or {spec.state_account}"
            )


def test_scopes_are_all_known():
    for row in econ.ECON:
        assert row.scope in econ.SCOPES, f"{row.launchpad}: bad scope {row.scope}"


def test_program_ids_match_the_registry():
    by_key = {spec.key: spec.program_id for spec in LAUNCHPADS}
    for row in econ.ECON:
        assert by_key[row.launchpad] == row.program_id, row.launchpad


def test_pumpfun_is_the_only_per_program_launchpad():
    """The finding that decides the table's shape.

    A warehouse keying curve economics on program_id gets exactly one launchpad
    right. Everything else is per-config or per-pool, so the same key would
    hand every LetsBonk coin the same raise target as every Cook.meme coin.
    """
    per_program = [row.launchpad for row in econ.ECON if row.scope == "program"]
    assert per_program == ["pumpfun"]


def test_pool_scoped_launchpads_need_no_table_at_all():
    """Their numbers are on the row an indexer already decodes."""
    pool_scoped = {row.launchpad for row in econ.ECON if row.scope == "pool"}
    assert pool_scoped == {"raydium_launchlab", "moonit", "gofundmeme"}
    for row in econ.ECON:
        if row.scope == "pool":
            assert not row.needs_a_table


def test_every_row_either_divides_two_fields_or_says_it_cannot():
    """The distinction that stops a progress bar being confidently wrong.

    Moonit stores neither a raised amount nor a raise target -- only tokens
    still on the curve -- so any two of its fields divided together give a
    number that means nothing. A row that cannot be divided must say so and
    must be marked derived.
    """
    for row in econ.ECON:
        if not row.has_progress:
            continue
        if row.progress_is_stored:
            assert not row.target_is_derived or row.launchpad == "pumpfun"
        else:
            assert row.target_is_derived, row.launchpad
            assert row.note, row.launchpad


def test_launchpads_without_a_graduation_report_no_progress():
    for row in econ.ECON:
        if row.scope == "none":
            assert not row.has_progress
            assert row.target_field is None
            assert row.note, f"{row.launchpad} must say why it has no progress"


def test_lookup_by_program_and_by_key_agree():
    for row in econ.ECON:
        assert econ.for_program(row.program_id) is row
        assert econ.for_launchpad(row.launchpad) is row
    assert econ.for_program("11111111111111111111111111111111") is None
