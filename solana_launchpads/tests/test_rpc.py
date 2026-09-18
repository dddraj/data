"""The JSON-RPC client.

This is the component that runs first against a real node, so the request
shapes and response parsing are pinned here rather than discovered in
production.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from launchpad_decoder import rpc  # noqa: E402
from launchpad_decoder.rpc import JsonRpcAccountSource, RpcError  # noqa: E402

ENDPOINT = "https://node.invalid"
OWNER = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


class FakeTransport:
    """Stands in for urlopen; records requests and replays canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.sleeps = []

    def __call__(self, request, timeout=None):
        self.requests.append(json.loads(request.data.decode()))
        item = self.responses.pop(0) if self.responses else {"result": None}
        if isinstance(item, Exception):
            raise item
        body = json.dumps(item).encode()

        class _Response(io.BytesIO):
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

        return _Response(body)


@pytest.fixture
def transport(monkeypatch):
    def install(responses):
        fake = FakeTransport(responses)
        monkeypatch.setattr(rpc.urllib.request, "urlopen", fake)
        monkeypatch.setattr(rpc.time, "sleep", lambda s: fake.sleeps.append(s))
        return fake

    return install


def account_value(data: bytes, owner: str = OWNER, executable: bool = False) -> dict:
    return {
        "data": [base64.b64encode(data).decode(), "base64"],
        "owner": owner,
        "lamports": 1_000_000,
        "executable": executable,
        "rentEpoch": 0,
    }


# --------------------------------------------------------------------------
# request shape
# --------------------------------------------------------------------------


def test_get_account_sends_a_well_formed_request_and_parses_the_result(transport):
    fake = transport(
        [{"result": {"context": {"slot": 4242}, "value": account_value(b"hello")}}]
    )
    source = JsonRpcAccountSource(ENDPOINT, commitment="finalized")
    account = source.get_account("So11111111111111111111111111111111111111112")

    sent = fake.requests[0]
    assert sent["jsonrpc"] == "2.0"
    assert sent["method"] == "getAccountInfo"
    assert sent["params"][0] == "So11111111111111111111111111111111111111112"
    assert sent["params"][1] == {"encoding": "base64", "commitment": "finalized"}

    assert account.data == b"hello"
    assert account.owner == OWNER
    assert account.slot == 4242
    assert account.size == 5


def test_missing_account_is_none_not_an_error(transport):
    transport([{"result": {"context": {"slot": 1}, "value": None}}])
    assert JsonRpcAccountSource(ENDPOINT).get_account("So11111111111111111111111111111111111111112") is None


def test_request_ids_increment(transport):
    fake = transport([{"result": {"context": {}, "value": None}}] * 2)
    source = JsonRpcAccountSource(ENDPOINT)
    source.get_account("a")
    source.get_account("b")
    assert [r["id"] for r in fake.requests] == [1, 2]


def test_get_multiple_accounts_batches_at_the_rpc_limit(transport):
    """The RPC caps getMultipleAccounts at 100 keys, so 250 is three calls."""
    fake = transport(
        [
            {"result": {"context": {"slot": 7}, "value": [None] * 100}},
            {"result": {"context": {"slot": 7}, "value": [None] * 100}},
            {"result": {"context": {"slot": 7}, "value": [None] * 50}},
        ]
    )
    keys = [f"key{i}" for i in range(250)]
    out = JsonRpcAccountSource(ENDPOINT).get_multiple_accounts(keys)
    assert len(out) == 250
    assert len(fake.requests) == 3
    assert [len(r["params"][0]) for r in fake.requests] == [100, 100, 50]


def test_get_program_accounts_builds_discriminator_and_mint_filters(transport):
    fake = transport(
        [
            {
                "result": {
                    "context": {"slot": 99},
                    "value": [{"pubkey": "curve1", "account": account_value(b"\x01\x02")}],
                }
            }
        ]
    )
    source = JsonRpcAccountSource(ENDPOINT)
    found = source.get_program_accounts(
        OWNER, data_size=150, memcmp=[(0, "4y6pru6YvC7"), (83, "SomeMint")]
    )

    config = fake.requests[0]["params"][1]
    assert config["encoding"] == "base64"
    assert config["withContext"] is True
    assert config["filters"] == [
        {"dataSize": 150},
        {"memcmp": {"offset": 0, "bytes": "4y6pru6YvC7", "encoding": "base58"}},
        {"memcmp": {"offset": 83, "bytes": "SomeMint", "encoding": "base58"}},
    ]
    assert [a.pubkey for a in found] == ["curve1"]
    assert found[0].slot == 99


