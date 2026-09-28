#!/usr/bin/env python3
"""build.py — fnos-cli2api 打包脚本（跨平台）

上游 caigee-cmd/cli2api（Go 控制面 + 内置 React 控制台 + Node worker，MIT）按
固定 tag 拉取后**原样编译**；本仓库只提供飞牛适配层（fngateway / cmd / config /
wizard / manifest / 图标），不改上游源码。

产物: cli2api-<manifest 版本>-<arch>.fpk（每架构一个包，装哪台给哪个）

与 D:\\fnos 下成熟项目一致的工程约定:
  * 中间产物全部落 .local-build/（已 gitignore）: fnpack / npm 缓存 / 上游源码 / 编译缓存
  * fnpack 自动下载（官方 static2.fnnas.com，1.2.3）
  * Windows 上 fnpack 产出 0666 → 打包后重建 tar，重写可执行位
  * fnpack 只打包它 schema 认识的根文件 → 补回 LICENSE.upstream / UPSTREAM.txt
  * manifest 取值中的 ASCII 分号会被 fnpack 静默截断 → 构建期硬失败

本脚本额外做四类「编译能过、真机才炸」的构建期校验（verify_* / check_*）:
  1. 控制台密钥名 cli2api_key 必须存在于打进包的控制台产物里（fngateway 免密登录依赖它）
  2. worker 与 Qoder CLI 的兼容针（worker/src/compat.mjs 的 NEEDLES）必须命中随包 CLI bundle
  3. 网关前缀 / socket 名 / 下游端口在 fngateway、app/ui/config、cmd/main、manifest 四处一致
  4. 包内二进制与原生依赖的架构、可执行位、版本号逐项核对

用法:
    python build.py                    # 全量: 拉上游 + 编译 + 打包（amd64 + arm64 各一个包）
    python build.py --arch arm64       # 只出 arm64 包
    python build.py --skip-upstream    # 复用已下载的上游源码
    python build.py --package-only     # 跳过编译，用 .local-build/bin 现成产物重新打包
    python build.py --no-sharp         # 不随包 sharp/libvips 原生库（省约 17MB/架构，图片能力不可用）
    python build.py --force            # 强制重新下载上游源码与 npm 包
"""
import argparse
import datetime
import gzip
import hashlib
import io
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BUILD_DIR = os.path.join(PROJECT_DIR, ".local-build")
STAGE_DIR = os.path.join(BUILD_DIR, "stage")
SRC_DIR = os.path.join(BUILD_DIR, "upstream-src")
BIN_CACHE = os.path.join(BUILD_DIR, "bin")
NPM_DL_DIR = os.path.join(BUILD_DIR, "npm-dl")
WORKER_MODULES_CACHE = os.path.join(BUILD_DIR, "worker-modules")
CACHE_FILE = os.path.join(BUILD_DIR, "versions.json")

APP_NAME = "cli2api"

# --- 上游（版本与 manifest 的 0.6.11-N 对应，只用 tag，不用 master）---
UPSTREAM_REPO = "caigee-cmd/cli2api"
UPSTREAM_TAG = "v0.6.11"

# --- 与飞牛侧的契约（build.py 里集中一份，用 check_consistency() 与各文件比对）---
GATEWAY_PREFIX = "/app/cli2api"
SOCKET_NAME = "cli2api.sock"
# 下游端口取上游 cli2api 的默认端口（internal/config/config.go: PORT 默认 3010），
# 与上游 deploy/docker-compose.yml 暴露的端口一致，客户端可照抄上游文档。
SERVICE_PORT = 3010
NODE_RUNTIME_APP = "nodejs_v24"
NODE_RUNTIME_DIR = "/var/apps/nodejs_v24/target/bin"

# --- 随包运行时组件 ---
# CLI 版本以 worker/src/compat.mjs 的 PINNED_QODERCLI_VERSION 为准（构建期读取并核对）。
# sharp 版本对齐 CLI bundle 内联的 sharp 清单（构建期核对，漂移即失败）。
SHARP_VERSION = "0.34.5"
SHARP_LIBVIPS_VERSION = "1.2.4"

MAIN_PROXY = "https://gh-proxy.com/"
FALLBACK_PROXY = "https://ghfast.top/"


def log(msg):
    """输出一行日志。

    Windows 控制台默认 GBK，直接 write 含 emoji 的字符串会抛 UnicodeEncodeError
    而中断构建（构建其实已经成功，只是打不出这一行）。替换掉不可编码字符。
    """
    try:
        sys.stdout.write(msg + "\n")
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        sys.stdout.write(msg.encode(enc, "replace").decode(enc, "replace") + "\n")
    sys.stdout.flush()


def _resolve_exe(cmd):
    """Windows 上把裸命令名解析成可执行文件（npm/npx 是 .cmd，CreateProcess 不认）。"""
    if os.name != "nt" or not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    if resolved:
        return [resolved] + list(cmd[1:])
    return cmd


def run(cmd, cwd=None, env=None, check=True):
    cmd = _resolve_exe(cmd)
    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True)
    if check and proc.returncode != 0:
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        log(f"  ERROR: 命令失败 (exit {proc.returncode}): {' '.join(cmd)}")
        log("  " + (err or out)[:2000])
        sys.exit(1)
    return proc


def load_cache():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_cache(key, value):
    os.makedirs(BUILD_DIR, exist_ok=True)
    c = load_cache()
    c[key] = value
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(c, f, ensure_ascii=False, indent=2)


def download(url, out_file, desc, use_proxy=True, force=False):
    """带缓存的下载；直连失败时走 GitHub 镜像。"""
    if not force and os.path.exists(out_file) and os.path.getsize(out_file) > 0:
        log(f"  复用已下载的 {desc}")
        return True

    log(f"  下载 {desc} ...")
    urls = [MAIN_PROXY + url, FALLBACK_PROXY + url, url] if use_proxy else [url]

    last_err = ""
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "build.py"})
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
            if data:
                os.makedirs(os.path.dirname(out_file), exist_ok=True)
                with open(out_file, "wb") as f:
                    f.write(data)
                log(f"  完成: {desc} ({len(data) / 1024 / 1024:.2f} MB)")
                return True
            last_err = "下载内容为空"
        except Exception as e:
            last_err = str(e)
    log(f"  ERROR: 下载失败 {desc}: {last_err}")
    return False


def read_manifest_version():
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        for line in f:
            if line.strip().startswith("version"):
                return line.split("=", 1)[1].strip()
    return ""


# fnpack 的 manifest 解析器把 ASCII 分号 `;` 当成取值终止符：值里第一个 `;`
# 之后的内容被静默丢弃（实测 fnpack 1.2.3，见 D:\\fnos 下项目的真实事故：
# changelog 里写了 `&lt;标识&gt;`，2672 字符只剩 1092 字符进包）。
# 危害是完全静默，因此这里做成构建期硬失败。
MANIFEST_VALUE_TERMINATORS = (";",)


