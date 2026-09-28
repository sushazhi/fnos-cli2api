// Command fngateway CLI2API 飞牛网关入口：反向代理 + 子进程监督。
//
// 进程模型（单进程，双监听，仅由 cmd/main 管理这一个 PID）：
//
//	┌─ 飞牛统一网关 ─→ ${TRIM_APPDEST}/cli2api.sock
//	│      └─ 控制台入口（仅管理员，见 internal/gateway/gate.go）：
//	│           剥前缀 /app/cli2api → 改写 HTML/重定向/Cookie →
//	│           注入桥接脚本与控制台会话 → 127.0.0.1:<内部端口>
//	├─ TCP ${TRIM_SERVICE_PORT}（3010，上游默认端口）
//	│      └─ 下游独立接入：只放行 /v1/* 与 /health，OpenAI 兼容客户端直连
//	└─ cli2api 子进程（上游原样二进制，零源码改动）：127.0.0.1:<内部端口>
//	       └─ 按账号拉起 Node worker（node 来自应用中心 nodejs_v24）
//
// 为什么必须有 TCP 下游端口：飞牛统一网关（1.2.0604+）会把下游客户端必带的
// Authorization 当成自己的票据拦截（invalid token），OpenAI 兼容客户端走不通
// 网关路径 —— 与 D:\fnos 下 workbuddy2api / deepseek.harness 两个项目的结论一致。
//
// 为什么控制台在网关下能免密登录：控制台词自己的登录口令是 SQLite 里的
// console key（app_secrets.proxy_api_key）。浏览器侧只放占位值通过登录守卫，
// 真正的密钥由本进程在服务端注入 x-api-key —— 密钥不下发到浏览器。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"cli2api-fnos/internal/consolekey"
	"cli2api-fnos/internal/gateway"
)

// version 构建期注入：-ldflags "-X main.version=..."
var version = "dev"

const (
	// gatewayPrefix 必须与 app/ui/config 里的 gatewayPrefix 一致。
	gatewayPrefix = "/app/cli2api"
	// defaultPort 与 manifest 的 service_port 一致（TRIM_SERVICE_PORT 优先）。
	// 取上游 cli2api 的默认端口（internal/config/config.go 的 PORT 默认值 3010），
	// 下游客户端按上游文档照抄 base_url 就能直连。
	defaultPort = 3010
	// workerBasePort Qoder worker 的起始端口（每个账号 +1），同样对齐上游默认值
	// （QODER_WORKER_BASE_PORT=32100，见上游 deploy/Dockerfile）。
	workerBasePort = 32100
	// nodeRuntimeDir 应用中心运行时依赖 nodejs_v24 的安装位置。
	nodeRuntimeDir = "/var/apps/nodejs_v24/target/bin"
	// readyTimeout cli2api 子进程就绪等待上限；应小于 cmd/main 的失效窗口。
	readyTimeout = 20 * time.Second
	// stopTimeout 子进程优雅退出等待上限。
	stopTimeout = 15 * time.Second
	// logMaxBytes 日志单文件上限（超出轮转到 .1）。
	logMaxBytes = 8 << 20
)

type layout struct {
	appDest string
	pkgVar  string
	socket  string
	port    int
	prefix  gateway.Prefix
}

