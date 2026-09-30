# 原生版选型评估：agent2api 还是本项目（fnos-cli2api）

> 评估对象：`github.com/aimod-cc/agent2api`（下称 **agent2api**） vs 本项目 `D:\fnos\fnos-cli2api`（打包上游 `caigee-cmd/cli2api`，下称 **本项目**）
> 问题：做飞牛 fnOS 原生 `.fpk`，用哪个上游/架构更好？
> 评估日期：2026-10（仓库状态以抓取到的 `main` 为准；上游 HEAD `ea1cc03` / v2.9.0）

> **修订说明（重要）**：本评估第二稿并入了一次全量 tarball 深挖（`codeload` 整包解压后逐文件 grep），**推翻初稿的两条判断**：
> 1. 初稿说「面板子路径适配是小改（Route B）」——**错**。见 §3.2 缺口 B：面板资源虽是相对路径，但 **7 个 JS 模块（含 913 KB 的 minified `ui/islands/ui.js`）里是根绝对 API 字面量**，仅改 HTML 属性不够。
> 2. 初稿漏了一个**阻断级**问题：**fnOS 代理会剥 `Set-Cookie`**，而上游修复 PR #41 **尚未合并**。见 §3.2 缺口 E。
> 修正后工作量从"1 周内"上修到 **8–16 天（MVP 3–5 天）**，见 §6。

---

## 0. 结论速览

**推荐：以 agent2api 为目标架构重做原生版，但不要"从零重写"，而是把本项目已验证的飞牛工程外壳（manifest / config / cmd / wizard / 打包流水线 / 双监听模型）整体复用，只替换内核。**

一句话理由：**本项目的价值 90% 在"飞牛外壳 + 网关适配经验"，不在 cli2api 这个内核；而 agent2api 恰好在内核层面比 cli2api 强一个代际**（无 Node 运行时、无按账号拉起的子进程、Qoder 用纯 Rust 协议移植而非"钉死版本的 CLI + minified 补丁"）。

**但有两个前置条件必须先确认**（见 §5、§7）：
1. **许可证**：agent2api 的**许可证不是纯 MIT**，明确禁止商用、禁止批量/大规模账号运营。自用/小范围非营利分享可以走；打算分发或商用会被卡住。
2. **面板 Cookie 穿透**：fnOS 代理会剥 `Set-Cookie`，导致「登录成功却被弹回登录页」。上游修复 PR #41 **未合并**（v2.9.0 里 grep `x-panel-auth-mode` 为 0 命中），必须在反代层自行兜住（§3.2 缺口 E）。

**并且——如果只看"少踩坑"，最优解是降级版**：不接统一网关，只做独立 TCP 端口（面板绑回环、`/v1` 对外）。这条 **3–5 天**就能出可用 fpk，且绕开了子路径与 Cookie 两个大坑。统一网关版是第二阶段的事。

**另外**：本项目已经在做 cli2api 的活儿，且**你之前已经用 agent2api 这一脉做出过 fpk 并装上真机跑过**（`workbuddy2api`，桌面 entry `entry:394708`），后来被本项目的 cli2api 版替换掉了。所以这不是"要不要试"，而是"要不要换回去、以及这次换回去要解决什么"。

---

## 1. 两者到底是什么

| | agent2api | 本项目（fnos-cli2api） |
|---|---|---|
| 上游内核 | 自研 Rust 多上游网关 | 第三方 `caigee-cmd/cli2api`（Node/TS） |
| 打包层 | 无（只有 Docker + 桌面安装包） | 自研 Go `fngateway` + Python `build.py` + 5 类构建守卫 |
| 交付形态 | Docker 镜像 / macOS+Windows 桌面包 | 飞牛 `.fpk`（`cli2api-0.6.11-3-arm64.fpk` 已出） |
| 许可证 | MIT 正文 + **附加使用声明（非纯 MIT）** | MIT |
| 成熟度 | 新仓库（149★/35 fork/19 open issues） | 已在自己环境验证可用 |

---

## 2. 关键差异矩阵

