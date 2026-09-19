# Decoding at the program level, and staying correct through upgrades

The brief: read launch price, supply, market cap and raise target for as many
Solana launchpads as possible, *at the program level*, so the same code can be
dropped into a live node decoder — keeping the static values but re-deriving
them in real time if a program is upgraded.

This document is how that is done and why each choice was made.

---

## 1. Identify by program and discriminator, never by name

A launch is recognised from two things that the program itself writes:

* the account's **owner** — the launchpad program id, and
* the account's first **8 bytes** — Anchor's account discriminator,
  `sha256("account:<StructName>")[..8]`.

```python
decoder.decode_account_data(owner=account.owner, data=account.data,
                            address=account.pubkey, slot=account.slot)
```

Nothing else is consulted. No mint list, no token metadata, no off-chain index,
no "is this address a pump.fun coin" lookup. Feed it every account update a
program emits and it returns `None` for the ones that are not curve state.

That is also what makes the subscription filter tight: the discriminator is a
`memcmp` at offset 0, so the node only sends curve accounts.

```python
from scripts.live_node_example import curve_account_filters
curve_account_filters(decoder)
# {"launchpad": "pumpfun", "owner": "6EF8rr…",
#  "account_type": "BondingCurve", "memcmp_offset": 0,
#  "memcmp_base58": "4y6pru6YvC7"}
```

---

## 2. Let the program describe its own layout

Account layouts come from an Anchor IDL compiled at runtime
(`anchor_idl.compile_idl`), not from hand-written struct offsets. Both IDL
dialects are handled — the pre-0.30 one (camelCase, `publicKey`,
`{"defined": "X"}`, no discriminators) and 0.30+ (snake_case, `pubkey`,
`{"defined": {"name": "X"}}`, explicit discriminators) — and field names are
normalised to snake_case so adapters see one spelling.

IDLs are resolved in this order:

1. **The program's own on-chain IDL account**, at
   `create_with_seed(find_program_address([], program), "anchor:idl", program)`.
   The payload is `disc(8) ‖ authority(32) ‖ len(u32) ‖ zlib(JSON)`.
   When a launchpad publishes here, a layout change ships itself.
2. **A bundled snapshot** in `launchpad_decoder/idl/`, taken from the project's
   official repo or SDK.

Check what a cluster actually serves with
`python scripts/launchpads.py idl-status`.

### Accounts written by older program versions

Anchor programs routinely append fields and realloc accounts lazily, so a live
cluster holds `BondingCurve` accounts written by several generations of
pump.fun at once. Strict borsh decoding refuses those. The decoder instead
decodes the longest valid prefix and lists what it could not reach in
`_truncated_fields`, which is the difference between "this decoder works on
mainnet" and "this decoder works on accounts created last week".

---

## 3. Read the parameters, do not hardcode them

The numbers a naive implementation hardcodes — pump.fun's 1.073e15 virtual
token reserve, its 30 SOL virtual quote reserve, the 85 SOL graduation target —
are **program state**. They live in `Global`, and pump.fun can change them with
one transaction, no redeploy required.

So every launch parameter is fetched from the chain, and every reported value
carries where it came from:

| `ValueSource` | Meaning |
|---|---|
| `onchain_state` | read from the curve/pool account itself |
| `onchain_config` | read from the program's global / platform / partner config |
| `onchain_idl` | layout came from the program's own IDL account |
| `bundled_idl` | layout came from a snapshot in this repo |
| `bundled_snapshot` | constant compiled into the program binary, not on chain |
| `derived` | computed from the above |
| `unavailable` | could not be determined |

```python
m = decoder.decode_address(curve)
m.raise_target_quote.value    # 85.005359057
m.raise_target_quote.source   # ValueSource.DERIVED
m.launch_price_quote.source   # ValueSource.ONCHAIN_CONFIG  <- read from Global
```

If the node is unreachable, the decoder falls back to the bundled snapshot,
marks the value `bundled_snapshot`, **and attaches a warning**. A stale number
that announces itself is safe; a stale number that looks fresh is not.

The only values that legitimately cannot come from chain are Moonit's curve
reserve constants, which are compiled into its program binary. Those are always
tagged `bundled_snapshot`.

---

## 4. Notice the upgrade

Every launchpad program here is deployed with the BPF upgradeable loader, so
its executable can change under you. `ProgramWatcher` reads each program's
`ProgramData` account:

```
u32 tag = 3
u64 last_deploy_slot
Option<Pubkey> upgrade_authority        (1 + 32 bytes)
<ELF bytes>
```

and fingerprints the deployment as
`(loader, last_deploy_slot, sha256(elf), len(elf))`. A change in that tuple
means a redeploy. The ELF hash catches a rebuild at the same slot; pass
`hash_executables=False` to skip it if you would rather not pull the whole
program account.

`deployment.immutable` is true when the loader is non-upgradeable or the
upgrade authority has been revoked — those programs need no watching at all.

---

## 5. React to the upgrade

```python
decoder.snapshot_static_params()      # baseline: polls the watcher, reads configs

report = decoder.refresh()            # on a timer, or on a ProgramData update
if report:
    for upgrade in report.upgrades:
        log.warning(upgrade.describe())
        # "6EF8rr… redeployed: slot 351000000 -> 351400000"
    for change in report.changes:
        log.warning(change.describe())
        # "pumpfun.configs.global.values.initial_virtual_sol_reserves:
        #   30000000000 -> 42000000000"
```

`refresh()` does four things, in order:

1. re-reads every tracked program's `ProgramData`;
2. for each program that moved, drops its compiled schema, its bundled schema
   and every config account cached from it;
3. re-resolves the IDL (on-chain first) and re-reads the configs;
4. diffs the new parameters against the previous snapshot and returns the
   list of changes.

The next `decode_*` call after that uses the new layout and the new parameters.
`tests/test_program_state.py` asserts exactly this: after a simulated redeploy
that doubles `initial_virtual_sol_reserves`, the reported raise target moves
from 85.005 SOL to 170.011 SOL with no code change and no restart.

### Two failure modes worth knowing

* **A config edited without a redeploy** is not picked up until something
  invalidates the cache. That is deliberate — re-reading `Global` on every
  decode would be a round trip per account update. If you need per-slot
  freshness, subscribe to the config accounts as well and call
  `decoder.configs.invalidate(program_id)` when one changes.
* **An upgrade that renames fields** is handled if the program publishes its
  IDL on chain; otherwise the adapter's `pick()` fallbacks catch the common
  renames (`virtual_sol_reserves` → `virtual_quote_reserves`) and anything else
  surfaces as a missing value rather than a wrong one.

### 5b. Hot reload is two halves, and the second one is easy to miss

Swapping a layout at runtime does nothing on its own if the stream is still
serving the old subscription. Most feeds compose their filter set **once, at
connect time** — a Geyser `SubscribeRequest`, a websocket `accountSubscribe` —
so an account whose owner is not already in that request never arrives, however
current the decoder's layouts are. The decode side hot-swaps and the data side
goes quiet, with no error anywhere.

Two things move the filter set:

* **A newly learned program.** Its accounts reached you only because one
  happened to be in the stream already; the rest need the owner subscribed.
* **A redeploy that renames an account struct.** The discriminator is
  `sha256("account:<Name>")[:8]`, so a rename moves it and the old memcmp
  matches nothing.

The second is nastier than it looks. A registry entry pins `state_account` by
name, and after a rename that name resolves to nothing — so the naive filter
builder emits *no filter at all* for that program rather than a stale one.
`curve_account_filters()` therefore falls back to whatever accounts the schema
now declares when the pinned name has gone.

`LiveDecoder` owns its filter set for this reason and reports a
`SubscriptionChange` whenever it moves:

```python
live = LiveDecoder(decoder)
node.subscribe(live.filters())

# ... later, driven by a ProgramData update or a learned program
if live.pending_resubscribe:
    print(live.pending_resubscribe.describe())   # "+newpad/Curve, -newpad/BondingCurve"
    node.subscribe(live.filters())
    live.pending_resubscribe = None
```

`tests/test_live_wiring.py` pins both cases, and that a redeploy which does
*not* touch the layout raises no resubscribe — churning the subscription on
every upgrade is its own outage.

### 5c. What a program with no published IDL falls back to

`SchemaCache.get()` tries the on-chain IDL first and never raises on failure:

1. `fetch_onchain_idl()` — the program's own IDL account. Preferred, because it
   is the program's current truth.
2. the **bundled IDL snapshot** in `launchpad_decoder/idl/`, when the on-chain
   read returns nothing or does not compile. The reason is recorded in
   `SchemaCache.errors` rather than thrown, so one program with a broken IDL
   cannot stop the other seven decoding.
