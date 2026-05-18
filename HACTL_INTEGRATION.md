# hactl ↔ companion: End-to-End Integration Guide

> Implementation & test instructions for the **hactl** Go project to download, install, and test hactl-companion from GitHub.

---

## Overview

hactl needs companion app to extend official HA API.
in the end run Docker-based integration tests that:

1. Build (or pull) the companion image from `hemm-ems/hactl-companion`
2. Spin up HA Core + companion in Docker
3. Onboard HA headless (same 5-step flow as `hatest.go`)
4. Exercise all companion endpoints from Go
5. Tear down

The test pattern mirrors hactl's existing `hatest.go` approach but adds the companion as a sidecar container.

---

## 1. Repo Changes in hactl

### 1.1 Vendor the OpenAPI spec

```
hactl/
├── testdata/
│   └── companion-v1.yaml        ← copy from hactl-companion/openapi/companion-v1.yaml
```

Update process: new companion release → copy YAML → run contract tests.

### 1.2 New Go files

```
hactl/
├── internal/
│   ├── companion/
│   │   ├── client.go             ← HTTP client for all companion endpoints
│   │   ├── client_test.go        ← unit tests (httptest mock)
│   │   └── types.go              ← response structs
│   └── integration/
│       ├── docker-compose.companion.yaml
│       ├── companion_test.go     ← live Docker integration tests
│       └── testutil.go           ← helpers (compose lifecycle, onboarding, wait)
```

---

## 2. Companion Go Client (`internal/companion/client.go`)

### 2.1 Client struct

```go
package companion

import (
    "context"
    "encoding/json"
    "fmt"
    "io"
    "net/http"
    "net/url"
    "time"
)

// Client talks to the hactl-companion add-on API.
type Client struct {
    BaseURL    string
    Token      string
    HTTPClient *http.Client
}

func New(baseURL, token string) *Client {
    return &Client{
        BaseURL:    baseURL,
        Token:      token,
        HTTPClient: &http.Client{Timeout: 30 * time.Second},
    }
}

func (c *Client) do(ctx context.Context, method, path string, query url.Values, body io.Reader) (*http.Response, error) {
    u, _ := url.Parse(c.BaseURL + path)
    if query != nil {
        u.RawQuery = query.Encode()
    }
    req, err := http.NewRequestWithContext(ctx, method, u.String(), body)
    if err != nil {
        return nil, err
    }
    req.Header.Set("Authorization", "Bearer "+c.Token)
    return c.HTTPClient.Do(req)
}
```

### 2.2 Endpoint methods

```go
// Health returns the companion health status.
func (c *Client) Health(ctx context.Context) (*HealthResponse, error) {
    resp, err := c.do(ctx, "GET", "/v1/health", nil, nil)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    var h HealthResponse
    return &h, json.NewDecoder(resp.Body).Decode(&h)
}

// ListConfigFiles returns YAML config files in /config.
func (c *Client) ListConfigFiles(ctx context.Context) (*ConfigFilesResponse, error) {
    resp, err := c.do(ctx, "GET", "/v1/config/files", nil, nil)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    var r ConfigFilesResponse
    return &r, json.NewDecoder(resp.Body).Decode(&r)
}

// ReadConfigFile returns the content of a YAML config file.
func (c *Client) ReadConfigFile(ctx context.Context, path string) (*ConfigFileResponse, error) {
    q := url.Values{"path": {path}}
    resp, err := c.do(ctx, "GET", "/v1/config/file", q, nil)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    if resp.StatusCode != 200 {
        return nil, fmt.Errorf("companion: read file %s: %d", path, resp.StatusCode)
    }
    var r ConfigFileResponse
    return &r, json.NewDecoder(resp.Body).Decode(&r)
}

// WriteConfigFile writes (or dry-runs) a YAML config file.
func (c *Client) WriteConfigFile(ctx context.Context, path string, content string, dryRun bool) (*ConfigWriteResponse, error) {
    q := url.Values{
        "path":    {path},
        "dry_run": {fmt.Sprintf("%t", dryRun)},
    }
    resp, err := c.do(ctx, "PUT", "/v1/config/file", q, strings.NewReader(content))
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    var r ConfigWriteResponse
    return &r, json.NewDecoder(resp.Body).Decode(&r)
}

// SupervisorInfo returns shaped system info from Supervisor.
func (c *Client) SupervisorInfo(ctx context.Context) (*http.Response, error) {
    return c.do(ctx, "GET", "/v1/supervisor/info", nil, nil)
}

// CoreLogs returns HA Core log lines.
func (c *Client) CoreLogs(ctx context.Context, lines int, level string) (*LogsResponse, error) {
    q := url.Values{"lines": {fmt.Sprintf("%d", lines)}}
    if level != "" {
        q.Set("level", level)
    }
    resp, err := c.do(ctx, "GET", "/v1/logs/core", q, nil)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    var r LogsResponse
    return &r, json.NewDecoder(resp.Body).Decode(&r)
}

// Reload triggers a domain reload via ha CLI.
func (c *Client) Reload(ctx context.Context, domain string) (*HaCliResponse, error) {
    resp, err := c.do(ctx, "POST", "/v1/ha/reload/"+domain, nil, nil)
    if err != nil {
        return nil, err
    }
    defer resp.Body.Close()
    var r HaCliResponse
    return &r, json.NewDecoder(resp.Body).Decode(&r)
}
```