| 维度 | agent2api | 本项目（cli2api） | 谁更好 |
|---|---|---|---|
| **运行时依赖** | 单个 Rust 静态二进制（~10 MB），零 Node | 依赖应用中心 `nodejs_v24` + 每账号一个 Node worker | **agent2api 大胜** |
| **内存** | 单进程，无按账号线性增长 | **~511 MB RSS / 账号**（2 账号 ≈ 1.0 GB），WASM 上下文刻意不释放 | **agent2api 大胜** |
| **上游协议耦合方式** | 协议层用 Rust 重新实现（Qoder 的 COSY 签名/PKCE/信封解包等 17 个文件自持） | 依赖 **Qoder CLI 钉死版本 `1.1.32`** + 6 个字面量 minified NEEDLE，漂移即硬失败 | **agent2api 大胜** |
| **前端资源引用** | `index.html` 相对路径；**但 7 个 JS 模块（含 913 KB minified `islands/ui.js`）是根绝对 API 字面量** | Vite/React 产物，需**构建期正则手术**改 minified router | agent2api 略好（仍必须做子路径改写） |
| **子路径适配** | **零 base-path 感知**（仓库级 grep = 0 命中），需完整 6 项改写，**3–6 天** | 已解决（Route A + 构建期剥前缀），但脆 | **本项目现成** |
| **fnOS 统一网关** | **不支持 Unix socket**（`TcpListener::bind(SocketAddr)`），需打补丁 | 已实现（`${TRIM_APPDEST}/cli2api.sock`） | **本项目现成** |
| **面板 Cookie 穿透** | 双 Cookie（`Path=/` + `Path=/api/panel`），**被 fnOS 代理剥 `Set-Cookie` → 登录弹回**；修复 PR #41 **未合并** | 已解决（服务端注入 `x-api-key`，密钥不下发浏览器） | **本项目现成** |
| **`/v1` 鉴权** | 原生接受 `Authorization: Bearer <key>` | 需 `fngateway` 注入 `x-api-key` | **agent2api 更好** |
| **管理员口令** | 可走 `AGENT2API_ADMIN_PASSWORD_HASH`（bcrypt） | **只能存 SQLite**，env 设不了，migration 不可变（checksum 不符即 panic） | **agent2api 更好** |
| **SIGTERM 优雅停机** | 已实现（`tokio::signal::unix`，同时处理 Ctrl+C 与 SIGTERM） | 已实现 | 平手（与 `cmd/main` 契约天然对齐） |
| **面板/网关分端口** | 原生支持（`AGENT2API_PANEL_PORT`） | 原生支持（双监听） | 平手 |
| **无头登录可用性** | 设备码类可用；**5 家需 loopback 回调 → NAS 场景不可用**；小浣熊自定义协议不可用 | 面板走服务端注入密钥，无此问题 | **本项目更好** |
| **提供方覆盖** | 7 家转发（WorkBuddy / 小浣熊 / CatPaw / AutoClaw / Qoder / Cline Free / Cline Pass）+ accio/codearts/trae/custom | Qoder Intl/CN、WorkBuddy Intl/CN、Trae CN Work + 实验性 Devin/Command Code | agent2api 更宽；本项目 Qoder 路线更"实测过" |
| **Linux 构建产物** | **发布不含 Linux 二进制**，只能源码构建（MSRV 1.88） | 已有可复现 fpk 流水线 | **本项目现成** |
| **许可证** | 禁商用/禁批量账号运营 | MIT | **本项目更好** |
| **本机工具链** | 需 rustc/cargo/docker（**本机都没有**） | 需 Go（**本机有**） | **本项目现成** |

---

## 3. agent2api 作为 fnOS 原生 fpk 的技术评估

### 3.1 对原生打包友好的地方（这是它真正的优势）

1. **专为无头设计**：`agent2api-server` 是无 GUI/webkit 依赖的独立 crate，本来就能脱离 Tauri 壳跑。
2. **Qoder 是真·协议移植，不是 CLI 包装**：`server/src/server/core/providers/qoder/` 下 17 个文件，自己实现 COSY 签名（`cosy.rs`）、PKCE 设备授权（`oauth.rs`）、SSE 信封解包（`stream.rs`）、模型目录（`models.rs`）。该目录内 `qodercli` / `node` / `npx` / `Command::new` / `wasm` 出现次数**均为 0**。
   → 这直接消掉了本项目最大的两个痛点：**钉死版本的 CLI 依赖**和**每账号 511 MB WASM worker**。
