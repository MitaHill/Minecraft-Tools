package main

import (
	"context"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strings"
	"syscall"
	"time"
)

func (agent *Agent) ready(response http.ResponseWriter, request *http.Request) {
	reply, err := firewallCall(request.Context(), agent.firewallSocket, FirewallRequest{Action: "ready"})
	if err != nil {
		http.Error(response, "firewall unavailable", http.StatusServiceUnavailable)
		return
	}
	response.Header().Set("Content-Type", "text/plain; charset=utf-8")
	_, _ = fmt.Fprintln(response, reply.NetworkNamespace)
}

func frpsReady(ctx context.Context, client *http.Client, url, namespace string) bool {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return false
	}
	response, err := client.Do(request)
	if err != nil {
		return false
	}
	defer response.Body.Close()
	body, err := io.ReadAll(io.LimitReader(response.Body, 128))
	return err == nil && response.StatusCode == http.StatusOK && strings.TrimSpace(string(body)) == namespace
}

func runFrps(ctx context.Context, args []string) error {
	namespace, err := os.Readlink("/proc/self/ns/net")
	if err != nil {
		return fmt.Errorf("read frps network namespace: %w", err)
	}
	const bin = "/usr/local/bin/frps"
	const readyURL = "http://agent:9000/ready"
	client := &http.Client{Timeout: 2 * time.Second}
	if !frpsReady(ctx, client, readyURL, namespace) {
		log.Print("waiting for the firewall in the frps network namespace")
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return nil
			case <-ticker.C:
			}
			if frpsReady(ctx, client, readyURL, namespace) {
				break
			}
		}
	}
	log.Print("firewall verified; starting frps")
	return syscall.Exec(bin, append([]string{bin}, args...), os.Environ())
}