### 2.3 Response types (`internal/companion/types.go`)

```go
package companion

type HealthResponse struct {
    Status  string `json:"status"`
    Version string `json:"version"`
}

type ConfigFilesResponse struct {
    Files []string `json:"files"`
}

type ConfigFileResponse struct {
    Path    string `json:"path"`
    Content string `json:"content"`
}

type ConfigWriteResponse struct {
    Status string `json:"status"`
    Diff   string `json:"diff,omitempty"`
    Backup string `json:"backup,omitempty"`
}

type LogsResponse struct {
    Source string   `json:"source"`
    Count  int      `json:"count"`
    Lines  []string `json:"lines"`
}

type HaCliResponse struct {
    Command  string `json:"command"`
    ExitCode int    `json:"exit_code"`
    Output   string `json:"output"`
}
```

---

## 3. Docker Compose for hactl Integration Tests

### 3.1 `docker-compose.companion.yaml`

This mirrors the companion repo's integration compose but **builds from GitHub** instead of local source:

```yaml
services:
  homeassistant:
    image: ghcr.io/home-assistant/home-assistant:stable
    container_name: hactl-test-ha
    ports:
      - "8123"
    volumes:
      - ha-config:/config
    networks:
      test-net:
        aliases:
          - homeassistant
    healthcheck:
      test: ["CMD", "python3", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8123/api/onboarding')"]
      interval: 10s
      timeout: 10s
      retries: 30
      start_period: 60s

  companion:
    # Option A: Pull released image from GHCR
    image: ghcr.io/hemm-ems/hactl-companion:latest
    # Option B: Build from GitHub source (for CI/dev)
    # build:
    #   context: https://github.com/hemm-ems/hactl-companion.git#main
    #   dockerfile: Dockerfile
    #   args:
    #     BASE_IMAGE: python:3.12-alpine
    container_name: hactl-test-companion
    command: ["python3", "-m", "companion"]
    environment:
      SUPERVISOR_TOKEN: hactl-integration-test-token
    ports:
      - "9100"
    volumes:
      - ha-config:/config
    networks:
      test-net:
        aliases:
          - companion

volumes:
  ha-config:

networks:
  test-net:
    driver: bridge
```

### 3.2 How it works

| Step | What happens |
|------|-------------|
| 1 | `docker compose up -d` pulls HA stable + companion image |
| 2 | HA Core starts, writes initial config to `/config` volume |
| 3 | Companion starts, mounts same `/config` volume, listens on `:9100` |
| 4 | Test code polls HA readiness (`/api/onboarding`) |
| 5 | Headless onboarding (create user → auth code → tokens) |
| 6 | Tests hit companion endpoints via mapped port |
| 7 | `docker compose down -v` tears everything down |