3. **面板是"零构建"静态资源（但有子路径坑，见缺口 B）**：`index.html` 里 `src="app.js"`、`href="css/...css"` 全相对路径，0 处 `pushState` / `EventSource` / `new WebSocket`，hash 导航。对比本项目要给 minified react-router 做正则改写（`CONSOLE_LOCATION_RE` 必须恰好命中 1 文件 1 处，否则 abort）——**在 HTML 这一层确实轻得多**；但 JS 里的根绝对字面量仍需桥接覆盖。
4. **自带"没有 Tauri 壳时的浏览器模式"**：`web_shim.rs` 注入 `window.workbuddyDesktop` 的 HTTP shim，`httpCall()` 走相对路径 `fetch`。也就是说"面板脱离桌面壳在浏览器里跑"这件事，上游已经替我们解决了。
5. **`/v1/*` 原生 Bearer**：不需要 `fngateway` 那种 `x-api-key` 注入层，网关适配器可以更薄。
6. **有现成的跨架构构建配方**：`.github/workflows/docker.yml` 用 `$BUILDPLATFORM` 做 amd64+arm64 **交叉编译**（不走 QEMU），说明 arm64 交叉构建路径是被维护的，可以照搬到 fpk 构建里。
7. **内存/体积**：单二进制 ~10 MB vs cli2api 的 ~95 MB Node 包 + 每账号 worker。

### 3.2 必须解决的缺口（5 个，都可解但都要动代码）

**缺口 A — Unix socket 监听（必修）**
`server/mod.rs` 是 `std::net::TcpListener::bind(SocketAddr::new(state.host, state.port))`，全仓库 **0 处** `UnixListener` / `.sock` / `TRIM_`。而飞牛统一网关要求应用监听 `${TRIM_APPDEST}/<appname>.sock`。
两条路：
- **A1（推荐，~100–200 行）**：在 `server/mod.rs` 加一个 `UnixListener` 分支，走 `axum::serve` + `into_make_service_with_connect_info`；`ConnectInfo` 需要包一层同时支持 `SocketAddr` 与 `UnixAddr` 的枚举（因为 `api/access.rs` 之类可能读 peer 信息）。
- **A2（零改上游，~300–500 行）**：写个 Go 小桥接（本项目已有 Go 工具链）在 socket ↔ `127.0.0.1:<port>` 之间转发，并把 `X-Trim-Userid/Isadmin/Username` 透传进去。代价是又多一个进程和一层转发。
> 注意：**网关模式下的服务不应同时开 TCP**（fnos-developer `gateway-proxy.md` §9.2）。如果保留 TCP 给外部 OpenAI 客户端，必须按本项目已有的"独立下游端口"模式处理，且下游端口要绑所有网卡（`net.JoinHostPort("", port)`），不能绑回环。

**缺口 B — 面板子路径前缀（必修，**比初稿判断的难得多**）**

初稿我说这是"小改（Route B），不需要重型机械"——**这条被深挖推翻了，必须更正**。

事实是：**全仓库零 base-path 感知**。对 `basePath|base_path|BASEPATH|gatewayPrefix|__fnGatewayBase|subPath|mount_path` 做仓库级 grep（`desktop-tauri/**/*.{rs,js,html}`）= **0 命中**，应用完全假定自己位于站点根 `/`。

- `server/http.rs` 的 `router()` = `panel_router(...).merge(gateway_router(...))`，**0 处 `.nest()`**。
- `ui/index.html` 的静态资源引用**确实是相对路径**（`css/tokens.css`、`app.js`，无前导 `/`）——这是初稿看到的"好迹象"，但**它只覆盖 HTML 属性层**。
- 真正的坑在于**根绝对 URL 散布在 JS 里**：
  - `ui/login.html` 5 处（`/altcha.min.js`、`fetch('/api/session')`、`fetch('/api/panel/captcha'|'/api/panel/status'|'/api/panel/setup')`、两处 `window.location.href='/'`）；
  - `server/src/server/web_shim.rs` 2 处（`fetch('/api/panel/refresh')`、`window.location.href='/login'`）；
  - **7 个 JS 模块里的字面量**：`ui/islands/ui.js`（**913 KB minified bundle**，含 `"/api/accounts`、`"/v1/models`）、`ui/aliyun-captcha.js`、`ui/autoclaw-oauth.js`、`ui/codearts-welfare.js`、`ui/custom-provider-ui.js`、`ui/providers.js`、`ui/sms-login.js`。
