# Solana launchpads at the program level

A survey of the programs that run bonding-curve token launches on Solana, and
of exactly which account field supplies each of the four numbers this project
cares about:

* **launch price** — what one token costs at the very first buy
* **supply** — how many tokens exist, and how many the curve will sell
* **market cap** — price × supply, at launch / now / at graduation
* **raise target** — how much quote token the curve must take in to graduate

Everything below was read out of the programs' own IDLs and source, not out of
a block explorer. Where a number is compiled into the program binary rather
than stored in an account, that is called out explicitly, because those are the
only values a decoder cannot verify against the chain.

---

## The single most important structural fact

**Most "launchpads" are not programs.** Two multi-tenant programs host the
majority of the long tail:

| Program | Tenancy account | Tenants include |
|---|---|---|
| Raydium LaunchLab | `PlatformConfig` (one per platform) | LetsBonk.fun, Cook.meme, Raydium's own UI |
| Meteora DBC | `PoolConfig` (one per partner curve preset) | Believe, Jupiter Studio, Bags, and dozens more |

A hand-maintained list of tenant names goes stale within weeks. The decoder
therefore *enumerates* them:

```bash
python scripts/launchpads.py platforms meteora_dbc
python scripts/launchpads.py platforms raydium_launchlab
```

Each row is a config account, and each config account **is** a launchpad's
parameter set: its own curve shape, its own supply, its own raise target. One
`getProgramAccounts` call gives you every launchpad on that program, including
the ones that launched this morning.

Verified tenant constant (from `letsbonkdotfun-sdk`):

| Platform | Program | `PlatformConfig` |
|---|---|---|
| LetsBonk.fun | LaunchLab | `FfYek5vEz23cMkWsdJwG2oa6EphsvXSHrGpdALN4g6W1` |

LetsBonk uses LaunchLab's *default* `GlobalConfig`
(`6s1xP3hpbAfFoNtUNF8mfHsjr2Bd97JxFJRWLbL6aHuX`, i.e. wSOL quote,
constant-product, index 0), so its curve maths is LaunchLab's constant-product
curve; only the fee split is LetsBonk-specific.

---

## 1. pump.fun

| | |
|---|---|
| Program | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` |
| Curve state | `BondingCurve` — PDA `["bonding-curve", mint]` |
| Parameters | `Global` — PDA `["global"]` = `4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf` |
| Curve | constant product over virtual reserves |
| Graduates to | PumpSwap (`pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA`) |
| Source | <https://github.com/pump-fun/pump-public-docs> |

`Global` holds every launch parameter, so **nothing needs hardcoding**:

| Field | Mainnet value | What it gives you |
|---|---|---|
| `initial_virtual_token_reserves` | 1,073,000,000,000,000 | launch price denominator |
| `initial_virtual_sol_reserves` | 30,000,000,000 | launch price numerator |
| `initial_real_token_reserves` | 793,100,000,000,000 | tokens the curve will sell |
| `token_total_supply` | 1,000,000,000,000,000 | supply (6 decimals → 1e9 tokens) |
| `fee_basis_points` | 100 | 1% protocol fee |

Derived, for the current mainnet values:

```
launch price       = 30e9 / 1.073e15 × 10^(6−9)   = 2.7958993e-8 SOL
launch market cap  = launch price × 1e9 tokens    = 27.96 SOL
raise target       = k/(vT − realT) − vQ          = 85.005359057 SOL
graduation price   = 115.005e9 / 279.9e12 × 1e-3  = 4.1088017e-7 SOL
graduation mcap    =                              = 410.88 SOL
```

Live state comes from `BondingCurve`: `virtual_token_reserves`,
`virtual_quote_reserves` (spelled `virtual_sol_reserves` in older builds),
`real_token_reserves`, `real_quote_reserves`, `complete`.

Note the recent generalisation to non-SOL quote mints: `BondingCurve` now
carries `quote_mint` and `creator_fee_bps`, and `Global` carries
`initial_virtual_quote_reserves` alongside the SOL-specific field. The decoder
reads either spelling.

**Versioning gotcha.** pump.fun appends fields to `BondingCurve` and reallocs
accounts lazily, so a live cluster holds accounts written by several
generations of the program. Strict borsh decoding fails on the old ones; the
decoder decodes the common prefix and reports the missing fields in
`_truncated_fields`.

---

## 2. PumpSwap (post-graduation)

| | |
|---|---|
| Program | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` |
| Pool state | `Pool` — PDA `["pool", index, creator, base_mint, quote_mint]` |
| Curve | plain constant-product AMM |

Included so a coin's price does not drop off a cliff at graduation. The `Pool`
account holds only pointers, so pricing needs the two vault token-account
balances; `virtual_quote_reserves` (an `i128`, non-zero only for "boost" pools)
is added to the quote side.

---

## 3. Raydium LaunchLab

