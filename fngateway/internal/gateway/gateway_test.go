package gateway

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// fakeUpstream 模拟 cli2api 的关键行为（路由形态与 upstream internal/server/router.go 一致）。
func fakeUpstream(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSONForTest(w, map[string]any{"ok": true})
	})
	mux.HandleFunc("/api/overview", func(w http.ResponseWriter, r *http.Request) {
		writeJSONForTest(w, map[string]any{
			"authorization": r.Header.Get("Authorization"),
			"x_api_key":     r.Header.Get("x-api-key"),
			"path":          r.URL.Path,
		})
	})
	mux.HandleFunc("/api/redirect", func(w http.ResponseWriter, r *http.Request) {
		http.Redirect(w, r, "/login", http.StatusFound)
	})
	mux.HandleFunc("/api/cookie", func(w http.ResponseWriter, r *http.Request) {
		http.SetCookie(w, &http.Cookie{Name: "cli2api_key", Value: "v", Path: "/"})
		w.WriteHeader(http.StatusNoContent)
	})
	mux.HandleFunc("/api/stream", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("data: hi\n\n"))
	})
	mux.HandleFunc("/assets/index-CQrAj8e4.js", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/javascript")
		_, _ = w.Write([]byte("console.log(1)"))
	})
	mux.HandleFunc("/v1/models", func(w http.ResponseWriter, r *http.Request) {
		writeJSONForTest(w, map[string]any{"object": "list"})
	})
	mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/", "/login", "/accounts":
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			_, _ = w.Write([]byte(indexHTMLForTest))
		default:
			http.NotFound(w, r)
		}
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

const indexHTMLForTest = `<!doctype html><html lang="zh-CN"><head>
<link rel="icon" href="/favicon.svg" />
<link rel="manifest" href="/site.webmanifest" />
<script type="module" crossorigin src="/assets/index-CQrAj8e4.js"></script>
</head><body><div id="root"></div></body></html>`

func writeJSONForTest(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}

func consoleHandler(t *testing.T, upstream string, injectKey string) http.Handler {
	t.Helper()
	proxy, err := NewProxy(Options{
		Prefix:       NewPrefix("/app/cli2api"),
		InternalAddr: strings.TrimPrefix(upstream, "http://"),
		RewriteHTML:  true,
		Bridge:       func() string { return SeedScript() + BridgeScript(NewPrefix("/app/cli2api")) },
		Prepare: func(r *http.Request) {
			if strings.HasPrefix(r.URL.Path, "/api/") {
				r.Header.Set("x-api-key", injectKey)
			}
		},
	})
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	return AdminOnly(proxy, NewPrefix("/app/cli2api"))
}

func adminRequest(method, path string) *http.Request {
	req := httptest.NewRequest(method, path, nil)
	req.Header.Set("X-Trim-Userid", "1000")
	req.Header.Set("X-Trim-Isadmin", "1")
	return req
}

// TestRewriteAccessURLs 单元级锁定改写规则：只动 access 里的相对路径。
func TestRewriteAccessURLs(t *testing.T) {
	body := []byte(`{"ok":true,"access":{"openai_base_url":"/v1","health":"/health",` +
		`"chat_completions":"/v1/chat/completions","messages":"/v1/messages",` +
		`"responses":"/v1/responses","models":"/v1/models",` +
		`"hint":"相对路径以外的原样保留"},` +
		`"worker":{"ready_count":12345678901234567890},"models":[{"id":"m"}]}`)

	out, changed := RewriteAccessURLs(body, "http://192.168.0.2:3010/")
	if !changed {
		t.Fatal("相对路径未被改写")
	}
	var doc map[string]any
	if err := json.Unmarshal(out, &doc); err != nil {
		t.Fatalf("改写后的响应不是合法 JSON: %v", err)
	}
	access := doc["access"].(map[string]any)
	for key, want := range map[string]string{
		"openai_base_url":  "http://192.168.0.2:3010/v1",
		"health":           "http://192.168.0.2:3010/health",
		"chat_completions": "http://192.168.0.2:3010/v1/chat/completions",
		"messages":         "http://192.168.0.2:3010/v1/messages",
		"responses":        "http://192.168.0.2:3010/v1/responses",
		"models":           "http://192.168.0.2:3010/v1/models",
		"hint":             "相对路径以外的原样保留",
	} {
		if got := access[key]; got != want {
			t.Errorf("access.%s = %v，期望 %q", key, got, want)
		}
	}
	// 大整数不能在 JSON 往返中掉精度（配额/用量字段）。
	if !strings.Contains(string(out), "12345678901234567890") {
		t.Errorf("大整数被改写损坏: %s", out)
	}

	// 幂等：已是绝对地址时不再改写。
	if _, changed := RewriteAccessURLs(out, "http://192.168.0.2:3010"); changed {
		t.Error("绝对地址被重复改写（应保持不变）")
	}
	// 没有 access 字段 / 非法 JSON / 空 base：一律原样放过，不能改坏响应。
	for name, in := range map[string][]byte{
		"无 access":   []byte(`{"ok":true}`),
		"非法 JSON":    []byte(`not json`),
		"access 非对象": []byte(`{"access":"x"}`),
	} {
		if _, changed := RewriteAccessURLs(in, "http://192.168.0.2:3010"); changed {
			t.Errorf("%s: 不应改写", name)
		}
	}
	if _, changed := RewriteAccessURLs(body, "   "); changed {
		t.Error("空 base 不应改写")
	}
}