- 而且 `static_files.rs::resolve_file()` 是**按完整 URL 路径**解析的：前缀不剥就 404，剥了但前端没改写就白屏。

→ 所以**仅改 HTML 属性远远不够**，运行时桥接必须覆盖到 minified bundle 里的每一个 `fetch` 调用点。**这是整个工程最大的一块，也是风险最集中的地方（估 3–6 天）**。两条路：
- **B1（不改上游，推荐）**：走 §2 的「应用内自带反代」模式——写个独立小反代进程挂在 socket 上，在反代层完成剥前缀 + HTML/JS 改写 + 桥接注入 + Cookie 修正，`agent2api-server` **原样运行零改动**。代价：多一个进程；桥接覆盖面是风险点。
- **B2（fork 改源码）**：给两个 router 加前缀、注入 `<base>`、改 12+ 个前端文件。更干净但**每次上游升级都要 rebase**。

**缺口 C — 面板登录与 fnOS 身份对接（必做设计决策）**
面板有自己的双令牌鉴权（`access` 2h + `refresh` 30d）+ bcrypt + ALTCHA PoW + 每 IP 锁定（5 次失败锁 5 分钟）。在飞牛里有两个选择：
- **C1**：保留面板自带登录，`allUsers=false` + `accessPerm=readonly`，只让管理员进；shim 的 401 流程通过**预置 API Key + 预填 localStorage** 绕开。
- **C2**：让网关身份头（`X-Trim-Isadmin`）直接决定能否进面板，跳过面板登录。改动更大但体验更"原生"。
> 另外 `panel_gate` / `v1_fail_closed` 在无头启动时会自动激活，除非 `AGENT2API_ALLOW_NO_KEY=1`——**这个默认值要在 fpk 里显式想清楚**，别让用户装完发现打不开。

**缺口 D — 构建与发布链路（必做）**
- 本机**没有 rustc / cargo / docker**（只有 Go/Node/Python/Git/WSL）。
- agent2api **发布不含 Linux 二进制**，所以 fpk 构建必须引入 Rust 交叉编译（`rustup target add aarch64-unknown-linux-gnu` + `gcc-aarch64-linux-gnu`）。
- 可行路径：① 本机装 rustup 直接交叉编译；② 用 WSL 建 Linux 构建环境；③ 在飞牛机器上/CI 里构建。**建议 ③ 或 ②**，因为 Dockerfile 里已有的 arm64 交叉配方可以直接抄。
- 还要把 UI 目录（`ui/`）和二进制一起打进 fpk，并设好 `AGENT2API_UI_DIR`。
- **MSRV 是 1.88，不是 README 写的 1.77**（`rusqlite 0.40 → libsqlite3-sys 0.38` 决定的）。照 README 装旧工具链会直接构建失败。
- 体积/耗时估算：server crate **303 文件 / 95,674 行**；`desktop-tauri/ui` **71 文件 / 2.05 MB**。release profile 已开 `panic="abort"` + `codegen-units=1` + `lto=true` + `opt-level="s"` + `strip=true`，所以**首次全量构建预计 20–40 分钟/架构**，二进制 strip 后约 10–20 MB，.fpk 估计 15–25 MB（估算值，未实测）。

**缺口 E — 面板 Cookie 被 fnOS 代理剥离（阻断级，初稿完全漏掉）**

面板鉴权是双 Cookie：access token（2h，`HttpOnly; Path=/`）+ refresh token（30d，`HttpOnly; Path=/api/panel`），均 `SameSite=Lax`。

**问题**：fnOS 代理会剥掉 `Set-Cookie`，导致**「登录成功却被弹回登录页」**。这不是推测——上游 **issue/PR #41**（by shadyrispy，仍 OPEN）记录的就是这个现象，其修复方案是探测式回退（`x-panel-auth-mode: body` 头 + `?auth-mode=body` 查询 + 长寿命探测 Cookie + `Authorization` 双头）。