func main() {
	showVersion := flag.Bool("version", false, "打印版本后退出")
	flag.Parse()
	if *showVersion {
		fmt.Printf("cli2api-fnos %s\n", version)
		return
	}

	lay := resolveLayout()
	panelLog, err := newRotatingWriter(filepath.Join(lay.pkgVar, "panel.log"), logMaxBytes)
	if err != nil {
		log.Printf("警告：初始化面板日志失败: %v", err)
	} else {
		log.SetOutput(io.MultiWriter(os.Stderr, panelLog))
		defer func() { _ = panelLog.Close() }()
	}

	log.Printf("=== CLI2API 飞牛网关 %s 启动 ===", version)
	log.Printf("  安装目录:   %s", lay.appDest)
	log.Printf("  数据目录:   %s", lay.pkgVar)
	log.Printf("  网关前缀:   %s", lay.prefix.Path)
	log.Printf("  Socket:     %s", lay.socket)
	log.Printf("  下游端口:   %d（仅 /v1/* 与 /health）", lay.port)

	// ---------------------------------------------------------------------
	// 1) 上游 cli2api 子进程
	// ---------------------------------------------------------------------
	childLog, err := newRotatingWriter(filepath.Join(lay.pkgVar, "cli2api.log"), logMaxBytes)
	if err != nil {
		log.Printf("警告：初始化上游日志失败: %v；上游输出将只进面板日志", err)
	} else {
		defer func() { _ = childLog.Close() }()
	}
	// 告警而非终止：缺 node 时 Qoder 账号起不来，但控制台仍要能打开看原因。
	node := findNode()
	if node == "" {
		log.Printf("⚠️  未找到 Node 运行时（应用中心应已安装 nodejs_v24）——Qoder 账号将无法启动")
	} else {
		log.Printf("  Node 运行时: %s", node)
	}
	if _, statErr := os.Stat(filepath.Join(lay.appDest, "worker", "src", "daemon.mjs")); statErr != nil {
		log.Printf("⚠️  未找到 worker 组件（%s）——Qoder 账号将无法启动", filepath.Join(lay.appDest, "worker", "src", "daemon.mjs"))
	}

	internalPort, err := pickPort()
	if err != nil {
		log.Fatalf("分配内部端口失败: %v", err)
	}
	internalAddr := "127.0.0.1:" + strconv.Itoa(internalPort)

	child := startChild(lay, internalPort, node, childLog)
	if child != nil {
		if readyErr := waitReady(internalAddr, readyTimeout); readyErr != nil {
			log.Printf("⚠️  上游未就绪: %v（控制台仍会打开并显示错误页）", readyErr)
		} else {
			log.Printf("✅ 上游 cli2api 就绪: http://%s", internalAddr)
		}
	}

	// ---------------------------------------------------------------------
	// 2) 控制台入口（Unix Socket，仅管理员）
	// ---------------------------------------------------------------------
	keyReader := consolekey.New(filepath.Join(lay.pkgVar, "data", "qoder.db"))
	warnKey := newThrottledWarn(30 * time.Second)

	consoleProxy, err := gateway.NewProxy(gateway.Options{
		Prefix:       lay.prefix,
		InternalAddr: internalAddr,
		RewriteHTML:  true,
		Bridge: func() string {
			return gateway.SeedScript() + gateway.BridgeScript(lay.prefix)
		},
		// 控制台「API 接入」页展示给外部客户端抄的地址，必须指向下游独立端口，
		// 而不是面板自己所在的飞牛桌面域（详见 gateway.RewriteAccessURLs）。
		AccessBase: func(r *http.Request) string {
			return downstreamBase(r, lay.port)
		},
		Prepare: func(r *http.Request) {
			// 控制台 API 一律以库里的密钥为准：网关已确认管理员身份，
			// 浏览器送来的值（占位值或旧值）不作数 —— 这样也天然免疫密钥轮换。
			if !strings.HasPrefix(r.URL.Path, "/api/") {
				return
			}
			key, keyErr := keyReader.Key()
			if keyErr != nil {
				warnKey("读取控制台密钥失败: %v", keyErr)
				return
			}
			r.Header.Set("x-api-key", key)
		},
	})
	if err != nil {
		log.Fatalf("构造控制台反代失败: %v", err)
	}

	gwLn, err := gateway.ListenUnix(lay.socket)
	if err != nil {
		log.Fatalf("%v", err)
	}
	consoleSrv := &http.Server{
		Handler:           gateway.AdminOnly(consoleProxy, lay.prefix),
		ReadHeaderTimeout: 30 * time.Second,
		IdleTimeout:       120 * time.Second,
		// 不设 WriteTimeout：对话测试台的流式回答可能持续数分钟。
	}
	go func() {
		if serveErr := consoleSrv.Serve(gwLn); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
			log.Fatalf("控制台入口退出: %v", serveErr)
		}
	}()
	log.Printf("✅ 统一网关入口就绪: unix:%s → %s", lay.socket, lay.prefix.Path)
	log.Printf("   访问方式: 飞牛桌面「CLI2API 网关」图标（仅管理员）")

	// ---------------------------------------------------------------------
	// 3) 下游独立接入端口（不经过统一网关，供任意 OpenAI 兼容客户端使用）
	// ---------------------------------------------------------------------
	// 只挂 /v1/* 与 /health：控制台在网关路径上，这里不暴露 /api/*；
	// 未知 /v1 路径由 cli2api 自己返回真 404（见 upstream internal/server/router.go）。
	publicProxy, err := gateway.NewProxy(gateway.Options{
		InternalAddr: internalAddr,
	})
	if err != nil {
		log.Fatalf("构造下游反代失败: %v", err)
	}
	publicHandler := publicRoutes(publicProxy)

	// 绑所有网卡：这是 manifest.service_port 声明的对外端口，局域网内的下游
	// 客户端直连它（网关路径带飞牛登录态，外部客户端走不通）。安全性由
	// publicRoutes 的路径白名单 + 上游 /v1/* 的密钥校验（withAPIKey）共同
	// 保证：控制台 /api/* 在此端口一律 404，不对外暴露。
	downLn, listenErr := net.Listen("tcp", ":"+strconv.Itoa(lay.port))
	if listenErr != nil {
		// 端口占用不应拖垮面板：用户可在应用中心看到提示并换端口。
		log.Printf("⚠️  下游端口 %d 监听失败（控制台不受影响）: %v", lay.port, listenErr)
	}
	var downSrv *http.Server
	if downLn != nil {
		downSrv = &http.Server{
			Handler:           publicHandler,
			ReadHeaderTimeout: 30 * time.Second,
			IdleTimeout:       120 * time.Second,
		}
		go func() {
			if serveErr := downSrv.Serve(downLn); serveErr != nil && !errors.Is(serveErr, http.ErrServerClosed) {
				log.Printf("下游接入服务退出: %v", serveErr)
			}
		}()
		log.Printf("✅ 下游接入端口就绪: http://<飞牛IP>:%d/v1（面板密钥鉴权）", lay.port)
	}

	// ---------------------------------------------------------------------
	// 4) 等待退出信号 / 子进程死亡
	// ---------------------------------------------------------------------
	ctx, stop := signalContext()
	defer stop()

	select {
	case <-ctx.Done():
		log.Printf("收到退出信号，正在停止…")
	case childErr := <-childDone(child):
		// 子进程是唯一功能载体：它死了留一个空壳没有意义，
		// 直接退出让应用中心显示为「已停止」，用户可一键重启。
		log.Printf("⚠️  上游 cli2api 退出: %v", childErr)
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	_ = consoleSrv.Shutdown(shutdownCtx)
	if downSrv != nil {
		_ = downSrv.Shutdown(shutdownCtx)
	}
	stopChild(child)

	// 清理 Socket，避免下次启动 bind 失败。
	_ = os.Remove(lay.socket)
	log.Printf("已退出")
}

