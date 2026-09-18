# Solana launchpad decoder

Decode any Solana bonding-curve launch — **launch price, supply, market cap and
raise target** — straight from account bytes, at the program level.

No indexer, no API keys, no third-party price feed, no dependencies outside the
Python standard library. Point it at a node (or hand it bytes from whatever
stream your node already produces) and it answers from the chain.

```python
from launchpad_decoder import LaunchpadDecoder, JsonRpcAccountSource

decoder = LaunchpadDecoder(JsonRpcAccountSource("https://your-node:8899"))
m = decoder.decode_address("<bonding curve or pool address>")

m.launch_price_quote.value       # 2.7958993476234855e-08  SOL per token
m.launch_mcap_quote.value        # 27.958993476234856      SOL
m.total_supply.value             # 1_000_000_000.0
m.raise_target_quote.value       # 85.005359057            SOL
m.graduation_mcap_quote.value    # 410.88016812075745      SOL
m.progress.value                 # 0.0
m.launch_price_quote.source      # ValueSource.ONCHAIN_CONFIG
```

## Why the provenance matters

The numbers a naive launchpad indexer hardcodes — pump.fun's `1.073e15`
virtual token reserve, its 30 SOL virtual quote reserve, the 85 SOL graduation
target — are **program state**, not constants. pump.fun can change them with a
single transaction and never redeploy.

So every value carries where it came from, and the decoder notices when the
ground moves:

```python
decoder.snapshot_static_params()

report = decoder.refresh()     # on a timer, or on a ProgramData account update
if report:
    print(report.describe())
    # 6EF8rr… redeployed: slot 351000000 -> 351400000
    # pumpfun.configs.global.values.initial_virtual_sol_reserves: 30000000000 -> 42000000000
```

`refresh()` re-reads each program's `ProgramData` account; on a redeploy it
drops the cached IDL and every cached config account, re-resolves them
(preferring the IDL the program publishes on chain), and returns the parameter
diff. The next decode uses the new numbers.

## What is covered

| Launchpad | Program | Curve |
|---|---|---|
| pump.fun | `6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P` | constant product, virtual reserves |
| PumpSwap *(graduation AMM)* | `pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA` | constant product AMM |
| Raydium LaunchLab | `LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj` | constant product / fixed / linear |
| Meteora DBC | `dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN` | piecewise sqrt-price segments |
| Moonit *(Moonshot)* | `MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qcqUQTrG` | 5 curve types, market-cap trigger |
| Heaven | `HEAVEnMX7RoaYCucpyFterLWzFJR8Ah26oNSnqBs5Jtn` | AMM with virtual seed, no graduation |
| Vertigo | `vrTGoBuy5rYSxAfV3jaRJWHH6nN9WK4NRExGxsk1bCJ` | one-sided constant product |
| GoFundMeme | `GFMioXjhuDWMEBtuaoaDPJFPEnL2yDHCWKoVPhj1MeA7` | power law over quote raised |
| Boop.fun | `boop8hVGQGqehUK2iVEMEnMrL5RbjywRzHKBmBE7ry4` | layout from its on-chain IDL |
| Daos.fun | `5jnapfrAN47UYkLkEf7HnprPPBCQLvkYWGZDeKkaP5hv` | layout from its on-chain IDL |

**That table is not the list of launchpads.** Raydium LaunchLab and Meteora DBC
are multi-tenant: LetsBonk.fun, Believe, Jupiter Studio, Bags, Cook.meme and
the rest are *config accounts inside those programs*, each with its own curve
shape, supply and raise target. Enumerate them from the chain instead of
maintaining a list:

```bash
python scripts/launchpads.py platforms meteora_dbc
python scripts/launchpads.py platforms raydium_launchlab
```

And a launchpad nobody has written an adapter for still decodes, as long as it
publishes an Anchor IDL on chain — the heuristic adapter maps field names to
curve roles and reports a confidence score.

## CLI