**关键**：在 v2.9.0 源码里 grep `x-panel-auth-mode` = **0 命中 → 该 PR 尚未合并**。所以：
- 要么在反代层自行把 `Set-Cookie` 的 `Path` 改写为 `/app/agent2api/api/panel` 并确保这一跳不被剥；
- 要么自行 cherry-pick PR #41；
- 要么等上游合并。
> 这条是初稿遗漏的**阻断级**风险，也是建议「先做降级版（独立端口、不走网关）」的最强理由之一——直连端口没有这一跳，Cookie 自然没问题。

### 3.3 其余需要实测确认的点

- 各家 provider 的**登录流程在无头环境是否可行**（深挖给出了明确三分法）：
  - ✅ **设备码轮询，纯浏览器可用**：WorkBuddy（WorkOS RFC 8628）、Qoder、Cline。
  - ⚠️ **回环回调，仅"浏览器与网关同机"可用**：AutoClaw OAuth、CatPaw、CodeArts、Accio、Trae。NAS 场景下用户从 LAN 上的 PC 访问 → 回调落到 **PC 自己的 127.0.0.1** 而非 NAS → **不可用**。作者注释里已写明这个取舍（`local_browser` 判据 = `state.host.is_loopback()`）。另外 CatPaw 的回调地址**硬绑** `http://127.0.0.1:<网关端口>`（上游 redirect 白名单只放行 127.0.0.1/localhost）；CodeArts/Accio/Trae 的回调路径固定拼 `http://127.0.0.1:<port>/oauth/callback`，**不可自定义**。
  - ❌ **自定义协议，浏览器无法中继**：小浣熊 `office-raccoon://auth/callback` → 只能"填写凭证"。
  - ⚠️ AutoClaw「导入桌面客户端会话」是 **Windows-only DPAPI** 路径，非 Windows 是桩。
  - ⚠️ ZCode 活动套餐渠道需要 WebView 内的阿里云无痕验证码 minter（`ui/zcode-captcha-pool.js`），headless 下该渠道不可用——但**优雅降级，不崩进程**。
  - **实务含义**：fpk 的用户主路径应是「粘贴凭证 / Cookie / Token」；网页登录只对设备码类顺畅。
- **面板运维动作在网页端被明确禁用**：改端口、重启、装更新、文件对话框、导出日志一律返回 `SHELL_UNAVAILABLE`（"该操作在网页端不可用（仅桌面端支持）"）。这些必须由 `cmd/main` 生命周期脚本承接。
- **`AGENT2API_HOST` 默认 `0.0.0.0`**：违反"默认拒绝未鉴权的非 loopback 监听"。网关模式必须显式设 `127.0.0.1`；`AGENT2API_ALLOW_NO_KEY` 绝不能开。
- **`AGENT2API_CAPTCHA_ENABLED`** Dockerfile 默认 `1`；NAS 局域网可考虑设 `0`，但会降低面板爆破成本，需权衡。
- Docker Hub 上 `aimodcc/agent2api` 的 arm64 镜像是否存在（本次从本机**未能验证**，Docker Hub 请求超时）。**注意**：即使有镜像，也可用 `docker create` + `docker cp` 把 `agent2api-server` 二进制**抠出来**直接进 fpk——这是绕开"必须本地装 Rust"的一条捷径。
- 面板在 `/app/<name>` 下的实际运行行为（前缀、深链、刷新）。
- **资源占用**：`MAX_CLIENTS=24`（出网连接池）、`MAX_BODY_SIZE=32MB`（请求体上限）、tokio 默认 worker = CPU 核数。**32MB 请求体上限在 4GB ARM NAS 上并发几条大请求就可能吃紧**，建议实测并考虑下调。ARM 上 SQLite WAL 与 `/vol*` 文件系统的兼容性亦未验证。
- `web_shim.rs` 有一句**过期文案**：提示用户设 `AGENT2API_PORT`，但二进制实际读 `AGENT2API_PROXY_PORT`。写文档别照抄。

---

## 4. 本项目（cli2api 打包层）的现状与代价

**已经做对的（这是资产，别丢）**：
- 完整的飞牛工程外壳：`manifest`（`service_port=3010`、`checkport=true`、`install_dep_apps=nodejs_v24`）、`config/privilege`（`run-as=package`）、`app/ui/config`、`wizard/*`、`cmd/*` 生命周期（status 契约：running→0 / not running→3 / bad arg→1）。
- 双监听模型：网关 socket + 独立下游 TCP `3010`（只放 `/v1/*` 和 `/health`）——这是**飞牛 1.2.0604+ 拦截 `Authorization` 头的必然解法**，也是 `workbuddy2api` / `deepseek.harness` 两个项目共同验证过的结论。
- 5 类构建守卫、`patch_console_bundle()` 构建期补前缀、Route A 全套服务端职责（剥 `Path`/`RawPath` 前缀、改写绝对 HTML 属性、注入运行时桥接）。