> **Note**: Without HA Supervisor, the companion's Supervisor proxy and ha CLI endpoints return 502. This is expected and tested explicitly (like `test_no_supervisor.py` in the companion repo).

---

## 4. Go Integration Test Helpers (`internal/integration/testutil.go`)

### 4.1 Compose lifecycle

```go
package integration

import (
    "fmt"
    "os/exec"
    "strings"
    "testing"
    "time"
    "net/http"
    "encoding/json"
    "io"

    "github.com/gorilla/websocket"  // or nhooyr.io/websocket
)

const (
    composeFile    = "docker-compose.companion.yaml"
    companionToken = "hactl-integration-test-token"
    clientID       = "http://hactl-test"
)

// ComposeUp starts the stack and returns HA + companion URLs.
func ComposeUp(t *testing.T) (haURL, companionURL string) {
    t.Helper()
    run(t, "docker", "compose", "-f", composeFile, "up", "-d", "--build")

    haPort := getMappedPort(t, "homeassistant", "8123")
    compPort := getMappedPort(t, "companion", "9100")
    haURL = fmt.Sprintf("http://localhost:%s", haPort)
    companionURL = fmt.Sprintf("http://localhost:%s", compPort)

    waitForHA(t, haURL, 180*time.Second)
    waitForURL(t, companionURL+"/v1/health", 30*time.Second)
    return
}

// ComposeDown tears down the stack.
func ComposeDown(t *testing.T) {
    t.Helper()
    run(t, "docker", "compose", "-f", composeFile, "down", "-v")
}

func getMappedPort(t *testing.T, service, port string) string {
    t.Helper()
    out, err := exec.Command("docker", "compose", "-f", composeFile, "port", service, port).Output()
    if err != nil {
        t.Fatalf("get port for %s:%s: %v", service, port, err)
    }
    parts := strings.Split(strings.TrimSpace(string(out)), ":")
    return parts[len(parts)-1]
}

func waitForHA(t *testing.T, baseURL string, timeout time.Duration) {
    t.Helper()
    deadline := time.Now().Add(timeout)
    for time.Now().Before(deadline) {
        resp, err := http.Get(baseURL + "/api/onboarding")
        if err == nil && resp.StatusCode == 200 {
            resp.Body.Close()
            return
        }
        if resp != nil {
            resp.Body.Close()
        }
        time.Sleep(2 * time.Second)
    }
    t.Fatalf("HA not ready at %s within %s", baseURL, timeout)
}

func waitForURL(t *testing.T, url string, timeout time.Duration) {
    t.Helper()
    deadline := time.Now().Add(timeout)
    for time.Now().Before(deadline) {
        resp, err := http.Get(url)
        if err == nil && resp.StatusCode == 200 {
            resp.Body.Close()
            return
        }
        if resp != nil {
            resp.Body.Close()
        }
        time.Sleep(1 * time.Second)
    }
    t.Fatalf("URL %s not reachable within %s", url, timeout)
}
```

### 4.2 Headless onboarding

This is the Go equivalent of the companion repo's `conftest.py` `_onboard_ha()`, which itself mirrors `hatest.go`:

```go
// OnboardHA runs the 5-step headless onboarding and returns a long-lived access token.
func OnboardHA(t *testing.T, baseURL string) string {
    t.Helper()

    // Step 1: Create owner user
    body := `{
        "client_id": "` + clientID + `",
        "name": "Test Owner",
        "username": "testowner",
        "password": "testpass1234!",
        "language": "en"
    }`
    resp := postJSON(t, baseURL+"/api/onboarding/users", body, "")
    var step1 struct{ AuthCode string `json:"auth_code"` }
    decodeJSON(t, resp, &step1)

    // Step 2: Exchange auth code for access token
    tokenResp := postForm(t, baseURL+"/auth/token", map[string]string{
        "grant_type": "authorization_code",
        "code":       step1.AuthCode,
        "client_id":  clientID,
    })
    var step2 struct{ AccessToken string `json:"access_token"` }
    decodeJSON(t, tokenResp, &step2)
    accessToken := step2.AccessToken

    // Step 3: Complete core_config wizard step
    postJSON(t, baseURL+"/api/onboarding/core_config", "{}", accessToken)

    // Step 4: Complete analytics wizard step
    postJSON(t, baseURL+"/api/onboarding/analytics", "{}", accessToken)

    // Step 5: Create long-lived token via WebSocket
    wsURL := strings.Replace(baseURL, "http://", "ws://", 1) + "/api/websocket"
    ws, _, err := websocket.DefaultDialer.Dial(wsURL, nil)
    if err != nil {
        t.Fatalf("WS connect: %v", err)
    }
    defer ws.Close()

    // Read auth_required
    _, _, _ = ws.ReadMessage()

    // Send auth
    ws.WriteJSON(map[string]string{
        "type":         "auth",
        "access_token": accessToken,
    })
    _, msg, _ := ws.ReadMessage()
    var authResp map[string]interface{}
    json.Unmarshal(msg, &authResp)
    if authResp["type"] != "auth_ok" {
        t.Fatalf("WS auth failed: %s", msg)
    }

    // Request long-lived token
    ws.WriteJSON(map[string]interface{}{
        "id":          1,
        "type":        "auth/long_lived_access_token",
        "client_name": "hactl-companion-e2e",
        "lifespan":    365,
    })
    _, msg, _ = ws.ReadMessage()
    var tokenResult struct {
        Success bool   `json:"success"`
        Result  string `json:"result"`
    }
    json.Unmarshal(msg, &tokenResult)
    if !tokenResult.Success {
        t.Fatalf("WS token creation failed: %s", msg)
    }
    return tokenResult.Result
}
```

---

## 5. Go Integration Tests (`internal/integration/companion_test.go`)

```go
//go:build integration

package integration

import (
    "context"
    "testing"

    "github.com/hemm-ems/hactl/internal/companion"
)

var (
    testClient      *companion.Client
    testHAURL       string
    testCompanionURL string
)

func TestMain(m *testing.M) {
    // Start stack
    t := &testing.T{} // placeholder for setup
    haURL, companionURL := ComposeUp(t)
    testHAURL = haURL
    testCompanionURL = companionURL

    // Onboard
    _ = OnboardHA(t, haURL)

    // Create companion client
    testClient = companion.New(companionURL, companionToken)

    // Wait for config files to appear (HA initialization)
    waitForConfigFiles(t, testClient, 60*time.Second)

    // Run tests
    code := m.Run()

    // Tear down
    ComposeDown(t)
    os.Exit(code)
}

// --- Health ---

func TestHealth(t *testing.T) {
    h, err := testClient.Health(context.Background())
    if err != nil {
        t.Fatalf("health: %v", err)
    }
    if h.Status != "ok" {
        t.Errorf("status = %q, want ok", h.Status)
    }
    if h.Version == "" {
        t.Error("version is empty")
    }
}

// --- Config Read ---

func TestListConfigFiles(t *testing.T) {
    files, err := testClient.ListConfigFiles(context.Background())
    if err != nil {
        t.Fatalf("list files: %v", err)
    }
    if len(files.Files) == 0 {
        t.Fatal("no config files returned")
    }
    found := false
    for _, f := range files.Files {
        if f == "configuration.yaml" {
            found = true
        }
    }
    if !found {
        t.Error("configuration.yaml not in file list")
    }
}

func TestReadConfigFile(t *testing.T) {
    f, err := testClient.ReadConfigFile(context.Background(), "configuration.yaml")
    if err != nil {
        t.Fatalf("read file: %v", err)
    }
    if f.Content == "" {
        t.Error("empty content")
    }
}

func TestSecretsYamlDenied(t *testing.T) {
    _, err := testClient.ReadConfigFile(context.Background(), "secrets.yaml")
    if err == nil {
        t.Error("expected error reading secrets.yaml")
    }
}

// --- Config Write ---

func TestDryRun(t *testing.T) {
    ctx := context.Background()
    f, _ := testClient.ReadConfigFile(ctx, "configuration.yaml")
    wr, err := testClient.WriteConfigFile(ctx, "configuration.yaml", f.Content, true)
    if err != nil {
        t.Fatalf("dry run: %v", err)
    }
    if wr.Status != "dry_run" {
        t.Errorf("status = %q, want dry_run", wr.Status)
    }
}

func TestWriteNewFile(t *testing.T) {
    ctx := context.Background()
    content := "hactl_test:\n  key: value\n"
    wr, err := testClient.WriteConfigFile(ctx, "hactl-e2e-test.yaml", content, false)
    if err != nil {
        t.Fatalf("write: %v", err)
    }
    if wr.Status != "applied" {
        t.Errorf("status = %q, want applied", wr.Status)
    }
    // Verify readable
    f, err := testClient.ReadConfigFile(ctx, "hactl-e2e-test.yaml")
    if err != nil {
        t.Fatalf("read back: %v", err)
    }
    if f.Content == "" {
        t.Error("written file has empty content")
    }
}

// --- Logs ---

func TestCoreLogs(t *testing.T) {
    logs, err := testClient.CoreLogs(context.Background(), 50, "")
    if err != nil {
        t.Fatalf("core logs: %v", err)
    }
    if logs.Count == 0 {
        t.Error("no log lines")
    }
    if logs.Source != "core" {
        t.Errorf("source = %q, want core", logs.Source)
    }
}

// --- Supervisor (expect 502 in standalone HA) ---

func TestSupervisorInfo502(t *testing.T) {
    resp, err := testClient.SupervisorInfo(context.Background())
    if err != nil {
        t.Fatalf("supervisor info: %v", err)
    }
    defer resp.Body.Close()
    if resp.StatusCode != 502 {
        t.Errorf("status = %d, want 502", resp.StatusCode)
    }
}

// --- HA CLI (expect 502 — no ha binary in standalone HA Core) ---

func TestReload502(t *testing.T) {
    _, err := testClient.Reload(context.Background(), "automation")
    // The client should surface the 502 error
    if err == nil {
        t.Error("expected error from reload without Supervisor")
    }
}
```