// publicRoutes 下游对外端口的路径白名单：只放行 /v1/* 与 /health。
//
// 这是本进程唯一对外裸露的入口，白名单必须在网关这层给出确定结论：
// 判定用 path.Clean 后的结果，否则 /v1/../api/keys 这类请求会靠 /v1/ 前缀
// 蒙混过关，再由上游路由归一化后落到控制台接口上。转发仍用原始路径，不改
// 上游行为（尾斜杠等由上游照常处理）。
func publicRoutes(proxy http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		p := path.Clean(r.URL.Path)
		if p == "/health" || strings.HasPrefix(p, "/v1/") {
			proxy.ServeHTTP(w, r)
			return
		}
		gateway.NotFound(w, r)
	})
}

// resolveLayout 解析安装布局；TRIM_* 由飞牛注入，缺失时按开发直跑处理。
func resolveLayout() layout {
	appDest := strings.TrimSpace(os.Getenv("TRIM_APPDEST"))
	if appDest == "" {
		if wd, err := os.Getwd(); err == nil {
			appDest = wd
		} else {
			appDest = "."
		}
	}
	pkgVar := strings.TrimSpace(os.Getenv("TRIM_PKGVAR"))
	if pkgVar == "" {
		pkgVar = filepath.Join(appDest, "var")
	}
	port := defaultPort
	if v := strings.TrimSpace(os.Getenv("TRIM_SERVICE_PORT")); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n < 65536 {
			port = n
		}
	}
	return layout{
		appDest: appDest,
		pkgVar:  pkgVar,
		socket:  filepath.Join(appDest, "cli2api.sock"),
		port:    port,
		prefix:  gateway.NewPrefix(gatewayPrefix),
	}
}