**代价（正在持续付的成本）**：
- **~511 MB RSS / Qoder 账号**，线性增长；`NODE_OPTIONS=--max-old-space-size=384` 只是 JS 堆软上限，WASM/外部内存不计数。多账号 = 多 Node worker + 多 HOME。
- **上游 CLI 钉死**：`PINNED_QODERCLI_VERSION = "1.1.32"` + 6 个字面量 NEEDLE，上游一漂移就硬失败；而上游发布节奏约 **2–4 次/周**。
- **管理员 key 只能存 SQLite**，env 设不了；migration 不可变，checksum 不符直接 panic。
- 构建依赖 `gh-proxy.com` / `ghfast.top` 镜像和 npm 下载。
- 社区 issue 以协议漂移、签到失效、**多账号被判定为同环境风控**为主（如 #222：3 个账号被限流）。

> 换句话说：**本项目现在是"能跑，但每一层都在跟上游赛跑"**。而 agent2api 的设计恰好把这几层赛跑都去掉了。

---

## 5. 决定性因素

1. **许可证（可能是唯一的一票否决项）**
   agent2api 是 MIT 正文 **+ 附加使用声明**：§3 明确**禁止商用、禁止为盈利再分发、禁止批量/大规模账号运营、禁止绕过计费/配额**（个人学习研究/自用与非盈利分享豁免）；§4 还写明**凭证明文存盘**（`~/.agent2api/`）。本项目是干净 MIT。
   → **自用 / 非盈利小范围分享：可以。打算商用或公开大规模分发：不行。**
2. **你已经有先例**
   `fnos-workbuddy2api`（agent2api 这一脉）曾经做出来并装上真机（桌面 entry `entry:394708`），后来被 cli2api 版替换。**这次要换回去，最好先搞清楚当初为什么换**——是内存、是登录、还是 provider 覆盖？如果当初换掉的原因（比如 cli2api 的 Qoder 支持更好）现在依然成立，那"换回去"就要先解决那个原因。
3. **维护成本的方向相反**
   本项目 = 一次性工程 + 持续对抗上游漂移；agent2api = 一次性工程 + 上游自己维护协议。**时间越往后，agent2api 的账越划算。**
4. **工程量：1.5–3 周，但可以 3–5 天先落地**
   缺口 A–E 都是"加代码"而非"改架构"，且大部分机械（manifest/config/cmd/wizard/打包/双监听）可**直接从本项目搬**。但缺口 B（子路径）和 E（Cookie）把"统一网关版"推到 **8–16 天**。
   **而"降级版"（不接网关，只做独立 TCP 端口）只要 3–5 天**，且天然绕开 B 和 E 两个最大的坑——这是强烈推荐的起手式。
5. **一个被低估的有利事实：本项目的外壳几乎可以整段复用**
   agent2api 的 env 契约（`HOST`/`PROXY_PORT`/`PANEL_PORT`/`UI_DIR`/`PROXY_HOME`）与飞牛 `cmd/main` 要的形状天然吻合，且**已实现 SIGTERM 优雅停机**（`tokio::signal::unix` 同时处理 Ctrl+C 与 SIGTERM），与本项目 `cmd/main` 的 `kill -TERM` → 等 10s → `kill -KILL` 契约**天然对齐**。它甚至已经支持"面板与网关分端口"（`AGENT2API_PANEL_PORT`），正是本项目双监听模型的现成对应物。

---

## 6. 推荐路线（分阶段，风险递增）

**阶段 0 — 先验证，不写代码（0.5 天）**
- 确认 Docker Hub `aimodcc/agent2api` 是否有 arm64 镜像；有就先在飞牛上 `docker run` 跑一遍，确认面板、Qoder 登录、`/v1` 可用。
- 明确许可证边界（自用 vs 分发）。
- 确认当初 `workbuddy2api` 被替换的原因。