def manifest_values(text):
    out = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def check_manifest_values(text):
    problems = []
    for key, val in manifest_values(text).items():
        for term in MANIFEST_VALUE_TERMINATORS:
            i = val.find(term)
            if i >= 0:
                problems.append(
                    f"{key}: 取值里第 {i} 个字符是 {term!r}，fnpack 会在此处截断 —— "
                    f"包内只会保留前 {i} 个字符（当前共 {len(val)} 个）。"
                    f"请改写该值（例如去掉 HTML 实体 &lt; &gt; &amp;）"
                )
    return problems


# ---------------------------------------------------------------------------
# 上游源码
# ---------------------------------------------------------------------------

def fetch_upstream(force=False):
    """按 tag 拉取上游源码（zip 归档，不依赖 git 命令）。"""
    stamp = os.path.join(SRC_DIR, ".upstream_ref")
    if not force and os.path.isdir(SRC_DIR) and os.path.exists(stamp):
        with open(stamp) as f:
            have = f.read().strip()
        if have == UPSTREAM_TAG:
            log(f"  上游源码已是 {UPSTREAM_TAG}，跳过下载")
            return UPSTREAM_TAG

    zip_path = os.path.join(BUILD_DIR, f"upstream-{UPSTREAM_TAG}.zip")
    url = f"https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_TAG}.zip"
    if not download(url, zip_path, f"上游源码 {UPSTREAM_TAG}", force=force):
        sys.exit(1)

    log("  解压上游源码 ...")
    extract_to = os.path.join(BUILD_DIR, "upstream-extract")
    shutil.rmtree(extract_to, ignore_errors=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(extract_to)

    entries = [d for d in os.listdir(extract_to)
               if os.path.isdir(os.path.join(extract_to, d))]
    if len(entries) != 1:
        log(f"  ERROR: 归档结构异常，顶层目录: {entries}")
        sys.exit(1)

    shutil.rmtree(SRC_DIR, ignore_errors=True)
    shutil.move(os.path.join(extract_to, entries[0]), SRC_DIR)
    shutil.rmtree(extract_to, ignore_errors=True)

    with open(stamp, "w") as f:
        f.write(UPSTREAM_TAG)
    save_cache("upstream_ref", UPSTREAM_TAG)
    return UPSTREAM_TAG


def check_upstream_tree():
    """编译前确认上游归档里带着我们要用的东西。

    两个关键点都不是「编译会报错」能兜住的:
      * internal/webui/static/ 是**入库**的控制台产物（go:embed all:static）。
        它是构建输入，不是构建输出 —— 上游若改成不入库，这里必须立刻发现，
        否则会打出一个没有控制台的包（或者 embed 直接编译失败）。
      * worker/src/compat.mjs 里写着与 CLI 版本绑定的兼容针与 PINNED 版本号，
        随包 CLI 必须命中，否则真机上 Qoder 账号起不来。
    """
    need = [
        "cmd/server/main.go",
        "worker/src/daemon.mjs",
        "worker/src/compat.mjs",
        "worker/src/rewrite-loader.mjs",
        "worker/package.json",
        "worker/last-plain.sample.json",
        "worker/NOTICE",
        "internal/webui/static/index.html",
        "LICENSE",
    ]
    missing = [p for p in need if not os.path.exists(os.path.join(SRC_DIR, *p.split("/")))]
    if missing:
        log("  ERROR: 上游源码缺少关键文件: " + ", ".join(missing))
        sys.exit(1)

    assets = os.path.join(SRC_DIR, "internal", "webui", "static", "assets")
    js = [f for f in os.listdir(assets) if f.endswith(".js")] if os.path.isdir(assets) else []
    if not js:
        log("  ERROR: 控制台产物 internal/webui/static/assets/*.js 不存在")
        sys.exit(1)
    # fngateway 用 localStorage['cli2api_key'] 放占位值、由服务端注入真密钥。
    # 上游若改名，免密登录会静默失效（面板卡在登录页），必须在构建期拦住。
    def has_key(name):
        with open(os.path.join(assets, name), encoding="utf-8", errors="ignore") as fh:
            return "cli2api_key" in fh.read()

    if not any(has_key(f) for f in js):
        log("  ERROR: 控制台产物里找不到 localStorage 键 cli2api_key —— "
            "fngateway 的 SeedScript/免密登录依赖它，上游可能改了键名")
        sys.exit(1)
    log("  上游源码检查通过（控制台产物入库 + cli2api_key 存在）")


# 控制台产物里 createBrowserLocation 的定位模式（压缩产物，函数名会变，结构稳定）。
# 这是 react-router 唯一读 window.location 做路由匹配的地方。
CONSOLE_LOCATION_RE = re.compile(
    r"\{pathname:(\w+),search:\w+,hash:\w+\}=\w+\|\|\w+\.location;")
CONSOLE_PATCH_MARK = "__fnGatewayBase"


def patch_console_bundle():
    """给入库的控制台产物打子路径适配补丁（幂等；定位不到就停下报错）。

    为什么必须改产物、运行时桥接救不了:
      react-router v7 的 history.location 是**实时 getter**，每次路由判断都重新读
      window.location；而 Chrome 里 Location 的属性是 [LegacyUnforgeable]
      （Location.prototype 上没有 pathname，实例上的不可配置），运行时脚本改不了
      它读到的值。网关为了刷新/深链必须把 /app/cli2api 写进 URL 栏，于是路由读到
      带前缀的路径、匹配不到任何 route，最终落在 <Route path="*"> 的兜底
      <Navigate> 上 —— 表现就是「点侧边栏没反应、内容区空白」。
      唯一可行的落点是让 App 自己的路由带 basename，而 basename 只能在
      createBrowserLocation 里注入。

    注入的剥离逻辑与 fngateway 的 already() 同构：优先取 window.__fnGatewayBase
    （桥接脚本注入，devcheck 换前缀时有效），否则用随包固定的 GATEWAY_PREFIX。
    """
    assets = os.path.join(SRC_DIR, "internal", "webui", "static", "assets")
    hits = []
    for name in sorted(os.listdir(assets)):
        if not name.endswith(".js"):
            continue
        path = os.path.join(assets, name)
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as f:
            text = f.read()
        n = len(CONSOLE_LOCATION_RE.findall(text))
        if n:
            hits.append((path, name, text, n))

    if len(hits) != 1 or hits[0][3] != 1:
        log(f"  ERROR: 控制台产物里 createBrowserLocation 定位模式命中 "
            f"{len(hits)} 个文件（各 {[h[3] for h in hits]} 处），期望 1 个文件 1 处。"
            "上游产物结构已变，请重新核对 patch_console_bundle()")
        sys.exit(1)

    path, name, text, _ = hits[0]
    if CONSOLE_PATCH_MARK in text:
        log(f"  控制台产物已打过子路径补丁（{name}）")
        return

    def inject(m):
        var = m.group(1)
        return m.group(0) + (
            '%s=(function(p){var b=window.__fnGatewayBase||"%s";'
            'if(p===b)return"/";if(p.indexOf(b+"/")===0)return p.slice(b.length)||"/";'
            'return p})(%s);' % (var, GATEWAY_PREFIX, var))

    patched = CONSOLE_LOCATION_RE.sub(inject, text, count=1)
    if CONSOLE_PATCH_MARK not in patched or len(patched) <= len(text):
        log(f"  ERROR: 控制台产物补丁注入失败（{name}）")
        sys.exit(1)
    # newline="" 双端都带：Windows 上默认换行转换会改写整个产物字节。
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(patched)
    log(f"  控制台产物子路径补丁: {name} ({len(text)} → {len(patched)} 字节)")


def pinned_qodercli_version():
    """从 worker/src/compat.mjs 读取钉住的 CLI 版本（唯一真源）。"""
    path = os.path.join(SRC_DIR, "worker", "src", "compat.mjs")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r'PINNED_QODERCLI_VERSION\s*=\s*"([^"]+)"', text)
    if not m:
        log("  ERROR: 无法从 worker/src/compat.mjs 解析 PINNED_QODERCLI_VERSION")
        sys.exit(1)
    return m.group(1)


