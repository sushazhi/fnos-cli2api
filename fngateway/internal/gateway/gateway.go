// Package gateway 飞牛统一网关适配层。
//
// 职责：把挂在 /app/cli2api 子路径下的请求透明地代理到 cli2api 内部服务，
// 并完成子路径改写（HTML 绝对路径、重定向、Cookie 作用域、SSE、PWA manifest），
// 使未做 base-path 适配的官方控制台能直接在网关下工作；
// 另提供下游独立端口的直通代理（仅 /v1/*，不做任何改写）。
//
// 参考 fnos-developer skill references/gateway-proxy.md 与 D:\fnos 下
// fnos-workbuddy2api / deepseek.harness 两个已验证项目的实现。
package gateway

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"regexp"
	"strings"
	"time"
)

// Prefix 网关挂载前缀（运行时注入）。
type Prefix struct {
	// Path 形如 /app/cli2api（无尾斜杠）。零值表示不做前缀处理。
	Path string
}

// NewPrefix 构造前缀对象；空值回落到本应用默认前缀。
func NewPrefix(p string) Prefix {
	p = strings.TrimRight(strings.TrimSpace(p), "/")
	if p == "" {
		p = "/app/cli2api"
	}
	return Prefix{Path: p}
}

// Strip 剥除前缀，返回以 / 开头的内部路径。
// safe 为 false 表示该路径本就不带前缀（调用方自行决定如何处理）。
func (p Prefix) Strip(path string) (string, bool) {
	if path == p.Path {
		return "/", true
	}
	if strings.HasPrefix(path, p.Path+"/") {
		return path[len(p.Path):], true
	}
	return path, false
}

// IsAssetPath 判断内部路径是否为带指纹的静态资源（可长缓存）。
func IsAssetPath(internal string) bool {
	return strings.HasPrefix(internal, "/assets/")
}

// ---------------------------------------------------------------------------
// 子路径改写
// ---------------------------------------------------------------------------

var (
	// HTML 中需要加前缀的路径属性：
	//   - 绝对路径  src="/assets/x.js"
	//   - 相对路径  src="./assets/x.js"
	// Vite（base:'/'）产出的是绝对路径；两种形式都要匹配，相对路径统一改写为
	// 绝对前缀路径 —— 文档 URL 可能是 /app/cli2api（不带尾斜杠），保留 './'
	// 会被浏览器解析到上一级 /app/assets/。
	htmlAttrRe = regexp.MustCompile(`(?i)\b(src|href|action|poster)\s*=\s*(["'])(\.?/[^"']*)`)
	// Set-Cookie 的 Path=/。
	cookiePathRe = regexp.MustCompile(`(?i)\bpath\s*=\s*/(;|$)`)
	// PWA manifest 链接：必须补 crossorigin，否则 manifest 请求不带凭证，
	// 被飞牛网关判为未登录（invalid token）。
	manifestLinkRe = regexp.MustCompile(`(?i)<link[^>]*rel\s*=\s*["']manifest["'][^>]*>`)
)

func (p Prefix) shouldRewrite(v string) bool {
	// 协议相对 //host/x 不动。
	if strings.HasPrefix(v, "//") {
		return false
	}
	rel := strings.TrimPrefix(v, "./")
	if rel == p.Path || strings.HasPrefix(rel, p.Path+"/") {
		return false
	}
	return true
}

// RewriteHTML 改写 HTML 文档中的资源路径与 manifest 链接。
func (p Prefix) RewriteHTML(body []byte) []byte {
	s := htmlAttrRe.ReplaceAllStringFunc(string(body), func(m string) string {
		sub := htmlAttrRe.FindStringSubmatch(m)
		if len(sub) != 4 {
			return m
		}
		attr, quote, val := sub[1], sub[2], sub[3]
		if !p.shouldRewrite(val) {
			return m
		}
		clean := "/" + strings.TrimPrefix(strings.TrimPrefix(val, "./"), "/")
		return attr + "=" + quote + p.Path + clean
	})

	s = manifestLinkRe.ReplaceAllStringFunc(s, func(m string) string {
		if strings.Contains(strings.ToLower(m), "crossorigin") {
			return m
		}
		return strings.TrimSuffix(m, ">") + ` crossorigin="use-credentials">`
	})
	return []byte(s)
}

