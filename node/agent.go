package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"maps"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const maxMessageSize = 1024 * 1024

var version = "0.0.0-local"

type Proxy struct {
	RunID      string `json:"run_id"`
	Name       string `json:"proxy_name"`
	RemotePort int    `json:"remote_port"`
	ClaimID    string `json:"claim_id,omitempty"`
	ClaimHash  string `json:"claim_hash,omitempty"`
	Closed     bool   `json:"closed,omitempty"`
}

type Agent struct {
	mu             sync.Mutex
	proxies        map[string]Proxy
	stateFile      string
	portStart      int
	portEnd        int
	firewallSocket string
	firewallState  map[int]FirewallRequest
	pendingBlocks  map[int]FirewallRequest
	dashboard      *Dashboard
}

func openAgent(path string, start, end int) (*Agent, error) {
	if start < 1024 || start > end || end > 65535 {
		return nil, errors.New("invalid game port range")
	}
	agent := &Agent{stateFile: path, portStart: start, portEnd: end, proxies: make(map[string]Proxy),
		firewallState: make(map[int]FirewallRequest), pendingBlocks: make(map[int]FirewallRequest)}
	body, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return agent, nil
	}
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(body, &agent.proxies); err != nil || agent.proxies == nil {
		return nil, errors.New("invalid persisted proxy registry")
	}
	return agent, nil
}

func writeState(path string, value any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".state-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if err := json.NewEncoder(file).Encode(value); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	return os.Rename(file.Name(), path)
}

func (agent *Agent) saveProxy(proxy Proxy, remove bool) error {
	previous, found := agent.proxies[proxy.Name]
	if remove {
		if !found || previous.RunID != proxy.RunID || previous.Closed {
			return nil
		}
		proxy = previous
		proxy.Closed = true
	} else if previous == proxy {
		return nil
	}
	proxies := maps.Clone(agent.proxies)
	if proxy.Closed {
		// Retain the final identity on each port for a block racing with client shutdown.
		for name, old := range proxies {
			if old.Closed && old.RemotePort == proxy.RemotePort {
				delete(proxies, name)
			}
		}
	}
	proxies[proxy.Name] = proxy
	if err := writeState(agent.stateFile, proxies); err != nil {
		return err
	}
	agent.proxies = proxies
	return nil
}

func (agent *Agent) syncFirewall(ctx context.Context) error {
	if agent.firewallSocket == "" {
		return nil
	}
	reply, err := firewallCall(ctx, agent.firewallSocket, FirewallRequest{Action: "state"})
	if err != nil {
		return err
	}
	clear(agent.firewallState)
	for _, command := range reply.Commands {
		agent.firewallState[command.Port] = command
	}
	return nil
}

func (agent *Agent) portBlocked(port int) bool {
	return agent.firewallState[port].Action == "block" || agent.pendingBlocks[port].Action == "block"
}

func (agent *Agent) executeCommand(ctx context.Context, command FirewallRequest) error {
	agent.mu.Lock()
	defer agent.mu.Unlock()
	previous := agent.firewallState[command.Port]
	pending := agent.pendingBlocks[command.Port]
	proxy := agent.proxies[command.ProxyName]
	registered := proxy.RunID == command.RunID && proxy.RemotePort == command.Port
	reserved := previous.ProxyName == command.ProxyName && previous.RunID == command.RunID
	if command.CommandID <= 0 || command.Port < agent.portStart || command.Port > agent.portEnd ||
		command.ProxyName == "" || command.RunID == "" || command.CommandID < max(previous.CommandID, pending.CommandID) {
		return errors.New("invalid or stale tunnel command")
	}
	switch command.Action {
	case "block":
		if !registered && !(reserved && previous.Action == "block") && pending != command {
			return errors.New("block does not match the registered tunnel generation")
		}
		if agent.dashboard != nil && previous.Action != "block" && pending != command {
			if proxy.Closed {
				for _, current := range agent.proxies {
					if !current.Closed && current.RemotePort == command.Port && current.Name != command.ProxyName {
						return errors.New("port belongs to another registered proxy")
					}
				}
			}
			proxies, err := agent.dashboard.activeProxies(ctx)
			if err != nil {
				return fmt.Errorf("cannot verify the port owner: %w", err)
			}
			for _, current := range proxies {
				if current.Status == "online" && current.Config.RemotePort == command.Port && current.Name != command.ProxyName {
					return errors.New("port belongs to another active proxy")
				}
			}
		}
		agent.pendingBlocks[command.Port] = command
	case "unblock":
		if !reserved {
			return errors.New("unblock does not match the reserved tunnel generation")
		}
	default:
		return errors.New("unknown tunnel command")
	}
	if _, err := firewallCall(ctx, agent.firewallSocket, command); err != nil {
		return err
	}
	agent.firewallState[command.Port] = command
	delete(agent.pendingBlocks, command.Port)
	return nil
}