def compat_needles():
    """读取 compat.mjs 的 NEEDLES 表（worker 在真机上用它判断 CLI 是否兼容）。"""
    path = os.path.join(SRC_DIR, "worker", "src", "compat.mjs")
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"export const NEEDLES = \{(.*?)\n\};", text, re.S)
    if not m:
        log("  ERROR: 无法从 worker/src/compat.mjs 解析 NEEDLES")
        sys.exit(1)
    return dict(re.findall(r'(\w+):\s*"([^"]+)"', m.group(1)))


# ---------------------------------------------------------------------------
# 图标
# ---------------------------------------------------------------------------

# 图标随仓库提交，构建期不下载也不重采样（设计资产、许可清晰、不依赖外部图源），
# 只校验齐备与尺寸 —— 缺失只会让桌面图标空白，属于「装完才发现」的静默缺陷。
ICON_TARGETS = (
    ("ICON.PNG", 64, 64),
    ("ICON_256.PNG", 256, 256),
    ("app/ui/images/icon_64.png", 64, 64),
    ("app/ui/images/icon_256.png", 256, 256),
)


def _png_size(path):
    with open(path, "rb") as f:
        head = f.read(26)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    if head[12:16] != b"IHDR":
        raise ValueError("缺少 IHDR")
    return struct.unpack(">II", head[16:24])


def verify_icons():
    for rel, w, h in ICON_TARGETS:
        p = os.path.join(PROJECT_DIR, *rel.split("/"))
        if not os.path.exists(p):
            log(f"  ERROR: 缺少图标 {rel}（图标随仓库提交，请补齐后再构建）")
            sys.exit(1)
        try:
            gw, gh = _png_size(p)
        except Exception as e:
            log(f"  ERROR: 图标 {rel} 无法解析: {e}")
            sys.exit(1)
        if (gw, gh) != (w, h):
            log(f"  ERROR: 图标 {rel} 尺寸应为 {w}x{h}，实际 {gw}x{gh}")
            sys.exit(1)
        log(f"  {rel}: {gw}x{gh}")


# ---------------------------------------------------------------------------
# 一致性校验（四处契约同一份值）
# ---------------------------------------------------------------------------

def check_consistency():
    """比对 fngateway / app/ui/config / cmd/main / manifest 四处契约。

    这些值任意一处写错都不会编译失败，只会表现为「桌面图标点不开」「socket
    找不到」「下游端口不通」，是这类网关应用最常见的低级事故。
    """
    problems = []
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        mf = manifest_values(f.read())
    with open(os.path.join(PROJECT_DIR, "cmd", "main"), "r", encoding="utf-8") as f:
        cmd_main = f.read()
    with open(os.path.join(PROJECT_DIR, "fngateway", "main.go"), "r", encoding="utf-8") as f:
        gw = f.read()
    with open(os.path.join(PROJECT_DIR, "app", "ui", "config"), "r", encoding="utf-8") as f:
        entry_cfg = json.load(f)
    with open(os.path.join(PROJECT_DIR, "config", "privilege"), "r", encoding="utf-8") as f:
        privilege = json.load(f)

    def gw_str(name):
        m = re.search(rf'{name}\s*=\s*"([^"]+)"', gw)
        return m.group(1) if m else None

    def gw_int(name):
        m = re.search(rf'{name}\s*=\s*(\d+)', gw)
        return int(m.group(1)) if m else None

    # 1) appname / 运行用户
    appname = mf.get("appname", "")
    if privilege.get("username") != appname or privilege.get("groupname") != appname:
        problems.append("config/privilege 的 username/groupname 必须等于 manifest.appname")
    m = re.search(r'^APP_NAME="([^"]+)"', cmd_main, re.M)
    if not m or m.group(1) != appname:
        problems.append("cmd/main 的 APP_NAME 与 manifest.appname 不一致")

    # 2) 网关前缀 / socket / 下游端口
    if gw_str("gatewayPrefix") != GATEWAY_PREFIX:
        problems.append(f"fngateway 的 gatewayPrefix 应为 {GATEWAY_PREFIX}")
    if gw_int("defaultPort") != SERVICE_PORT:
        problems.append(f"fngateway 的 defaultPort 应为 {SERVICE_PORT}")
    if mf.get("service_port") != str(SERVICE_PORT):
        problems.append(f"manifest.service_port 应为 {SERVICE_PORT}")
    if mf.get("checkport") != "true":
        problems.append("manifest.checkport 应为 true（端口冲突时应用中心能提示）")
    if not re.search(r'filepath\.Join\(appDest,\s*"%s"\)' % re.escape(SOCKET_NAME), gw):
        problems.append(f"fngateway 未监听 {SOCKET_NAME}")
    if '${TRIM_APPDEST}/${APP_NAME}.sock' not in cmd_main:
        problems.append("cmd/main 的 socket 路径必须写成 ${TRIM_APPDEST}/${APP_NAME}.sock")
    if gw_int("workerBasePort") == SERVICE_PORT:
        problems.append("workerBasePort 与下游端口冲突")

    # 3) 桌面入口
    uidir = mf.get("desktop_uidir", "")
    if uidir != "ui":
        problems.append("manifest.desktop_uidir 应为 ui（与 stage/app/ui 对应）")
    launch = mf.get("desktop_applaunchname", "")
    urls = entry_cfg.get(".url") or {}
    if launch not in urls:
        problems.append(f"manifest.desktop_applaunchname={launch!r} 在 app/ui/config 里没有对应入口")
    for entry_id, entry in urls.items():
        if entry.get("gatewayPrefix") != GATEWAY_PREFIX:
            problems.append(f"入口 {entry_id} 的 gatewayPrefix 应为 {GATEWAY_PREFIX}")
        if entry.get("gatewaySocket") != SOCKET_NAME:
            problems.append(f"入口 {entry_id} 的 gatewaySocket 应为 {SOCKET_NAME}")
        if entry.get("url") != GATEWAY_PREFIX:
            problems.append(f"入口 {entry_id} 的 url 应为 {GATEWAY_PREFIX}")
        if entry.get("allUsers") is not False:
            problems.append(f"入口 {entry_id} 必须 allUsers=false（控制台只对管理员开放）")
        icon = entry.get("icon", "")
        if "{0}" not in icon:
            problems.append(f"入口 {entry_id} 的 icon 应含 {{0}} 占位（图标按尺寸选择）")

    # 4) Node 运行时依赖
    dep_apps = [d.strip() for d in mf.get("install_dep_apps", "").split(",") if d.strip()]
    if NODE_RUNTIME_APP not in dep_apps:
        problems.append(f"manifest.install_dep_apps 必须声明 {NODE_RUNTIME_APP}")
    if gw_str("nodeRuntimeDir") != NODE_RUNTIME_DIR:
        problems.append(f"fngateway 的 nodeRuntimeDir 应为 {NODE_RUNTIME_DIR}")

    # 5) 端口读取来源（不要硬编码 SERVICE_PORT 到脚本里）
    if "TRIM_SERVICE_PORT" not in gw:
        problems.append("fngateway 必须优先读取 TRIM_SERVICE_PORT")

    if problems:
        log("  ERROR: 契约不一致，已中止：")
        for p in problems:
            log(f"    - {p}")
        sys.exit(1)
    log("  契约一致性通过（前缀/socket/端口/入口/依赖/运行用户）")