**阶段 1 — 降级版 MVP（3–5 天，强烈建议先做这个）**
> 形态：**不接统一网关**，只做独立 TCP 端口。面板绑 `127.0.0.1`（或经反代），`/v1/*` 对外。**绕开缺口 B 与 E 全部风险。**
- 复制本项目的 `manifest` / `config` / `cmd` / `wizard` 骨架。
- Rust 交叉编译 `agent2api-server`（arm64 + amd64），连 `ui/` 一起打包；或用 Docker 镜像 `docker create` + `docker cp` 抠二进制。
- 生命周期脚本承接"网页端被禁用"的运维动作（改端口 = 改 env 后 restart）。
- 环境变量注入：`AGENT2API_PROXY_HOME=${TRIM_PKGVAR}`、`AGENT2API_UI_DIR=${TRIM_APPDEST}/ui`、`AGENT2API_HOST=127.0.0.1`、`AGENT2API_PANEL_PORT`、`AGENT2API_CAPTCHA_ENABLED=0`、预置 `AGENT2API_ADMIN_USER/PASSWORD`。**升级幂等**：绝不无条件重建 `agent2api.db` 与管理员凭证。
- 真机验收：全生命周期 + 权限拒绝 + 端口冲突 + 路径不存在。

**阶段 2 — 接统一网关（+3–6 天，B 是大头）**
- 写应用内自带反代（B1，推荐，零改上游）挂在 `${TRIM_APPDEST}/agent2api.sock`，反代到 `127.0.0.1:3065`；完成剥前缀 + HTML/JS 改写 + 运行时桥接 + `Set-Cookie` Path 修正 + SSE 头（`X-Accel-Buffering: no` / `Cache-Control: no-transform`）。
- 同时解决缺口 E：Cookie Path 改写，或自行 cherry-pick PR #41。
- **注意安全红线**：网关模式的服务**不应同时开 TCP**；若保留下游端口给外部客户端，该端口要绑所有网卡（`net.JoinHostPort("", port)`），不能绑回环。

**阶段 3 — 登录体验 + provider 无头化（+1–4 天）**
- 非 loopback 部署时，把 AutoClaw/CatPaw/CodeArts/Accio/Trae 的"网页登录"按钮替换为"填写凭证"引导；小浣熊直接走凭证。
- 确认设备码类（WorkBuddy/Qoder/Cline）经网关子路径后轮询 URL 仍可达。
- 端到端回归：网关入口 / 下游端口 / 面板深链 / 账号增删 / 流式转发。

**另建议**：向上游提 issue 询问 fnOS 支持意向，并把本报告反馈给作者——PR #41 若合并，阶段 2 的 Cookie 部分工作量会大幅下降。

**不建议**：直接丢掉本项目从零开始。飞牛外壳和网关适配经验是这个项目最贵的部分，重写一遍纯属浪费。

---

## 7. 需要你拍板的事

1. **这个 fpk 的用途**：纯自用 / 非盈利分享 / 商用分发？（决定许可证是否一票否决）
2. **当初为什么把 `workbuddy2api` 换成 cli2api 版**？那个原因现在还存在吗？
3. **先做降级版（3–5 天，独立端口）还是直接上统一网关版（1.5–3 周）**？我建议先降级版。
4. **provider 范围**：只要 Qoder / WorkBuddy / Cline（设备码类，无头最友好），还是要 7 家全上（后 5 家需凭证导入）？
5. **构建环境**：本机装 Rust 工具链、走 WSL、还是从 Docker 镜像抠二进制？

---

## 8. 未验证项与风险清单

