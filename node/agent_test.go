package main

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func callPlugin(t *testing.T, agent *Agent, operation, payload string) bool {
	t.Helper()
	request := httptest.NewRequest("POST", "/handler?op="+operation, strings.NewReader(payload))
	response := httptest.NewRecorder()
	agent.plugin(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("plugin returned %d: %s", response.Code, response.Body.String())
	}
	var result struct {
		Reject bool `json:"reject"`
	}
	if err := json.NewDecoder(response.Body).Decode(&result); err != nil {
		t.Fatal(err)
	}
	return result.Reject
}

func TestProxyRegistrationAndDashboardSnapshot(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proxies.json")
	agent, err := openAgent(path, 30001, 30002)
	if err != nil {
		t.Fatal(err)
	}
	valid := `{"content":{"user":{"run_id":"run-a"},"proxy_name":"alpha","proxy_type":"tcp","remote_port":30001,"metas":{"install_id":"install-a"}}}`
	if callPlugin(t, agent, "NewProxy", valid) {
		t.Fatal("valid TCP registration was rejected")
	}
	for _, invalid := range []string{
		strings.Replace(valid, `30001`, `0`, 1),
		strings.Replace(valid, `30001`, `30003`, 1),
		strings.Replace(valid, `"tcp"`, `"udp"`, 1),
		strings.Replace(valid, `"remote_port"`, `"group":"shared","remote_port"`, 1),
	} {
		if !callPlugin(t, agent, "NewProxy", invalid) {
			t.Fatalf("invalid registration was accepted: %s", invalid)
		}
	}
	if callPlugin(t, agent, "CloseProxy", `{"content":{"user":{"run_id":"old-run"},"proxy_name":"alpha"}}`) {
		t.Fatal("close notification was rejected")
	}

	agent, err = openAgent(path, 30001, 30002)
	if err != nil {
		t.Fatal(err)
	}
	web := httptest.NewServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/proxy/tcp" {
			http.NotFound(writer, request)
			return
		}
		writer.Write([]byte(`{"proxies":[
			{"name":"alpha","status":"online","conf":{"remotePort":30001}},
			{"name":"unknown","status":"online","conf":{"remotePort":30002}}
		]}`))
	}))
	defer web.Close()
	dashboard, err := newDashboard(web.URL, "", "")
	if err != nil {
		t.Fatal(err)
	}
	proxies, err := dashboard.snapshot(context.Background(), agent)
	if err != nil {
		t.Fatal(err)
	}
	if len(proxies) != 1 || proxies[0] != (Proxy{RunID: "run-a", Name: "alpha", RemotePort: 30001}) {
		t.Fatalf("unexpected snapshot: %#v", proxies)
	}
}

func TestRejectsCommandsForAnOldGenerationOrPortRevision(t *testing.T) {
	agent, err := openAgent(filepath.Join(t.TempDir(), "proxies.json"), 30001, 30002)
	if err != nil {
		t.Fatal(err)
	}
	agent.proxies["alpha"] = Proxy{RunID: "new-run", Name: "alpha", RemotePort: 30001}
	oldGeneration := FirewallRequest{Action: "block", Port: 30001, CommandID: 18, RunID: "old-run", ProxyName: "alpha"}
	if err := agent.executeCommand(context.Background(), oldGeneration); err == nil || !strings.Contains(err.Error(), "generation") {
		t.Fatalf("old generation was not rejected: %v", err)
	}
	if agent.portBlocked(30001) {
		t.Fatal("old generation reserved the new tunnel's port")
	}
	agent.firewallState[30001] = FirewallRequest{Action: "unblock", Port: 30001, CommandID: 21, RunID: "new-run", ProxyName: "alpha"}
	staleBlock := FirewallRequest{Action: "block", Port: 30001, CommandID: 20, RunID: "new-run", ProxyName: "alpha"}
	if err := agent.executeCommand(context.Background(), staleBlock); err == nil || !strings.Contains(err.Error(), "stale") {
		t.Fatalf("old command ID was not rejected: %v", err)
	}
	if agent.portBlocked(30001) {
		t.Fatal("old command blocked the port again after unblocking")
	}
}