# ---------------------------------------------------------------------------
# 编译
# ---------------------------------------------------------------------------

def go_env(arch):
    env = dict(os.environ)
    env["CGO_ENABLED"] = "0"
    env["GOOS"] = "linux"
    env["GOARCH"] = arch
    # 缓存放 .local-build 内，避免受限环境写不了默认 GOCACHE
    env["GOCACHE"] = os.path.join(BUILD_DIR, ".gocache")
    env["GOTMPDIR"] = os.path.join(BUILD_DIR, ".gotmp")
    os.makedirs(env["GOCACHE"], exist_ok=True)
    os.makedirs(env["GOTMPDIR"], exist_ok=True)
    return env


def build_binaries(arch):
    """编译指定架构的两个二进制到 .local-build/bin/。"""
    os.makedirs(BIN_CACHE, exist_ok=True)
    env = go_env(arch)

    log(f"  编译上游 cli2api ({arch}) ...")
    out = os.path.join(BIN_CACHE, f"cli2api-linux-{arch}")
    # 版本号用上游 tag（0.6.11）而不是 fnOS 包版本（0.6.11-1）: 上游控制台的
    # 更新检查会拿它跟 GitHub release 比，带 -1 后缀可能被判成「更新」而误报。
    run(["go", "build", "-trimpath",
         "-ldflags",
         f"-s -w -X github.com/caigee-cmd/cli2api/internal/buildinfo.Version={UPSTREAM_TAG.lstrip('v')} "
         f"-X github.com/caigee-cmd/cli2api/internal/buildinfo.Commit={UPSTREAM_TAG}",
         "-o", out, "./cmd/server"], cwd=SRC_DIR, env=env)

    log(f"  编译 fngateway 适配层 ({arch}) ...")
    gw = os.path.join(PROJECT_DIR, "fngateway")
    out2 = os.path.join(BIN_CACHE, f"fngateway-linux-{arch}")
    run(["go", "build", "-trimpath", "-ldflags", f"-s -w -X main.version={UPSTREAM_TAG.lstrip('v')}",
         "-o", out2, "."], cwd=gw, env=env)


# ---------------------------------------------------------------------------
# 随包 Node 运行时（worker + Qoder CLI + 原生依赖）
# ---------------------------------------------------------------------------

def npm_arch(arch):
    """fnOS 架构名 → npm 架构名。"""
    return "x64" if arch == "amd64" else "arm64"


def tarball_name(spec):
    """@scope/name@ver → scope-name-ver.tgz（npm pack 的命名规则）。"""
    m = re.match(r"^(@[^/]+/[^@]+)@(.+)$", spec) or re.match(r"^([^@]+)@(.+)$", spec)
    if not m:
        log(f"  ERROR: 无法解析 npm 包标识 {spec!r}")
        sys.exit(1)
    return f"{m.group(1).lstrip('@').replace('/', '-')}-{m.group(2)}.tgz"


def npm_pack(spec, force=False):
    """下载 npm 包 tarball（缓存复用），返回本地路径。"""
    os.makedirs(NPM_DL_DIR, exist_ok=True)
    tgz = os.path.join(NPM_DL_DIR, tarball_name(spec))
    if not force and os.path.exists(tgz) and os.path.getsize(tgz) > 0:
        log(f"  复用已下载的 {tarball_name(spec)}")
        return tgz
    log(f"  下载 {spec} ...")
    if os.path.exists(tgz):
        os.remove(tgz)
    run(["npm", "pack", spec, "--pack-destination", NPM_DL_DIR, "--silent"],
        cwd=BUILD_DIR)
    if not os.path.exists(tgz):
        log(f"  ERROR: npm pack 未产出 {tarball_name(spec)}")
        sys.exit(1)
    log(f"  完成: {spec} ({os.path.getsize(tgz) / 1024 / 1024:.2f} MB)")
    return tgz


def extract_npm_pkg(tgz, dest_dir):
    """把 npm tarball 解到 dest_dir（去掉包内 `package/` 前缀，保留文件模式）。"""
    os.makedirs(dest_dir, exist_ok=True)
    with tarfile.open(tgz, "r:gz") as t:
        members = []
        for m in t.getmembers():
            name = m.name
            if name == "package":
                continue
            if not name.startswith("package/"):
                continue
            m.name = name[len("package/"):]
            members.append(m)
        try:
            t.extractall(dest_dir, members=members, filter="tar")
        except TypeError:
            # Python < 3.12 没有 filter 参数
            t.extractall(dest_dir, members=members)


def worker_dependency_specs():
    """读上游 worker/package.json 的运行时依赖（钉住版本号），返回 name@version 列表。"""
    with open(os.path.join(SRC_DIR, "worker", "package.json"), encoding="utf-8") as f:
        pkg = json.load(f)
    specs = []
    for name, ver in (pkg.get("dependencies") or {}).items():
        specs.append(f"{name}@{ver.lstrip('^~=')}")
    if not specs:
        log("  ERROR: 上游 worker/package.json 没有运行时依赖，构建假设已失效")
        sys.exit(1)
    return specs


def stage_worker_deps(specs):
    """把 worker 的 Node 运行时依赖装进缓存目录（undici，纯 JS，架构无关）。

    用 `npm install <name>@<pin>` 而不是 `npm ci`：上游 package-lock.json 里的
    resolved 指向 registry.npmmirror.com，与本机配置的 registry 不一致时，
    npm 11+ 会以 EALLOWREMOTE 直接拒绝（"Fetching packages of type remote have
    been disabled"）。依赖本身是精确钉版本的、且无传递依赖，按名字安装等价且稳定。
    """
    key = hashlib.sha256("|".join(specs).encode()).hexdigest()
    stamp = os.path.join(WORKER_MODULES_CACHE, ".deps-key")
    nm = os.path.join(WORKER_MODULES_CACHE, "node_modules")
    if os.path.isdir(nm) and os.path.exists(stamp):
        with open(stamp) as f:
            if f.read().strip() == key:
                log("  复用 worker 依赖缓存")
                return
    log(f"  npm install worker 依赖 ({', '.join(specs)}) ...")
    shutil.rmtree(WORKER_MODULES_CACHE, ignore_errors=True)
    os.makedirs(WORKER_MODULES_CACHE, exist_ok=True)
    run(["npm", "install", "--omit=dev", "--no-audit", "--no-fund",
         "--no-save", "--package-lock=false"] + specs, cwd=WORKER_MODULES_CACHE)
    for spec in specs:
        name = spec.rsplit("@", 1)[0]
        pj = os.path.join(nm, *name.split("/"), "package.json")
        if not os.path.exists(pj):
            log(f"  ERROR: worker 依赖未装上: {name}")
            sys.exit(1)
        with open(pj, encoding="utf-8") as f:
            got = json.load(f)
        log(f"  + {got.get('name')}@{got.get('version')}")
    with open(stamp, "w") as f:
        f.write(key)


