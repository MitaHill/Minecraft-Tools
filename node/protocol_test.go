package main

import (
	"bufio"
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestControlHTTPSChecksCertificateAndExchangesHello(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if request.URL.Path != "/api/v1/node/connect" {
			http.NotFound(writer, request)
			return
		}
		socket, err := websocket.Accept(writer, request, nil)
		if err != nil {
			t.Error(err)
			return
		}
		defer socket.CloseNow()
		kind, _, err := socket.Read(request.Context())
		if err != nil || kind != websocket.MessageText {
			t.Errorf("reading hello: message type %v, error %v", kind, err)
			return
		}
		if err := socket.Write(request.Context(), kind, []byte("{\"type\":\"hello_ack\",\"seq\":1,\"payload\":{}}\n")); err != nil {
			t.Error(err)
		}
	}))
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	untrusted, err := newControlConnection(server.URL, "", "", "", "test-node", "secret", "test")
	if err != nil {
		t.Fatal(err)
	}
	if connection, err := untrusted.dial(ctx); err == nil {
		connection.Close()
		t.Fatal("accepted a server certificate outside the system trust store")
	}

	caFile := filepath.Join(t.TempDir(), "test-ca.pem")
	ca := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw})
	if err := os.WriteFile(caFile, ca, 0600); err != nil {
		t.Fatal(err)
	}
	trusted, err := newControlConnection(server.URL, caFile, "", "", "test-node", "secret", "test")
	if err != nil {
		t.Fatal(err)
	}
	connection, err := trusted.dial(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	session := ControlSession{connection: connection, reader: bufio.NewReaderSize(connection, maxMessageSize)}
	if _, err := session.send("hello", trusted.hello); err != nil {
		t.Fatal(err)
	}
}
