package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net"
	"net/netip"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"
)

type portRangePolicy struct {
	Revision int64    `json:"revision"`
	Start    int      `json:"start"`
	End      int      `json:"end"`
	Ranges   [][2]int `json:"ranges,omitempty"`
}

func (policy portRangePolicy) effectiveRanges() [][2]int {
	if len(policy.Ranges) > 0 {
		return policy.Ranges
	}
	return [][2]int{{policy.Start, policy.End}}
}

func sameRangePolicy(previous, next portRangePolicy) bool {
	return previous.Revision == next.Revision &&
		previous.Start == next.Start &&
		previous.End == next.End &&
		slices.Equal(previous.Ranges, next.Ranges)
}

func portInRanges(ranges [][2]int, port int) bool {
	return slices.ContainsFunc(ranges, func(item [2]int) bool {
		return item[0] <= port && port <= item[1]
	})
}

func mergePortRanges(ranges [][2]int) [][2]int {
	sorted := slices.Clone(ranges)
	slices.SortFunc(sorted, func(a, b [2]int) int { return a[0] - b[0] })
	merged := make([][2]int, 0, len(sorted))
	for _, item := range sorted {
		if n := len(merged); n > 0 && item[0] <= merged[n-1][1]+1 {
			merged[n-1][1] = max(item[1], merged[n-1][1])
		} else {
			merged = append(merged, item)
		}
	}
	return merged
}

type ipBan struct {
	IP        string   `json:"ip"`
	Reason    string   `json:"reason"`
	ExpiresAt *float64 `json:"expires_at"`
	network   netip.Prefix
}

type blacklistPolicy struct {
	Revision int64   `json:"revision"`
	Entries  []ipBan `json:"entries"`
}

type nodePolicy struct {
	Range     *portRangePolicy `json:"range,omitempty"`
	Blacklist *blacklistPolicy `json:"blacklist,omitempty"`
}

func normalizedIP(address string) string {
	ip, err := netip.ParseAddr(address)
	if err != nil || ip.Zone() != "" {
		return ""
	}
	return ip.Unmap().String()
}

func allowedPorts(physicalStart, physicalEnd int, configured string) ([][2]int, error) {
	ports := make([][2]int, 0)
	if configured == "" {
		return append(ports, [2]int{physicalStart, physicalEnd}), nil
	}
	for value := range strings.SplitSeq(configured, ",") {
		first, last, hasEnd := strings.Cut(value, "-")
		if !hasEnd {
			last = first
		}
		start, startErr := strconv.Atoi(first)
		end, endErr := strconv.Atoi(last)
		if startErr != nil || endErr != nil || start < 1 || end < start || end > 65535 {
			return nil, fmt.Errorf("invalid frps port range %q", value)
		}
		start, end = max(start, physicalStart), min(end, physicalEnd)
		if start <= end {
			ports = append(ports, [2]int{start, end})
		}
	}
	slices.SortFunc(ports, func(a, b [2]int) int { return a[0] - b[0] })
	merged := make([][2]int, 0, len(ports))
	for _, port := range ports {
		n := len(merged)
		if n > 0 && port[0] <= merged[n-1][1]+1 {
			merged[n-1][1] = max(port[1], merged[n-1][1])
		} else {
			merged = append(merged, port)
		}
	}
	return merged, nil
}

func inDeployedRange(ports [][2]int, ranges [][2]int) bool {
	deployed := mergePortRanges(ports)
	for _, item := range ranges {
		if !slices.ContainsFunc(deployed, func(allowed [2]int) bool {
			return allowed[0] <= item[0] && item[1] <= allowed[1]
		}) {
			return false
		}
	}
	return true
}

func (agent *Agent) loadPolicy() error {
	body, err := os.ReadFile(agent.policyFile)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	var policy nodePolicy
	if err := json.Unmarshal(body, &policy); err != nil {
		return fmt.Errorf("read cached node policy: %w", err)
	}
	if policy.Blacklist != nil {
		if err := policy.Blacklist.parseEntries(); err != nil {
			return fmt.Errorf("read cached node policy: %w", err)
		}
	}
	agent.policy = policy
	agent.policyReady = policy.Range != nil
	return nil
}

func (agent *Agent) replacePolicy(next nodePolicy) error {
	if err := writeState(agent.policyFile, next); err != nil {
		return err
	}
	agent.policy = next
	return nil
}

