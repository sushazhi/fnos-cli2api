package main

import (
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
)

// TestLayoutPortDefaultsToUpstream 锁定下游端口默认值 = 上游 cli2api 的默认端口。
//
// 上游 internal/config/config.go 里 PORT 的默认值是 3010（deploy/docker-compose.yml
// 也暴露 3010）。取上游默认值，下游客户端照抄上游文档即可直连；同时飞牛注入的
// TRIM_SERVICE_PORT 必须优先，否则应用中心改端口会静默失效。
func TestLayoutPortDefaultsToUpstream(t *testing.T) {
	t.Setenv("TRIM_SERVICE_PORT", "")
	if got := resolveLayout().port; got != 3010 {
		t.Fatalf("默认下游端口 = %d，期望上游默认值 3010", got)
	}
	t.Setenv("TRIM_SERVICE_PORT", "4321")
	if got := resolveLayout().port; got != 4321 {
		t.Fatalf("TRIM_SERVICE_PORT=4321 时端口 = %d，期望 4321", got)
	}
	// 越界值必须回落到默认端口，不能把 0 或 >65535 带进监听。
	for _, bad := range []string{"0", "70000", "abc"} {
		t.Setenv("TRIM_SERVICE_PORT", bad)
		if got := resolveLayout().port; got != 3010 {
			t.Fatalf("TRIM_SERVICE_PORT=%q 未被拒绝: 端口 = %d，期望回落 3010", bad, got)
		}
	}
}

// TestDownstreamBase 锁定「API 接入」页展示的基址：主机取面板请求的 Host，
// 端口固定为下游端口，且永远是 http（下游是明文 HTTP 监听）。
func TestDownstreamBase(t *testing.T) {
	cases := []struct {
		host string
		port int
		want string
	}{
		{"192.168.0.2:8666", 3010, "http://192.168.0.2:3010"},
		{"192.168.0.2:12965", 3010, "http://192.168.0.2:3010"},
		{"nas", 3010, "http://nas:3010"},
		{"nas.local:8666", 3010, "http://nas.local:3010"},
		{"[fe80::1]:8666", 3010, "http://[fe80::1]:3010"},
		{"", 3010, ""},
		{"nas:8666", 0, ""},
	}
	for _, c := range cases {
		r := httptest.NewRequest(http.MethodGet, "http://placeholder/app/cli2api/api/overview", nil)
		r.Host = c.host
		if got := downstreamBase(r, c.port); got != c.want {
			t.Errorf("downstreamBase(host=%q, port=%d) = %q，期望 %q", c.host, c.port, got, c.want)
		}
	}
}

// TestPublicRoutesWhitelist 锁定下游对外端口的路径白名单。
//
// 这是进程里唯一对外裸露的入口：控制台接口（/api/*）一旦从这里可达，
// 等于把管理员面板开到局域网。用 Clean 后的路径判定，才能挡住靠 /v1/
// 前缀蒙混、再指望上游归一化的绕过写法。
func TestPublicRoutesWhitelist(t *testing.T) {
	var reached int
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reached++
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(r.URL.Path))
	})
	h := publicRoutes(upstream)

	cases := []struct {
		path    string
		reached bool
		name    string
	}{
		{path: "/health", reached: true, name: "健康检查放行"},
		{path: "/v1/chat/completions", reached: true, name: "chat 放行"},
		{path: "/v1/models", reached: true, name: "models 放行"},
		{path: "/v1/messages", reached: true, name: "anthropic 放行"},
		{path: "/v1/responses", reached: true, name: "responses 放行"},

		{path: "/", reached: false, name: "根路径拒绝"},
		{path: "/api/keys", reached: false, name: "控制台接口拒绝"},
		{path: "/api/overview", reached: false, name: "控制台总览拒绝"},
		{path: "/assets/index.js", reached: false, name: "控制台静态资源拒绝"},
		{path: "/login", reached: false, name: "控制台页面拒绝"},
		{path: "/v1", reached: false, name: "无尾斜杠的 /v1 拒绝"},
		{path: "/v1x/models", reached: false, name: "前缀相似路径拒绝"},

		{path: "/v1/../api/keys", reached: false, name: "点段逃逸到控制台接口拒绝"},
		{path: "/v1/../api/overview", reached: false, name: "点段逃逸到总览拒绝"},
		{path: "/v1/./../../etc/passwd", reached: false, name: "多层点段逃逸拒绝"},
		{path: "/v1/../../api/keys", reached: false, name: "越界后回退拒绝"},
	}

	for _, c := range cases {
		reached = 0
		req := httptest.NewRequest(http.MethodGet, c.path, nil)
		// 万一构造请求时路径就被规范化了，点段用例会「因为错误的原因」通过。
		if req.URL.Path != c.path {
			t.Fatalf("%s: 夹具路径被规范化成 %q，用例失去意义", c.name, req.URL.Path)
		}
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)

		got := reached > 0
		if got != c.reached {
			t.Errorf("%s: %s 转发=%v，期望 %v（状态码 %d）", c.name, c.path, got, c.reached, rec.Code)
		}
		if !c.reached && rec.Code != http.StatusNotFound {
			t.Errorf("%s: %s 拒绝状态码 %d，期望 404", c.name, c.path, rec.Code)
		}
	}
}