def stage_worker_node_modules(arch, cli_version, with_sharp):
    """把 worker 的 Node 依赖装进 stage（worker 依赖 + 双区 CLI + 原生依赖）。

    布局与官方 Docker 镜像等价（npm 平铺语义），使 bundle 内的 require 能解析:
        ${TRIM_APPDEST}/worker/node_modules/@qoder-ai/qodercli/bundle/qodercli.js
        ${TRIM_APPDEST}/worker/node_modules/@qoder-ai/qodercli-ripgrep-linux-<arch>/bin/rg
        ${TRIM_APPDEST}/worker/node_modules/undici/

    为什么用 `npm pack` 手工摊开而不是 `npm install`:
      npm 会按**构建机**平台过滤 optionalDependencies —— 在 Windows 上装不出
      linux-x64 / linux-arm64 的 ripgrep 与 sharp 原生库，交叉打包必然失败。
    """
    worker_dir = os.path.join(STAGE_DIR, "app", "worker")
    nm = os.path.join(worker_dir, "node_modules")
    na = npm_arch(arch)

    stage_worker_deps(worker_dependency_specs())
    shutil.copytree(os.path.join(WORKER_MODULES_CACHE, "node_modules"), nm, dirs_exist_ok=True)

    pkgs = [
        f"@qoder-ai/qodercli@{cli_version}",
        f"@qodercn-ai/qoderclicn@{cli_version}",
        # ripgrep 是 Glob/Grep 工具的实现（上游 postinstall 也会检查它）。
        f"@qoder-ai/qodercli-ripgrep-linux-{na}@{cli_version}",
        f"@qodercn-ai/qoderclicn-ripgrep-linux-{na}@{cli_version}",
    ]
    if with_sharp:
        # sharp 的 JS 已内联进 CLI bundle，运行时只需要平台原生包；
        # bundle 里内联的清单写死了版本，构建期核对（见 verify_cli_bundle）。
        pkgs += [
            f"@img/sharp-linux-{na}@{SHARP_VERSION}",
            f"@img/sharp-libvips-linux-{na}@{SHARP_LIBVIPS_VERSION}",
        ]
    # 不随包 @crosscopy/clipboard: CLI bundle 里唯一的使用点是
    # `if (process.platform !== "darwin") return null;` 的剪贴板读取分支，
    # Linux 上永不加载（原生包也只有 darwin/win32 变体）。

    for spec in pkgs:
        tgz = npm_pack(spec)
        m = re.match(r"^(@[^/]+/[^@]+)@", spec)
        rel = m.group(1).split("/")           # @qoder-ai/qodercli → ["@qoder-ai","qodercli"]
        dest = os.path.join(nm, *rel)
        shutil.rmtree(dest, ignore_errors=True)
        extract_npm_pkg(tgz, dest)
        with open(os.path.join(dest, "package.json"), encoding="utf-8") as fh:
            ver = json.load(fh)
        if ver.get("name") != m.group(1):
            log(f"  ERROR: {spec} 解出的包名是 {ver.get('name')!r}")
            sys.exit(1)
        log(f"  + {m.group(1)}@{ver.get('version')}")


def verify_cli_bundle(arch, with_sharp):
    """核对随包 CLI bundle 与 worker 的兼容针/版本（真机起不来都源于此）。"""
    na = npm_arch(arch)
    needles = compat_needles()
    for site, pkg in (("global", "@qoder-ai/qodercli"), ("cn", "@qodercn-ai/qoderclicn")):
        bundle = os.path.join(STAGE_DIR, "app", "worker", "node_modules",
                              pkg, "bundle", "qodercli.js" if site == "global" else "qoderclicn.js")
        if not os.path.exists(bundle):
            log(f"  ERROR: 缺少 {site} 区 CLI bundle: {bundle}")
            sys.exit(1)
        with open(bundle, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        # worker 的 inspectQodercliSource 要求这 5 个针全部命中（skipMain 可选）。
        must = ("prepareInfer", "createWasm", "modelCatalog", "quotaApi", "checkinAuth")
        missing = [k for k in must if needles.get(k, "\0") not in source]
        if missing:
            log(f"  ERROR: {pkg} 的 bundle 未命中兼容针 {missing}；"
                f"worker/src/compat.mjs 与随包 CLI 版本不匹配（钉住 {pinned_qodercli_version()}）")
            sys.exit(1)
        if with_sharp:
            # CLI bundle 内联了 sharp 的整份 JS 与它的 package.json 描述符，
            # 原生包版本必须与内联版本一致（否则 sharp 加载 .node 时 ABI 不匹配）。
            m = re.search(r'name:"sharp".{0,300}?version:"([^"]+)"', source, re.S)
            if not m:
                log(f"  ERROR: {pkg} 里找不到内联的 sharp 版本描述符")
                sys.exit(1)
            if m.group(1) != SHARP_VERSION:
                log(f"  ERROR: CLI bundle 内联的 sharp 版本是 {m.group(1)}，"
                    f"build.py 的 SHARP_VERSION={SHARP_VERSION} 需要同步更新")
                sys.exit(1)
        log(f"  {pkg}: 兼容针命中（{len(must)}/{len(must)}）")


def verify_sharp_native(arch):
    """核对 sharp 原生包与它要求的 libvips 伴随版本（读真实包元数据，不靠记忆）。"""
    na = npm_arch(arch)
    pj = os.path.join(STAGE_DIR, "app", "worker", "node_modules",
                      f"@img/sharp-linux-{na}", "package.json")
    with open(pj, encoding="utf-8") as f:
        meta = json.load(f)
    want = f"@img/sharp-libvips-linux-{na}"
    got = (meta.get("optionalDependencies") or {}).get(want)
    if got != SHARP_LIBVIPS_VERSION:
        log(f"  ERROR: @img/sharp-linux-{na}@{SHARP_VERSION} 要求 {want}={got}，"
            f"build.py 的 SHARP_LIBVIPS_VERSION={SHARP_LIBVIPS_VERSION} 需要同步更新")
        sys.exit(1)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

def copy_tree(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, dirs_exist_ok=True)
    elif os.path.isfile(src):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)