```bash
python scripts/launchpads.py list                  # the registry
python scripts/launchpads.py math pumpfun          # curve numbers, no node needed

export SOLANA_RPC=https://your-node:8899
python scripts/launchpads.py params pumpfun        # live launch params + provenance
python scripts/launchpads.py decode <address>      # one curve
python scripts/launchpads.py platforms meteora_dbc # every DBC launchpad
python scripts/launchpads.py idl-status            # who publishes an IDL on chain
python scripts/launchpads.py deployments           # deploy slot + upgrade authority
python scripts/launchpads.py watch --interval 60   # log upgrades and param changes
```

## Wiring into a live node

The hot path takes bytes and never calls out, so it drops into any account
stream — Yellowstone/Geyser gRPC, a Geyser plugin, `accountSubscribe`, or a
ledger replay. Implement four methods (`get_account`,
`get_multiple_accounts`, `get_program_accounts`, `get_slot`) over your source
and the rest is unchanged.

```bash
python scripts/live_node_example.py    # runnable, simulated feed, no node required
```

It prints the exact subscription filters to register (program id + the 8-byte
account discriminator as a `memcmp`), the `ProgramData` addresses to watch for
redeploys, and a simulated pump.fun curve walking from 28 SOL to 411 SOL.

## Layout

```
launchpad_decoder/
  anchor_idl.py     compile any Anchor IDL (both dialects) into account decoders
  borsh.py          borsh reader, with prefix decoding for older account versions
  base58.py         base58 codec
  pubkey.py         PDA derivation (ed25519 on-curve check, create_with_seed)
  pdas.py           per-launchpad PDA helpers, each verified in tests
  curves.py         all curve maths, in raw on-chain units
  types.py          LaunchMetrics, Param, ValueSource, CurveFamily
  rpc.py            AccountSource protocol + JSON-RPC and static implementations
  program_state.py  upgradeable-loader parsing, ProgramWatcher, on-chain IDL loader
  registry.py       launchpad registry + platform discovery metadata
  decoder.py        the façade
  adapters/         one per launchpad, plus the heuristic fallback
  idl/              bundled IDL snapshots
docs/
  LAUNCHPADS.md             per-launchpad reference: accounts, fields, gotchas
  CURVE_MATH.md             every formula, derived
  PROGRAM_LEVEL_DECODING.md the architecture and the upgrade story
scripts/
  launchpads.py             CLI
  live_node_example.py      live-feed wiring
tests/                      96 tests, no network
```

## Tests

```bash
pip install pytest
python -m pytest tests/ -q
```

They run entirely offline against synthetic, byte-exact fixtures built by a
Borsh *encoder* driven by the same IDL layouts the decoder uses — so encoder
and decoder check each other.

Ground truth is pinned wherever a published value exists:

* pump.fun's PDA (`4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf`), LaunchLab's
  default `GlobalConfig` and LetsBonk's `PlatformConfig` are all re-derived
  from seeds and compared to the published addresses;
* the account discriminators in the programs' IDLs are re-derived from
  `sha256("account:<Name>")`;
* pump.fun's 85.005359057 SOL raise target and 410.88 SOL graduation cap;
* Moonit's `marketCapToMinimalTokens` table from `@heliofi/launchpad-common`
  (two entries exact, two to 1e-13 relative — the SDK computes them in
  floating point);
* a simulated program upgrade that moves the raise target from 85 to 170 SOL.

## Caveats

* Layout snapshots in `launchpad_decoder/idl/` were current when captured; the
  decoder prefers each program's on-chain IDL when one exists, and reports
  which source it used in `metrics.schema_source`.
* Moonit's curve reserve constants are compiled into its program binary, so
  they cannot be verified against a cluster. They are always tagged
  `bundled_snapshot`.
* Market cap here is always price × **total supply**. Moonit's own market cap
  is price × tokens *sold*; see `docs/CURVE_MATH.md`.
* Raise targets are pre-fee. pump.fun's 85.005 SOL becomes ≈85.9 SOL paid once
  the 1% fee is added.
