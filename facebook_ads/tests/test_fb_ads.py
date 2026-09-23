"""Request shapes sent to the Graph API, and the MCP protocol surface.

Nothing here touches the network: a fake opener records each request and
replays canned Graph responses.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fb_ads.graph import GraphClient, GraphError, normalize_account_id  # noqa: E402
from fb_ads.server import McpServer, build_from_env  # noqa: E402
from fb_ads.tools import AdsTools  # noqa: E402

TOKEN = "EAAtesttoken"


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        body = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        return _Response(json.dumps(body).encode())

    def sent(self, i=-1):
        req = self.requests[i]
        parsed = urllib.parse.urlsplit(req.full_url)
        if req.data:
            fields = dict(urllib.parse.parse_qsl(req.data.decode()))
        else:
            fields = dict(urllib.parse.parse_qsl(parsed.query))
        return req.get_method(), parsed.path, fields


def http_error(code, error):
    return urllib.error.HTTPError(
        "https://graph.facebook.com", code, "err", {}, io.BytesIO(json.dumps({"error": error}).encode())
    )


def make(*responses, **kw):
    opener = FakeOpener(*responses)
    client = GraphClient(TOKEN, opener=opener, sleep=lambda s: None, app_secret=kw.pop("app_secret", None))
    return AdsTools(client, **kw), opener


def test_normalize_account_id():
    assert normalize_account_id("123") == "act_123"
    assert normalize_account_id(" act_123 ") == "act_123"


def test_list_campaigns_follows_paging_without_duplicating_token():
    nxt = f"https://graph.facebook.com/v24.0/act_1/campaigns?access_token={TOKEN}&after=abc&limit=2"
    tools, opener = make(
        {"data": [{"id": "1"}, {"id": "2"}], "paging": {"next": nxt}},
        {"data": [{"id": "3"}]},
    )
    rows = tools.call("list_campaigns", {"account_id": "1", "effective_status": ["ACTIVE"]})
    assert [r["id"] for r in rows] == ["1", "2", "3"]

    method, path, fields = opener.sent(0)
    assert (method, path) == ("GET", "/v24.0/act_1/campaigns")
    assert json.loads(fields["effective_status"]) == ["ACTIVE"]
    assert fields["access_token"] == TOKEN

    second = opener.requests[1].full_url
    assert second.count("access_token=") == 1 and "after=abc" in second


def test_insights_level_adds_name_fields_and_time_range():
    tools, opener = make({"data": []})
    tools.call(
        "get_insights",
        {"object_id": "act_1", "level": "campaign", "since": "2026-09-01", "until": "2026-09-07",
         "breakdowns": ["age", "gender"]},
    )
    _, path, fields = opener.sent()
    assert path == "/v24.0/act_1/insights"
    assert fields["fields"].startswith("campaign_id,campaign_name,spend")
    assert json.loads(fields["time_range"]) == {"since": "2026-09-01", "until": "2026-09-07"}
    assert fields["breakdowns"] == "age,gender"
    assert "date_preset" not in fields


def test_insights_rejects_half_a_range():
    tools, _ = make()
    with pytest.raises(GraphError):
        tools.call("get_insights", {"object_id": "act_1", "since": "2026-09-01"})


def test_create_campaign_is_paused_and_sets_budget_sharing_flag():
    tools, opener = make({"id": "c1"})
    assert tools.call(
        "create_campaign", {"account_id": "9", "name": "Diwali", "objective": "OUTCOME_SALES"}
    ) == {"id": "c1"}
    method, path, fields = opener.sent()
    assert (method, path) == ("POST", "/v24.0/act_9/campaigns")
    assert fields["status"] == "PAUSED"
    assert fields["special_ad_categories"] == "[]"
    assert fields["is_adset_budget_sharing_enabled"] == "false"
    assert "daily_budget" not in fields


def test_cbo_campaign_omits_budget_sharing_flag():
    tools, opener = make({"id": "c1"})
    tools.call("create_campaign", {"account_id": "9", "name": "x", "objective": "OUTCOME_TRAFFIC", "daily_budget": 50000})
    _, _, fields = opener.sent()
    assert fields["daily_budget"] == "50000"
    assert "is_adset_budget_sharing_enabled" not in fields


def test_create_ad_set_encodes_targeting_as_json():
    tools, opener = make({"id": "s1"})
    targeting = {"geo_locations": {"countries": ["IN"]}, "age_min": 18}
    tools.call(
        "create_ad_set",
        {"account_id": "act_9", "campaign_id": "c1", "name": "IN 18+", "optimization_goal": "LINK_CLICKS",
         "targeting": targeting, "daily_budget": 20000},
    )
    _, path, fields = opener.sent()
    assert path == "/v24.0/act_9/adsets"
    assert json.loads(fields["targeting"]) == targeting
    assert fields["status"] == "PAUSED" and fields["billing_event"] == "IMPRESSIONS"


def test_budget_cap_blocks_before_any_request():
    tools, opener = make(max_daily_budget=10000)
    with pytest.raises(GraphError, match="FB_ADS_MAX_DAILY_BUDGET"):
        tools.call("update_budget", {"object_id": "s1", "daily_budget": 10001})
    assert opener.requests == []


def test_update_budget_needs_exactly_one_budget():
    tools, _ = make()
    with pytest.raises(GraphError):
        tools.call("update_budget", {"object_id": "s1"})
    with pytest.raises(GraphError):
        tools.call("update_budget", {"object_id": "s1", "daily_budget": 1, "lifetime_budget": 2})


def test_duplicate_copy_is_paused():
    tools, opener = make({"copied_campaign_id": "c2"})
    tools.call("duplicate", {"object_id": "c1", "deep_copy": True})
    _, path, fields = opener.sent()
    assert path == "/v24.0/c1/copies"
    assert fields["status_option"] == "PAUSED" and fields["deep_copy"] == "true"


def test_read_only_hides_write_tools():
    tools, _ = make(read_only=True)
    names = {t.name for t in tools.list()}
    assert "get_insights" in names
    assert not names & {"set_status", "update_budget", "create_campaign", "duplicate"}


def test_appsecret_proof_is_sent():
    tools, opener = make({"data": []}, app_secret="shh")
    tools.call("list_ad_accounts", {})
    assert len(opener.sent()[2]["appsecret_proof"]) == 64


def test_graph_error_is_readable_and_redacts_token():
    err = http_error(400, {"message": f"Invalid token {TOKEN}", "code": 190, "error_subcode": 463})
    tools, _ = make(err)
    with pytest.raises(GraphError) as info:
        tools.call("list_ad_accounts", {})
    assert TOKEN not in str(info.value)
    assert "subcode 463" in str(info.value) and info.value.code == 190


def test_rate_limit_is_retried_but_bad_request_is_not():
    limited = http_error(400, {"message": "too many calls", "code": 17})
    tools, opener = make(limited, {"data": [{"id": "act_1"}]})
    assert tools.call("list_ad_accounts", {}) == [{"id": "act_1"}]
    assert len(opener.requests) == 2

    tools, opener = make(http_error(400, {"message": "bad param", "code": 100}))
    with pytest.raises(GraphError):
        tools.call("list_ad_accounts", {})
    assert len(opener.requests) == 1


# -- MCP protocol ---------------------------------------------------------


def run(server, *messages):
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in messages)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_mcp_handshake_list_and_call():
    tools, _ = make({"data": [{"id": "act_1", "currency": "INR"}]})
    replies = run(
        McpServer(tools),
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_ad_accounts", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "nope"},
    )
    assert [r["id"] for r in replies] == [1, 2, 3, 4]
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    by_name = {t["name"]: t for t in replies[1]["result"]["tools"]}
    assert by_name["set_status"]["annotations"]["readOnlyHint"] is False
    assert by_name["get_insights"]["annotations"]["readOnlyHint"] is True
    assert json.loads(replies[2]["result"]["content"][0]["text"])[0]["currency"] == "INR"
    assert replies[3]["error"]["code"] == -32601


def test_tool_errors_come_back_as_is_error_results():
    tools, _ = make(http_error(400, {"message": "Invalid parameter", "code": 100}))
    (reply,) = run(
        McpServer(tools),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "set_status", "arguments": {"object_id": "c1", "status": "ACTIVE"}}},
    )
    assert reply["result"]["isError"] is True
    assert "Invalid parameter" in reply["result"]["content"][0]["text"]


def test_bad_arguments_are_is_error_not_crash():
    tools, _ = make()
    (reply,) = run(
        McpServer(tools),
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "set_status", "arguments": {}}},
    )
    assert reply["result"]["isError"] is True


def test_build_from_env():
    server = build_from_env({"META_ACCESS_TOKEN": TOKEN, "FB_ADS_READ_ONLY": "1", "META_API_VERSION": "v25.0"})
    assert server.tools.read_only and server.tools.client.base.endswith("/v25.0")
    with pytest.raises(GraphError):
        build_from_env({})