| 项 | 状态 | 影响 |
|---|---|---|
| agent2api 的 Linux/arm64 构建在本机能否走通 | 未验证（本机无 rustc/cargo/docker） | 决定阶段 1 工期；可用 Docker 镜像 `docker cp` 抠二进制绕过 |
| Docker Hub 是否有 arm64 镜像 | **未验证**（请求超时） | 影响能否先跑 Docker 验证 |
| 面板在 `/app/<name>` 下实际行为（前缀/深链/刷新） | 未验证（需真机） | 决定缺口 B 工作量 |
| fnOS 各版本是否都剥 `Set-Cookie` | 未验证（PR #41 只记录了现象） | 决定缺口 E 是否必须自研 |
| 各 provider 无头登录可行性 | 三分法已明确（证据充分） | 第一版建议砍掉网页登录 |
| agent2api 是否做 Origin/Host 校验、强度如何 | 未逐一确认 | 网关改写 `Host` 时可能触发失败 |
| 真实 RSS / 并发承载（4GB ARM NAS） | **无数据** | `MAX_BODY_SIZE=32MB` 可能吃紧，建议实测后下调 |
| ARM 上 SQLite WAL 与 `/vol*` 文件系统兼容性 | 未验证 | 潜在数据层问题 |
| 构建耗时与产物体积 | **估算**（20–40 分钟/架构；fpk 15–25 MB） | 影响迭代节奏 |
| 网关模式下 `service_port` 如何声明 | 未确认 | 影响 manifest 写法 |
| 本机与飞牛真机的连通性 | 本会话未连接设备 | 阶段 0 需要 |
| agent2api 上游 17 个 open issue 的具体内容 | 仅知标题（#41/#46/#48/#35 等） | 可能有坑（如某个 provider 已失效） |

> **重要免责**：本次所有 fnOS 侧结论来自 `fnos-developer` skill（v1.3.1，语料快照 2026-08~09）及其收录的 8 个生产项目实战经验，**非官方文档直引，且未在真机做过任何安装测试**。落地前请以 `https://developer.fnnas.com/docs/guide/` 与 `fnpack build` 实测为准。

---

### 附：本次评估的证据来源

- **agent2api 全量源码深挖**（`codeload` tarball 整包解压，HEAD `ea1cc03` / v2.9.0，5,197,137 B）：
  - `server/Cargo.toml`（独立 crate、MSRV 1.88、`license-file` 而非 SPDX `"MIT"`）、`Dockerfile`（`-p agent2api-server`、arm64 交叉工具链、`debian:bookworm-slim`、rustls 纯 Rust、rusqlite bundled）
  - `server/src/server/mod.rs`（TCP-only bind、`panel_port` 分端口、无 UnixListener）、`http.rs`（router、无 nest、`MAX_BODY_SIZE=32MB`、CORS）、`static_files.rs`（`resolve_file()` 按完整 URL 解析）、`web_shim.rs`（浏览器 shim、`SHELL_UNAVAILABLE`、根绝对 URL、登录三分法注释）、`access.rs`（双 Cookie 作用域）、`api/panel.rs`（双令牌鉴权）、`api/session.rs`（loopback 回调硬绑 127.0.0.1）、`core/egress.rs`（`MAX_CLIENTS=24`）、`paths.rs`
  - `core/providers/mod.rs`（7 家注册表 + `supports_web_login()`）、`core/providers/qoder/*`（17 文件协议移植，`qodercli`/`node`/`wasm` 均 0 命中）
  - `ui/index.html`（相对路径）、`ui/login.html`（5 处根绝对）、`ui/islands/ui.js`（913 KB minified）、`ui/zcode-captcha-pool.js`、`ui/aliyun-captcha.js`
  - `LICENSE`（MIT + 附加使用声明）、`.github/workflows/{build,docker}.yml`、`docker-compose.yml`
  - 仓库级 grep：`basePath|gatewayPrefix|subPath|mount_path` = **0 命中**；`UnixListener|unix::net` = **0 命中**；`x-panel-auth-mode` = **0 命中**
  - GitHub API：releases（18 tag，仅 Windows/macOS 产物）、issues（17 open，#41/#46/#48/#35）
- **本项目**：`README.md`、`build.py`、`manifest`、`cmd/main`、`fngateway/main.go`、`fngateway/internal/gateway/gateway.go`。
- **技能参考**：`fnos-developer/references/gateway-proxy.md`（§2 应用内自带反代、§3 六项改写、§9.2 网关模式只监听 socket）、`references/package-model.md`（socket 必须落 `TRIM_APPDEST`）、`references/security-review.md`、`references/build-test.md`（fnpack 白名单）、`templates/{ui.gateway.json,cmd-main.sh}`、`references/inbox.md`（`Location` 不可改、react-router v7 构建期补前缀、`service_port` 绑回环失效）。
- **先例**：`D:\fnos\nas\docs\*` 中 `WorkBuddy2API 网关` / `entry:394708`；`fnos-logmanager/.local-build/appdata-perf-report.md`。