def assemble(arch, version, with_sharp):
    log(f"  组装应用包 ({arch}) ...")
    cli_version = pinned_qodercli_version()
    # 每次从零重建 stage，避免上一架构的残留混入（原生依赖是按架构选的）。
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    os.makedirs(os.path.join(STAGE_DIR, "app"), exist_ok=True)

    for sub in ("cmd", "config", "wizard"):
        copy_tree(os.path.join(PROJECT_DIR, sub), os.path.join(STAGE_DIR, sub))

    # app/bin: fngateway（唯一被 cmd/main 管理的进程）+ 上游 cli2api（子进程）
    bin_dir = os.path.join(STAGE_DIR, "app", "bin")
    os.makedirs(bin_dir, exist_ok=True)
    for name in (f"fngateway-linux-{arch}", f"cli2api-linux-{arch}"):
        src = os.path.join(BIN_CACHE, name)
        if not os.path.exists(src):
            log(f"  ERROR: 缺少编译产物 {name}，请先完整编译该架构")
            sys.exit(1)
        shutil.copy2(src, os.path.join(bin_dir, name))

    # app/worker: 上游 worker 源码 + 随包 Node 依赖
    worker_dir = os.path.join(STAGE_DIR, "app", "worker")
    os.makedirs(worker_dir, exist_ok=True)
    for rel in ("src", "NOTICE", "last-plain.sample.json"):
        copy_tree(os.path.join(SRC_DIR, "worker", rel), os.path.join(worker_dir, rel))
    stage_worker_node_modules(arch, cli_version, with_sharp)
    verify_cli_bundle(arch, with_sharp)
    if with_sharp:
        verify_sharp_native(arch)

    # app/ui: 桌面入口 + 图标
    copy_tree(os.path.join(PROJECT_DIR, "app", "ui", "config"),
              os.path.join(STAGE_DIR, "app", "ui", "config"))
    copy_tree(os.path.join(PROJECT_DIR, "app", "ui", "images"),
              os.path.join(STAGE_DIR, "app", "ui", "images"))

    for f in ("ICON.PNG", "ICON_256.PNG"):
        src = os.path.join(PROJECT_DIR, f)
        if not os.path.exists(src):
            log(f"  ERROR: 缺少图标 {f}（图标随仓库提交，请补齐后再构建）")
            sys.exit(1)
        shutil.copy2(src, os.path.join(STAGE_DIR, f))

    # manifest: 写回版本，并把 platform 落到**本架构**（每个 fpk 只装一个架构的
    # 二进制与原生库，声明成 all 会让 x86 机器也能装上一个跑不起来的包）。
    with open(os.path.join(PROJECT_DIR, "manifest"), "r", encoding="utf-8") as f:
        mf = f.read()
    mf = re.sub(r"(?m)^version\s*=.*", f"version = {version}", mf)
    mf = re.sub(r"(?m)^platform\s*=.*", f"platform = {'x86' if arch == 'amd64' else 'arm'}", mf)

    problems = check_manifest_values(mf)
    if problems:
        log("  ERROR: manifest 取值会被 fnpack 截断，已中止：")
        for p in problems:
            log(f"    - {p}")
        sys.exit(1)
    with open(os.path.join(STAGE_DIR, "manifest"), "w", encoding="utf-8") as f:
        f.write(mf)

    # 上游署名与许可（MIT 要求随二进制附带许可全文；Apache-2.0 组件随包内 LICENSE）
    with open(os.path.join(STAGE_DIR, "UPSTREAM.txt"), "w", encoding="utf-8") as f:
        f.write(
            "本应用内置上游 cli2api 与 Qoder CLI 运行时组件。\n\n"
            f"上游项目: https://github.com/{UPSTREAM_REPO}\n"
            f"上游版本: {UPSTREAM_TAG}\n"
            "上游许可: MIT License\n"
            "上游版权: Copyright (c) 2026 caigee-cmd\n"
            f"构建时间: {datetime.datetime.now().isoformat(timespec='seconds')}\n\n"
            "依据 MIT 许可，源码与二进制形式的再分发均保留原始版权声明与许可声明。\n"
            "上游许可全文见同目录 LICENSE.upstream；源码可自 "
            f"https://github.com/{UPSTREAM_REPO} 获取。\n\n"
            "随包第三方运行时组件（原样分发，各自许可见其包内 LICENSE/NOTICE）:\n"
            f"  - @qoder-ai/qodercli@{cli_version} / @qodercn-ai/qoderclicn@{cli_version}"
            "  (Apache-2.0)\n"
            "  - @qoder-ai/qodercli-ripgrep-* / @qodercn-ai/qoderclicn-ripgrep-*"
            "  (ripgrep: MIT/Unlicense)\n"
            "  - @img/sharp-*  (Apache-2.0)  /  @img/sharp-libvips-*  (LGPL-3.0)\n"
            "  - undici  (MIT)\n"
        )
    lic = os.path.join(SRC_DIR, "LICENSE")
    if os.path.exists(lic):
        copy_tree(lic, os.path.join(STAGE_DIR, "LICENSE.upstream"))
    else:
        log("  [警告] 上游 LICENSE 缺失，包内将缺少 LICENSE.upstream；"
            "MIT 再分发义务未完成，请检查 upstream-src 是否完整")


# ---------------------------------------------------------------------------
# 打包与权限修正
# ---------------------------------------------------------------------------

FNPACK_BASE = "https://static2.fnnas.com/fnpack/fnpack-1.2.3"
FNPACK_VER = "1.2.3"


def fnpack_platform_arch():
    """官方 fnpack 的平台文件名规则（Linux arm64 的文件名是 arm，不是 arm64）。

    2026-09 实测: fnpack-1.2.3-linux-arm 返回 200，-linux-arm64 返回 404。
    （官方文档正文写的是 arm64，以实际文件名为准。）
    """
    s = platform.system().lower()
    if s.startswith("win"):
        return "windows", "amd64"
    if s.startswith("darwin"):
        m = platform.machine().lower()
        return "darwin", "arm64" if m in ("aarch64", "arm64") else "amd64"
    m = platform.machine().lower()
    return "linux", "arm" if m in ("aarch64", "arm64") else "amd64"


def get_fnpack():
    is_win = platform.system().lower().startswith("win")
    name = "fnpack.exe" if is_win else "fnpack"

    tools = os.path.join(BUILD_DIR, "tools")
    for cand in (os.path.join(tools, name), os.path.join(tools, "fnpack")):
        if os.path.exists(cand):
            return cand

    found = shutil.which("fnpack")
    if found:
        return found

    plat, arch = fnpack_platform_arch()
    url = f"{FNPACK_BASE}-{plat}-{arch}"
    os.makedirs(tools, exist_ok=True)
    dest = os.path.join(tools, name)
    log(f"  本地无 fnpack，自动下载 {plat}-{arch} ...")
    if not download(url, dest, f"fnpack {FNPACK_VER}", use_proxy=False, force=True):
        log("  ERROR: 无法获取 fnpack")
        log("  可手动下载后放到 .local-build/tools/fnpack 或加入 PATH：")
        log(f"      {url}")
        sys.exit(1)
    if not is_win:
        os.chmod(dest, 0o755)
    return dest


NEED_EXEC_OUTER = re.compile(r"^cmd/[^/]+$")
# app.tgz 内需要可执行位的文件（fnpack 在 Windows 上产出 0666）:
#   bin/fngateway-linux-*  bin/cli2api-linux-*        Go 二进制（由脚本/网关直接执行）
#   .../bin/rg                                         ripgrep（CLI 直接 spawn）
#   .../bin/*.sh  .../scripts/*.sh                     Qoder 安全插件脚本
INNER_EXEC_PATTERNS = (
    re.compile(r"^bin/[^/]+$"),
    re.compile(r"(^|/)bin/[^/]+\.(sh|cmd|exe)$"),
    re.compile(r"(^|/)scripts/[^/]+\.(sh|cmd)$"),
    re.compile(r"^worker/node_modules/.*/bin/[^/]+$"),
)


