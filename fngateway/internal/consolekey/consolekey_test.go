package consolekey

import (
	"database/sql"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// TestReaderReadsAppSecrets 验证只读 DSN 与 app_secrets 查询在真实 SQLite 上可用。
// 表结构照抄上游 internal/store 的迁移结果（name/value/created_at/updated_at）。
func TestReaderReadsAppSecrets(t *testing.T) {
	path := filepath.Join(t.TempDir(), "qoder.db")

	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	if _, err := db.Exec(`CREATE TABLE app_secrets (
		name TEXT PRIMARY KEY,
		value TEXT NOT NULL,
		created_at TEXT NOT NULL DEFAULT '',
		updated_at TEXT NOT NULL DEFAULT ''
	)`); err != nil {
		t.Fatalf("create table: %v", err)
	}
	if _, err := db.Exec(`INSERT INTO app_secrets (name, value) VALUES (?, ?)`, "proxy_api_key", "sk-console-abc"); err != nil {
		t.Fatalf("insert: %v", err)
	}
	if err := db.Close(); err != nil {
		t.Fatalf("close: %v", err)
	}

	r := New(path)
	got, err := r.Key()
	if err != nil {
		t.Fatalf("Key: %v", err)
	}
	if got != "sk-console-abc" {
		t.Fatalf("Key = %q, want %q", got, "sk-console-abc")
	}

	// 缓存命中不应重新读盘：删除库文件后仍返回缓存值。
	if err := os.Remove(path); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if got, err := r.Key(); err != nil || got != "sk-console-abc" {
		t.Fatalf("cached Key = %q, %v; want cached value", got, err)
	}

	// TTL 过期后必须报错（库已删除），不能返回陈旧值。
	//
	// 只把 ttl 设成 1ns 是不行的：Windows 上 Go 的单调时钟粒度远大于 1ns，
	// 两次调用之间 time.Since 会返回 0，于是 0 < 1ns 成立、走了缓存分支，
	// 测试在 Windows 上必然误报（Linux 上恰好通过）。把 loadedAt 拨到过去，
	// 显式表达"缓存已过期"，与平台时钟精度无关。
	r.ttl = time.Nanosecond
	r.loadedAt = time.Now().Add(-time.Second)
	if _, err := r.Key(); err == nil {
		t.Fatal("expected error after cache expiry with missing db, got nil")
	}
}