// downstreamBase 由面板请求的 Host 推出下游 API 的绝对基址（形如 http://192.168.0.2:3010）。
//
// 面板挂在飞牛桌面的域下（http://192.168.0.2:8666/app/cli2api），而控制台「API 接入」
// 页上的地址是给外部客户端抄的：必须换成本应用的下游端口，并且固定 http ——
// 下游监听是明文 HTTP，桌面即使走 8667(https) 也不改变这一点。
// 取不到 Host（异常请求）时返回空串，调用方据此跳过改写。
func downstreamBase(r *http.Request, port int) string {
	host := strings.TrimSpace(r.Host)
	if h, _, err := net.SplitHostPort(host); err == nil {
		host = h
	}
	host = strings.TrimSpace(host)
	if host == "" || port <= 0 {
		return ""
	}
	// 裸 IPv6 字面量要补方括号，否则拼出的 URL 不可解析。
	if strings.Contains(host, ":") && !strings.HasPrefix(host, "[") {
		host = "[" + host + "]"
	}
	return "http://" + host + ":" + strconv.Itoa(port)
}

// findNode 定位 Node 运行时：优先应用中心 nodejs_v24，其次同系列版本，最后 PATH。
func findNode() string {
	candidates := []string{filepath.Join(nodeRuntimeDir, "node")}
	if matches, err := filepath.Glob("/var/apps/nodejs_v*/target/bin/node"); err == nil {
		candidates = append(candidates, matches...)
	}
	for _, c := range candidates {
		if info, err := os.Stat(c); err == nil && !info.IsDir() && info.Mode()&0o111 != 0 {
			return c
		}
	}
	if p, err := exec.LookPath("node"); err == nil {
		return p
	}
	return ""
}

// pickPort 让内核分配一个空闲回环端口后立即释放。
//
// 存在极小的竞态窗口（释放到子进程 bind 之间），但比固定端口冲突可控得多；
// 子进程起不来时日志会明确报出 bind 失败。
func pickPort() (int, error) {
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	defer func() { _ = ln.Close() }()
	addr, ok := ln.Addr().(*net.TCPAddr)
	if !ok {
		return 0, errors.New("非 TCP 监听地址")
	}
	return addr.Port, nil
}

// startChild 拉起上游 cli2api（工作目录与全部相对路径都由环境变量给定）。
func startChild(lay layout, internalPort int, node string, childLog *rotatingWriter) *exec.Cmd {
	binName := "cli2api-linux-" + runtime.GOARCH // amd64 / arm64，与自身架构一致
	bin := filepath.Join(lay.appDest, "bin", binName)
	if !fileExists(bin) {
		// 命名不一致时的兜底：扫 bin 目录，避免因架构名差异起不来。
		if matches, _ := filepath.Glob(filepath.Join(lay.appDest, "bin", "cli2api-linux-*")); len(matches) > 0 {
			bin = matches[0]
		}
	}
	if !fileExists(bin) {
		log.Printf("⚠️  未找到上游程序 %s，控制台将显示 502", bin)
		return nil
	}

	env := append(os.Environ(), childEnv(lay, node, strconv.Itoa(internalPort))...)

	cmd := exec.Command(bin)
	cmd.Dir = lay.pkgVar
	cmd.Env = env
	cmd.Stdout = childLog
	cmd.Stderr = childLog
	if err := cmd.Start(); err != nil {
		log.Printf("⚠️  启动上游失败: %v，控制台将显示 502", err)
		return nil
	}
	log.Printf("  上游进程:   pid=%d %s", cmd.Process.Pid, bin)
	return cmd
}