func TestManagedProxyKeepsOnlyProofDigestAndRetainsClosedGeneration(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proxies.json")
	agent, err := openAgent(path, 30001, 30002)
	if err != nil {
		t.Fatal(err)
	}
	const claim = "8f1eb6e685b04c5f9cc289241ac3c56e"
	const name = "minecraft-" + claim
	proof := strings.Repeat("x", 43)
	payload := fmt.Sprintf(`{"content":{"user":{"run_id":"run-a"},"proxy_name":%q,"proxy_type":"tcp","remote_port":30001,"metas":{"tunnel_id":%q,"tunnel_proof":%q}}}`, name, claim, proof)
	if callPlugin(t, agent, "NewProxy", payload) {
		t.Fatal("valid managed proxy was rejected")
	}
	closed := fmt.Sprintf(`{"content":{"user":{"run_id":"run-a"},"proxy_name":%q}}`, name)
	callPlugin(t, agent, "CloseProxy", closed)
	if !callPlugin(t, agent, "NewUserConn", closed) {
		t.Fatal("closed proxy accepted a new user")
	}
	saved, err := os.ReadFile(path)
	if err != nil || strings.Contains(string(saved), proof) {
		t.Fatalf("proof must not be stored with the proxy: %v", err)
	}
	agent, err = openAgent(path, 30001, 30002)
	if err != nil {
		t.Fatal(err)
	}
	registered := agent.proxies[name]
	want := Proxy{RunID: "run-a", Name: name, RemotePort: 30001, ClaimID: claim,
		ClaimHash: fmt.Sprintf("%x", sha256.Sum256([]byte(proof))), Closed: true}
	if registered != want {
		t.Fatalf("unexpected saved generation: %#v", registered)
	}
}

func TestLoginBlacklistExpiresAndNormalizesAddress(t *testing.T) {
	path := filepath.Join(t.TempDir(), "proxies.json")
	agent, err := openAgent(path, 30001, 30002)
	if err != nil {
		t.Fatal(err)
	}
	expired := float64(time.Now().Add(-time.Minute).Unix())
	agent.policy.Blacklist = &blacklistPolicy{Entries: []ipBan{
		{IP: "192.0.2.8", Reason: "blocked"},
		{IP: "2001:db8::8", Reason: "expired", ExpiresAt: &expired},
	}}
	if err := agent.policy.Blacklist.parseEntries(); err != nil {
		t.Fatal(err)
	}
	if !callPlugin(t, agent, "Login", `{"content":{"client_address":"[::ffff:192.0.2.8]:23456"}}`) {
		t.Fatal("blacklisted IPv4 address was accepted as an IPv4-mapped IPv6 address")
	}
	if callPlugin(t, agent, "Login", `{"content":{"client_address":"[2001:db8::8]:23456"}}`) {
		t.Fatal("expired blacklist entry still rejects login")
	}
}

func TestAllowedPortsIntersectAndPreserveGaps(t *testing.T) {
	ports, err := allowedPorts(30001, 30010, "30000-30002,30003,30008-30015")
	if err != nil {
		t.Fatal(err)
	}
	if !inDeployedRange(ports, 30001, 30003) || !inDeployedRange(ports, 30008, 30010) ||
		inDeployedRange(ports, 30003, 30008) || inDeployedRange(ports, 30000, 30002) {
		t.Fatalf("incorrect deployed ranges: %v", ports)
	}
}

func TestBlacklistCIDRChoosesMostSpecificActiveRule(t *testing.T) {
	expired := float64(time.Now().Add(-time.Minute).Unix())
	policy := &blacklistPolicy{Entries: []ipBan{
		{IP: "192.0.2.0/24", Reason: "subnet"},
		{IP: "192.0.2.8", Reason: "host"},
		{IP: "192.0.2.9", Reason: "expired", ExpiresAt: &expired},
		{IP: "2001:db8::/32", Reason: "ipv6 subnet"},
		{IP: "::/0", Reason: "any IPv6"},
	}}
	if err := policy.parseEntries(); err != nil {
		t.Fatal(err)
	}
	agent := &Agent{policy: nodePolicy{Blacklist: policy}}
	for _, example := range []struct {
		ip   string
		want string
	}{
		{"::ffff:192.0.2.8", "host"},
		{"192.0.2.9", "subnet"},
		{"192.0.3.8", ""},
		{"2001:db8::8", "ipv6 subnet"},
		{"2001:db9::8", "any IPv6"},
	} {
		if got := agent.blockedIP(example.ip); got != example.want {
			t.Errorf("blockedIP(%q) = %q, want %q", example.ip, got, example.want)
		}
	}
}