| | |
|---|---|
| Program | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` |
| Curve state | `PoolState` — PDA `["pool", base_mint, quote_mint]` |
| Parameters | `GlobalConfig` (curve type, fees), `PlatformConfig` (per tenant) |
| Curve | selectable: constant product / fixed price / linear |
| IDL | <https://github.com/raydium-io/raydium-idl> |

`PoolState` is self-contained — supply, tokens for sale and raise target are
all on the pool:

| Field | Meaning |
|---|---|
| `supply` | total base supply |
| `total_base_sell` | tokens the curve will sell |
| `total_quote_fund_raising` | **the raise target** |
| `virtual_base` / `virtual_quote` | curve parameters (meaning depends on curve type) |
| `real_base` / `real_quote` | tokens sold / quote raised |
| `migrate_fee` | deducted before seeding the AMM |
| `vesting_schedule.total_locked_amount` | held back from migration |
| `status` | 0 funding, 1 awaiting migration, 2 migrated |

`GlobalConfig.curve_type` selects the maths (0 constant product, 1 fixed,
2 linear). PDA seeds, both verified:

* `GlobalConfig` = `["global_config", quote_mint, u8(curve_type), u16_be(index)]`
* `PlatformConfig` = `["platform_config", platform_admin_wallet]`

**Quantisation gotcha.** For the linear curve, `virtual_base` stores the slope
`a = 2·raise·2^64 / totalSell²` as a **u64**. For a 6-decimal token with an
85 SOL target that evaluates to about 7, so LaunchLab itself quantises the
slope by ~0.8%. That is the program's behaviour, not a decoding artefact.

---

## 4. Meteora Dynamic Bonding Curve

| | |
|---|---|
| Program | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` |
| Curve state | `VirtualPool` — PDA `["pool", config, max(mintA,mintB), min(mintA,mintB)]` |
| Parameters | `PoolConfig` — one per partner |
| Curve | piecewise concentrated liquidity, Q64.64 sqrt prices |
| Source | <https://github.com/MeteoraAg/dynamic-bonding-curve> |

The most general design in the survey, and the easiest to read: the entire
launch is one `PoolConfig` account.

| Field | What it gives you |
|---|---|
| `sqrt_start_price` | **launch price** = (√P / 2^64)² |
| `migration_sqrt_price` | **graduation price** |
| `migration_quote_threshold` | **raise target** |
| `pre_migration_token_supply` | **supply** (or derive it, for dynamic-supply configs) |
| `swap_base_amount` | tokens the curve sells |
| `migration_base_threshold` | tokens reserved for the migrated pool |
| `curve[20]` | `(sqrt_price, liquidity)` segments |
| `token_decimal` | base decimals, no mint lookup needed |
| `pool_fees.base_fee.cliff_fee_numerator` | fee, in 1e-9 units |

`VirtualPool` then only needs `sqrt_price`, `base_reserve`, `quote_reserve`
and `is_migrated`.

The decoder cross-checks `swap_base_amount` against the base amount implied by
integrating the curve segments (`get_base_token_for_swap` in the program) and
warns if they disagree by more than 0.1% — that mismatch is the usual sign of a
misread config.

---

## 5. Moonit (formerly Moonshot / DEX Screener)

| | |
|---|---|
| Program | `MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG` |
| Curve state | `CurveAccount` — PDA `["token", mint]` |
| Curve | five types: `LinearV1`, `ConstantProductV1`, `ConstantProductV2`, `FlatCurveV1`, `FlatCurveV1AntiSnipe` |
| SDK | <https://github.com/gomoonit/moonit-sdk>, maths in `@heliofi/launchpad-common` |

The odd one out: Moonit sizes its curve from a **market-cap threshold**, not a
quote target. `CurveAccount.marketcap_threshold` is the graduation trigger, and
the SOL required has to be solved for.

| Field | Meaning |
|---|---|
| `total_supply` | supply |
| `curve_amount` | tokens still on the curve (`sold = total_supply − curve_amount`) |
| `curve_type` | which maths applies |
| `marketcap_threshold` / `marketcap_currency` | graduation trigger |
| `coef_b` | linear-curve intercept, in minimal collateral units |
| `decimals`, `collateral_currency`, `migration_fee` | |

The reserve constants are **compiled into the program**, not stored on chain —
the only values in this whole survey that a decoder cannot verify against the
cluster. They are tagged `bundled_snapshot` in the output:

| Curve type | initial virtual token | initial virtual collateral | sells |
|---|---|---|---|
| `ConstantProductV1` | 1.073e18 | 30 SOL | 80% |
| `ConstantProductV2` | 1.060e18 | 14 SOL | 80% |
| `LinearV1` | — | — | 55% |
| `FlatCurveV1(AntiSnipe)` | — | — | 49% |

Verification: the SDK publishes a `marketCapToMinimalTokens` table, and the
integer solver here reproduces two of its entries exactly and the other two to
1e-13 relative (the SDK computes them in floating-point BigNumber).

