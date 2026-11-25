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
	return &Dashboard{endpoint: strings.TrimRight(address, "/") + "/api/proxy/tcp", user: user,
		password: password, client: &http.Client{Timeout: 4 * time.Second,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (dashboard *Dashboard) activeProxies(ctx context.Context) ([]dashboardProxy, error) {
	request, err := http.NewRequestWithContext(ctx, "GET", dashboard.endpoint, nil)
	if err != nil {
		return nil, err
	}
	if dashboard.user != "" {
		request.SetBasicAuth(dashboard.user, dashboard.password)
	}
	response, err := dashboard.client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("frps dashboard returned HTTP %d", response.StatusCode)
	}
	var result struct {
		Proxies []dashboardProxy `json:"proxies"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 8*maxMessageSize)).Decode(&result); err != nil {
		return nil, err
	}
	return result.Proxies, nil
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
	agent.mu.Lock()
	defer agent.mu.Unlock()
	active := make([]Proxy, 0, len(proxies))
	for _, entry := range proxies {
		proxy, known := agent.proxies[entry.Name]
		if known && !proxy.Closed && entry.Status == "online" && entry.Config.RemotePort == proxy.RemotePort {
			active = append(active, proxy)
		}
	}
	sort.Slice(active, func(i, j int) bool { return active[i].Name < active[j].Name })
	return active, nil
}