func (agent *Agent) applyRange(ctx context.Context, policy portRangePolicy, ports [][2]int) error {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	ranges := policy.effectiveRanges()
	if !inDeployedRange(ports, ranges) {
		return errors.New("范围超出 frps 与 Docker 已部署的端口")
	}
	if previous := agent.policy.Range; previous != nil && sameRangePolicy(*previous, policy) {
		agent.policyReady = true
		return nil
	}
	active, err := agent.dashboard.activeProxies(ctx)
	if err != nil {
		return fmt.Errorf("读取 frps 映射: %w", err)
	}
	excluded := func(port int) bool { return !portInRanges(ranges, port) }
	conflicts := make(map[int]bool)
	for _, proxy := range active {
		if proxy.Status == "online" && excluded(proxy.Config.RemotePort) {
			conflicts[proxy.Config.RemotePort] = true
		}
	}
	for name, registered := range agent.recentProxies {
		if time.Since(registered) > 30*time.Second {
			delete(agent.recentProxies, name)
		} else if proxy := agent.proxies[name]; !proxy.Closed && excluded(proxy.RemotePort) {
			conflicts[proxy.RemotePort] = true
		}
	}
	if len(conflicts) != 0 {
		ports := slices.Sorted(maps.Keys(conflicts))
		return fmt.Errorf("以下端口仍在使用或注册中：%v", ports)
	}
	next := agent.policy
	next.Range = &policy
	if err := agent.replacePolicy(next); err != nil {
		return err
	}
	agent.policyReady = true
	return nil
}

func (policy *blacklistPolicy) parseEntries() error {
	for i := range policy.Entries {
		entry := &policy.Entries[i]
		if address := normalizedIP(entry.IP); address != "" {
			ip := netip.MustParseAddr(address)
			entry.network = netip.PrefixFrom(ip, ip.BitLen())
			continue
		}
		prefix, err := netip.ParsePrefix(entry.IP)
		if err != nil || prefix != prefix.Masked() || prefix.Addr().Is4In6() {
			return fmt.Errorf("invalid blacklist target %q", entry.IP)
		}
		entry.network = prefix
	}
	return nil
}

func (entry ipBan) active(now float64) bool {
	return entry.ExpiresAt == nil || *entry.ExpiresAt > now
}

func (agent *Agent) hasActiveBlacklist() bool {
	if agent.policy.Blacklist == nil {
		return false
	}
	now := float64(time.Now().UnixMilli()) / 1000
	return slices.ContainsFunc(agent.policy.Blacklist.Entries, func(entry ipBan) bool {
		return entry.active(now)
	})
}

func (agent *Agent) blockedIP(address string) string {
	if agent.policy.Blacklist == nil {
		return ""
	}
	ip, err := netip.ParseAddr(address)
	if err != nil || ip.Zone() != "" {
		return ""
	}
	ip = ip.Unmap()
	now := float64(time.Now().UnixMilli()) / 1000
	best := -1
	reason := ""
	for _, entry := range agent.policy.Blacklist.Entries {
		if entry.network.Bits() > best && entry.active(now) && entry.network.Contains(ip) {
			best, reason = entry.network.Bits(), entry.Reason
		}
	}
	return reason
}

func (agent *Agent) applyBlacklist(ctx context.Context, policy blacklistPolicy) error {
	if err := policy.parseEntries(); err != nil {
		return err
	}
	agent.mu.Lock()
	next := agent.policy
	next.Blacklist = &policy
	err := agent.replacePolicy(next)
	agent.mu.Unlock()
	if err != nil || len(policy.Entries) == 0 {
		return err
	}

	// Leave enough time for the next heartbeat even if a broad rule matches many sockets.
	batch, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	clients, err := agent.dashboard.activeClients(batch)
	if err != nil {
		return err
	}
	seen := make(map[string]bool)
	var firstError error
	for _, ip := range clients {
		if seen[ip] {
			continue
		}
		seen[ip] = true
		agent.mu.Lock()
		blocked := agent.blockedIP(ip) != ""
		agent.mu.Unlock()
		if !blocked {
			continue
		}
		if err := batch.Err(); err != nil {
			return fmt.Errorf("remaining blacklist connections will be retried: %w", err)
		}
		attempt, stop := context.WithTimeout(batch, 4*time.Second)
		_, err := firewallCall(attempt, agent.firewallSocket, FirewallRequest{
			Action: "disconnect-client", ClientIP: ip,
		})
		stop()
		if err != nil && firstError == nil {
			firstError = err
		}
	}
	return firstError
}

func (agent *Agent) checkLogin(address string) string {
	host, _, err := net.SplitHostPort(address)
	if err != nil {
		return "invalid FRP client address"
	}
	return agent.blockedIP(normalizedIP(host))
}
