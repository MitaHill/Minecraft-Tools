package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"
)

const firewallSocket = "/run/minecrafttools/firewall.sock"

type FirewallRequest struct {
	Action    string `json:"action"`
	Port      int    `json:"port,omitempty"`
	CommandID int64  `json:"command_id,omitempty"`
	RunID     string `json:"run_id,omitempty"`
	ProxyName string `json:"proxy_name,omitempty"`
	ClientIP  string `json:"client_ip,omitempty"`
}

type FirewallReply struct {
	OK               bool              `json:"ok"`
	Error            string            `json:"error,omitempty"`
	NetworkNamespace string            `json:"network_namespace,omitempty"`
	Commands         []FirewallRequest `json:"commands,omitempty"`
}

type Firewall struct {
	mu               sync.Mutex
	state            map[int]FirewallRequest
	stateFile        string
	portStart        int
	portEnd          int
	networkNamespace string
	healthy          bool
}

func firewallCall(ctx context.Context, socket string, request FirewallRequest) (FirewallReply, error) {
	var reply FirewallReply
	dialer := net.Dialer{Timeout: time.Second}
	conn, err := dialer.DialContext(ctx, "unix", socket)
	if err != nil {
		return reply, err
	}
	defer conn.Close()
	stop := context.AfterFunc(ctx, func() { _ = conn.Close() })
	defer stop()
	if err := conn.SetDeadline(time.Now().Add(8 * time.Second)); err != nil {
		return reply, err
	}
	if err := json.NewEncoder(conn).Encode(request); err != nil {
		return reply, err
	}
	if err := json.NewDecoder(io.LimitReader(conn, maxMessageSize)).Decode(&reply); err != nil {
		return reply, err
	}
	if !reply.OK {
		return reply, errors.New(reply.Error)
	}
	return reply, nil
}

func runNetworkTool(ctx context.Context, input, name string, args ...string) ([]byte, error) {
	timed, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	command := exec.CommandContext(timed, name, args...)
	command.Stdin = strings.NewReader(input)
	output, err := command.CombinedOutput()
	if err != nil {
		return nil, fmt.Errorf("%s: %w (%s)", name, err, strings.TrimSpace(string(output)))
	}
	return output, nil
}

func blockedPorts(state map[int]FirewallRequest) []int {
	ports := make([]int, 0, len(state))
	for port, record := range state {
		if record.Action == "block" {
			ports = append(ports, port)
		}
	}
	slices.Sort(ports)
	return ports
}

func applyFirewall(ctx context.Context, ports []int) error {
	var entries []string
	for _, port := range ports {
		entries = append(entries, strconv.Itoa(port))
	}
	var setup, initial string
	if _, err := runNetworkTool(ctx, "", "nft", "list", "table", "inet", "minecrafttools"); err == nil {
		setup = "delete table inet minecrafttools\n"
	}
	if len(entries) != 0 {
		initial = "elements = { " + strings.Join(entries, ", ") + " };"
	}
	setup += fmt.Sprintf(`table inet minecrafttools {
  set blocked_ports { type inet_service; %s }
  chain ingress {
    type filter hook input priority filter; policy accept;
    tcp dport @blocked_ports counter reject with tcp reset
  }
  chain egress {
    type filter hook output priority filter; policy accept;
    tcp sport @blocked_ports tcp flags & rst == 0 counter drop
  }
}
`, initial)
	if _, err := runNetworkTool(ctx, setup, "nft", "-f", "-"); err != nil {
		return err
	}
	return verifyFirewall(ctx, ports)
}

var expectedHooks = map[string]string{"ingress": "input", "egress": "output"}

var expectedRules = map[string]string{
	"ingress": `[
		{"match":{"op":"==","left":{"payload":{"protocol":"tcp","field":"dport"}},"right":"@blocked_ports"}},
		{"counter":null},{"reject":{"type":"tcp reset"}}]`,
	"egress": `[
		{"match":{"op":"==","left":{"payload":{"protocol":"tcp","field":"sport"}},"right":"@blocked_ports"}},
		{"match":{"op":"!","left":{"payload":{"protocol":"tcp","field":"flags"}},"right":4}},
		{"counter":null},{"drop":null}]`,
}