3. nothing. A program with neither is left undecodable and `learn_program()`
   records why in `decoder.unlearnable`. That is a NULL, not a guess.

The cache is keyed on `deployment.revision()` — `(last_deploy_slot,
executable_len, …)` — so the swap is automatic: a redeploy changes the
revision, the next `get()` misses, and the layout is re-fetched. That is the
whole hot-reload mechanism, and it is the same one that makes "constants are
program state" work.

**Cost of the ProgramData re-read.** `watcher.poll()` is one `getAccountInfo`
per tracked program — eight for the whole registry — and with
`hash_executable=False` (the default) only the 45-byte
`ProgramData` header is needed, so a `dataSlice` keeps each one tiny. A timer
is entirely affordable at any interval you like.

A subscription is still better, and the address list is already there:
`watcher.programdata_addresses()` returns exactly the accounts to subscribe to,
which is what `programdata_filters()` in the example does. Then a redeploy
pushes to you and there is no polling interval to tune. Poll only as a
backstop, if at all.

---

## 6. Handle launchpads that do not exist yet

Two mechanisms, and neither needs a release.

**Multi-tenant discovery.** LaunchLab and Meteora DBC host most of the long
tail, and a new tenant is a new config account, not a new program:

```python
decoder.discover_platforms("meteora_dbc")   # every PoolConfig, i.e. every DBC launchpad
decoder.discover_platforms("raydium_launchlab")
```

**The heuristic adapter.** Point the decoder at an unknown program that
publishes an on-chain IDL and it maps field names to curve roles
(`virtual_token_reserves` → virtual base, `migration_quote_threshold` → raise
target, …), infers the curve family, and returns the same metrics with a
confidence score:

```python
m.raw_state["_heuristic_field_map"]    # {"virtual_base": "virtual_token_reserves", …}
m.raw_state["_heuristic_confidence"]   # 0.8
```

A pump.fun-shaped fork is priced correctly with zero code. Something genuinely
novel scores low and says so, rather than emitting a confident wrong number.

---

## 6b. Coins with no pool row

There is a specific failure mode worth naming, because it is self-sustaining.

If the launchpad table is *learned from curves that already have pool rows*,
then a launchpad whose coins produced no pool rows can never enter the table,
and its coins can never be priced. No pool row → no launchpad → no pool row.
The coins look like a data-quality problem; they are a bootstrap problem, and
no amount of reprocessing the same rows will fix it.

Nothing here reads pool rows. The entry point is the **creator program**, which
those coins already have.

### Triage: a few programs, or a long tail?

`SELECT creator_program, count(*) ... GROUP BY 1` answers the cardinality.
What it cannot tell you is whether a price is *reachable* for each one. That is
`triage_programs`:

```bash
python scripts/launchpads.py triage unpriced.csv     # program_id,coin_count
```

```
 coins  verdict           conf  program
    21  self_describing   0.75  EYLAenNyYN8q…  decodable now via its on-chain IDL
     7  no_curve_shape    0.00  6iQpPpj844Df…  publishes an IDL but nothing curve-shaped
     3  opaque            0.00  DEHbmbzAkALd…  no on-chain IDL: needs an SDK or reverse engineering
     1  not_a_program     0.00  6vAwn1hPHPhN…  not executable — check how creator_program was populated
```

Five verdicts, each a different piece of work:

| Verdict | What it means | What to do |
|---|---|---|
| `known_launchpad` | already in the registry | decode it |
| `self_describing` | publishes an Anchor IDL with curve-shaped accounts | confirm on a sample, then backfill |
| `no_curve_shape` | has an IDL, but no bonding-curve account | probably not a launchpad — check the column |
| `opaque` | no on-chain IDL | needs an SDK, a published IDL, or reverse engineering |
| `not_a_program` | the account is not executable | `creator_program` was populated from the wrong key |

The summary line is the answer to "head or tail": how many programs, how many
coins, and how many of those coins are decodable today.

### From a mint to a price, with no pool row

```python
from launchpad_decoder.discovery import price_mint
metrics = price_mint(decoder, creator_program, mint)
```

Two routes, cheapest first:

1. **PDA derivation**, for launchpads that derive the curve address from the
   mint. pump.fun is the case that matters: its `BondingCurve` stores no base
   mint at all, so a memcmp search can never find it — only the PDA can.
