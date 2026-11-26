package main

import (
	"bufio"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"math/rand/v2"
	"net"
	"net/http"
	"net/url"
	"os"
	"slices"
	"time"

	"github.com/coder/websocket"
)

type ControlConnection struct {
	address  string
	endpoint string
	tls      *tls.Config
	hello    map[string]any
}

func newControlConnection(address, caFile, certFile, keyFile, nodeID, secret, frpsVersion string) (*ControlConnection, error) {
	parsed, err := url.Parse(address)
	if err != nil || parsed.Hostname() == "" || parsed.User != nil || parsed.ForceQuery ||
		parsed.RawQuery != "" || parsed.Fragment != "" ||
		(parsed.Scheme == "tls" && (parsed.Port() == "" || parsed.Path != "")) ||
		(parsed.Scheme == "https" && parsed.Path != "" && parsed.Path != "/") ||
		(parsed.Scheme != "tls" && parsed.Scheme != "https") {
		return nil, errors.New("CONTROL_URL must be https://hostname[:port] or tls://hostname:port")
	}
	if nodeID == "" || secret == "" {
		return nil, errors.New("NODE_ID and a node secret are required")
	}
	config := &tls.Config{ServerName: parsed.Hostname(), MinVersion: tls.VersionTLS12}
	if caFile != "" {
		data, err := os.ReadFile(caFile)
		if err != nil {
			return nil, err
		}
		certs := x509.NewCertPool()
		if !certs.AppendCertsFromPEM(data) {
			return nil, errors.New("CONTROL_CA_FILE contains no certificates")
		}
		config.RootCAs = certs
	}
	if (certFile == "") != (keyFile == "") {
		return nil, errors.New("CONTROL_CLIENT_CERT_FILE and CONTROL_CLIENT_KEY_FILE must be set together")
	}
	if certFile != "" {
		if _, err := tls.LoadX509KeyPair(certFile, keyFile); err != nil {
			return nil, fmt.Errorf("load node client certificate: %w", err)
		}
		config.GetClientCertificate = func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
			if err != nil {
				return nil, fmt.Errorf("reload node client certificate: %w", err)
			}
			return &certificate, nil
		}
	}
	endpoint := ""
	if parsed.Scheme == "https" {
		parsed.Scheme = "wss"
		parsed.Path = "/api/v1/node/connect"
		endpoint = parsed.String()
	}
	return &ControlConnection{address: parsed.Host, endpoint: endpoint, tls: config, hello: map[string]any{
		"protocol_version": 1, "node_id": nodeID, "secret": secret,
		"agent_version": version, "frps_version": frpsVersion, "blacklist_cidr": true,
	}}, nil
}

type ControlSession struct {
	connection net.Conn
	reader     *bufio.Reader
	seq        int64
}

type ControlReply struct {
	Type    string `json:"type"`
	Seq     int64  `json:"seq"`
	Payload struct {
		Commands []FirewallRequest `json:"commands"`
		Policy   *nodePolicy       `json:"policy"`
	} `json:"payload"`
}

func (session *ControlSession) send(kind string, payload any) (ControlReply, error) {
	var response ControlReply
	session.seq++
	body, err := json.Marshal(map[string]any{"type": kind, "seq": session.seq, "payload": payload})
	if err != nil {
		return response, err
	}
	if len(body)+1 > maxMessageSize {
		return response, errors.New("control message too large")
	}
	if err := session.connection.SetDeadline(time.Now().Add(6 * time.Second)); err != nil {
		return response, err
	}
	if _, err := session.connection.Write(append(body, '\n')); err != nil {
		return response, err
	}
	line, err := session.reader.ReadSlice('\n')
	if err != nil {
		return response, err
	}
	if err := json.Unmarshal(line, &response); err != nil {
		return response, err
	}
	if response.Type != kind+"_ack" || response.Seq != session.seq {
		return response, fmt.Errorf("unexpected acknowledgement for %s", kind)
	}
	return response, nil
}

func (session *ControlSession) ackPolicy(kind string, revision int64, failure error) error {
	message := ""
	if failure != nil {
		log.Printf("could not apply %s policy: %v", kind, failure)
		detail := []rune(failure.Error())
		message = string(detail[:min(300, len(detail))])
	}
	_, err := session.send("policy_ack", map[string]any{
		"kind": kind, "revision": revision, "error": message,
	})
	return err
}