func verifyFirewall(ctx context.Context, desired []int) error {
	output, err := runNetworkTool(ctx, "", "nft", "-j", "-s", "-nn", "list", "table", "inet", "minecrafttools")
	if err != nil {
		return err
	}
	var result struct {
		Entries []struct {
			Set *struct {
				Name     string `json:"name"`
				Type     string `json:"type"`
				Elements []int  `json:"elem"`
			} `json:"set"`
			Chain *struct {
				Name     string `json:"name"`
				Type     string `json:"type"`
				Hook     string `json:"hook"`
				Priority int    `json:"prio"`
				Policy   string `json:"policy"`
			} `json:"chain"`
			Rule *struct {
				Chain string `json:"chain"`
				Expr  []any  `json:"expr"`
			} `json:"rule"`
		} `json:"nftables"`
	}
	if err := json.Unmarshal(output, &result); err != nil {
		return err
	}
	validSet := false
	chains := make(map[string]bool)
	rules := make(map[string]bool)
	for _, entry := range result.Entries {
		switch {
		case entry.Set != nil:
			slices.Sort(entry.Set.Elements)
			if validSet || entry.Set.Name != "blocked_ports" || entry.Set.Type != "inet_service" || !slices.Equal(entry.Set.Elements, desired) {
				return errors.New("kernel firewall set diverges from desired state")
			}
			validSet = true
		case entry.Chain != nil:
			chain := entry.Chain
			if expectedHooks[chain.Name] != chain.Hook || chain.Hook == "" || chain.Type != "filter" || chain.Priority != 0 || chain.Policy != "accept" || chains[chain.Name] {
				return errors.New("kernel firewall chain diverges from desired state")
			}
			chains[chain.Name] = true
		case entry.Rule != nil:
			rule := entry.Rule
			var want []any
			if rules[rule.Chain] || json.Unmarshal([]byte(expectedRules[rule.Chain]), &want) != nil || !reflect.DeepEqual(want, rule.Expr) {
				return errors.New("kernel firewall rule diverges from desired state")
			}
			rules[rule.Chain] = true
		}
	}
	if !validSet || len(chains) != len(expectedHooks) || len(rules) != len(expectedRules) {
		return errors.New("kernel firewall is missing its set, chains, or rules")
	}
	return nil
}

func closePortConnections(ctx context.Context, port int) error {
	return closeConnections(ctx, "connected", fmt.Sprintf("sport = :%d", port))
}

func closeConnections(ctx context.Context, state, filter string) error {
	if _, err := runNetworkTool(ctx, "", "ss", "-HKnt", "state", state, filter); err != nil {
		return err
	}
	output, err := runNetworkTool(ctx, "", "ss", "-Hnt", "state", state, filter)
	if err != nil {
		return err
	}
	if len(strings.TrimSpace(string(output))) != 0 {
		return errors.New("TCP connections remain after socket destroy")
	}
	return nil
}

func verifySocketDestroy(ctx context.Context) error {
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		return err
	}
	defer listener.Close()
	dialer := net.Dialer{Timeout: time.Second}
	client, err := dialer.DialContext(ctx, "tcp4", listener.Addr().String())
	if err != nil {
		return err
	}
	defer client.Close()
	server, err := listener.Accept()
	if err != nil {
		return err
	}
	defer server.Close()
	return closePortConnections(ctx, listener.Addr().(*net.TCPAddr).Port)
}

func (firewall *Firewall) handle(ctx context.Context, request FirewallRequest) (FirewallReply, error) {
	firewall.mu.Lock()
	defer firewall.mu.Unlock()
	if request.Action == "disconnect-client" {
		ip := normalizedIP(request.ClientIP)
		if ip == "" {
			return FirewallReply{}, errors.New("invalid FRP client IP")
		}
		// Docker publishes the frps control listener on port 7000 inside node-net.
		if err := closeConnections(ctx, "established", "sport = :7000 and dst "+ip); err != nil {
			return FirewallReply{}, err
		}
		return FirewallReply{OK: true}, nil
	}
	if request.Action == "ready" {
		if !firewall.healthy {
			return FirewallReply{}, errors.New("firewall is reconciling")
		}
		if err := verifyFirewall(ctx, blockedPorts(firewall.state)); err != nil {
			firewall.healthy = false
			return FirewallReply{}, err
		}
		return FirewallReply{OK: true, NetworkNamespace: firewall.networkNamespace}, nil
	}
	if request.Action == "state" {
		if !firewall.healthy {
			return FirewallReply{}, errors.New("firewall is reconciling")
		}
		var commands []FirewallRequest
		for _, command := range firewall.state {
			commands = append(commands, command)
		}
		return FirewallReply{OK: true, Commands: commands}, nil
	}
	if (request.Action != "block" && request.Action != "unblock") || request.CommandID <= 0 ||
		request.Port < firewall.portStart || request.Port > firewall.portEnd ||
		request.ProxyName == "" || request.RunID == "" {
		return FirewallReply{}, errors.New("invalid firewall command")
	}
	previous := firewall.state[request.Port]
	if request.CommandID < previous.CommandID || (request.CommandID == previous.CommandID && request != previous) {
		return FirewallReply{}, errors.New("stale firewall command")
	}
	if request != previous {
		if request.Action == "unblock" && (previous.Action != "block" ||
			previous.ProxyName != request.ProxyName || previous.RunID != request.RunID) {
			return FirewallReply{}, errors.New("unblock does not match the blocked tunnel")
		}
		firewall.state[request.Port] = request
		if err := writeState(firewall.stateFile, firewall.state); err != nil {
			if previous.CommandID == 0 {
				delete(firewall.state, request.Port)
			} else {
				firewall.state[request.Port] = previous
			}
			return FirewallReply{}, err
		}
	}
	firewall.healthy = false
	if err := applyFirewall(ctx, blockedPorts(firewall.state)); err != nil {
		return FirewallReply{}, err
	}
	if request.Action == "block" {
		if err := closePortConnections(ctx, request.Port); err != nil {
			return FirewallReply{}, err
		}
	}
	firewall.healthy = true
	return FirewallReply{OK: true}, nil
}

