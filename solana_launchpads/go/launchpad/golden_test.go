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
	FeeBps          float64  `json:"fee_bps"`
	Violations      []string `json:"violations"`
	Truncated       bool     `json:"truncated"`
	SuspectField    string   `json:"suspect_field"`
}

type goldenCase struct {
	Name      string       `json:"name"`
	Note      string       `json:"note"`
	GlobalHex string       `json:"global_hex"`
	CurveHex  string       `json:"curve_hex"`
	Expect    goldenExpect `json:"expect"`
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

func TestGoldenVectorsMatchThePythonReference(t *testing.T) {
	golden := loadGolden(t)
	cases := golden["pumpfun"]
	if len(cases) == 0 {
		t.Fatal("no pump.fun golden vectors; run scripts/gen_golden.py")
	}

	for _, tc := range cases {
		t.Run(tc.Name, func(t *testing.T) {
			var g PumpfunGlobal
			if _, err := DecodePumpfunGlobal(mustHex(t, tc.GlobalHex), &g); err != nil {
				t.Fatalf("decoding Global: %v", err)
			}
			params := NewPumpfunParams(&g, 9)

			var curve PumpfunBondingCurve
			truncatedAt, err := DecodePumpfunBondingCurve(mustHex(t, tc.CurveHex), &curve)
			if err != nil {
				t.Fatalf("decoding BondingCurve: %v", err)
			}
			got := params.Metrics(&curve, truncatedAt)

			checks := []struct {
				field string
				got   float64
				want  float64
			}{
				{"total_supply", got.TotalSupply, tc.Expect.TotalSupply},
				{"tokens_for_sale", got.TokensForSale, tc.Expect.TokensForSale},
				{"tokens_sold", got.TokensSold, tc.Expect.TokensSold},
				{"launch_price", got.LaunchPrice, tc.Expect.LaunchPrice},
				{"current_price", got.CurrentPrice, tc.Expect.CurrentPrice},
				{"graduation_price", got.GraduationPrice, tc.Expect.GraduationPrice},
				{"launch_mcap", got.LaunchMcap, tc.Expect.LaunchMcap},
				{"current_mcap", got.CurrentMcap, tc.Expect.CurrentMcap},
				{"graduation_mcap", got.GraduationMcap, tc.Expect.GraduationMcap},
				{"raise_target", got.RaiseTarget, tc.Expect.RaiseTarget},
				{"raised", got.Raised, tc.Expect.Raised},
				{"progress", got.Progress, tc.Expect.ProgressValue},
				{"fee_bps", got.FeeBps, tc.Expect.FeeBps},
			}
			for _, c := range checks {
				if !closeEnough(c.got, c.want) {
					t.Errorf("%s: go %.17g, python %.17g", c.field, c.got, c.want)
				}
			}

			if got.Complete != tc.Expect.Complete {
				t.Errorf("complete: go %v, python %v", got.Complete, tc.Expect.Complete)
			}

			var names []string
			for _, v := range got.Violations {
				names = append(names, v.Name)
			}
			sort.Strings(names)
			want := append([]string(nil), tc.Expect.Violations...)
			sort.Strings(want)
			if len(names) != len(want) {
				t.Fatalf("violations: go %v, python %v", names, want)
			}
			for i := range names {
				if names[i] != want[i] {
					t.Errorf("violations: go %v, python %v", names, want)
					break
				}
			}

			if got.SuspectField != tc.Expect.SuspectField {
				t.Errorf("suspect field: go %q, python %q", got.SuspectField, tc.Expect.SuspectField)
			}
			if (truncatedAt != "") != tc.Expect.Truncated {
				t.Errorf("truncation: go %q, python truncated=%v", truncatedAt, tc.Expect.Truncated)
			}
		})
	}
}

func TestGoldenAnomalyIsRejected(t *testing.T) {
	// The production row must not be published as a price, in either language.
	golden := loadGolden(t)
	for _, tc := range golden["pumpfun"] {
		if tc.Name != "field_mixing_anomaly" {
			continue
		}
		var g PumpfunGlobal
		if _, err := DecodePumpfunGlobal(mustHex(t, tc.GlobalHex), &g); err != nil {
			t.Fatal(err)
		}
		var curve PumpfunBondingCurve
		truncatedAt, err := DecodePumpfunBondingCurve(mustHex(t, tc.CurveHex), &curve)
		if err != nil {
			t.Fatal(err)
		}
		m := NewPumpfunParams(&g, 9).Metrics(&curve, truncatedAt)
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
	golden := loadGolden(t)
	for _, tc := range golden["pumpfun"] {
		if tc.Name == "field_mixing_anomaly" {
			continue
		}
		var g PumpfunGlobal
		if _, err := DecodePumpfunGlobal(mustHex(t, tc.GlobalHex), &g); err != nil {
			t.Fatal(err)
		}
		var curve PumpfunBondingCurve
		truncatedAt, err := DecodePumpfunBondingCurve(mustHex(t, tc.CurveHex), &curve)
		if err != nil {
			t.Fatal(err)
		}
		if m := NewPumpfunParams(&g, 9).Metrics(&curve, truncatedAt); !m.OK() {
			t.Errorf("%s: unexpected violations %v", tc.Name, m.Violations)
		}
	}
}