---

## 6. Makefile Target for hactl

Add to hactl's `Makefile`:

```makefile
COMPANION_COMPOSE := internal/integration/docker-compose.companion.yaml

.PHONY: test-companion

test-companion:
	docker compose -f $(COMPANION_COMPOSE) up -d --build
	go test -tags=integration -v -count=1 -timeout=300s ./internal/integration/...; \
	status=$$?; \
	docker compose -f $(COMPANION_COMPOSE) down -v; \
	exit $$status
```

Run: `make test-companion`

---

## 7. Image Acquisition Options

### Option A: Pull from GHCR (production, CI)

```yaml
companion:
  image: ghcr.io/hemm-ems/hactl-companion:latest
```

Requires the companion release workflow to push to GHCR. Simplest for CI.

### Option B: Build from GitHub source (dev, pre-release)

```yaml
companion:
  build:
    context: https://github.com/hemm-ems/hactl-companion.git#main
    dockerfile: Dockerfile
    args:
      BASE_IMAGE: python:3.12-alpine
  command: ["python3", "-m", "companion"]
```

Docker natively supports `git://` build contexts. No clone step needed.

### Option C: Clone + build locally (maximum control)

```bash
git clone https://github.com/hemm-ems/hactl-companion.git /tmp/hactl-companion
docker build -t hactl-companion:dev --build-arg BASE_IMAGE=python:3.12-alpine /tmp/hactl-companion
```

Then reference `image: hactl-companion:dev` in compose.

### Recommendation

Use **Option B** for `make test-companion` (always tests latest `main`). Use **Option A** for CI against stable releases.

---

## 8. CI Integration (GitHub Actions)

Add to hactl's `.github/workflows/ci.yml`:

```yaml
  companion-integration:
    runs-on: ubuntu-latest
    needs: [test]  # run after unit tests pass
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: "1.22"
      - name: Start HA + companion stack
        run: docker compose -f internal/integration/docker-compose.companion.yaml up -d
      - name: Wait for stack readiness
        run: |
          for i in $(seq 1 60); do
            if curl -sf http://localhost:$(docker compose -f internal/integration/docker-compose.companion.yaml port homeassistant 8123 | cut -d: -f2)/api/onboarding; then
              echo "HA ready"; break
            fi
            sleep 3
          done
      - name: Run companion integration tests
        run: go test -tags=integration -v -count=1 -timeout=300s ./internal/integration/...
      - name: Tear down
        if: always()
        run: docker compose -f internal/integration/docker-compose.companion.yaml down -v
```

---

## 9. OpenAPI Contract Tests

Vendor `companion-v1.yaml` in the hactl repo and validate client conformance:

```go
//go:build integration

package integration

import (
    "os"
    "testing"

    "github.com/getkin/kin-openapi/openapi3"
)

func TestClientConformsToOpenAPISpec(t *testing.T) {
    // Load vendored spec
    data, err := os.ReadFile("testdata/companion-v1.yaml")
    if err != nil {
        t.Fatalf("read spec: %v", err)
    }
    loader := openapi3.NewLoader()
    spec, err := loader.LoadFromData(data)
    if err != nil {
        t.Fatalf("parse spec: %v", err)
    }

    // Verify all spec paths are covered by our client
    expectedPaths := []string{
        "/v1/health",
        "/v1/config/files",
        "/v1/config/file",
        "/v1/config/block",
        "/v1/supervisor/info",
        "/v1/supervisor/addons",
        "/v1/supervisor/backups",
        "/v1/supervisor/backups/new",
        "/v1/supervisor/addon/{slug}/logs",
        "/v1/logs/core",
        "/v1/logs/supervisor",
        "/v1/logs/addon/{slug}",
        "/v1/ha/reload/{domain}",
        "/v1/ha/restart",
        "/v1/ha/resolution",
        "/v1/ha/check-config",
    }
    for _, p := range expectedPaths {
        if spec.Paths.Find(p) == nil {
            t.Errorf("path %s missing from spec", p)
        }
    }
}
```

---

## 10. End-to-End Flow Summary

```
┌─────────────────────────────────────────────────────────────────┐
│  make test-companion                                             │
│                                                                  │
│  1. docker compose up -d                                         │
│     ├── Pull/build companion image (from GitHub)                 │
│     ├── Pull HA Core stable image                                │
│     └── Start both on shared bridge network + named volume       │
│                                                                  │
│  2. Go test setup (TestMain)                                     │
│     ├── Poll HA /api/onboarding until ready                      │
│     ├── Headless onboarding (5 steps → long-lived token)         │
│     ├── Poll companion /v1/health until ready                    │
│     └── Wait for /config to be populated                         │
│                                                                  │
│  3. Integration tests                                            │
│     ├── TestHealth (companion alive)                             │
│     ├── TestListConfigFiles (reads HA's /config)                 │
│     ├── TestReadConfigFile (configuration.yaml)                  │
│     ├── TestSecretsYamlDenied (security)                         │
│     ├── TestDryRun (diff preview)                                │
│     ├── TestWriteNewFile (create + readback)                     │
│     ├── TestCoreLogs (home-assistant.log)                        │
│     ├── TestSupervisorInfo502 (no Supervisor = 502)              │
│     └── TestReload502 (no ha CLI = 502)                          │
│                                                                  │
│  4. docker compose down -v                                       │
│     └── Remove containers, volumes, network                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## 11. Checklist

- [ ] Create `internal/companion/client.go` + `types.go`
- [ ] Create `internal/companion/client_test.go` (unit tests with httptest)
- [ ] Vendor `openapi/companion-v1.yaml` → `testdata/companion-v1.yaml`
- [ ] Create `internal/integration/docker-compose.companion.yaml`
- [ ] Create `internal/integration/testutil.go` (compose lifecycle + onboarding)
- [ ] Create `internal/integration/companion_test.go`
- [ ] Add `make test-companion` to Makefile
- [ ] Add CI job `companion-integration` to GitHub Actions
- [ ] Run `make test-companion` green locally
- [ ] Document in hactl README