// InjectHead 把片段插入 </head> 之前（无 head 时插到 </body> 前、再退化为前置）。
func InjectHead(body []byte, snippet string) []byte {
	if snippet == "" {
		return body
	}
	s := string(body)
	if idx := indexFold(s, "</head>"); idx >= 0 {
		return []byte(s[:idx] + snippet + s[idx:])
	}
	if idx := indexFold(s, "</body>"); idx >= 0 {
		return []byte(s[:idx] + snippet + s[idx:])
	}
	return []byte(snippet + s)
}

func indexFold(s, sub string) int {
	return strings.Index(strings.ToLower(s), strings.ToLower(sub))
}

// RewriteLocation 改写重定向 Location 的站内绝对路径。
func (p Prefix) RewriteLocation(loc string) string {
	if loc == "" {
		return loc
	}
	if !strings.HasPrefix(loc, "/") || strings.HasPrefix(loc, "//") {
		return loc
	}
	if loc == p.Path || strings.HasPrefix(loc, p.Path+"/") {
		return loc
	}
	return p.Path + loc
}

// RewriteCookie 把 Set-Cookie 的 Path=/ 收窄到子路径。
func (p Prefix) RewriteCookie(v string) string {
	return cookiePathRe.ReplaceAllString(v, "Path="+p.Path+"/$1")
}

// ---------------------------------------------------------------------------
// 反向代理
// ---------------------------------------------------------------------------

// accessJSONPaths 控制台「API 接入」页的数据源（见 upstream internal/server/router.go）。
// 只有这两个响应带 access 字段，也只需要改写它们。
var accessJSONPaths = map[string]struct{}{
	"/api/overview":         {},
	"/api/overview/summary": {},
}