def _fix_tar(tar_bytes, outer_re, inner_patterns=None, extra=None):
    """重建 tar: 给匹配项设置 0755；对 app.tgz 递归处理；补齐 extra 文件。

    extra 为 [(归档内路径, 磁盘源文件)]: fnpack 只打包它 schema 认识的条目，
    根目录的署名/许可文件会被丢掉，必须在这里补回去。
    """
    src = tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:")
    out_buf = io.BytesIO()
    dst = tarfile.open(fileobj=out_buf, mode="w:", format=tarfile.USTAR_FORMAT)
    present = set()
    for m in src.getmembers():
        present.add(m.name.strip("/"))
        if m.isreg():
            data = src.extractfile(m).read()
            if m.name == "app.tgz" and inner_patterns is not None:
                inner = gzip.decompress(data)
                fixed = _fix_tar(inner, None, inner_patterns)
                data = gzip.compress(fixed, compresslevel=6)
            elif outer_re is not None and outer_re.match(m.name):
                m.mode = 0o755
            elif inner_patterns and any(p.search(m.name) for p in inner_patterns):
                m.mode = 0o755
            ti = tarfile.TarInfo(m.name)
            ti.mode = m.mode
            ti.size = len(data)
            ti.mtime = m.mtime
            ti.type = m.type
            dst.addfile(ti, io.BytesIO(data))
        else:
            dst.addfile(m)

    for arcname, srcpath in (extra or []):
        if arcname in present or not os.path.exists(srcpath):
            continue
        with open(srcpath, "rb") as f:
            data = f.read()
        ti = tarfile.TarInfo(arcname)
        ti.mode = 0o644
        ti.size = len(data)
        ti.mtime = int(os.path.getmtime(srcpath))
        ti.type = tarfile.REGTYPE
        dst.addfile(ti, io.BytesIO(data))
        present.add(arcname)

    dst.close()
    return out_buf.getvalue()


def fix_fpk_modes(fpk_path):
    """Windows 上 fnpack 产出 0666，需补可执行位，并补回被丢弃的署名/许可文件。"""
    with gzip.open(fpk_path, "rb") as f:
        raw = f.read()
    extra = [
        ("LICENSE.upstream", os.path.join(STAGE_DIR, "LICENSE.upstream")),
        ("UPSTREAM.txt", os.path.join(STAGE_DIR, "UPSTREAM.txt")),
    ]
    fixed = _fix_tar(raw, NEED_EXEC_OUTER, INNER_EXEC_PATTERNS, extra=extra)
    with gzip.open(fpk_path, "wb") as f:
        f.write(fixed)


def verify_fpk(fpk_path, arch, with_sharp):
    """打包后自检，返回 (ok, 消息列表)。"""
    msgs = []
    ok = True
    na = npm_arch(arch)
    elf_machine = {"amd64": 0x3E, "arm64": 0xB7}[arch]
    cli_version = pinned_qodercli_version()

    with tarfile.open(fpk_path, "r:gz") as t:
        names = set(t.getnames())
        for need in ("manifest", "app.tgz", "cmd/main", "cmd/install_init",
                     "config/privilege", "config/resource", "wizard/install",
                     "ICON.PNG", "ICON_256.PNG", "LICENSE.upstream", "UPSTREAM.txt"):
            if need not in names:
                msgs.append(f"缺失 {need}")
                ok = False

        bad = [m.name for m in t.getmembers()
               if m.isfile() and m.name.startswith("cmd/") and not (m.mode & 0o111)]
        if bad:
            msgs.append(f"cmd/* 不可执行: {', '.join(bad)}")
            ok = False

        inner = None
        inner_names = set()
        inner_files = {}
        if "app.tgz" in names:
            inner = tarfile.open(fileobj=io.BytesIO(t.extractfile("app.tgz").read()), mode="r:gz")
            inner_names = set(inner.getnames())
            inner_files = {m.name: m for m in inner.getmembers() if m.isfile()}

        if inner is None:
            msgs.append("app.tgz 无法解析")
            return False, msgs

        def read_inner(name):
            return inner.extractfile(inner_files[name]).read()

        # 二进制: 存在、ELF、架构相符、可执行
        for binname in (f"bin/fngateway-linux-{arch}", f"bin/cli2api-linux-{arch}"):
            if binname not in inner_files:
                msgs.append(f"缺失 {binname}")
                ok = False
                continue
            head = read_inner(binname)[:20]
            if head[:4] != b"\x7fELF":
                msgs.append(f"{binname} 不是 ELF")
                ok = False
                continue
            em = struct.unpack("<H", head[18:20])[0]
            if em != elf_machine:
                got = {0x3E: "x86_64", 0xB7: "AArch64"}.get(em, hex(em))
                msgs.append(f"{binname} 架构不符: 期望 {arch}, 实际 {got}")
                ok = False
            if not (inner_files[binname].mode & 0o111):
                msgs.append(f"{binname} 不可执行（应用起不来）")
                ok = False

        # 控制台的子路径补丁随 go:embed 进了上游二进制，包内必须搜得到标记；
        # 缺了它控制台在 /app/cli2api 下路由失配（点侧边栏没反应、内容区空白）。
        cli_bin = f"bin/cli2api-linux-{arch}"
        if cli_bin in inner_files and CONSOLE_PATCH_MARK.encode() not in read_inner(cli_bin):
            msgs.append(f"{cli_bin} 里找不到控制台子路径补丁标记 {CONSOLE_PATCH_MARK}"
                        "—— 控制台产物没被 patch_console_bundle() 处理")
            ok = False

        # worker 运行时: 源码、模板、CLI bundle、ripgrep、原生库
        worker_need = [
            "worker/src/daemon.mjs",
            "worker/src/compat.mjs",
            "worker/src/rewrite-loader.mjs",
            "worker/last-plain.sample.json",
            "worker/node_modules/undici/index.js",
            f"worker/node_modules/@qoder-ai/qodercli/bundle/qodercli.js",
            f"worker/node_modules/@qodercn-ai/qoderclicn/bundle/qoderclicn.js",
            f"worker/node_modules/@qoder-ai/qodercli-ripgrep-linux-{na}/bin/rg",
            f"worker/node_modules/@qodercn-ai/qoderclicn-ripgrep-linux-{na}/bin/rg",
        ]
        if with_sharp:
            # sharp 原生包用 exports "./sharp.node" 暴露 .node；libvips 侧是
            # lib/libvips-cpp.so.<版本>（版本号随 libvips 升级变化，不做精确匹配）。
            worker_need += [f"worker/node_modules/@img/sharp-linux-{na}/lib/sharp-linux-{na}.node"]
        for need in worker_need:
            if need not in inner_files:
                msgs.append(f"缺失 {need}")
                ok = False

        if with_sharp:
            vips_prefix = f"worker/node_modules/@img/sharp-libvips-linux-{na}/lib/libvips-cpp.so."
            if not any(n.startswith(vips_prefix) for n in inner_names):
                msgs.append(f"缺失 {vips_prefix}*（sharp 无法加载 libvips）")
                ok = False

        for rg in (f"worker/node_modules/@qoder-ai/qodercli-ripgrep-linux-{na}/bin/rg",
                   f"worker/node_modules/@qodercn-ai/qoderclicn-ripgrep-linux-{na}/bin/rg"):
            if rg in inner_files:
                if not (inner_files[rg].mode & 0o111):
                    msgs.append(f"{rg} 不可执行（Glob/Grep 工具会失效）")
                    ok = False
                head = read_inner(rg)[:20]
                if head[:4] != b"\x7fELF" or struct.unpack("<H", head[18:20])[0] != elf_machine:
                    msgs.append(f"{rg} 不是 {arch} 的 ELF")
                    ok = False

        # CLI 版本必须与 worker 钉住的版本一致
        pkg_json = "worker/node_modules/@qoder-ai/qodercli/package.json"
        if pkg_json in inner_files:
            try:
                ver = json.loads(read_inner(pkg_json).decode("utf-8")).get("version")
                if ver != cli_version:
                    msgs.append(f"随包 CLI 版本 {ver} != worker 钉住的 {cli_version}")
                    ok = False
            except Exception as e:
                msgs.append(f"{pkg_json} 解析失败: {e}")
                ok = False

        # 桌面入口图标（缺失不会安装失败，只会让桌面图标空白）
        if "ui/config" not in inner_names:
            msgs.append("缺失 ui/config（桌面入口未定义，飞牛桌面不会出现图标）")
            ok = False
        else:
            try:
                cfg = json.loads(read_inner("ui/config").decode("utf-8"))
            except Exception as e:
                msgs.append(f"ui/config 不是合法 JSON: {e}")
                cfg = None
            for entry_id, entry in ((cfg or {}).get(".url") or {}).items():
                icon = (entry or {}).get("icon", "")
                if "{0}" not in icon:
                    continue
                for size in (64, 256):
                    want = "ui/" + icon.replace("{0}", str(size))
                    if want not in inner_names:
                        msgs.append(f"桌面入口 {entry_id} 的图标缺失: {want}")
                        ok = False

        # 包内 manifest 必须与 stage 逐值一致（fnpack 会静默截断含 ';' 的取值）
        try:
            packed_vals = manifest_values(t.extractfile("manifest").read().decode("utf-8"))
            with open(os.path.join(STAGE_DIR, "manifest"), "r", encoding="utf-8") as sf:
                stage_vals = manifest_values(sf.read())
            for key, want in stage_vals.items():
                got = packed_vals.get(key)
                if got != want:
                    msgs.append(f"包内 manifest 的 {key} 与 stage 不一致"
                                f"（{len(want)} 字符 → "
                                f"{'缺失' if got is None else str(len(got)) + ' 字符'}）")
                    ok = False
        except Exception as e:
            msgs.append(f"manifest 取值比对失败: {e}")
            ok = False

    return ok, msgs


# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="fnOS 应用统一打包脚本")
    ap.add_argument("--arch", "-a", default="all", choices=["all", "amd64", "arm64"],
                    help="目标架构（每个架构出一个 fpk）")
    ap.add_argument("--version", "-v", default="", help="版本号（默认读 manifest）")
    ap.add_argument("--force", "-f", action="store_true", help="强制重新下载")
    ap.add_argument("--skip-upstream", action="store_true", help="复用已下载上游源码")
    ap.add_argument("--package-only", action="store_true", help="仅打包，不编译")
    ap.add_argument("--no-sharp", action="store_true",
                    help="不随包 sharp/libvips 原生库（省约 17MB/架构；图片相关能力不可用）")
    args = ap.parse_args()

    version = args.version.strip() or read_manifest_version()
    if not version:
        log("ERROR: 无法确定版本号")
        sys.exit(1)
    arches = ["amd64", "arm64"] if args.arch == "all" else [args.arch]
    with_sharp = not args.no_sharp

    log("=" * 46)
    log(f" {APP_NAME} 构建")
    log(f" 版本: {version}   架构: {', '.join(arches)}")
    log(f" 平台: {platform.system()}   sharp 原生库: {'随包' if with_sharp else '跳过'}")
    log("=" * 46)

    if not args.package_only:
        log("[1/5] 准备上游源码 ...")
        if args.skip_upstream and os.path.isdir(SRC_DIR):
            log(f"  复用现有源码 ({UPSTREAM_TAG})")
        else:
            fetch_upstream(force=args.force)
        check_upstream_tree()
        patch_console_bundle()
        log(f"  随包 Qoder CLI 版本: {pinned_qodercli_version()}（读自 worker/src/compat.mjs）")

        log("[2/5] 校验图标 + 契约一致性 ...")
        verify_icons()
        check_consistency()

        log("[3/5] 编译二进制 ...")
        for a in arches:
            build_binaries(a)
    else:
        log("[1/5]-[3/5] 跳过源码与编译，使用 .local-build/bin 中已有产物")

    log("[4/5] 组装 ...")
    for a in arches:
        if args.package_only:
            missing = [n for n in (f"fngateway-linux-{a}", f"cli2api-linux-{a}")
                       if not os.path.exists(os.path.join(BIN_CACHE, n))]
            if missing:
                log(f"  ERROR: --package-only 缺少 {a} 的编译产物: {', '.join(missing)}")
                log("         请去掉 --package-only 重新完整构建。")
                sys.exit(1)
        assemble(a, version, with_sharp)

        log("[5/5] fnpack 打包 ...")
        fnpack = get_fnpack()
        raw = os.path.join(STAGE_DIR, f"{APP_NAME}.fpk")
        if os.path.exists(raw):
            os.remove(raw)
        log(f"  fnpack build ({a}) ...")
        proc = subprocess.run([fnpack, "build", "-d", "."], cwd=STAGE_DIR, capture_output=True)
        out_text = ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", "replace")
        if not os.path.exists(raw):
            log(f"  ERROR: fnpack 失败\n{out_text[:1500]}")
            sys.exit(1)

        before = None
        with tarfile.open(raw, "r:gz") as t:
            for m in t.getmembers():
                if m.name == "cmd/install_init":
                    before = m.mode
                    break
        fix_fpk_modes(raw)
        with tarfile.open(raw, "r:gz") as t:
            for m in t.getmembers():
                if m.name == "cmd/install_init":
                    log(f"  cmd/install_init 权限: {before:o} -> {m.mode:o}")
                    break

        ok, msgs = verify_fpk(raw, a, with_sharp)
        if not ok:
            for m in msgs:
                log(f"  [阻断] {m}")
            log("  自检未通过")
            sys.exit(1)

        final = os.path.join(PROJECT_DIR, f"{APP_NAME}-{version}-{a}.fpk")
        shutil.move(raw, final)
        size = os.path.getsize(final) / 1024 / 1024
        log(f"  ✅ {os.path.basename(final)}  ({size:.1f} MB)  自检通过")

    log("")
    log("=" * 46)
    log(" 构建完成")
    log("=" * 46)


if __name__ == "__main__":
    main()
