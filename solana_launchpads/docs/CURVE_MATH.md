# Curve mathematics

Derivations for every formula in `launchpad_decoder/curves.py`. All of it works
in **raw on-chain units** (lamports, base units) and converts once at the edge,
because that is how the programs compute and it is the only way the numbers
match a simulated trade.

Two conversions used throughout:

```
ui_amount(raw, d)            = raw / 10^d
price_ui(price_raw, db, dq)  = price_raw × 10^(db − dq)
```

`price_raw` is quote-raw per base-raw; `price_ui` is whole quote per whole
token. For a 6-decimal token quoted in SOL that factor is `10^-3`, which is
where a lot of hand-rolled launchpad indexers go wrong by a thousand.

---

## 1. Constant product over virtual reserves

Used by pump.fun, Raydium LaunchLab (constant-product), Moonit
`ConstantProductV1/V2`, Boop and Vertigo.

The curve holds *virtual* reserves `(vBase, vQuote)` with `k = vBase · vQuote`
held constant. No real quote is needed to seed it; the virtual quote reserve
sets the opening price.

**Spot price**

```
P = vQuote / vBase
```

**Buying**, in and out, exactly as the programs round:

```
baseOut(quoteIn)  = ⌊ quoteIn · vBase / (vQuote + quoteIn) ⌋
quoteIn(baseOut)  = ⌈ vQuote · baseOut / (vBase − baseOut) ⌉
```

Rounding is always in the curve's favour: `baseOut` floors, `quoteIn` ceils.

**Raise target.** The curve completes when its sellable supply `S` is gone.
Substituting `baseOut = S`:

```
raiseTarget = ⌈ vQuote₀ · S / (vBase₀ − S) ⌉
```

**Graduation price.** At completion the reserves are
`(vBase₀ − S, vQuote₀ + raiseTarget)`, so

```
P_grad = (vQuote₀ + raiseTarget) / (vBase₀ − S)
```

### Worked example: pump.fun

With `vBase₀ = 1.073e15`, `vQuote₀ = 3e10`, `S = 7.931e14`, 6-decimal token,
9-decimal SOL, 1e9 total supply:

```
P₀        = 3e10 / 1.073e15                     = 2.7959e-5  raw
          × 10^(6−9)                            = 2.7959e-8  SOL/token
mcap₀     = 2.7959e-8 × 1e9                     = 27.96 SOL

raise     = ⌈3e10 · 7.931e14 / 2.799e14⌉        = 85,005,359,057 lamports
                                                = 85.005359 SOL

P_grad    = 1.15005e11 / 2.799e14               = 4.1088e-4  raw
                                                = 4.1088e-7  SOL/token
mcap_grad = 4.1088e-7 × 1e9                     = 410.88 SOL
```

Those are the familiar pump.fun numbers: a ~28 SOL opening cap, ~85 SOL to
graduate, ~411 SOL at migration. `tests/test_curves.py` pins all of them.

Note `raiseTarget` is **pre-fee**. pump.fun charges `fee_basis_points` on top,
so a buyer pays ≈ 85.005 × 1.01 SOL to fill the curve.

### The reserve offset, and why it beats matching k

pump.fun's buy and sell add the post-fee amount to the virtual **and** the real
quote reserve together. So their difference never moves:

```
virtual_quote - real_quote == initial_virtual_quote_reserves
```

for the whole life of the curve, through any number of buys and sells.

That is more useful than it looks, because pump.fun seeds **two** kinds of curve
with different openings — 30 SOL for SOL-quoted coins, and
`initial_virtual_quote_reserves` (4.292 at the time of writing) for non-SOL ones
— and the curve account does not record which it is. The offset does, exactly:

| `virtual_quote - real_quote` | variant |
|---|---|
| `initial_virtual_sol_reserves` | SOL-quoted |
| `initial_virtual_quote_reserves` | non-SOL-quoted |
| anything else | the pair did not come from one read |

This beats classifying by matching k against each candidate, for two reasons.
It needs no tolerance — it is integer equality. And it keeps working after the
curve has traded away from its opening, whereas counting curves that still sit
in a narrow band *around* an opening value finds almost none of them, because
curves that trade leave the band immediately.

An offset near zero is its own diagnosis: that is what a virtual column
carrying the **real** reserve looks like, since the two are then the same
number or the real one was defaulted away.

Both opening constants give the same launch market cap in fiat terms, which is
a useful sanity check on the reading: 30 SOL over 1.073e9 tokens is ~28 SOL,
and 4.292 over the same supply is 4,000 in a 6-decimal quote — both about
$4,000 at the SOL price the constants were chosen at.

---

## 2. Linear price

### Raydium LaunchLab (`curve_type = 2`)

`virtual_base` stores the slope `a` in Q64 and `real_base` is tokens sold:

```
P(sold) = a · sold / 2^64
```

so the curve opens at price zero. LaunchLab sizes it as

```
totalSell = 2 · raise · (supply − locked) / (3 · raise − migrateFee)
a         = 2 · raise · 2^64 / totalSell²
```