// RewriteAccessURLs 把控制台 overview 响应里 access.* 的相对路径补成绝对地址。
//
// 为什么必须改写：上游的 access 字段全是相对路径（/v1、/v1/chat/completions…），
// 控制台用 absUrl() 以 location.origin 补全（frontend/src/lib/url.ts）。而面板挂在
// 飞牛桌面的域下 —— 例如 http://192.168.0.2:8666/app/cli2api —— 补出来的就是
// 「飞牛桌面端口 + /v1」，既不是本应用的下游端口，也过不了统一网关的登录态校验：
// 用户照着抄到客户端里必然连不通（飞牛 1.2.0604+ 还会把 Authorization 判成
// 非法票据）。这里直接把绝对地址喂给前端，absUrl() 对 http(s):// 开头的值原样返回。
//
// 只动 access 对象里以 "/" 开头的字符串：数字、模型数组、hint 文案一律原样保留 ——
// 用 json.Number 解码就是为了不让配额/用量这类大整数在往返中掉精度。
func RewriteAccessURLs(body []byte, base string) ([]byte, bool) {
	base = strings.TrimRight(strings.TrimSpace(base), "/")
	if base == "" {
		return nil, false
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	var doc map[string]any
	if err := dec.Decode(&doc); err != nil {
		return nil, false
	}
	access, ok := doc["access"].(map[string]any)
	if !ok {
		return nil, false
	}
	changed := false
	for key, val := range access {
		s, ok := val.(string)
		if !ok || !strings.HasPrefix(s, "/") {
			continue
		}
		access[key] = base + s
		changed = true
	}
	if !changed {
		return nil, false
	}
	out, err := json.Marshal(doc)
	if err != nil {
		return nil, false
	}
	return out, true
}

// Options 反代构造参数。
type Options struct {
	// Prefix 网关前缀；零值表示直通（下游独立端口）。
	Prefix Prefix
	// InternalAddr 内部服务地址，形如 127.0.0.1:3010。
	InternalAddr string
	// Timeout 建立到内部服务的连接与响应头超时。0 表示使用默认值。
	Timeout time.Duration
	// RewriteHTML 为 true 时改写真响应（HTML/Location/Cookie），
	// 并给 HTML 加 no-store、给指纹资源加长缓存。
	RewriteHTML bool
	// Bridge 返回注入 HTML 的脚本片段（RewriteHTML 为 true 时生效）。
	Bridge func() string
	// Prepare 在转发前改写出站请求（例如注入控制台密钥）。
	Prepare func(*http.Request)
	// AccessBase 返回控制台「API 接入」页应展示的下游基址（形如
	// http://192.168.0.2:3010）。为 nil 表示不改写。见 RewriteAccessURLs。
	AccessBase func(*http.Request) string
}

// NewProxy 构造到内部服务的反向代理。
func NewProxy(opt Options) (http.Handler, error) {
	if strings.TrimSpace(opt.InternalAddr) == "" {
		return nil, errors.New("gateway: 内部服务地址为空")
	}
	timeout := opt.Timeout
	if timeout <= 0 {
		timeout = 30 * time.Second
	}

	target := &url.URL{Scheme: "http", Host: opt.InternalAddr}
	rp := httputil.NewSingleHostReverseProxy(target)

	// 默认 ErrorHandler 返回 502 纯文本；这里给友好 HTML 并记录。
	rp.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		log.Printf("网关代理失败 %s %s: %v", r.Method, r.URL.Path, err)
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusBadGateway)
		_, _ = io.WriteString(w, `<!doctype html><meta charset="utf-8">`+
			`<body style="font-family:system-ui;padding:40px;background:#0f1115;color:#e6e6e6">`+
			`<h2>⚠️ CLI2API 服务不可达</h2>`+
			`<p>上游 cli2api 可能仍在启动中，请稍候刷新；若持续出现，请在飞牛应用中心重启本应用。</p>`+
			`<pre style="color:#888">`+htmlEscape(err.Error())+`</pre></body>`)
	}

	// DisableCompression：避免拿到 gzip 内容后无法做 HTML 改写。
	// 回源是明文 HTTP 回环，不需要 HTTP/2 协商。
	rp.Transport = &http.Transport{
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
		MaxIdleConns:          64,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
		DisableCompression:    true,
		ForceAttemptHTTP2:     false,
		ResponseHeaderTimeout: timeout,
	}

	orig := rp.Director
	rp.Director = func(req *http.Request) {
		orig(req)

		// 1) 剥除网关前缀（Path 与 RawPath 都要处理，否则编码路径会错乱）。
		if opt.Prefix.Path != "" {
			internal, _ := opt.Prefix.Strip(req.URL.Path)
			if !strings.HasPrefix(internal, "/") {
				internal = "/" + internal
			}
			req.URL.Path = internal
			if req.URL.RawPath != "" {
				rawInternal, _ := opt.Prefix.Strip(req.URL.RawPath)
				if !strings.HasPrefix(rawInternal, "/") {
					rawInternal = "/" + rawInternal
				}
				req.URL.RawPath = rawInternal
			}
		}

		// 2) 清理干扰内部服务或可被客户端伪造的头。
		req.Header.Del("Accept-Encoding")
		req.Header.Del("X-Forwarded-Host")
		req.Header.Del("X-Forwarded-Proto")
		req.Header.Set("X-Forwarded-Prefix", opt.Prefix.Path)
		// 本应用以飞牛登录态为唯一身份（见 gate.go），
		// 内部服务只认密钥，不读 X-Trim-*，无需为它们盖章。

		// 3) 出站改写（控制台密钥注入等）。
		if opt.Prepare != nil {
			opt.Prepare(req)
		}
	}

	rp.ModifyResponse = func(resp *http.Response) error {
		if !opt.RewriteHTML {
			tuneSSE(resp)
			return nil
		}

		// 3) 重定向 Location 改写。
		if loc := resp.Header.Get("Location"); loc != "" {
			resp.Header.Set("Location", opt.Prefix.RewriteLocation(loc))
		}
		// 4) Cookie 作用域改写。
		if cookies, ok := resp.Header["Set-Cookie"]; ok {
			for i, c := range cookies {
				cookies[i] = opt.Prefix.RewriteCookie(c)
			}
		}

		// 5) SSE 流式响应：关闭缓冲，否则事件流被攒住。
		if tuneSSE(resp) {
			return nil
		}

		ct := resp.Header.Get("Content-Type")
		switch {
		case strings.HasPrefix(ct, "text/html"):
			body, err := io.ReadAll(resp.Body)
			_ = resp.Body.Close()
			if err != nil {
				return err
			}
			body = opt.Prefix.RewriteHTML(body)
			if opt.Bridge != nil {
				body = InjectHead(body, opt.Bridge())
			}
			resp.Body = io.NopCloser(strings.NewReader(string(body)))
			resp.ContentLength = int64(len(body))
			resp.Header.Set("Content-Length", fmt.Sprint(len(body)))
			// 控制台页面每次都要重新注入脚本与 no-store，禁止中间层缓存。
			resp.Header.Set("Cache-Control", "no-store")
			// 改写会注入脚本；若上游带了 CSP，注入会被拦掉。
			resp.Header.Del("Content-Security-Policy")
			resp.Header.Del("Content-Security-Policy-Report-Only")
		case resp.StatusCode == http.StatusOK && IsAssetPath(resp.Request.URL.Path):
			// 带指纹的构建产物：内容与文件名绑定，可长期缓存。
			resp.Header.Set("Cache-Control", "public, max-age=31536000, immutable")
		}

		// 6) 控制台「API 接入」页的下游地址改写。
		if opt.AccessBase != nil && strings.HasPrefix(ct, "application/json") {
			if _, ok := accessJSONPaths[resp.Request.URL.Path]; ok {
				if base := opt.AccessBase(resp.Request); base != "" {
					body, err := io.ReadAll(resp.Body)
					_ = resp.Body.Close()
					if err != nil {
						return err
					}
					if rewritten, changed := RewriteAccessURLs(body, base); changed {
						body = rewritten
					}
					resp.Body = io.NopCloser(bytes.NewReader(body))
					resp.ContentLength = int64(len(body))
					resp.Header.Set("Content-Length", fmt.Sprint(len(body)))
				}
			}
		}
		return nil
	}

	return rp, nil
}