// TestPublicRoutesForwardsPathVerbatim 放行时不得改写路径：上游要按原样
// 收到请求（含尾斜杠），否则等于网关偷偷改了路由语义。
func TestPublicRoutesForwardsPathVerbatim(t *testing.T) {
	var seen string
	upstream := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r.URL.Path
		w.WriteHeader(http.StatusOK)
	})
	h := publicRoutes(upstream)

	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions/", nil)
	h.ServeHTTP(httptest.NewRecorder(), req)

	if seen != "/v1/chat/completions/" {
		t.Errorf("上游收到的路径为 %q，期望原样 /v1/chat/completions/", seen)
	}
}

// TestChildEnvInjectsWorkerHeapCap 锁定「给每个 Qoder worker 注入 V8 堆上限」。
//
// 每个启用账号是一个常驻 Node 进程（实测空闲 RSS ≈ 511MB），不给上限时
// V8 按物理内存推算默认堆，多账号会吃满内存并开始 swap。注入点必须落在
// childEnv 里，才能沿 环境继承链 传到 worker：
//
//	fngateway(本函数) → cli2api 子进程(providers/qoder/starter.go proxyEnv
//	只过滤代理变量，其余原样继承) → node daemon.mjs
func TestChildEnvInjectsWorkerHeapCap(t *testing.T) {
	lay := layout{appDest: "/appdest", pkgVar: "/pkgvar", socket: "/appdest/cli2api.sock", port: 3010}

	find := func(env []string, key string) (string, bool) {
		for _, kv := range env {
			if strings.HasPrefix(kv, key+"=") {
				return strings.TrimPrefix(kv, key+"="), true
			}
		}
		return "", false
	}

	// 默认：注入 --max-old-space-size，值是正数。
	t.Setenv(workerHeapEnv, "")
	got, ok := find(childEnv(lay, "/usr/bin/node", "4567"), "NODE_OPTIONS")
	if !ok {
		t.Fatalf("默认未注入 NODE_OPTIONS，worker 将按物理内存自算堆上限")
	}
	if want := "--max-old-space-size=" + strconv.Itoa(defaultWorkerMaxOldSpaceMB); got != want {
		t.Errorf("NODE_OPTIONS = %q，期望 %q", got, want)
	}

	// 显式覆盖生效。
	t.Setenv(workerHeapEnv, "256")
	if got, _ := find(childEnv(lay, "", "4567"), "NODE_OPTIONS"); got != "--max-old-space-size=256" {
		t.Errorf("覆盖后 NODE_OPTIONS = %q，期望 --max-old-space-size=256", got)
	}

	// 0 表示关闭注入（回退 Node 默认行为），必须真的不出现该键。
	t.Setenv(workerHeapEnv, "0")
	if got, ok := find(childEnv(lay, "", "4567"), "NODE_OPTIONS"); ok {
		t.Errorf("%s=0 时应不注入 NODE_OPTIONS，实际为 %q", workerHeapEnv, got)
	}

	// 非法值必须回退默认，而不是把垃圾串塞进 Node 命令行。
	for _, bad := range []string{"abc", "-1", "12.5"} {
		t.Setenv(workerHeapEnv, bad)
		want := "--max-old-space-size=" + strconv.Itoa(defaultWorkerMaxOldSpaceMB)
		if got, _ := find(childEnv(lay, "", "4567"), "NODE_OPTIONS"); got != want {
			t.Errorf("%s=%q 时 NODE_OPTIONS = %q，期望回退 %q", workerHeapEnv, bad, got, want)
		}
	}
}
