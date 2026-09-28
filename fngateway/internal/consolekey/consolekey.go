// Package consolekey 读取 cli2api 控制台密钥（console key）。
//
// 为什么必须直接读 SQLite：cli2api 首次启动时随机生成控制台密钥并写入
// app_secrets 表（name = proxy_api_key，见 upstream internal/control/bootstrap.go）；
// GET /api/system/console-key 只返回前缀，完整值仅在 rotate 时下发一次。
// 网关要在浏览器侧免密进入控制台（飞牛登录态即身份），就必须自己从库里取。
//
// 只读打开是安全的：cli2api 未启用 WAL（store.OpenStore 只设 foreign_keys 与
// busy_timeout），不存在只读连接无法恢复 -wal 的问题；并发由 busy_timeout 兜住。
package consolekey

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite" // 纯 Go 驱动，fpk 交叉编译不需要 cgo
)

// secretName 控制台密钥在 app_secrets 中的行名（与上游 constant 一致）。
const secretName = "proxy_api_key"

// Reader 带 TTL 缓存的密钥读取器。
//
// 为什么要缓存：控制台每页会并发打多个 /api/* 请求，每个请求都开一次
// SQLite 连接既慢又无谓；密钥只在 rotate 时变化，秒级 TTL 足够。
type Reader struct {
	path string
	ttl  time.Duration

	mu       sync.Mutex
	key      string
	err      error
	loadedAt time.Time
}

// New 创建读取器；path 指向 cli2api 的 ${QODER_DATA_DIR}/qoder.db。
func New(path string) *Reader {
	return &Reader{path: strings.TrimSpace(path), ttl: 3 * time.Second}
}

// Key 返回当前控制台密钥。未就绪（库不存在或尚未生成）返回错误。
func (r *Reader) Key() (string, error) {
	if r == nil || r.path == "" {
		return "", errors.New("consolekey: 未配置数据库路径")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if time.Since(r.loadedAt) < r.ttl && (r.key != "" || r.err != nil) {
		return r.key, r.err
	}
	key, err := readKey(r.path)
	r.key, r.err, r.loadedAt = key, err, time.Now()
	return key, err
}

func readKey(path string) (string, error) {
	if _, statErr := os.Stat(path); statErr != nil {
		return "", fmt.Errorf("控制台密钥库不可读: %w", statErr)
	}
	dsn := "file:" + filepath.ToSlash(path) + "?mode=ro&_pragma=busy_timeout(5000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return "", fmt.Errorf("打开密钥库失败: %w", err)
	}
	defer func() { _ = db.Close() }()
	db.SetMaxOpenConns(1)

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	var value string
	err = db.QueryRowContext(ctx, `SELECT value FROM app_secrets WHERE name = ?`, secretName).Scan(&value)
	if errors.Is(err, sql.ErrNoRows) {
		return "", errors.New("控制台密钥尚未生成（cli2api 首次启动后可用）")
	}
	if err != nil {
		return "", fmt.Errorf("查询控制台密钥失败: %w", err)
	}
	value = strings.TrimSpace(value)
	if value == "" {
		return "", errors.New("控制台密钥为空")
	}
	return value, nil
}
