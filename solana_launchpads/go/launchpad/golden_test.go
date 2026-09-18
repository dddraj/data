package launchpad

import (
	"encoding/hex"
	"encoding/json"
	"math"
	"os"
	"sort"
	"testing"
)

// The golden file is produced by scripts/gen_golden.py from the Python
// reference implementation: real account bytes plus the numbers Python
// computes from them. This package is a port, and a port that disagrees with
// its reference is worthless, so any divergence fails here.
//
// Because the vectors carry bytes rather than field values, they compare the
// two layout compilers as well as the two sets of maths.
//
// Regenerate after changing either side:
//
//	python scripts/gen_golden.py && (cd go && go test ./...)

type goldenExpect struct {
	TotalSupply     float64  `json:"total_supply"`
	TokensForSale   float64  `json:"tokens_for_sale"`
	TokensSold      float64  `json:"tokens_sold"`
	LaunchPrice     float64  `json:"launch_price"`
	CurrentPrice    float64  `json:"current_price"`
	GraduationPrice float64  `json:"graduation_price"`
	LaunchMcap      float64  `json:"launch_mcap"`
	CurrentMcap     float64  `json:"current_mcap"`
	GraduationMcap  float64  `json:"graduation_mcap"`
	RaiseTarget     float64  `json:"raise_target"`
	Raised          float64  `json:"raised"`
	ProgressValue   float64  `json:"progress"`
	Complete        bool     `json:"complete"`
	Migrated        bool     `json:"migrated"`
	CurveType       string   `json:"curve_type"`
	FeeBps          float64  `json:"fee_bps"`
	Violations      []string `json:"violations"`
	Truncated       bool     `json:"truncated"`
	SuspectField    string   `json:"suspect_field"`
}

type goldenCase struct {
	Name          string       `json:"name"`
	Note          string       `json:"note"`
	ConfigHex     string       `json:"config_hex"`
	PlatformHex   string       `json:"platform_hex"`
	StateHex      string       `json:"state_hex"`
	QuoteDecimals int          `json:"quote_decimals"`
	Expect        goldenExpect `json:"expect"`
}

func loadGolden(t *testing.T) map[string][]goldenCase {
	t.Helper()
	raw, err := os.ReadFile("testdata/golden.json")
	if err != nil {
		t.Fatalf("reading golden vectors: %v", err)
	}
	var out map[string][]goldenCase
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("parsing golden vectors: %v", err)
	}
	return out
}

func goldenSection(t *testing.T, launchpad string) []goldenCase {
	t.Helper()
	cases := loadGolden(t)[launchpad]
	if len(cases) == 0 {
		t.Fatalf("no %s golden vectors; run scripts/gen_golden.py", launchpad)
	}
	return cases
}

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex in golden file: %v", err)
	}
	return b
}

// closeEnough allows for the two languages formatting float64 differently,
// but nothing wider than that: these must agree to ~15 significant figures.
func closeEnough(got, want float64) bool {
	if want == 0 {
		return math.Abs(got) < 1e-12
	}
	return math.Abs(got-want)/math.Abs(want) < 1e-12
}

// compareMetrics asserts that one decoded Metrics matches what Python produced
// from the same bytes.
func compareMetrics(t *testing.T, got Metrics, want goldenExpect, truncatedAt string) {
	t.Helper()

	checks := []struct {
		field string
		got   float64
		want  float64
	}{
		{"total_supply", got.TotalSupply, want.TotalSupply},
		{"tokens_for_sale", got.TokensForSale, want.TokensForSale},
		{"tokens_sold", got.TokensSold, want.TokensSold},
		{"launch_price", got.LaunchPrice, want.LaunchPrice},
		{"current_price", got.CurrentPrice, want.CurrentPrice},
		{"graduation_price", got.GraduationPrice, want.GraduationPrice},
		{"launch_mcap", got.LaunchMcap, want.LaunchMcap},
		{"current_mcap", got.CurrentMcap, want.CurrentMcap},
		{"graduation_mcap", got.GraduationMcap, want.GraduationMcap},
		{"raise_target", got.RaiseTarget, want.RaiseTarget},
		{"raised", got.Raised, want.Raised},
		{"progress", got.Progress, want.ProgressValue},
		{"fee_bps", got.FeeBps, want.FeeBps},
	}
	for _, c := range checks {
		if !closeEnough(c.got, c.want) {
			t.Errorf("%s: go %.17g, python %.17g", c.field, c.got, c.want)
		}
	}

	if got.Complete != want.Complete {
		t.Errorf("complete: go %v, python %v", got.Complete, want.Complete)
	}
	if got.Migrated != want.Migrated {
		t.Errorf("migrated: go %v, python %v", got.Migrated, want.Migrated)
	}
	if got.CurveType != want.CurveType {
		t.Errorf("curve type: go %q, python %q", got.CurveType, want.CurveType)
	}

	var names []string
	for _, v := range got.Violations {
		names = append(names, v.Name)
	}
	sort.Strings(names)
	wantNames := append([]string(nil), want.Violations...)
	sort.Strings(wantNames)
	if len(names) != len(wantNames) {
		t.Fatalf("violations: go %v, python %v", names, wantNames)
	}
	for i := range names {
		if names[i] != wantNames[i] {
			t.Errorf("violations: go %v, python %v", names, wantNames)
			break
		}
	}

	if got.SuspectField != want.SuspectField {
		t.Errorf("suspect field: go %q, python %q", got.SuspectField, want.SuspectField)
	}
	if (truncatedAt != "") != want.Truncated {
		t.Errorf("truncation: go %q, python truncated=%v", truncatedAt, want.Truncated)
	}
}