type pluginRequest struct {
	Content struct {
		User struct {
			RunID string `json:"run_id"`
		} `json:"user"`
		ProxyName  string            `json:"proxy_name"`
		ProxyType  string            `json:"proxy_type"`
		RemotePort int               `json:"remote_port"`
		Group      string            `json:"group"`
		Metas      map[string]string `json:"metas"`
	} `json:"content"`
}

func pluginReply(response http.ResponseWriter, reason string) {
	response.Header().Set("Content-Type", "application/json")
	if reason != "" {
		_ = json.NewEncoder(response).Encode(map[string]any{"reject": true, "reject_reason": reason})
		return
	}
	_ = json.NewEncoder(response).Encode(map[string]bool{"reject": false, "unchange": true})
}

func (agent *Agent) plugin(response http.ResponseWriter, request *http.Request) {
	var payload pluginRequest
	body := http.MaxBytesReader(response, request.Body, maxMessageSize)
	if err := json.NewDecoder(body).Decode(&payload); err != nil {
		http.Error(response, "invalid plugin request", http.StatusBadRequest)
		return
	}
	content := payload.Content
	proxy := Proxy{RunID: content.User.RunID, Name: content.ProxyName,
		RemotePort: content.RemotePort}
	agent.mu.Lock()
	defer agent.mu.Unlock()
	switch operation := request.URL.Query().Get("op"); operation {
	case "Login":
	case "NewUserConn":
		registered, found := agent.proxies[proxy.Name]
		if !found || registered.Closed || registered.RunID != proxy.RunID || agent.portBlocked(registered.RemotePort) {
			pluginReply(response, "tunnel unavailable")
			return
		}
	case "NewProxy":
		if content.ProxyType != "tcp" || content.Group != "" ||
			proxy.RemotePort < agent.portStart || proxy.RemotePort > agent.portEnd {
			pluginReply(response, "only a dedicated TCP port in the configured range is allowed")
			return
		}
		if proxy.Name == "" || proxy.RunID == "" || len(proxy.Name) > 200 || len(proxy.RunID) > 200 {
			pluginReply(response, "invalid proxy identity")
			return
		}
		claim, proof := content.Metas["tunnel_id"], content.Metas["tunnel_proof"]
		if len(claim) == 32 && len(proof) == 43 && proxy.Name == "minecraft-"+claim {
			if _, err := hex.DecodeString(claim); err == nil {
				proofHash := sha256.Sum256([]byte(proof))
				proxy.ClaimID = claim
				proxy.ClaimHash = hex.EncodeToString(proofHash[:])
			}
		}
		if err := agent.syncFirewall(request.Context()); err != nil || agent.portBlocked(proxy.RemotePort) {
			pluginReply(response, "firewall unavailable or port blocked")
			return
		}
		if previous, found := agent.proxies[proxy.Name]; found && previous != proxy && agent.dashboard != nil {
			online, err := agent.dashboard.isOnline(request.Context(), proxy.Name)
			if err != nil || online {
				pluginReply(response, "existing proxy identity has not been released")
				return
			}
		}
		if err := agent.saveProxy(proxy, false); err != nil {
			log.Printf("persist proxy: %v", err)
			pluginReply(response, "proxy registry unavailable")
			return
		}
	case "CloseProxy":
		if err := agent.saveProxy(proxy, true); err != nil {
			log.Printf("remove proxy: %v", err)
		}
	default:
		pluginReply(response, "unsupported operation")
		return
	}
	pluginReply(response, "")
}