// TestOverviewAccessURLsRewrite 端到端：面板下「API 接入」返回的地址必须是
// 下游端口，而不是面板自己所在的飞牛桌面域 —— 用户就是照这里抄到客户端里的。
func TestOverviewAccessURLsRewrite(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		_, _ = w.Write([]byte(`{"ok":true,"access":{"openai_base_url":"/v1","models":"/v1/models"},` +
			`"proxy":{"port":3010}}`))
	}))
	t.Cleanup(upstream.Close)

	proxy, err := NewProxy(Options{
		Prefix:       NewPrefix("/app/cli2api"),
		InternalAddr: strings.TrimPrefix(upstream.URL, "http://"),
		RewriteHTML:  true,
		AccessBase: func(r *http.Request) string {
			return "http://" + strings.Split(r.Host, ":")[0] + ":3010"
		},
	})
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}

	req := httptest.NewRequest("GET", "/app/cli2api/api/overview", nil)
	req.Host = "192.168.0.2:8666" // 飞牛桌面域（面板所在处）
	rec := httptest.NewRecorder()
	proxy.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 %d", rec.Code)
	}
	var doc struct {
		Access map[string]string `json:"access"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	if got := doc.Access["openai_base_url"]; got != "http://192.168.0.2:3010/v1" {
		t.Errorf("openai_base_url = %q，期望 http://192.168.0.2:3010/v1", got)
	}
	if got := doc.Access["models"]; got != "http://192.168.0.2:3010/v1/models" {
		t.Errorf("models = %q，期望 http://192.168.0.2:3010/v1/models", got)
	}
	if strings.Contains(rec.Body.String(), "8666") {
		t.Errorf("面板域端口泄漏到了接入地址里: %s", rec.Body.String())
	}
	// 改写后 Content-Length 必须跟着走，否则前端会截断。
	if cl := rec.Header().Get("Content-Length"); cl != "" && cl != fmt.Sprint(rec.Body.Len()) {
		t.Errorf("Content-Length=%s，实际 body=%d", cl, rec.Body.Len())
	}
}

func TestAdminGate(t *testing.T) {
	up := fakeUpstream(t)
	h := consoleHandler(t, up.URL, "sk-console-test")

	cases := []struct {
		name    string
		req     *http.Request
		want    int
		wantCT  string
		wantSub string
	}{
		{"非管理员访问 API 得到 JSON 403", httptest.NewRequest("GET", "/app/cli2api/api/overview", nil), http.StatusForbidden, "application/json", "forbidden"},
		{"非管理员访问页面得到 HTML 403", httptest.NewRequest("GET", "/app/cli2api/", nil), http.StatusForbidden, "text/html", "需要管理员权限"},
		{"isadmin 非真值同样拒绝", func() *http.Request {
			r := httptest.NewRequest("GET", "/app/cli2api/api/overview", nil)
			r.Header.Set("X-Trim-Userid", "1000")
			r.Header.Set("X-Trim-Isadmin", "0")
			return r
		}(), http.StatusForbidden, "application/json", "forbidden"},
		{"缺 userid 拒绝（防止伪造 isadmin）", func() *http.Request {
			r := httptest.NewRequest("GET", "/app/cli2api/api/overview", nil)
			r.Header.Set("X-Trim-Isadmin", "1")
			return r
		}(), http.StatusForbidden, "application/json", "forbidden"},
		{"管理员放行", adminRequest("GET", "/app/cli2api/api/overview"), http.StatusOK, "application/json", "x_api_key"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, tc.req)
			if rec.Code != tc.want {
				t.Fatalf("status = %d, want %d", rec.Code, tc.want)
			}
			if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, tc.wantCT) {
				t.Fatalf("content-type = %q, want prefix %q", ct, tc.wantCT)
			}
			if !strings.Contains(rec.Body.String(), tc.wantSub) {
				t.Fatalf("body 缺少 %q: %s", tc.wantSub, rec.Body.String())
			}
		})
	}
}

func TestConsoleRequestsReachUpstream(t *testing.T) {
	up := fakeUpstream(t)
	h := consoleHandler(t, up.URL, "sk-console-test")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/api/overview"))
	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, body=%s", rec.Code, rec.Body.String())
	}
	var got map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if got["path"] != "/api/overview" {
		t.Fatalf("前缀未剥除: path = %v", got["path"])
	}
	// 服务端注入的密钥必须覆盖浏览器送来的占位值。
	if got["x_api_key"] != "sk-console-test" {
		t.Fatalf("x-api-key = %v, want 注入值", got["x_api_key"])
	}
}

func TestHTMLRewriteAndInjection(t *testing.T) {
	up := fakeUpstream(t)
	h := consoleHandler(t, up.URL, "sk-console-test")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/accounts"))
	body := rec.Body.String()

	for _, want := range []string{
		`src="/app/cli2api/assets/index-CQrAj8e4.js"`,
		`href="/app/cli2api/favicon.svg"`,
		`href="/app/cli2api/site.webmanifest"`,
		`crossorigin="use-credentials"`,
		`data-fn-gateway-bridge="1"`,
		`data-fn-gateway-seed="1"`,
		`cli2api_key`,
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("HTML 缺少 %q\n%s", want, body)
		}
	}
	if strings.Count(body, "</head>") != 1 {
		t.Fatalf("head 注入破坏了文档结构")
	}
	for _, header := range []string{"Cache-Control", "Content-Length"} {
		if rec.Header().Get(header) == "" {
			t.Fatalf("缺少响应头 %s", header)
		}
	}
	if cc := rec.Header().Get("Cache-Control"); cc != "no-store" {
		t.Fatalf("HTML 必须 no-store，实际 %q", cc)
	}
	// 幂等：不带前缀的原始路径不应被二次改写。
	if strings.Contains(body, "/app/cli2api/app/cli2api") {
		t.Fatalf("出现双重前缀")
	}
}

// 控制台产物的子路径补丁（build.py 的 patch_console_bundle）靠这个全局取前缀：
// 桥接脚本必须先把前缀暴露出来，产物里的读侧剥离才生效。
func TestBridgeScriptExposesGatewayBase(t *testing.T) {
	script := SeedScript() + BridgeScript(NewPrefix("/app/cli2api"))
	for _, want := range []string{
		`var P="/app/cli2api";`,
		`window.__fnGatewayBase=P;`,
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("桥接脚本缺少 %q", want)
		}
	}
	// 读侧前缀剥离已移到构建期补丁：Chrome 的 Location 属性是 [LegacyUnforgeable]，
	// 运行时 patch Location.prototype 永远装不上，重新加回来只会让人误以为生效了。
	if strings.Contains(script, "Location.prototype") {
		t.Fatalf("桥接脚本不应再 patch Location.prototype（运行时不可能生效）")
	}
}

func TestAssetCacheAndTrue404(t *testing.T) {
	up := fakeUpstream(t)
	h := consoleHandler(t, up.URL, "sk-console-test")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/assets/index-CQrAj8e4.js"))
	if rec.Code != http.StatusOK {
		t.Fatalf("asset status = %d", rec.Code)
	}
	if cc := rec.Header().Get("Cache-Control"); !strings.Contains(cc, "immutable") {
		t.Fatalf("指纹资源应长缓存，实际 %q", cc)
	}

	// 未命中资源必须保持 404（不能退化成 200 + index.html，否则发版后永久白屏）。
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/assets/missing-D3adbeef.js"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("缺失资源 status = %d, want 404", rec.Code)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/not-a-route"))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("未知路由 status = %d, want 404", rec.Code)
	}
}

func TestLocationCookieSSE(t *testing.T) {
	up := fakeUpstream(t)
	h := consoleHandler(t, up.URL, "sk-console-test")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/api/redirect"))
	if loc := rec.Header().Get("Location"); loc != "/app/cli2api/login" {
		t.Fatalf("Location = %q, want /app/cli2api/login", loc)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/api/cookie"))
	if sc := rec.Header().Get("Set-Cookie"); !strings.Contains(sc, "Path=/app/cli2api/") {
		t.Fatalf("Set-Cookie = %q, want 前缀作用域", sc)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, adminRequest("GET", "/app/cli2api/api/stream"))
	if rec.Header().Get("X-Accel-Buffering") != "no" {
		t.Fatalf("SSE 应关闭缓冲")
	}
	if rec.Header().Get("Content-Length") != "" {
		t.Fatalf("SSE 不应带 Content-Length")
	}
}

func TestPublicProxyOnlyServesV1(t *testing.T) {
	up := fakeUpstream(t)
	proxy, err := NewProxy(Options{InternalAddr: strings.TrimPrefix(up.URL, "http://")})
	if err != nil {
		t.Fatalf("NewProxy: %v", err)
	}
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/health" || strings.HasPrefix(r.URL.Path, "/v1/") {
			proxy.ServeHTTP(w, r)
			return
		}
		NotFound(w, r)
	})

	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/v1/chat/completions", nil)
	req.Header.Set("Authorization", "Bearer sk-downstream")
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusNotFound {
		// 假上游只注册了 /v1/models，这里确认请求确实被转发了（而不是被本层拦掉）。
		t.Fatalf("status = %d, want 上游 404", rec.Code)
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/v1/models", nil))
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "list") {
		t.Fatalf("/v1/models 未直通: %d %s", rec.Code, rec.Body.String())
	}

	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/app/cli2api/api/overview", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("下游端口不得暴露控制台 API，status = %d", rec.Code)
	}
}
