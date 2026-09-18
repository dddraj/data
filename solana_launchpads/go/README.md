# launchpad — Go decoder

Production port of the Python reference implementation in the parent directory.
Decodes Solana bonding-curve launch state into prices, supply, market caps and
raise targets.

Standard library only. **134 ns and zero allocations** to decode a curve and
price it.

```go
import "github.com/dddraj/data/solana_launchpads/go/launchpad"

// once, from the program's Global account
var g launchpad.PumpfunGlobal
if _, err := launchpad.DecodePumpfunGlobal(globalAccountData, &g); err != nil { ... }
params := launchpad.NewPumpfunParams(&g, 9) // 9 = SOL quote decimals

// per curve, on every account update
var curve launchpad.PumpfunBondingCurve
truncatedAt, err := launchpad.DecodePumpfunBondingCurve(accountData, &curve)
m := params.Metrics(&curve, truncatedAt)

m.LaunchPrice      // 2.7958993476234855e-08 SOL per token
m.CurrentPrice     // ...
m.RaiseTarget      // 85.005359057 SOL
m.GraduationMcap   // 410.88016812075745 SOL
m.OK()             // false if the reserve pair failed its invariant
```

## Scope

Only what belongs in an ingest or serving path. Research tooling — triaging
unknown creator programs, auditing stored rows against the chain, checking
bundled layouts against a cluster, program-upgrade watching — stays in Python
and runs out of band. Those are operator tools, not hot-path code.

## Three things worth knowing

**Layouts are generated, not transcribed.** `layouts_gen.go` comes from the same
IDL snapshots the Python side uses:

```sh
python scripts/gen_go.py && (cd go && gofmt -w . && go test ./...)
```

Hand-copying a byte offset is exactly the kind of mistake that produces a
plausible wrong number, so nobody does it.

**`Identify` takes the program id, and that is not padding.** Anchor derives
discriminators from the struct name alone, so they are unique only *within* a
program. PumpSwap's `GlobalConfig` and Raydium LaunchLab's `GlobalConfig` are
byte-identical; so are PumpSwap's `Pool` and Vertigo's `Pool`. Resolving on the
discriminator alone decodes one program's account with another's layout.

**Truncation is normal, not an error.** `Decode*` returns the name of the first
field the account was too short to hold. pump.fun appends fields and reallocs
accounts lazily, so a live cluster holds accounts written by several
generations of the program at once. Everything the prefix covers is valid.

## Agreement with the reference

`testdata/golden.json` holds real account bytes plus the numbers the Python
decoder produces from them. `golden_test.go` asserts this package computes the
same, to 1e-12 relative, on every field. A port that disagrees with its
reference is worthless, so divergence fails the build.

Regenerate with `python scripts/gen_golden.py`.

The vectors cover a fresh curve, mid-curve, a completed curve, a legacy
truncated account, and the field-mixing anomaly measured in production — the
last of which must come back `OK() == false` with `SuspectField ==
"virtual_quote"` in both languages.

## The invariant check

`CheckConstantProductPair` tests whether a reserve pair actually lies on the
curve it claims to be on. `vBase*vQuote` is invariant; flooring moves it *up* by
parts per billion over a curve's life and never down. A pair missing k by a
visible margin did not come off one account read — in practice two fields
sourced from different places and merged into one row, which passes every
schema check and produces a confidently wrong price.

`DiagnosePair` then names which of the two to distrust, by comparing how far
each has travelled past its bound, and reports what the other implies it should
have been.

One limitation, stated in the code as well: the opening k comes from the
program's config *as it stands now*. If a launchpad ever changed its opening
reserves, curves created before the change sit on a different k and are flagged
although they are correct. That false positive is a cluster of old curves all
off by the same ratio; mixed fields scatter. Bucket the ratio to tell them
apart.

## Tests

```sh
go test ./...                              # 25 tests
go test -run=XXX -bench=. -benchmem ./...  # 134 ns/op, 0 allocs/op
```

The allocation budget is asserted, not just measured: `TestDecodingACurveDoesNotAllocate`
and `TestPricingAHealthyCurveDoesNotAllocate` fail if either path starts
allocating.

## Coverage

Generated layouts exist for every launchpad in the registry — pump.fun,
PumpSwap, Raydium LaunchLab, Meteora DBC, Moonit, Heaven, Vertigo, GoFundMeme —
so their accounts decode today. Metrics adapters are currently written for
pump.fun; the others decode to typed structs and need their curve maths wired
up the same way, which is a small job per launchpad given `curves.go` already
carries the maths. See `docs/CURVE_MATH.md` for the derivations.
