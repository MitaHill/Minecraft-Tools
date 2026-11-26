package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadOrCreateNodeIDPersists(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("NODE_ID", "")
	first, err := loadOrCreateNodeID(dir)
	if err != nil {
		t.Fatal(err)
	}
	if !nodeIDPattern.MatchString(first) {
		t.Fatalf("unexpected node id %q", first)
	}
	second, err := loadOrCreateNodeID(dir)
	if err != nil {
		t.Fatal(err)
	}
	if second != first {
		t.Fatalf("node id changed: %q then %q", first, second)
	}
	info, err := os.Stat(filepath.Join(dir, "node-id"))
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm() != 0600 {
		t.Fatalf("node id mode = %v, want 0600", info.Mode().Perm())
	}
}

func TestLoadOrCreateNodeIDRejectsInvalidFile(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("NODE_ID", "")
	if err := os.WriteFile(filepath.Join(dir, "node-id"), []byte("Not A Node\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadOrCreateNodeID(dir); err == nil {
		t.Fatal("accepted an invalid node id")
	}
}

func TestLoadOrCreateNodeIDUsesLegacyEnvAndPersistsIt(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("NODE_ID", "legacy-node")
	first, err := loadOrCreateNodeID(dir)
	if err != nil {
		t.Fatal(err)
	}
	if first != "legacy-node" {
		t.Fatalf("node id = %q, want legacy-node", first)
	}
	if err := os.Unsetenv("NODE_ID"); err != nil {
		t.Fatal(err)
	}
	second, err := loadOrCreateNodeID(dir)
	if err != nil {
		t.Fatal(err)
	}
	if second != "legacy-node" {
		t.Fatalf("node id changed after env removal: %q", second)
	}
}