// decodePumpfunGolden rebuilds one pump.fun vector from its bytes.
func decodePumpfunGolden(t *testing.T, tc goldenCase) (Metrics, string) {
	t.Helper()
	var g PumpfunGlobal
	if _, err := DecodePumpfunGlobal(mustHex(t, tc.ConfigHex), &g); err != nil {
		t.Fatalf("decoding Global: %v", err)
	}
	var curve PumpfunBondingCurve
	truncatedAt, err := DecodePumpfunBondingCurve(mustHex(t, tc.StateHex), &curve)
	if err != nil {
		t.Fatalf("decoding BondingCurve: %v", err)
	}
	return NewPumpfunParams(&g, tc.QuoteDecimals).Metrics(&curve, truncatedAt), truncatedAt
}

func TestGoldenPumpfunMatchesThePythonReference(t *testing.T) {
	for _, tc := range goldenSection(t, "pumpfun") {
		t.Run(tc.Name, func(t *testing.T) {
			got, truncatedAt := decodePumpfunGolden(t, tc)
			compareMetrics(t, got, tc.Expect, truncatedAt)
		})
	}
}

func TestGoldenRaydiumLaunchlabMatchesThePythonReference(t *testing.T) {
	for _, tc := range goldenSection(t, "raydium_launchlab") {
		t.Run(tc.Name, func(t *testing.T) {
			var g RaydiumLaunchlabGlobalConfig
			if _, err := DecodeRaydiumLaunchlabGlobalConfig(mustHex(t, tc.ConfigHex), &g); err != nil {
				t.Fatalf("decoding GlobalConfig: %v", err)
			}
			var pc RaydiumLaunchlabPlatformConfig
			if _, err := DecodeRaydiumLaunchlabPlatformConfig(mustHex(t, tc.PlatformHex), &pc); err != nil {
				t.Fatalf("decoding PlatformConfig: %v", err)
			}
			var pool RaydiumLaunchlabPoolState
			truncatedAt, err := DecodeRaydiumLaunchlabPoolState(mustHex(t, tc.StateHex), &pool)
			if err != nil {
				t.Fatalf("decoding PoolState: %v", err)
			}
			params := NewRaydiumLaunchlabParams(&g).WithPlatform(&pc)
			compareMetrics(t, params.Metrics(&pool, truncatedAt), tc.Expect, truncatedAt)
		})
	}
}

func TestGoldenMeteoraDbcMatchesThePythonReference(t *testing.T) {
	for _, tc := range goldenSection(t, "meteora_dbc") {
		t.Run(tc.Name, func(t *testing.T) {
			var cfg MeteoraDbcPoolConfig
			if _, err := DecodeMeteoraDbcPoolConfig(mustHex(t, tc.ConfigHex), &cfg); err != nil {
				t.Fatalf("decoding PoolConfig: %v", err)
			}
			var pool MeteoraDbcVirtualPool
			truncatedAt, err := DecodeMeteoraDbcVirtualPool(mustHex(t, tc.StateHex), &pool)
			if err != nil {
				t.Fatalf("decoding VirtualPool: %v", err)
			}
			params := NewMeteoraDbcParams(&cfg, tc.QuoteDecimals)
			compareMetrics(t, params.Metrics(&pool, truncatedAt), tc.Expect, truncatedAt)
		})
	}
}

func TestGoldenAnomalyIsRejected(t *testing.T) {
	// The production row must not be published as a price, in either language.
	for _, tc := range goldenSection(t, "pumpfun") {
		if tc.Name != "field_mixing_anomaly" {
			continue
		}
		m, _ := decodePumpfunGolden(t, tc)
		if m.OK() {
			t.Fatal("the k-violating pair should not report OK")
		}
		if m.PriceSource != "SUSPECT" {
			t.Errorf("price source = %q, want SUSPECT", m.PriceSource)
		}
		if m.SuspectField != "virtual_quote" {
			t.Errorf("suspect field = %q, want virtual_quote", m.SuspectField)
		}
		return
	}
	t.Fatal("field_mixing_anomaly vector missing")
}

func TestGoldenHealthyCurvesAreOK(t *testing.T) {
	for _, tc := range goldenSection(t, "pumpfun") {
		if tc.Name == "field_mixing_anomaly" {
			continue
		}
		if m, _ := decodePumpfunGolden(t, tc); !m.OK() {
			t.Errorf("%s: unexpected violations %v", tc.Name, m.Violations)
		}
	}
}