func (control *ControlConnection) dial(ctx context.Context) (net.Conn, error) {
	if control.endpoint == "" {
		dialer := tls.Dialer{NetDialer: &net.Dialer{Timeout: 6 * time.Second}, Config: control.tls}
		return dialer.DialContext(ctx, "tcp", control.address)
	}
	upgrade, cancel := context.WithTimeout(ctx, 6*time.Second)
	defer cancel()
	client := &http.Client{
		Transport:     &http.Transport{TLSClientConfig: control.tls},
		CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
	}
	socket, _, err := websocket.Dial(upgrade, control.endpoint, &websocket.DialOptions{HTTPClient: client})
	if err != nil {
		return nil, err
	}
	connection := websocket.NetConn(ctx, socket, websocket.MessageText)
	socket.SetReadLimit(maxMessageSize)
	return connection, nil
}

func (control *ControlConnection) session(ctx context.Context, snapshot func(context.Context) ([]Proxy, error), execute func(context.Context, FirewallRequest) error, agent *Agent) error {
	ports, err := agent.dashboard.deployedPorts(ctx, agent.portStart, agent.portEnd)
	if err != nil {
		return fmt.Errorf("read deployed ports: %w", err)
	}
	control.hello["deployed_ports"] = ports
	connection, err := control.dial(ctx)
	if err != nil {
		return err
	}
	defer connection.Close()
	stop := context.AfterFunc(ctx, func() { _ = connection.Close() })
	defer stop()
	session := ControlSession{connection: connection, reader: bufio.NewReaderSize(connection, maxMessageSize)}
	initial, err := session.send("hello", control.hello)
	if err != nil {
		return err
	}
	rangeRevision := int64(-1)
	apply := func(policy *nodePolicy) error {
		if policy == nil {
			return nil
		}
		if current := policy.Blacklist; current != nil {
			err := agent.applyBlacklist(ctx, *current)
			if err := session.ackPolicy("blacklist", current.Revision, err); err != nil {
				return err
			}
		}
		if current := policy.Range; current != nil {
			err := agent.applyRange(ctx, *current, ports)
			if current.Revision == rangeRevision {
				return nil
			}
			if err == nil {
				rangeRevision = current.Revision
			}
			return session.ackPolicy("range", current.Revision, err)
		}
		return nil
	}
	if initial.Payload.Policy == nil {
		agent.mu.Lock()
		agent.policyReady = true // Control servers without policy support use the deployment limits.
		agent.mu.Unlock()
	}
	if err := apply(initial.Payload.Policy); err != nil {
		return err
	}
	log.Print("connected to control server")
	var previous []Proxy
	reported := false
	update := func() error {
		proxies, err := snapshot(ctx)
		if err != nil {
			log.Printf("frps snapshot unavailable: %v", err)
			return nil
		}
		if reported && slices.Equal(previous, proxies) {
			return nil
		}
		if _, err := session.send("tunnel_snapshot", map[string]any{"tunnels": proxies}); err != nil {
			return err
		}
		previous = proxies
		reported = true
		return nil
	}
	if err := update(); err != nil {
		return err
	}
	snapshots := time.NewTicker(2 * time.Second)
	defer snapshots.Stop()
	heartbeats := time.NewTicker(10 * time.Second)
	defer heartbeats.Stop()
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-snapshots.C:
			if err := update(); err != nil {
				return err
			}
		case <-heartbeats.C:
			reply, err := session.send("heartbeat", map[string]any{})
			if err != nil {
				return err
			}
			if err := apply(reply.Payload.Policy); err != nil {
				return err
			}
			for _, command := range reply.Payload.Commands {
				result := map[string]any{"command_id": command.CommandID, "ok": true}
				if err := execute(ctx, command); err != nil {
					log.Printf("command %d failed: %v", command.CommandID, err)
					result["ok"] = false
					message := []rune(err.Error())
					result["error"] = string(message[:min(300, len(message))])
				}
				if _, err := session.send("ack", result); err != nil {
					return err
				}
				reported = false
			}
		}
	}
}

func (control *ControlConnection) run(ctx context.Context, snapshot func(context.Context) ([]Proxy, error), execute func(context.Context, FirewallRequest) error, agent *Agent) {
	delay := time.Second
	for ctx.Err() == nil {
		started := time.Now()
		err := control.session(ctx, snapshot, execute, agent)
		if ctx.Err() != nil {
			return
		}
		log.Printf("control disconnected: %v", err)
		if time.Since(started) >= 30*time.Second {
			delay = time.Second
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(delay/2 + time.Duration(rand.Int64N(int64(delay/2)))):
		}
		delay = min(2*delay, time.Minute)
	}
}
