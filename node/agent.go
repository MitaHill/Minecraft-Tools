package main

import (
	"context"
	"crypto/rand"
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
	"regexp"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unicode/utf8"
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
	ClientIP   string `json:"client_ip,omitempty"`
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
	policy         nodePolicy
	policyFile     string
	policyReady    bool
	recentProxies  map[string]time.Time
}

func openAgent(path string, start, end int) (*Agent, error) {
	if start < 1024 || start > end || end > 65535 {
		return nil, errors.New("invalid game port range")
	}
	agent := &Agent{stateFile: path, portStart: start, portEnd: end, proxies: make(map[string]Proxy),
		firewallState: make(map[int]FirewallRequest), pendingBlocks: make(map[int]FirewallRequest),
		policyReady: true, recentProxies: make(map[string]time.Time)}
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
		ProxyName     string            `json:"proxy_name"`
		ProxyType     string            `json:"proxy_type"`
		RemotePort    int               `json:"remote_port"`
		Group         string            `json:"group"`
		Metas         map[string]string `json:"metas"`
		ClientAddress string            `json:"client_address"`
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
		if !agent.policyReady {
			pluginReply(response, "node policy is synchronizing")
			return
		}
		if content.ClientAddress != "" {
			if reason := agent.checkLogin(content.ClientAddress); reason != "" {
				pluginReply(response, reason)
				return
			}
		} else if agent.hasActiveBlacklist() {
			pluginReply(response, "FRP client address unavailable")
			return
		}
	case "NewUserConn":
		registered, found := agent.proxies[proxy.Name]
		if !found || registered.Closed || registered.RunID != proxy.RunID || agent.portBlocked(registered.RemotePort) {
			pluginReply(response, "tunnel unavailable")
			return
		}
	case "NewProxy":
		ranges := [][2]int{{agent.portStart, agent.portEnd}}
		if policy := agent.policy.Range; policy != nil {
			ranges = policy.effectiveRanges()
		}
		if !agent.policyReady {
			pluginReply(response, "node policy is synchronizing")
			return
		}
		if agent.hasActiveBlacklist() {
			clients, err := agent.dashboard.activeClients(request.Context())
			if err != nil || clients[proxy.RunID] == "" {
				pluginReply(response, "FRP client address unavailable")
				return
			}
			if reason := agent.blockedIP(clients[proxy.RunID]); reason != "" {
				pluginReply(response, reason)
				return
			}
		}
		if content.ProxyType != "tcp" || content.Group != "" ||
			!portInRanges(ranges, proxy.RemotePort) {
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
		now := time.Now()
		for name, registered := range agent.recentProxies {
			if now.Sub(registered) > 30*time.Second {
				delete(agent.recentProxies, name)
			}
		}
		agent.recentProxies[proxy.Name] = now
	case "CloseProxy":
		if previous, ok := agent.proxies[proxy.Name]; ok && previous.RunID == proxy.RunID {
			delete(agent.recentProxies, proxy.Name)
		}
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

var nodeIDPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,62}$`)

// loadOrCreateNodeID gives every agent a stable identity without asking the
// operator for one. NODE_ID stays supported as a legacy override so upgrading
// an already registered node does not create a second node record.
func loadOrCreateNodeID(dataDir string) (string, error) {
	path := filepath.Join(dataDir, "node-id")
	if configured := strings.TrimSpace(os.Getenv("NODE_ID")); configured != "" {
		if !nodeIDPattern.MatchString(configured) {
			return "", errors.New("invalid NODE_ID")
		}
		existing, err := os.ReadFile(path)
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return "", err
		}
		if err != nil || strings.TrimSpace(string(existing)) != configured {
			if err := writeNodeID(path, configured); err != nil {
				return "", err
			}
		}
		return configured, nil
	}
	body, err := os.ReadFile(path)
	if err == nil {
		id := strings.TrimSpace(string(body))
		if !nodeIDPattern.MatchString(id) {
			return "", fmt.Errorf("invalid node id in %s", path)
		}
		return id, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", err
	}
	buffer := make([]byte, 16)
	if _, err := rand.Read(buffer); err != nil {
		return "", err
	}
	id := "node-" + hex.EncodeToString(buffer)
	if err := writeNodeID(path, id); err != nil {
		return "", err
	}
	return id, nil
}

func writeNodeID(path, id string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".node-id-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	if err := file.Chmod(0600); err != nil {
		file.Close()
		return err
	}
	if _, err := fmt.Fprintln(file, id); err != nil {
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
	dataDir := envOr("NODE_DATA_DIR", "/data")
	nodeID, err := loadOrCreateNodeID(dataDir)
	if err != nil {
		return err
	}
	agent, err := openAgent(filepath.Join(dataDir, "proxies.json"), start, end)
	if err != nil {
		return err
	}
	connection, err := newControlConnection(os.Getenv("CONTROL_URL"), os.Getenv("CONTROL_CA_FILE"),
		os.Getenv("CONTROL_CLIENT_CERT_FILE"), os.Getenv("CONTROL_CLIENT_KEY_FILE"),
		nodeID, envOr("FRPS_VERSION", "0.72.0"))
	if err != nil {
		return err
	}
	if name := strings.TrimSpace(os.Getenv("NODE_NAME")); name != "" {
		if utf8.RuneCountInString(name) > 80 {
			return errors.New("NODE_NAME is longer than 80 characters")
		}
		connection.hello["node_name"] = name
	}
	if host := strings.TrimSpace(os.Getenv("NODE_PUBLIC_HOST")); host != "" {
		if len(host) > 253 {
			return errors.New("NODE_PUBLIC_HOST is longer than 253 characters")
		}
		connection.hello["public_host"] = host
	}
	publicPort, err := strconv.Atoi(envOr("FRP_PUBLIC_CONTROL_PORT", "7000"))
	if err != nil || publicPort < 1 || publicPort > 65535 {
		return errors.New("FRP_PUBLIC_CONTROL_PORT must be between 1 and 65535")
	}
	connection.hello["frp_public_port"] = publicPort
	dashboardPassword, err := configuredSecret("FRPS_ADMIN_PASSWORD")
	if err != nil {
		return err
	}
	dashboard, err := newDashboard(os.Getenv("FRPS_ADMIN_URL"), os.Getenv("FRPS_ADMIN_USER"), dashboardPassword)
	if err != nil {
		return err
	}
	agent.dashboard = dashboard
	agent.policyFile = filepath.Join(filepath.Dir(agent.stateFile), "policy.json")
	agent.policyReady = false
	if err := agent.loadPolicy(); err != nil {
		return err
	}
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
		}, agent.executeCommand, agent)
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
	case "version":
		fmt.Println(version)
	default:
		log.Fatal("unknown command")
	}
	if err != nil {
		log.Fatal(err)
	}
}