def test_get_program_accounts_handles_a_node_that_ignores_with_context(transport):
    """Older nodes return a bare list rather than {context, value}."""
    transport([{"result": [{"pubkey": "curve1", "account": account_value(b"x")}]}])
    found = JsonRpcAccountSource(ENDPOINT).get_program_accounts(OWNER)
    assert [a.pubkey for a in found] == ["curve1"]
    assert found[0].slot == 0


def test_get_program_accounts_limit_is_client_side(transport):
    transport(
        [
            {
                "result": {
                    "context": {"slot": 1},
                    "value": [
                        {"pubkey": f"c{i}", "account": account_value(b"x")} for i in range(5)
                    ],
                }
            }
        ]
    )
    found = JsonRpcAccountSource(ENDPOINT).get_program_accounts(OWNER, limit=2)
    assert len(found) == 2


def test_token_helpers_unwrap_the_context_envelope(transport):
    fake = transport(
        [
            {"result": {"context": {"slot": 1}, "value": {"amount": "1000", "decimals": 6}}},
            {"result": {"context": {"slot": 1}, "value": {"amount": "55", "decimals": 9}}},
        ]
    )
    source = JsonRpcAccountSource(ENDPOINT)
    assert source.get_token_supply("mint")["decimals"] == 6
    assert source.get_token_account_balance("vault")["amount"] == "55"
    assert [r["method"] for r in fake.requests] == [
        "getTokenSupply",
        "getTokenAccountBalance",
    ]


def test_get_slot_passes_the_commitment(transport):
    fake = transport([{"result": 123456}])
    assert JsonRpcAccountSource(ENDPOINT, commitment="processed").get_slot() == 123456
    assert fake.requests[0]["params"] == [{"commitment": "processed"}]


# --------------------------------------------------------------------------
# failure handling
# --------------------------------------------------------------------------


def test_rpc_error_payload_is_surfaced_not_swallowed(transport):
    transport([{"error": {"code": -32602, "message": "Invalid param"}}])
    with pytest.raises(RpcError, match="Invalid param"):
        JsonRpcAccountSource(ENDPOINT).get_slot()


def test_a_response_with_no_result_is_an_error(transport):
    transport([{"jsonrpc": "2.0", "id": 1}])
    with pytest.raises(RpcError, match="no result"):
        JsonRpcAccountSource(ENDPOINT).get_slot()


def test_transient_failures_are_retried_with_backoff(transport):
    fake = transport(
        [
            urllib.error.URLError("connection reset"),
            urllib.error.URLError("connection reset"),
            {"result": 7},
        ]
    )
    assert JsonRpcAccountSource(ENDPOINT).get_slot() == 7
    assert fake.sleeps == [1, 2]  # 2^0, 2^1


def test_rate_limits_and_server_errors_are_retried(transport):
    fake = transport(
        [
            urllib.error.HTTPError(ENDPOINT, 429, "Too Many Requests", {}, None),
            urllib.error.HTTPError(ENDPOINT, 503, "Unavailable", {}, None),
            {"result": 7},
        ]
    )
    assert JsonRpcAccountSource(ENDPOINT).get_slot() == 7
    assert len(fake.sleeps) == 2


def test_a_bad_request_fails_immediately_instead_of_burning_backoff(transport):
    """A 400 will fail identically every time; retrying it just wastes 14s."""
    fake = transport([urllib.error.HTTPError(ENDPOINT, 400, "Bad Request", {}, None)])
    with pytest.raises(RpcError, match="Bad Request"):
        JsonRpcAccountSource(ENDPOINT).get_slot()
    assert fake.sleeps == []
    assert len(fake.requests) == 1


def test_retries_give_up_after_max_retries(transport):
    fake = transport([urllib.error.URLError("down")] * 4)
    with pytest.raises(RpcError, match="down"):
        JsonRpcAccountSource(ENDPOINT, max_retries=4).get_slot()
    assert len(fake.requests) == 4


def test_custom_headers_are_sent(transport):
    fake = transport([{"result": 1}])
    source = JsonRpcAccountSource(ENDPOINT, headers={"x-api-key": "secret"})
    source.get_slot()
    assert source.headers["x-api-key"] == "secret"
    assert source.headers["Content-Type"] == "application/json"
    assert len(fake.requests) == 1


def test_json_parsed_data_yields_no_bytes_rather_than_crashing(transport):
    transport(
        [
            {
                "result": {
                    "context": {"slot": 1},
                    "value": {
                        "data": {"parsed": {}, "program": "spl-token"},
                        "owner": OWNER,
                        "lamports": 5,
                    },
                }
            }
        ]
    )
    account = JsonRpcAccountSource(ENDPOINT).get_account("x")
    assert account.data == b""
