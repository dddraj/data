# Facebook Ads Manager via Claude

An MCP server that lets Claude read and change your Meta (Facebook/Instagram)
Ads Manager: campaigns, ad sets, ads, budgets and performance reports. It talks
to the Marketing API directly and depends only on the Python standard library.

> **Hinglish mein:** Token set karo, Claude Code ko is repo mein kholo, aur
> seedha bolo: *"pichle 7 din mein kaunsa campaign sabse mehenga CPC de raha
> hai? Usko pause kar do"*. Claude report nikalega, aur pause karne se pehle
> aapse permission maangega.

## 1. Get an access token

1. Go to [developers.facebook.com](https://developers.facebook.com/apps) and
   create an app of type **Business**. Add the **Marketing API** product.
2. In [Business Settings → Users → System users](https://business.facebook.com/settings/system-users),
   create a system user (Admin). Under **Assign assets**, give it your ad
   account(s), with *Manage campaigns* for write access.
3. Click **Generate new token**, pick your app, and grant `ads_read` and
   `ads_management` (plus `business_management` if you want). System-user
   tokens don't expire.

For a quick test you can instead use a user token from the
[Graph API Explorer](https://developers.facebook.com/tools/explorer/) with the
same permissions (it expires in about an hour).

## 2. Connect it to Claude

**Claude Code** (this repo already ships `.mcp.json`):

```bash
export META_ACCESS_TOKEN="EAA..."
export FB_ADS_MAX_DAILY_BUDGET=200000   # optional: cap = 2,000.00 per day
cd data && claude                       # approve the "facebook-ads" server once
```

Or from anywhere: `claude mcp add facebook-ads -e META_ACCESS_TOKEN=EAA... -- python3 /path/to/data/facebook_ads/run_server.py`

**Claude Desktop**: in *Settings → Developer → Edit config*, add

```json
{
  "mcpServers": {
    "facebook-ads": {
      "command": "python3",
      "args": ["/path/to/data/facebook_ads/run_server.py"],
      "env": { "META_ACCESS_TOKEN": "EAA...", "FB_ADS_MAX_DAILY_BUDGET": "200000" }
    }
  }
}
```

| Variable | Meaning |
| --- | --- |
| `META_ACCESS_TOKEN` | **Required.** Token from step 1. |
| `META_APP_SECRET` | Optional. Sends `appsecret_proof`, which apps with *Require App Secret* enabled need. |
| `META_API_VERSION` | Optional. Defaults to `v24.0`. |
| `FB_ADS_MAX_DAILY_BUDGET` | Optional. Rejects any daily budget above this, in the smallest currency unit. |
| `FB_ADS_READ_ONLY` | `1` hides every tool that changes anything. |

## 3. What you can ask

- "Show all my ad accounts and how much each has spent"
- "Last 30 days performance of every active campaign: spend, CTR, CPC, purchases, ROAS"
- "Break campaign 1202… down by age and gender for last week"
- "Pause every ad set with CPC over ₹25 in the last 7 days"
- "Raise the Diwali campaign daily budget to ₹1,500"
- "Duplicate the best ad set and target Maharashtra, 25–40"
- "Create a Sales campaign with a ₹500/day ad set for India, 18–45, optimizing for purchases on pixel 123…"

## Tools

| Read | Write (created **PAUSED**) |
| --- | --- |
| `list_ad_accounts` | `set_status` (ACTIVE / PAUSED / ARCHIVED) |
| `list_campaigns` | `update_budget` |
| `list_ad_sets` | `rename` |
| `list_ads` | `create_campaign` |
| `get_object` | `create_ad_set` |
| `get_insights` | `create_ad_creative`, `create_ad` |
| | `duplicate` |

## Safety

- Everything Claude creates or duplicates starts **PAUSED**. Only `set_status`
  with `ACTIVE` spends money, and Claude Code asks you to approve each write
  tool call unless you allow it permanently.
- Budgets use Meta's own unit, the currency's smallest unit: `50000` =
  ₹500.00 or $500.00. The server tells Claude to check the account `currency` first.
- The token never appears in error messages, and nothing is logged.

## Tests

```bash
python3 -m pytest facebook_ads/tests
```

The tests replay canned Graph responses, so they don't need a token or network.