2. **A server-side `memcmp`** at the exact byte offset the program's own
   layout puts a mint field at. `field_offsets()` computes it from the IDL, so
   this is one filtered `getProgramAccounts` per candidate field, not a scan.

### The registry is a head start, not a gate

`LaunchpadDecoder` adopts an unregistered program that publishes an on-chain
IDL, the first time it sees an account from it:

```python
decoder.decode_account_data(owner=some_unknown_program, data=..., address=...)
# -> LaunchMetrics(launchpad="learned:bigpad", ...)

decoder.learned_programs   # programs adopted at runtime
decoder.unlearnable        # and the ones that could not be, with the reason
```

Pass `auto_learn=False` to require an explicit registry entry.

This is the part that actually breaks the cycle: the table is populated from
what the *program* says about itself, not from rows the program's coins
happened to produce.

### What stays NULL

A program in the `opaque` bucket has no reachable price, and the right value is
still NULL. Nothing here manufactures an opening price from a program that will
not describe itself — it reports which programs those are and how many coins
each one is holding up, so the decision to spend time on one is made against a
number rather than a hunch.

---

## 6c. First contact with a real node

A bundled IDL snapshot always raises the same question: is this still what the
program looks like? `selftest` answers it against the live cluster.

```bash
python scripts/launchpads.py selftest            # add --sample to decode a real curve
```

```
pumpfun
  [ok  ] program exists                             owner BPFLoaderUpgradeab1e111…
  [ok  ] deployment readable                        slot 351400000, authority 2P56vRW…
  [ok  ] bundled layout still decodes this program  no breaking drift
  [warn] bundled snapshot is current                BondingCurve: program appended
                                                    ['is_boosted']; the prefix still
                                                    decodes, but re-capture to read
                                                    the new fields
  [ok  ] config global decodes                      4wTV1Ymi… -> Global

7/8 passed, 0 blocking, 1 warnings
```

The severity split is the point. Two kinds of drift, with very different costs:

| | Effect | Severity |
|---|---|---|
| a field **appended** | the bundled prefix still decodes correctly | warning |
| an account type added or removed | informational | warning |
| a field **reordered** | silently decodes to *wrong numbers* | blocking |
| a discriminator changed | accounts stop being recognised | blocking |
| a field dropped | snapshot reads past the end | blocking |
| no on-chain IDL **and** no snapshot | nothing can decode it | blocking |

Only blocking checks exit non-zero. Failing a build over a harmless appended
field trains people to ignore the check, which costs more than the drift does.

Run it on first contact, and again after any upgrade `refresh()` reports — the
two together are the whole staleness story: the watcher says *something moved*,
the selftest says *whether it matters*.

---

## 7. Transport independence

Everything goes through the `AccountSource` protocol:

```python
class AccountSource(Protocol):
    def get_account(self, pubkey) -> AccountInfo | None: ...
    def get_multiple_accounts(self, pubkeys) -> list[AccountInfo | None]: ...
    def get_program_accounts(self, program_id, **kw) -> list[AccountInfo]: ...
    def get_slot(self) -> int: ...
```

`JsonRpcAccountSource` ships as the default; `StaticAccountSource` backs the
tests. To run against Yellowstone/Geyser, a Geyser plugin, or an accountsdb
snapshot reader, implement those four methods over your source — the hot path
(`decode_account_data`) takes bytes and never calls out at all.

`scripts/live_node_example.py` is a runnable end-to-end wiring (subscription
filters, the decode call, and the ProgramData hook) with a simulated feed, so
you can check the integration before pointing it at a node.

---

## 8. What this design will not do for you

* **It will not price a curve whose parameters live in an account you did not
  fetch.** A Meteora DBC pool without its `PoolConfig` has no launch price, and
  the decoder says so instead of guessing.
* **It will not invent a raise target for a launchpad that has none.** Heaven
  and Vertigo pools never graduate; those fields stay unset.
* **It will not convert to USD.** `metrics.mcap_in(sol_price_usd)` does the
  arithmetic, but the quote price has to come from somewhere you trust.
* **It will not survive a program that changes its *semantics* without changing
  its layout.** If a launchpad reinterprets an existing field, only a human
  reading the diff catches that. The upgrade report exists to tell you when to
  go look.
