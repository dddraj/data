"""The Ads Manager operations Claude can call.

Each tool is a JSON-schema'd wrapper over one or two Graph API calls. Anything
that can spend money is guarded:

* objects are always *created* PAUSED -- turning spend on is a separate,
  explicit `set_status` call that Claude Code asks you to approve;
* `FB_ADS_MAX_DAILY_BUDGET` caps any daily budget Claude sets;
* `FB_ADS_READ_ONLY=1` hides every write tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from .graph import GraphClient, GraphError, normalize_account_id

ACCOUNT_FIELDS = "id,name,account_status,currency,timezone_name,amount_spent,balance,spend_cap"
CAMPAIGN_FIELDS = (
    "id,name,status,effective_status,objective,daily_budget,lifetime_budget,"
    "bid_strategy,special_ad_categories,start_time,stop_time,created_time"
)
ADSET_FIELDS = (
    "id,name,status,effective_status,campaign_id,daily_budget,lifetime_budget,"
    "optimization_goal,billing_event,bid_strategy,bid_amount,targeting,start_time,end_time"
)
AD_FIELDS = "id,name,status,effective_status,campaign_id,adset_id,creative{id,name},created_time"
INSIGHT_FIELDS = (
    "spend,impressions,reach,frequency,clicks,ctr,cpc,cpm,"
    "actions,cost_per_action_type,purchase_roas"
)
LEVEL_NAME_FIELDS = {
    "campaign": "campaign_id,campaign_name",
    "adset": "campaign_name,adset_id,adset_name",
    "ad": "campaign_name,adset_name,ad_id,ad_name",
}

OBJECTIVES = [
    "OUTCOME_AWARENESS",
    "OUTCOME_TRAFFIC",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_APP_PROMOTION",
    "OUTCOME_SALES",
]
DATE_PRESETS = [
    "today", "yesterday", "last_3d", "last_7d", "last_14d", "last_28d", "last_30d",
    "last_90d", "this_month", "last_month", "this_quarter", "maximum",
]
EFFECTIVE_STATUSES = [
    "ACTIVE", "PAUSED", "ARCHIVED", "CAMPAIGN_PAUSED", "ADSET_PAUSED",
    "PENDING_REVIEW", "DISAPPROVED", "WITH_ISSUES", "IN_PROCESS",
]
BUDGET_NOTE = (
    "Budgets are in the account currency's smallest unit, exactly as Meta stores "
    "them: 50000 = 500.00 INR/USD (paise/cents). Check `currency` from "
    "list_ad_accounts first."
)


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Any]
    writes: bool = False


def _schema(properties: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_ID = {"type": "string"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100}
_STATUS_FILTER = {
    "type": "array",
    "items": {"enum": EFFECTIVE_STATUSES},
    "description": "Only return objects whose effective_status is one of these.",
}
_BUDGET = {"type": "integer", "minimum": 1}


class AdsTools:
    def __init__(
        self,
        client: GraphClient,
        *,
        read_only: bool = False,
        max_daily_budget: Optional[int] = None,
    ) -> None:
        self.client = client
        self.read_only = read_only
        self.max_daily_budget = max_daily_budget
        self._tools = {t.name: t for t in self._define() if not (read_only and t.writes)}

    # -- registry -------------------------------------------------------
    def list(self) -> List[Tool]:
        return list(self._tools.values())

    def call(self, name: str, arguments: Optional[Dict[str, Any]]) -> Any:
        tool = self._tools.get(name)
        if tool is None:
            raise GraphError(f"unknown tool: {name}")
        return tool.handler(**(arguments or {}))

    def _define(self) -> List[Tool]:
        return [
            Tool(
                "list_ad_accounts",
                "List the ad accounts this token can manage, with currency, timezone and spend.",
                _schema({"limit": _LIMIT}),
                self.list_ad_accounts,
            ),
            Tool(
                "list_campaigns",
                "List campaigns in an ad account.",
                _schema(
                    {"account_id": _ID, "effective_status": _STATUS_FILTER, "limit": _LIMIT},
                    ["account_id"],
                ),
                self.list_campaigns,
            ),
            Tool(
                "list_ad_sets",
                "List ad sets under an ad account (act_...) or a campaign id.",
                _schema(
                    {"parent_id": _ID, "effective_status": _STATUS_FILTER, "limit": _LIMIT},
                    ["parent_id"],
                ),
                self.list_ad_sets,
            ),
            Tool(
                "list_ads",
                "List ads under an ad account (act_...), campaign id or ad set id.",
                _schema(
                    {"parent_id": _ID, "effective_status": _STATUS_FILTER, "limit": _LIMIT},
                    ["parent_id"],
                ),
                self.list_ads,
            ),
            Tool(
                "get_object",
                "Read any Graph object (campaign, ad set, ad, creative, account) by id.",
                _schema(
                    {"object_id": _ID, "fields": {"type": "string", "description": "Comma-separated."}},
                    ["object_id"],
                ),
                self.get_object,
            ),
            Tool(
                "get_insights",
                "Performance report (spend, reach, CTR, CPC, conversions, ROAS...) for an "
                "account, campaign, ad set or ad. Use `level` to break an account/campaign "
                "down per child object, and either `date_preset` or `since`+`until` (YYYY-MM-DD).",
                _schema(
                    {
                        "object_id": _ID,
                        "date_preset": {"enum": DATE_PRESETS, "default": "last_7d"},
                        "since": {"type": "string"},
                        "until": {"type": "string"},
                        "level": {"enum": ["account", "campaign", "adset", "ad"]},
                        "breakdowns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "e.g. age, gender, country, region, publisher_platform, platform_position, device_platform",
                        },
                        "time_increment": {
                            "type": "string",
                            "description": "'1' for daily rows, 'monthly', or 'all_days'.",
                        },
                        "fields": {"type": "string", "description": "Override the default metric list."},
                        "limit": _LIMIT,
                    },
                    ["object_id"],
                ),
                self.get_insights,
            ),
            Tool(
                "set_status",
                "Turn a campaign, ad set or ad on (ACTIVE), off (PAUSED) or archive it. "
                "ACTIVE starts spending money.",
                _schema(
                    {"object_id": _ID, "status": {"enum": ["ACTIVE", "PAUSED", "ARCHIVED"]}},
                    ["object_id", "status"],
                ),
                self.set_status,
                writes=True,
            ),
            Tool(
                "update_budget",
                "Change the daily or lifetime budget of a campaign (CBO) or ad set. " + BUDGET_NOTE,
                _schema(
                    {"object_id": _ID, "daily_budget": _BUDGET, "lifetime_budget": _BUDGET},
                    ["object_id"],
                ),
                self.update_budget,
                writes=True,
            ),
            Tool(
                "rename",
                "Rename a campaign, ad set or ad.",
                _schema({"object_id": _ID, "name": {"type": "string"}}, ["object_id", "name"]),
                self.rename,
                writes=True,
            ),
            Tool(
                "create_campaign",
                "Create a campaign (always PAUSED). Give `daily_budget` or `lifetime_budget` "
                "for Advantage campaign budget; omit both to budget per ad set. " + BUDGET_NOTE,
                _schema(
                    {
                        "account_id": _ID,
                        "name": {"type": "string"},
                        "objective": {"enum": OBJECTIVES},
                        "special_ad_categories": {
                            "type": "array",
                            "items": {"enum": ["CREDIT", "EMPLOYMENT", "HOUSING", "ISSUES_ELECTIONS_POLITICS", "FINANCIAL_PRODUCTS_SERVICES"]},
                            "default": [],
                        },
                        "daily_budget": _BUDGET,
                        "lifetime_budget": _BUDGET,
                        "bid_strategy": {
                            "enum": ["LOWEST_COST_WITHOUT_CAP", "LOWEST_COST_WITH_BID_CAP", "COST_CAP", "LOWEST_COST_WITH_MIN_ROAS"]
                        },
                    },
                    ["account_id", "name", "objective"],
                ),
                self.create_campaign,
                writes=True,
            ),
            Tool(
                "create_ad_set",
                "Create an ad set (always PAUSED). `targeting` is a Meta targeting spec, e.g. "
                '{"geo_locations":{"countries":["IN"]},"age_min":18,"age_max":45}. ' + BUDGET_NOTE,
                _schema(
                    {
                        "account_id": _ID,
                        "campaign_id": _ID,
                        "name": {"type": "string"},
                        "optimization_goal": {
                            "type": "string",
                            "description": "e.g. LINK_CLICKS, LANDING_PAGE_VIEWS, OFFSITE_CONVERSIONS, LEAD_GENERATION, REACH, IMPRESSIONS, THRUPLAY",
                        },
                        "billing_event": {"type": "string", "default": "IMPRESSIONS"},
                        "targeting": {"type": "object"},
                        "daily_budget": _BUDGET,
                        "lifetime_budget": _BUDGET,
                        "bid_amount": _BUDGET,
                        "start_time": {"type": "string", "description": "ISO 8601"},
                        "end_time": {"type": "string", "description": "ISO 8601; required with lifetime_budget"},
                        "promoted_object": {
                            "type": "object",
                            "description": 'e.g. {"pixel_id":"...","custom_event_type":"PURCHASE"} or {"page_id":"..."}',
                        },
                        "destination_type": {"type": "string"},
                    },
                    ["account_id", "campaign_id", "name", "optimization_goal", "targeting"],
                ),
                self.create_ad_set,
                writes=True,
            ),
            Tool(
                "create_ad_creative",
                "Create an ad creative from an object_story_spec, e.g. "
                '{"page_id":"...","link_data":{"link":"https://...","message":"...","image_hash":"...","call_to_action":{"type":"SHOP_NOW"}}}.',
                _schema(
                    {"account_id": _ID, "name": {"type": "string"}, "object_story_spec": {"type": "object"}},
                    ["account_id", "name", "object_story_spec"],
                ),
                self.create_ad_creative,
                writes=True,
            ),
            Tool(
                "create_ad",
                "Create an ad (always PAUSED) in an ad set from an existing creative id.",
                _schema(
                    {"account_id": _ID, "adset_id": _ID, "name": {"type": "string"}, "creative_id": _ID},
                    ["account_id", "adset_id", "name", "creative_id"],
                ),
                self.create_ad,
                writes=True,
            ),
            Tool(
                "duplicate",
                "Duplicate a campaign, ad set or ad (copy is PAUSED). `deep_copy` also copies children.",
                _schema(
                    {
                        "object_id": _ID,
                        "deep_copy": {"type": "boolean", "default": False},
                        "rename_suffix": {"type": "string", "default": " - Copy"},
                    },
                    ["object_id"],
                ),
                self.duplicate,
                writes=True,
            ),
        ]

    # -- guards ---------------------------------------------------------
    def _check_budget(self, daily_budget: Optional[int]) -> None:
        if daily_budget is not None and self.max_daily_budget is not None:
            if daily_budget > self.max_daily_budget:
                raise GraphError(
                    f"daily_budget {daily_budget} exceeds FB_ADS_MAX_DAILY_BUDGET "
                    f"({self.max_daily_budget}); raise the cap to allow it"
                )

    @staticmethod
    def _one_budget(daily_budget: Optional[int], lifetime_budget: Optional[int]) -> None:
        if daily_budget is not None and lifetime_budget is not None:
            raise GraphError("set daily_budget or lifetime_budget, not both")

    @staticmethod
    def _parent(parent_id: str) -> str:
        return normalize_account_id(parent_id) if parent_id.startswith("act_") else parent_id

    # -- reads ----------------------------------------------------------
    def list_ad_accounts(self, limit: int = 100):
        return self.client.get_all("me/adaccounts", fields=ACCOUNT_FIELDS, max_items=limit)

    def list_campaigns(self, account_id: str, effective_status=None, limit: int = 100):
        return self.client.get_all(
            f"{normalize_account_id(account_id)}/campaigns",
            fields=CAMPAIGN_FIELDS,
            effective_status=effective_status,
            max_items=limit,
        )

    def list_ad_sets(self, parent_id: str, effective_status=None, limit: int = 100):
        return self.client.get_all(
            f"{self._parent(parent_id)}/adsets",
            fields=ADSET_FIELDS,
            effective_status=effective_status,
            max_items=limit,
        )

    def list_ads(self, parent_id: str, effective_status=None, limit: int = 100):
        return self.client.get_all(
            f"{self._parent(parent_id)}/ads",
            fields=AD_FIELDS,
            effective_status=effective_status,
            max_items=limit,
        )

    def get_object(self, object_id: str, fields: Optional[str] = None):
        return self.client.get(self._parent(object_id), fields=fields)

    def get_insights(
        self,
        object_id: str,
        date_preset: str = "last_7d",
        since: Optional[str] = None,
        until: Optional[str] = None,
        level: Optional[str] = None,
        breakdowns: Optional[List[str]] = None,
        time_increment: Optional[str] = None,
        fields: Optional[str] = None,
        limit: int = 100,
    ):
        if bool(since) != bool(until):
            raise GraphError("give both since and until, or neither")
        fields = fields or INSIGHT_FIELDS
        if level in LEVEL_NAME_FIELDS:
            fields = f"{LEVEL_NAME_FIELDS[level]},{fields}"
        params: Dict[str, Any] = {
            "fields": fields,
            "level": level,
            "breakdowns": ",".join(breakdowns) if breakdowns else None,
            "time_increment": time_increment,
        }
        if since:
            params["time_range"] = {"since": since, "until": until}
        else:
            params["date_preset"] = date_preset
        return self.client.get_all(f"{self._parent(object_id)}/insights", max_items=limit, **params)

    # -- writes ---------------------------------------------------------
    def set_status(self, object_id: str, status: str):
        return self.client.post(object_id, status=status)

    def update_budget(
        self, object_id: str, daily_budget: Optional[int] = None, lifetime_budget: Optional[int] = None
    ):
        if daily_budget is None and lifetime_budget is None:
            raise GraphError("give daily_budget or lifetime_budget")
        self._one_budget(daily_budget, lifetime_budget)
        self._check_budget(daily_budget)
        return self.client.post(object_id, daily_budget=daily_budget, lifetime_budget=lifetime_budget)

    def rename(self, object_id: str, name: str):
        return self.client.post(object_id, name=name)

    def create_campaign(
        self,
        account_id: str,
        name: str,
        objective: str,
        special_ad_categories: Optional[List[str]] = None,
        daily_budget: Optional[int] = None,
        lifetime_budget: Optional[int] = None,
        bid_strategy: Optional[str] = None,
    ):
        self._one_budget(daily_budget, lifetime_budget)
        self._check_budget(daily_budget)
        campaign_budget = daily_budget is not None or lifetime_budget is not None
        return self.client.post(
            f"{normalize_account_id(account_id)}/campaigns",
            name=name,
            objective=objective,
            status="PAUSED",
            special_ad_categories=special_ad_categories or [],
            daily_budget=daily_budget,
            lifetime_budget=lifetime_budget,
            bid_strategy=bid_strategy,
            # Required by the API when the campaign does not own the budget.
            is_adset_budget_sharing_enabled=None if campaign_budget else False,
        )

    def create_ad_set(
        self,
        account_id: str,
        campaign_id: str,
        name: str,
        optimization_goal: str,
        targeting: Dict[str, Any],
        billing_event: str = "IMPRESSIONS",
        daily_budget: Optional[int] = None,
        lifetime_budget: Optional[int] = None,
        bid_amount: Optional[int] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        promoted_object: Optional[Dict[str, Any]] = None,
        destination_type: Optional[str] = None,
    ):
        self._one_budget(daily_budget, lifetime_budget)
        self._check_budget(daily_budget)
        return self.client.post(
            f"{normalize_account_id(account_id)}/adsets",
            campaign_id=campaign_id,
            name=name,
            status="PAUSED",
            optimization_goal=optimization_goal,
            billing_event=billing_event,
            targeting=targeting,
            daily_budget=daily_budget,
            lifetime_budget=lifetime_budget,
            bid_amount=bid_amount,
            start_time=start_time,
            end_time=end_time,
            promoted_object=promoted_object,
            destination_type=destination_type,
        )

    def create_ad_creative(self, account_id: str, name: str, object_story_spec: Dict[str, Any]):
        return self.client.post(
            f"{normalize_account_id(account_id)}/adcreatives",
            name=name,
            object_story_spec=object_story_spec,
        )

    def create_ad(self, account_id: str, adset_id: str, name: str, creative_id: str):
        return self.client.post(
            f"{normalize_account_id(account_id)}/ads",
            adset_id=adset_id,
            name=name,
            status="PAUSED",
            creative={"creative_id": creative_id},
        )

    def duplicate(self, object_id: str, deep_copy: bool = False, rename_suffix: str = " - Copy"):
        return self.client.post(
            f"{object_id}/copies",
            deep_copy=deep_copy,
            status_option="PAUSED",
            rename_options={"rename_strategy": "DEEP_RENAME", "rename_suffix": rename_suffix},
        )