Integrating `P` from 0 to `totalSell` gives `a · totalSell² / 2^65 = raise`, so
the raise target is consistent by construction — and the final price is exactly
twice the average price.

`a` is a `u64`. For a 6-decimal token and an 85 SOL raise it lands near 7, so
the slope carries about 0.8% quantisation error. That belongs to the program.

### Moonit `LinearV1`

Price is affine in tokens sold, in whole units:

```
P(x) = a·x + b            b = coef_b / 10^collateralDecimals
cost(0→n) = ½·a·n² + b·n
```

`a` is not stored; it is solved from the market-cap threshold. With `D` the
sellable supply (55% of total for `LinearV1`):

```
a = (mcThreshold / D − b) / D
```

which makes `P(D)·D = mcThreshold` by construction. The raise target is then
`cost(0→D) = ½·a·D² + b·D`; with `b = 0` that is exactly half the threshold.

---

## 3. Fixed price

```
P = vQuote / vBase          (constant for the whole curve)
raise = P × sellable supply
```

---

## 4. Piecewise sqrt-price liquidity (Meteora DBC)

DBC borrows concentrated-liquidity maths. Prices are Q64.64 square roots and
liquidity is Q64.64:

```
P = (√P / 2^64)²
```

The curve is up to 20 segments `(sqrt_price_i, liquidity_i)`, each active from
the previous segment's bound (or `sqrt_start_price` for the first) up to its
own. Within a segment, the Uniswap-v3 deltas apply:

```
Δbase  = L · (√Pu − √Pl) / (√Pu · √Pl)
Δquote = L · (√Pu − √Pl) / 2^128
```

(the `2^128` is `2^64` squared: both `L` and `√P` carry a Q64 scale.)

**Tokens the curve will sell** — walk the segments from `sqrt_start_price`,
stopping at `migration_sqrt_price`:

```
baseForSwap = Σ Δbase(lower_i, min(sqrt_price_i, √P_migration), L_i)
```

This is `get_base_token_for_swap` in the program, and it should equal
`PoolConfig.swap_base_amount`; the decoder warns when it does not.

**Migration price from a threshold** — the inverse walk, consuming
`migration_quote_threshold` segment by segment and, in the segment where it
runs out,

```
√P_next = √P + Δquote · 2^128 / L
```

This is `get_migration_threshold_price`, used when a config stores
`migration_sqrt_price` as zero.

So for DBC all four answers are one account read:

| Answer | Field |
|---|---|
| launch price | `(sqrt_start_price / 2^64)²` |
| graduation price | `(migration_sqrt_price / 2^64)²` |
| supply | `pre_migration_token_supply` |
| raise target | `migration_quote_threshold` |

For dynamic-supply configs (`fixed_token_supply_flag = 0`) the supply is
`swap_base_amount + migration_base_threshold + locked vesting`.

---

## 5. Power law (GoFundMeme)

Parameterised by quote raised `x` rather than tokens sold. Tokens issued over a
raise interval are the integral of a power law:

```
area(x₁,x₂) = ∫ (x + c)^(−e) dx
            = [ (x+c)^(1−e) / (1−e) ]  for e ≠ 1
            = ln(x₂+c) − ln(x₁+c)      for e = 1
```

The scale is fixed so that raising exactly `target` issues exactly the tradable
supply:

```
scale = initial_tokens / area(0, target)
```

and therefore

```
tokens(x₁→x₂) = scale · area(x₁,x₂)
price(x)      = (x + c)^e / scale
```

Launch price is `c^e / scale`, graduation price is `(target + c)^e / scale`.
With `e = 1` the two differ by exactly `(target + c) / c`.

---

## 6. AMM with a virtual seed (Heaven)

A constant-product pool seeded with virtual quote liquidity:

```
P_launch  = initial_quote_vault / initial_base_vault
P_current = quote_vault / base_vault
raised    = quote_vault − initial_quote_vault
```

No graduation, so there is no raise target. The pool also caches
`min/curr/max_price` and `min/curr/max_mc` as `f64`; the decoder prefers the
cached values and cross-checks them against the reserves.

---

## Market cap, and one definitional warning

Everywhere in this project:

```
market cap = price × total supply
```

That is the DexScreener/Birdeye convention and it is what makes numbers
comparable across launchpads.

**Moonit does not use that definition.** Its `getMarketCap` is
price × *tokens sold*, and `marketcap_threshold` is a trigger on *that*
quantity. The Moonit adapter solves the threshold in Moonit's own terms and
then reports `*_mcap_quote` in the common terms, with the native threshold
preserved in `raw_state._marketcap_threshold_ui`. Comparing Moonit's on-chain
threshold against another launchpad's market cap without that conversion gives
an answer that is wrong by the fraction of supply sold.

---

## Rounding

The programs use integer arithmetic; so does this code, wherever the program
does. Deviating anywhere the program floors or ceils compounds: a
per-trade error of one lamport becomes tens of thousands of tokens near the
start of a pump.fun curve, where a lamport buys ~35,000 tokens.

Floating point is used only where the program itself stores floats (Heaven's
`f64` prices, GoFundMeme's `curve_constant`/`curve_exponent`) or where the
result is already a ratio that will be reported as a float.