// childEnv 构造上游子进程环境（相对路径一律显式给绝对路径，不依赖 CWD）。
func childEnv(lay layout, node, portText string) []string {
	workerDir := filepath.Join(lay.appDest, "worker")
	env := []string{
		"HOST=127.0.0.1",
		"PORT=" + portText,
		"HOME=" + filepath.Join(lay.pkgVar, "home"),
		"QODER_DATA_DIR=" + filepath.Join(lay.pkgVar, "data"),
		"QODER_RUNTIME_DIR=" + filepath.Join(lay.pkgVar, "runtime"),
		"QODER_HOME=" + filepath.Join(lay.pkgVar, "home", ".qoder"),
		"QODER_WORKER_DAEMON=" + filepath.Join(workerDir, "src", "daemon.mjs"),
		"QODERCLI_JS=" + filepath.Join(workerDir, "node_modules", "@qoder-ai", "qodercli", "bundle", "qodercli.js"),
		"QODERCNCLI_JS=" + filepath.Join(workerDir, "node_modules", "@qodercn-ai", "qoderclicn", "bundle", "qoderclicn.js"),
		"PLAIN_TEMPLATE_PATH=" + filepath.Join(workerDir, "last-plain.sample.json"),
		"QODER_WORKER_BASE_PORT=" + strconv.Itoa(workerBasePort),
	}
	if node != "" {
		env = append(env, "QODER_NODE_BINARY="+node)
		// worker 与 Qoder CLI 都可能自行调用 node，PATH 里必须有它。
		path := os.Getenv("PATH")
		env = append(env, "PATH="+filepath.Dir(node)+string(os.PathListSeparator)+path)
	}
	return env
}

func waitReady(addr string, timeout time.Duration) error {
	client := &http.Client{Timeout: 5 * time.Second}
	url := "http://" + addr + "/health"
	deadline := time.Now().Add(timeout)
	var last error
	for time.Now().Before(deadline) {
		resp, err := client.Get(url)
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			last = fmt.Errorf("health 状态码 %d", resp.StatusCode)
		} else {
			last = err
		}
		time.Sleep(400 * time.Millisecond)
	}
	if last == nil {
		last = errors.New("超时")
	}
	return last
}

// childDone 返回子进程退出通道；无子进程时返回永不触发的通道。
func childDone(cmd *exec.Cmd) <-chan error {
	ch := make(chan error, 1)
	if cmd == nil || cmd.Process == nil {
		return ch
	}
	go func() { ch <- cmd.Wait() }()
	return ch
}

// stopChild 先 SIGTERM 让上游优雅收尾（它会一并停掉 Node worker），超时强杀。
func stopChild(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = cmd.Process.Signal(syscall.SIGTERM)
	done := make(chan struct{})
	go func() { _, _ = cmd.Process.Wait(); close(done) }()
	select {
	case <-done:
		log.Printf("上游已退出")
	case <-time.After(stopTimeout):
		log.Printf("上游优雅退出超时，强制结束 pid=%d", cmd.Process.Pid)
		_ = cmd.Process.Kill()
	}
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

// signalContext 监听 SIGINT/SIGTERM（cmd/main 用 SIGTERM 请求优雅退出）。
func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

// throttledWarn 限流日志：控制台密钥读取失败会伴随每个 /api 请求，
// 不能让它把日志刷爆。
type throttledWarn struct {
	mu   sync.Mutex
	last time.Time
	gap  time.Duration
}

func newThrottledWarn(gap time.Duration) func(string, ...any) {
	t := &throttledWarn{gap: gap}
	return func(format string, args ...any) {
		t.mu.Lock()
		defer t.mu.Unlock()
		if time.Since(t.last) < t.gap {
			return
		}
		t.last = time.Now()
		log.Printf(format, args...)
	}
}