func (firewall *Firewall) restore(ctx context.Context) error {
	ports := blockedPorts(firewall.state)
	firewall.healthy = false
	if err := applyFirewall(ctx, ports); err != nil {
		return err
	}
	for _, port := range ports {
		if err := closePortConnections(ctx, port); err != nil {
			return err
		}
	}
	firewall.healthy = true
	return nil
}

func (firewall *Firewall) monitor(ctx context.Context) {
	ticker := time.NewTicker(3 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
		firewall.mu.Lock()
		if !firewall.healthy || verifyFirewall(ctx, blockedPorts(firewall.state)) != nil {
			if err := firewall.restore(ctx); err != nil && ctx.Err() == nil {
				log.Printf("could not restore firewall: %v", err)
			}
		}
		firewall.mu.Unlock()
	}
}

func runFirewall(ctx context.Context) error {
	start, end, err := portRange()
	if err != nil {
		return err
	}
	path := envOr("FIREWALL_STATE", "/var/lib/minecrafttools-firewall/state.json")
	namespace, err := os.Readlink("/proc/self/ns/net")
	if err != nil {
		return fmt.Errorf("read firewall network namespace: %w", err)
	}
	firewall := &Firewall{state: make(map[int]FirewallRequest), stateFile: path, portStart: start, portEnd: end, networkNamespace: namespace}
	data, err := os.ReadFile(path)
	if err == nil {
		if err := json.Unmarshal(data, &firewall.state); err != nil || firewall.state == nil {
			return errors.New("invalid persisted firewall state")
		}
		for port, command := range firewall.state {
			if port < start || port > end || command.Port != port || command.CommandID <= 0 ||
				(command.Action != "block" && command.Action != "unblock") {
				return errors.New("persisted firewall state is outside the configured scope")
			}
		}
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	if err := writeState(path, firewall.state); err != nil {
		return fmt.Errorf("cannot persist firewall state: %w", err)
	}
	if err := verifySocketDestroy(ctx); err != nil {
		return fmt.Errorf("cannot destroy TCP connections in the node namespace: %w", err)
	}
	if err := firewall.restore(ctx); err != nil {
		return err
	}
	socket := envOr("FIREWALL_SOCKET", firewallSocket)
	if err := os.MkdirAll(filepath.Dir(socket), 0750); err != nil {
		return err
	}
	if err := os.Remove(socket); err != nil && !errors.Is(err, os.ErrNotExist) {
		return err
	}
	listener, err := net.Listen("unix", socket)
	if err != nil {
		return err
	}
	defer listener.Close()
	if err := os.Chmod(socket, 0660); err != nil {
		return err
	}
	stop := context.AfterFunc(ctx, func() { _ = listener.Close() })
	defer stop()
	child, shutdown := context.WithCancel(ctx)
	defer shutdown()
	go firewall.monitor(child)
	log.Print("firewall ready")
	for {
		connection, err := listener.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		go func() {
			defer connection.Close()
			_ = connection.SetDeadline(time.Now().Add(8 * time.Second))
			var request FirewallRequest
			if err := json.NewDecoder(io.LimitReader(connection, maxMessageSize)).Decode(&request); err != nil {
				return
			}
			reply, err := firewall.handle(ctx, request)
			if err != nil {
				reply.Error = err.Error()
			}
			_ = json.NewEncoder(connection).Encode(reply)
		}()
	}
}