// tuneSSE 针对事件流关闭缓冲；返回 true 表示该响应按 SSE 处理。
func tuneSSE(resp *http.Response) bool {
	if !strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		return false
	}
	resp.Header.Set("Cache-Control", "no-cache, no-transform")
	resp.Header.Set("X-Accel-Buffering", "no")
	resp.Header.Del("Content-Length")
	return true
}

func htmlEscape(s string) string {
	r := strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;")
	return r.Replace(s)
}

// ---------------------------------------------------------------------------
// 监听
// ---------------------------------------------------------------------------

// ListenUnix 在指定路径上监听 Unix Socket（先清理残留），权限 0600。
func ListenUnix(path string) (net.Listener, error) {
	if _, err := os.Stat(path); err == nil {
		if rmErr := os.Remove(path); rmErr != nil {
			return nil, fmt.Errorf("清理旧 Socket %s 失败: %w", path, rmErr)
		}
	}
	ln, err := net.Listen("unix", path)
	if err != nil {
		return nil, fmt.Errorf("监听 Unix Socket %s 失败: %w", path, err)
	}
	// 网关以 root 身份连接，收紧到属主可读写。
	if err := os.Chmod(path, 0o600); err != nil {
		log.Printf("警告：设置 Socket 权限失败: %v", err)
	}
	return ln, nil
}
