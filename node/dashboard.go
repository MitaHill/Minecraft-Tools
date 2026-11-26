package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

type Dashboard struct {
	endpoint string
	user     string
	password string
	client   *http.Client
}

type dashboardProxy struct {
	Name   string `json:"name"`
	Status string `json:"status"`
	Config struct {
		RemotePort int `json:"remotePort"`
	} `json:"conf"`
}

func newDashboard(address, user, password string) (*Dashboard, error) {
	parsed, err := url.Parse(address)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") ||
		parsed.User != nil || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, errors.New("FRPS_ADMIN_URL must be an HTTP(S) base URL")
	}
	return &Dashboard{endpoint: strings.TrimRight(address, "/"), user: user,
		password: password, client: &http.Client{Timeout: 4 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (dashboard *Dashboard) get(ctx context.Context, path string, data any) error {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, dashboard.endpoint+path, nil)
	if err != nil {
		return err
	}
	if dashboard.user != "" {
		request.SetBasicAuth(dashboard.user, dashboard.password)
	}
	response, err := dashboard.client.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("frps dashboard returned HTTP %d", response.StatusCode)
	}
	return json.NewDecoder(io.LimitReader(response.Body, 8*maxMessageSize)).Decode(data)
}

func (dashboard *Dashboard) activeProxies(ctx context.Context) ([]dashboardProxy, error) {
	var result struct {
		Proxies []dashboardProxy `json:"proxies"`
	}
	err := dashboard.get(ctx, "/api/proxy/tcp", &result)
	return result.Proxies, err
}

func (dashboard *Dashboard) activeClients(ctx context.Context) (map[string]string, error) {
	var result []struct {
		RunID string `json:"runID"`
		IP    string `json:"clientIP"`
	}
	if err := dashboard.get(ctx, "/api/clients?status=online", &result); err != nil {
		return nil, err
	}
	clients := make(map[string]string, len(result))
	for _, entry := range result {
		if ip := normalizedIP(entry.IP); ip != "" {
			clients[entry.RunID] = ip
		}
	}
	return clients, nil
}

func (dashboard *Dashboard) deployedPorts(ctx context.Context, start, end int) ([][2]int, error) {
	var config struct {
		AllowPorts string `json:"allowPortsStr"`
	}
	if err := dashboard.get(ctx, "/api/serverinfo", &config); err != nil {
		return nil, err
	}
	return allowedPorts(start, end, config.AllowPorts)
}

func (dashboard *Dashboard) isOnline(ctx context.Context, name string) (bool, error) {
	proxies, err := dashboard.activeProxies(ctx)
	if err != nil {
		return false, err
	}
	for _, proxy := range proxies {
		if proxy.Name == name && proxy.Status == "online" {
			return true, nil
		}
	}
	return false, nil
}

func (dashboard *Dashboard) snapshot(ctx context.Context, agent *Agent) ([]Proxy, error) {
	proxies, err := dashboard.activeProxies(ctx)
	if err != nil {
		return nil, err
	}
	clients, _ := dashboard.activeClients(ctx)
	agent.mu.Lock()
	defer agent.mu.Unlock()
	active := make([]Proxy, 0, len(proxies))
	for _, entry := range proxies {
		proxy, known := agent.proxies[entry.Name]
		if known && !proxy.Closed && entry.Status == "online" && entry.Config.RemotePort == proxy.RemotePort {
			proxy.ClientIP = clients[proxy.RunID]
			active = append(active, proxy)
		}
	}
	sort.Slice(active, func(i, j int) bool { return active[i].Name < active[j].Name })
	return active, nil
}