**Definition mismatch worth knowing.** Moonit's own "market cap" is
price × *tokens sold*, not price × total supply. This project reports
price × total supply everywhere for cross-launchpad comparability, and puts
Moonit's native threshold in `raw_state._marketcap_threshold_ui`.

---

## 6. Heaven

| | |
|---|---|
| Program | `HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn` |
| Pool state | `liquidityPoolState` |
| Curve | constant-product AMM seeded with virtual quote liquidity |
| SDK | `heaven-sdk` on npm (pre-0.30 Anchor IDL, camelCase, no discriminators) |

Heaven has no separate curve program and **no graduation**: it owns its AMM and
seeds new pools with virtual quote liquidity, so a launch trades like a pool
from block one.

Conveniently, the pool caches its own analytics as `f64`:
`min_price` / `curr_price` / `max_price` and `min_mc` / `curr_mc` / `max_mc`.
`initial_base_token_vault_balance` and `initial_quote_token_vault_balance` give
the seeded reserves, and the decoder recomputes the launch price from them and
warns if the cached `min_price` disagrees by more than 2%.

Because the IDL is the legacy dialect, discriminators are not in the document;
they are derived as `sha256("account:liquidityPoolState")[:8]`.

---

## 7. Vertigo

| | |
|---|---|
| Program | `vrTGoBuy5rYSxAfV3jaRJWHH6nN9WK4NRExGxsk1bCJ` |
| Pool state | `Pool` |
| Curve | one-sided constant product |
| SDK | `@vertigo-amm/vertigo-sdk` |

Small and elegant: `shift` is the virtual quote reserve, and the SDK sets it to
the intended **initial market cap in lamports**.

```
price = (shift + token_a_reserves) / token_b_reserves
launch market cap = shift        (when the pool is seeded with the whole mint)
```

No graduation, so `raise_target_quote` and `graduation_price_quote` are left
unset rather than invented.

---

## 8. GoFundMeme

| | |
|---|---|
| Program | `GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7` |
| Curve state | `BondingCurvePool` |
| Curve | power law in quote raised |
| SDK | `@gofundmeme/sdk` |

Parameterised by quote *raised* rather than tokens sold:

```
tokens(x₁→x₂) = scale · ∫ (x + c)^(−e) dx
scale         = initial_tokens / ∫₀^target (x + c)^(−e) dx
price(x)      = (x + c)^e / scale
```

with `c = curve_constant`, `e = curve_exponent` (both `f64` on the pool) and
`target = target_raise`. `total_supply`, `initial_tokens`, `token_balance` and
`current_sol` complete the picture. Everything is closed-form.

---

## 9. Programs decoded from their on-chain IDL

Some launchpads publish their Anchor IDL to the canonical IDL account
(`create_with_seed(find_program_address([], program), "anchor:idl", program)`).
For those the decoder learns the layout at runtime and prices the curve from
field names, with a reported confidence score.

| Launchpad | Program | Notes |
|---|---|---|
| Boop.fun | `boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4` | pump.fun-shaped virtual reserves; graduates to Raydium |
| Daos.fun | `5jnapfrAN47UYkLkEf7HnprPPBCQLvkYWGZDeKkaP5hv` | fund-raise first, then the DAO token trades on a virtual AMM |

Check what a cluster actually serves:

```bash
python scripts/launchpads.py idl-status
```

A pump.fun-shaped fork deployed tomorrow, with fields named
`virtual_token_reserves` / `virtual_sol_reserves`, is priced correctly by the
heuristic adapter with no code change. One that is genuinely novel is reported
as low-confidence rather than silently mis-priced.

---

## Curve families, and what each one needs

| Family | Programs | Minimum inputs for a full answer |
|---|---|---|
| constant product over virtual reserves | pump.fun, LaunchLab (type 0), Moonit CP v1/v2, Boop, Vertigo | initial + current virtual reserves, tokens for sale |
| linear | LaunchLab (type 2), Moonit `LinearV1` | slope, tokens sold, sellable supply |
| fixed price | LaunchLab (type 1), Moonit flat curves | the two virtual reserves |
| piecewise sqrt price | Meteora DBC | `sqrt_start_price`, `migration_sqrt_price`, `curve[]`, threshold |
| power law | GoFundMeme | `curve_constant`, `curve_exponent`, `target_raise`, `initial_tokens` |
| AMM with virtual seed | Heaven | initial and current vault balances |
| AMM over real reserves | PumpSwap | vault balances + mint supply |

## What is *not* covered, and why

* **IDO / sale platforms with no curve** (Raydium AcceleRaytor, Solanium and
  similar). There is no curve to decode — allocations are fixed price.
* **Any launchpad whose program could not be pinned to a verified address.**
  Naming a program id that turns out to be wrong is worse than omitting it; the
  on-chain IDL path plus `discover_platforms()` covers these the moment you
  have the address.
* **The tenant lists of LaunchLab and DBC.** Deliberately not hardcoded — see
  the first section.