func envOr(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}

func configuredSecret(name string) (string, error) {
	if path := os.Getenv(name + "_FILE"); path != "" {
		data, err := os.ReadFile(path)
		return strings.TrimSpace(string(data)), err
	}
	return os.Getenv(name), nil
}

func portRange() (int, int, error) {
	start, err := strconv.Atoi(envOr("NODE_PORT_START", "30000"))
	if err != nil {
		return 0, 0, fmt.Errorf("NODE_PORT_START: %w", err)
	}
	end, err := strconv.Atoi(envOr("NODE_PORT_END", "30100"))
	if err != nil {
		return 0, 0, fmt.Errorf("NODE_PORT_END: %w", err)
	}
	if start < 1024 || start > end || end > 65535 {
		return 0, 0, errors.New("invalid game port range")
	}
	return start, end, nil
}

func runAgent(ctx context.Context) error {
	start, end, err := portRange()
	if err != nil {
		return err
	}
	agent, err := openAgent(filepath.Join(envOr("NODE_DATA_DIR", "/data"), "proxies.json"), start, end)
	if err != nil {
		return err
	}
	secret, err := configuredSecret("NODE_SECRET")
	if err != nil {
		return err
	}
	connection, err := newControlConnection(os.Getenv("CONTROL_URL"), os.Getenv("CONTROL_CA_FILE"),
		os.Getenv("CONTROL_CLIENT_CERT_FILE"), os.Getenv("CONTROL_CLIENT_KEY_FILE"),
		os.Getenv("NODE_ID"), secret, envOr("FRPS_VERSION", "0.72.0"))
	if err != nil {
		return err
	}
	dashboardPassword, err := configuredSecret("FRPS_ADMIN_PASSWORD")
	if err != nil {
		return err
	}
	dashboard, err := newDashboard(os.Getenv("FRPS_ADMIN_URL"), os.Getenv("FRPS_ADMIN_USER"), dashboardPassword)
	if err != nil {
		return err
	}
	agent.dashboard = dashboard
	agent.firewallSocket = envOr("FIREWALL_SOCKET", firewallSocket)
	if err := agent.syncFirewall(ctx); err != nil {
		return fmt.Errorf("firewall not ready: %w", err)
	}
	mux := http.NewServeMux()
	mux.HandleFunc("POST /handler", agent.plugin)
	mux.HandleFunc("GET /ready", agent.ready)
	server := http.Server{Addr: envOr("PLUGIN_LISTEN", "127.0.0.1:9000"), Handler: mux,
		ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 10 * time.Second}
	child, cancel := context.WithCancel(ctx)
	defer cancel()
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		connection.run(child, func(ctx context.Context) ([]Proxy, error) {
			return dashboard.snapshot(ctx, agent)
		}, agent.executeCommand)
	}()
	go func() {
		<-child.Done()
		shutdown, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		_ = server.Shutdown(shutdown)
	}()
	err = server.ListenAndServe()
	cancel()
	<-finished
	if errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return err
}

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	command := "agent"
	if len(os.Args) > 1 {
		command = os.Args[1]
	}
	var err error
	switch command {
	case "agent":
		err = runAgent(ctx)
	case "firewall-helper":
		err = runFirewall(ctx)
	case "firewall-health":
		_, err = firewallCall(ctx, envOr("FIREWALL_SOCKET", firewallSocket), FirewallRequest{Action: "ready"})
	case "frps":
		err = runFrps(ctx, os.Args[2:])
	default:
		log.Fatal("unknown command")
	}
	if err != nil {
		log.Fatal(err)
	}
}
