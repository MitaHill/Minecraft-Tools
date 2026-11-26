package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPortRangePolicyFallsBackToSingleRange(t *testing.T) {
	legacy := portRangePolicy{Revision: 4, Start: 30001, End: 30003}
	if got := legacy.effectiveRanges(); len(got) != 1 || got[0] != [2]int{30001, 30003} {
		t.Fatalf("unexpected legacy ranges: %#v", got)
	}
	multi := portRangePolicy{
		Revision: 5,
		Start:    30001,
		End:      31000,
		Ranges:   [][2]int{{30001, 30003}, {31000, 31000}},
	}
	if got := multi.effectiveRanges(); len(got) != 2 {
		t.Fatalf("unexpected multi ranges: %#v", got)
	}
}

func TestPortInRanges(t *testing.T) {
	ranges := [][2]int{{30001, 30003}, {31000, 31000}}
	for port, want := range map[int]bool{
		30000: false, 30001: true, 30003: true, 30004: false, 31000: true, 31001: false,
	} {
		if got := portInRanges(ranges, port); got != want {
			t.Errorf("port %d: got %v want %v", port, got, want)
		}
	}
}

func TestMergePortRangesMergesAdjacentRanges(t *testing.T) {
	merged := mergePortRanges([][2]int{{30003, 30005}, {30001, 30002}, {31000, 31000}})
	want := [][2]int{{30001, 30005}, {31000, 31000}}
	if len(merged) != len(want) {
		t.Fatalf("unexpected merged ranges: %#v", merged)
	}
	for index := range want {
		if merged[index] != want[index] {
			t.Fatalf("unexpected merged ranges: %#v", merged)
		}
	}
}

func TestInDeployedRangeChecksEverySegment(t *testing.T) {
	deployed := [][2]int{{30000, 30002}, {30003, 30005}, {31000, 31010}}
	if !inDeployedRange(deployed, [][2]int{{30001, 30004}, {31000, 31000}}) {
		t.Fatal("expected ranges within merged deployed ranges")
	}
	if inDeployedRange(deployed, [][2]int{{30001, 30006}}) {
		t.Fatal("expected gap between deployed ranges to be rejected")
	}
	if inDeployedRange(deployed, [][2]int{{30001, 30002}, {31011, 31011}}) {
		t.Fatal("expected range outside deployed ports to be rejected")
	}
}

func TestLoadPolicyAcceptsLegacyCachedRange(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	if err := os.WriteFile(path, []byte(
		`{"range":{"revision":2,"start":30001,"end":30002}}`,
	), 0o600); err != nil {
		t.Fatal(err)
	}
	agent := &Agent{policyFile: path}
	if err := agent.loadPolicy(); err != nil {
		t.Fatal(err)
	}
	if !agent.policyReady || agent.policy.Range == nil {
		t.Fatal("expected legacy cached policy to be ready")
	}
	if got := agent.policy.Range.effectiveRanges(); got[0] != [2]int{30001, 30002} {
		t.Fatalf("unexpected ranges: %#v", got)
	}
}

func TestSameRangePolicyDetectsAddedRanges(t *testing.T) {
	legacy := portRangePolicy{Revision: 0, Start: 33001, End: 33002}
	withRanges := portRangePolicy{
		Revision: 0, Start: 33001, End: 33002, Ranges: [][2]int{{33001, 33002}},
	}
	if sameRangePolicy(legacy, legacy) != true {
		t.Fatal("identical policies should match")
	}
	if sameRangePolicy(legacy, withRanges) {
		t.Fatal("policy gaining ranges must be re-applied")
	}
	if !sameRangePolicy(withRanges, withRanges) {
		t.Fatal("identical multi-range policies should match")
	}
}
