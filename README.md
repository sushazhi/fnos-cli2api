# fnos-cli2api

把 [cli2api](https://github.com/caigee-cmd/cli2api)（Qoder CLI → OpenAI 兼容 API 网关）打包成**飞牛 fnOS 原生应用**。

- **零源码改动**：上游 Go 服务原样编译，控制台静态资源直接使用上游仓库内已提交的产物（无需前端构建）。
- **统一网关接入**：面板走 `/app/cli2api`，复用飞牛登录态，仅管理员可访问。
- **下游独立端口**：`3010`（上游默认端口），标准 OpenAI SDK 直连。
- **上游官方图标**：应用图标取自上游仓库 `frontend/public/apple-touch-icon.svg`，由 `assets/render-icons.py` 栅格化。
- **随包携带运行时**：Qoder CLI 国内区 + 全球区组件、ripgrep 原生库、sharp/libvips 原生库全部随包，安装时不联网。

---

## 1. 架构

```
飞牛桌面 → CLI2API 网关（仅管理员）
                │
                │  统一网关 /app/cli2api
                ▼
        ${TRIM_APPDEST}/cli2api.sock
                │
        ┌───────┴────────────────────────────────────────┐
        │  fngateway（本项目新增的适配层，Go）            │
        │  · 剥离 /app/cli2api 前缀                       │
        │  · 注入 x-api-key（从 qoder.db 读控制台密钥）   │
        │  · 把 X-Trim-Userid/Isadmin 交给面板做鉴权      │
        │  · 托管 cli2api 子进程                          │
        └───────┬────────────────────────────────────────┘
                │  127.0.0.1:内部端口（回环，不对外）
                ▼
        cli2api 上游二进制（零改动）
                │
                │  QODER_NODE_BINARY / QODER_WORKER_DAEMON / ...
                ▼
        Node worker（nodejs_v24 运行时）+ Qoder CLI 组件

外部 OpenAI 客户端 ──→ 0.0.0.0:3010 ──→ 仅放行 /v1/* 与 /health
```

### 为什么要 fngateway 这一层

1. **子路径适配**：上游服务的路由与静态资源引用都假定挂在根路径，网关前缀 `/app/cli2api` 需要剥离与改写（含重定向 Location、Cookie Path）。
2. **免密登录**：上游控制台需要一把 API 密钥才能进入。网关侧从 SQLite 读取该密钥并注入，浏览器只拿到占位串，**密钥不出现在前端**。
3. **身份约束**：控制台是管理员专属，网关根据 `X-Trim-Isadmin` 拒绝非管理员。
4. **端口隔离**：飞牛 1.2.0604+ 会拦截非飞牛票据的 `Authorization` 头（判定为 `invalid token`）。外部客户端因此必须走独立 TCP 端口，且该端口只放行 `/v1/*`。
5. **内存约束**：每个 Qoder 账号是一个常驻 Node worker，内存随账号数线性增长。网关在 `childEnv()` 里给所有 worker 注入 `NODE_OPTIONS=--max-old-space-size=...`（见 §5.3），这是 fpk 路径上唯一可用的内存闸门——fnOS 的 `config/resource` 与 `manifest` 都没有内存上限字段。
6. **接入地址纠偏**：控制台「API 接入」页的地址是给外部客户端抄的，但上游只返回相对路径（`/v1`），前端用 `location.origin` 补全 —— 面板挂在飞牛桌面域下，补出来就成了 `http://<飞牛IP>:8666/v1`（桌面端口 + 统一网关），抄过去必然连不通。网关在 `/api/overview` 的响应里把 `access.*` 换成下游端口的绝对地址（`http://<飞牛IP>:3010/v1`），前端 `absUrl()` 对绝对地址原样返回，面板上显示与复制到的就是能用的地址。

### 控制台的子路径适配为什么改产物

上游控制台是 react-router v7 的 SPA，它每次路由判断都**实时读 `window.location`**；
而 Chrome 里 `Location` 的属性是 `[LegacyUnforgeable]`（原型上没有 `pathname`
描述符、实例上不可配置），运行时脚本改不掉它读到的值。统一网关又必须把
`/app/cli2api` 写进 URL 栏（否则刷新与深链会落到飞牛网关的 404 上），于是路由
读到的是带前缀路径、匹配不到任何路由，最终落在 `<Route path="*">` 的兜底
`<Navigate>` 上：外壳在、内容区空白、点侧边栏没反应。

因此 `build.py` 在编译前对入库的控制台产物打一处**构建期补丁**
（`patch_console_bundle`）：在 `createBrowserLocation` 里把前缀剥掉再交给路由
（前缀优先取桥接脚本注入的 `window.__fnGatewayBase`，否则用随包固定的
`/app/cli2api`）。写入侧仍由 `fngateway` 的桥接脚本补前缀，两侧配合后 URL 栏
与路由各拿到自己需要的形态。定位锚点不是「恰好 1 个文件 1 处」时构建直接失败，
不会静默打出一个点不动的面板。

---

## 2. 目录结构

```
fnos-cli2api/
├── manifest                  # fpk 清单（platform 由 build.py 按架构改写）
├── ICON.PNG / ICON_256.PNG   # 应用图标
├── app/
│   └── ui/
│       ├── config            # 桌面入口定义（.url，allUsers=false）
│       └── images/           # 入口图标
├── cmd/                      # 生命周期脚本（fnOS 约定 9 个）
│   ├── install_init / install_callback
│   ├── upgrade_init / upgrade_callback
│   ├── config_init / config_callback
│   ├── uninstall_init / uninstall_callback
│   └── main                  # start / stop / restart / status
├── config/
│   ├── privilege             # run-as=package，普通用户身份
│   └── resource              # 空：不申请任何 fnOS 系统资源
├── wizard/                   # 安装/升级/配置/卸载向导文案
├── fngateway/                # 统一网关适配层（Go，本项目的核心新增代码）
│   ├── main.go               # 布局解析、子进程托管、双监听
│   ├── rotate.go             # 日志轮转
│   └── internal/
│       ├── consolekey/       # 从 qoder.db 读控制台密钥（modernc.org/sqlite，纯 Go）
│       └── gateway/          # 前缀剥离、Header 注入、TCP 端口门禁
├── assets/
│   ├── apple-touch-icon.svg  # 图标真源：上游官方图标（frontend/public/apple-touch-icon.svg）
│   ├── render-icons.py       # 由 SVG 栅格化出 64/256 图标（含尺寸与配色自检）
│   └── icon-master.png       # 964px 设计源（由 render-icons.py 生成，不进包）
└── build.py                  # 一键打包（拉上游 → 交叉编译 → 组装 → fnpack）
```

**构建产物**（不入库）：

```
cli2api-<版本>-1-amd64.fpk
cli2api-<版本>-1-arm64.fpk
```

---

## 3. 构建

### 3.1 前置条件

| 依赖 | 说明 |
|---|---|
| Python 3.8+ | 运行 `build.py` |
| Go 1.27+ | 交叉编译 fngateway 与上游 cli2api |
| Node.js / npm | 拉取 worker 的 npm 依赖包 |
| 网络（首次） | 拉取上游源码与 npm 包；可用 `--skip-upstream` 复用缓存 |

> Windows 上构建无需 WSL；`build.py` 会处理 Windows 下 tar 权限位丢失的问题（`fnpack` 产物会被重新解包、补回可执行位后重打包），所以**产物一定是正确的**，源码工作区缺可执行位不影响 fpk。

### 3.1.1 提交到仓库时的两个坑（Windows）

1. Git for Windows 默认 `core.fileMode=false`，`chmod +x cmd/*` 不会被记录，克隆到 Linux 后 `cmd/*` 是 644。仓库建好后执行一次：

   ```bash
   git add cmd && git update-index --chmod=+x cmd/config_callback cmd/config_init \
     cmd/install_callback cmd/install_init cmd/main \
     cmd/uninstall_callback cmd/uninstall_init cmd/upgrade_callback cmd/upgrade_init
   ```

2. `.gitattributes` 已固定 `* text=auto eol=lf`。**不要**去掉它：`cmd/*` 一旦变成 CRLF，fnOS 上安装/启动会直接失败。

### 3.2 命令

```bash
python build.py                      # 默认双架构（amd64 + arm64）
python build.py --arch amd64         # 只出 amd64
python build.py --arch arm64         # 只出 arm64
python build.py --skip-upstream      # 复用已下载的上游源码
python build.py --package-only       # 跳过编译，只重新打包
python build.py --no-sharp           # 不打包 sharp/libvips（省约 17MB 解包体积）
python build.py --force              # 强制重新下载上游与 npm 包
python build.py --version 0.6.13-1   # 覆盖版本号
```

首次构建约需数分钟（主要是下载两个 Qoder CLI 组件包，合计约 57MB）。npm 包会缓存在 `.local-build/`，重复构建不重复下载。

### 3.2.1 换图标

应用图标不在构建期下载，也不由 `build.py` 重采样：真源是随仓库提交的上游官方 SVG
（`assets/apple-touch-icon.svg`，即上游
[`frontend/public/apple-touch-icon.svg`](https://github.com/caigee-cmd/cli2api/blob/main/frontend/public/apple-touch-icon.svg) 的副本），
用本机 Chrome/Edge 的 headless 渲染把 64/256 两种尺寸**按目标像素直接栅格化**：

```bash
python assets/render-icons.py        # 重新生成 ICON.PNG / ICON_256.PNG / app/ui/images/icon_*.png
```

脚本自带三项自检（尺寸、四角透明、官方配色 `#18181B`/`#FCFCFC`/`#22D3EE`），
渲染歪了会当场报错；换图标只需替换 `assets/apple-touch-icon.svg` 后重跑一次。

### 3.3 构建期守卫

`build.py` 在打包前后有五类校验，用于拦截"能编过、上机就炸"的缺陷：

1. **上游树完整性**：上游关键文件（含控制台静态产物）存在，且产物中确实带 `cli2api_key`（否则免密登录会失效）。
2. **worker ↔ CLI 兼容性**：`worker/src/compat.mjs` 里 `PINNED_QODERCLI_VERSION` 与随包 CLI 版本一致；5 个 compat 探针（`prepareInfer`/`createWasm`/`modelCatalog`/`quotaApi`/`checkinAuth`）在压缩后的 CLI bundle 中确实存在；CLI bundle 内联的 sharp 版本与 `SHARP_VERSION` 一致，且 `@img/sharp-libvips-*` 伴生版本对得上。
3. **跨文件契约一致性**：`fngateway` 常量、`app/ui/config` 入口、`cmd/main` 的 socket 路径、`manifest` 的前缀/端口/依赖/图标命名必须互相吻合。
4. **打包后自检**：解包成品，逐个确认必需条目、`cmd/*` 可执行位、两个 Go 二进制的 ELF 架构与可执行位、worker 依赖清单、sharp 原生库路径、ripgrep 可执行位、CLI 版本号、入口图标、manifest 值（含 fnpack 的 `;` 截断检查），以及上游二进制里能搜到控制台子路径补丁的标记。
5. **控制台子路径补丁**：`patch_console_bundle()` 的定位锚点必须恰好命中 1 个文件 1 处，注入后必须能读到标记；对不上就中止并提示重新核对（幂等，重复构建不重复注入）。

任一失败即中止并给出可操作提示，不产出 fpk。

### 3.4 自动跟随上游（GitHub Actions）

`.github/workflows/build-and-release.yml` 每周日跑一次（cron
`17 4 * * 0`，UTC，即北京时间 12:17；也可在 Actions 页面手动触发）：

1. `scripts/upstream_sync.py check` 查上游最新正式 Release，与 `build.py` 的
   `UPSTREAM_TAG` 比较。
2. **有更新**：把版本落进仓库文件（`build.py` 的 `UPSTREAM_TAG` 与两处说明性注释、
   `manifest` 的 `version` 与 `changelog`、`README.md` 的三处当前版本引用），
   构建 amd64 + arm64 两个 fpk，**构建通过后**才提交推回 `main`，并建一个以
   上游 tag 命名的 Release（`v0.6.14` 这类），附上两个 fpk 与 `SHA256SUMS.txt`。
3. **无更新**：直接跳过，不构建、不提交、不发布，流程仍是绿的。

顺序上「先构建、后提交」是有意的：`main` 上不会出现"版本号已改但构建不出来"的提交。

检查步骤排在检出之后、所有 `setup-*` 之前，并用 runner 自带的 `python3`
（`upstream_sync.py` 只依赖标准库，不需要固定 Python 版本）。所以「无更新」的一次
运行不会安装 Python / Go / Node，只花检出 + 几次 API 查询的时间。

手动触发时的 `force` 选项会忽略上游比较、按仓库里已锁定的版本重新出包；若上游
此时正好有更新的版本，会顺带把新版本一起带上，不会反而重发旧版本。

`scripts/upstream_sync.py` 的替换全部基于**锚点**并要求恰好命中 1 处，命中后内容
还必须真的变化，否则报错退出。所以 README 里描述历史事实的版本号（如「自 0.6.13-1
起，网关会给每个 worker 注入 V8 老生代上限」）不会被误改 —— 它记录的是该能力
**引入**的版本，不是当前版本。要新增版本引用，请按同样方式给锚点，不要改成全局替换。

> 说明：Actions 的 `schedule` 在仓库连续 60 天无提交后会暂停，届时手动跑一次即可恢复。

---

## 4. 安装与升级

```bash
# 安装
appcenter-cli install-fpk cli2api-0.6.13-1-amd64.fpk

# 查看状态 / 启停
appcenter-cli list
appcenter-cli start cli2api
appcenter-cli stop cli2api
```

`manifest` 中 `install_dep_apps = nodejs_v24`，应用中心会自动带上 Node.js 运行时依赖。

**升级**：数据目录（账号凭证、API 密钥、控制台密钥）全部保留，每账号运行时缓存在升级后自动清理（可重建，不影响登录状态）。

**卸载**：向导里选择是否保留数据。选择"保留数据"后重新安装可继续使用原有账号与密钥；选择"删除全部数据"会连凭证一起清除且**无法恢复**。

---

## 5. 使用

### 5.1 控制台（管理员）

飞牛桌面 → **CLI2API 网关**。免密进入，可管理账号、模型、密钥、日志，并内置对话测试台。

### 5.2 下游接入（外部客户端）

```bash
curl http://<飞牛IP>:3010/v1/chat/completions \
  -H "Authorization: Bearer <在控制台生成的密钥>" \
  -H "Content-Type: application/json" \
  -d '{"model":"...","messages":[{"role":"user","content":"hi"}]}'
```

已支持：`/v1/chat/completions`、`/v1/models`、`/v1/messages`、`/v1/responses`、`/health`。

其余路径（含控制台接口 `/api/*`）一律返回 404，不会从这个端口泄漏出去。密钥在控制台「密钥管理」里创建；控制台自身的密钥也可以直接当 `/v1` 密钥用。

> **注意**：统一网关路径 `/app/cli2api` 仅供面板自身使用，**不要**把外部 OpenAI 客户端指向它——它带飞牛登录态校验，且飞牛 1.2.0604+ 会拦截外部 `Authorization` 头。
>
> 控制台「API 接入」页显示/复制的地址已经是上面这个下游端口（网关改写了 `/api/overview` 的 `access` 字段，见 §1）。若你看到的是 `:8666`（飞牛桌面端口），说明装的是旧包。

### 5.3 内存占用与调优

**每个启用的 Qoder 账号 = 一个常驻 Node worker 进程**，内存随账号数线性增长。实测（arm64 / 4GB 设备，2 个账号空闲）：

| 进程 | RSS |
|---|---|
| `node .../worker/src/daemon.mjs`（账号 1） | ≈ 511 MB |
| `node .../worker/src/daemon.mjs`（账号 2） | ≈ 512 MB |
| `cli2api-linux-arm64`（上游，含账号编排） | ≈ 31 MB |
| `fngateway-linux-arm64`（网关适配层） | ≈ 11 MB |

即 **两个账号 ≈ 1.0 GB 都花在 Node worker 上**，Go 侧两个进程合计仅 ~42 MB。

每个 worker 的固定开销来自：把整个 `qodercli.js` bundle 读入并编译、一个常驻的 WASM 上下文、以及模型目录快照；该 WASM 上下文**设计上不释放**（`worker/src/daemon.mjs` 的 `sealContext()` 主动封掉 `free()`），因此内存不会随空闲回落。

自 `0.6.13-1` 起，网关会给每个 worker 注入 V8 老生代上限：
```bash
# 默认 384（MB），每个账号一份，不是总量
NODE_OPTIONS=--max-old-space-size=384
```

可用环境变量覆盖（0 = 不注入，回退 Node 按物理内存自算的默认值）：

```bash
QODER_WORKER_MAX_OLD_SPACE_MB=256      # 2GB 设备建议
QODER_WORKER_MAX_OLD_SPACE_MB=384      # 4GB 设备建议（默认）
QODER_WORKER_MAX_OLD_SPACE_MB=512      # 8GB+ 设备建议
QODER_WORKER_MAX_OLD_SPACE_MB=0        # 关闭注入
```

生效值会写进 `${TRIM_PKGVAR}/panel.log`（`Worker 堆上限:` 一行）。

> **别指望它把 511MB 的基线压下去。** 实测空闲 RSS 511MB 远低于 Node 在 4GB 设备上的默认堆上限（约 2GB），说明**当前占用的大头不是 JS 堆，而是 WASM 实例与 bundle 编译产物**——这些不计入 `--max-old-space-size`。该上限的作用是**兜底**：防止 JS 堆侧随时间无界增长（例如 `daemon.mjs` 的 `rewarmContext()` 会新建 WASM 上下文，而旧上下文的 `free()` 已被 `sealContext()` 封掉），避免把 3.9GB 设备拖进 swap 死亡螺旋。

> **也不是硬上限**：`--max-old-space-size` 只管 V8 老生代，WASM linear memory / ArrayBuffer 属于 external，不计数。设得过小会让 worker OOM 退出并被 `internal/runtime` 反复重启（日志里出现 `JavaScript heap out of memory`），此时上调该值。

**真正能按比例省内存的只有一条：减少同时启用的账号数。** 在控制台禁用不常用的账号会直接停掉对应 worker（`SyncAccount` 的 `true→false` 分支），内存即时归还（每个省 ~511MB）。若这台 3.9GB 设备不是每天都两个账号并发用，把不常用的那个平时禁用是最有效的做法。

### 5.4 数据与日志位置

| 内容 | 路径 |
|---|---|
| 数据目录（`qoder.db`：账号凭证/密钥） | `${TRIM_PKGVAR}`，即 `/vol*/@appdata/cli2api` |
| 生命周期日志（启停） | `${TRIM_PKGVAR}/main.log` |
| 网关日志（面板侧，含子进程起停） | `${TRIM_PKGVAR}/panel.log` |
| 上游日志（cli2api 自身输出） | `${TRIM_PKGVAR}/cli2api.log` |
| 网关 PID | `${TRIM_PKGVAR}/fngateway.pid` |
| 网关 socket | `${TRIM_APPDEST}/cli2api.sock` |

`panel.log` / `cli2api.log` 单文件超过 8MB 会自动轮转为 `.1`。排查账号起不来时先看 `cli2api.log`。

---

## 6. 权限与安全设计

| 项 | 取值 | 说明 |
|---|---|---|
| 运行身份 | `run-as=package`（用户 `cli2api`） | 非 root；不加入任何特权组 |
| fnOS 系统资源 | 无（`config/resource` 为空） | 不申请文件共享、Docker、GPU 等能力 |
| 控制台可见性 | `allUsers=false` + `accessPerm=readonly` | 仅管理员 |
| 控制台密钥 | 存 `qoder.db`，由网关服务端读取注入 | 前端只有占位串，密钥不进浏览器、不进日志 |
| 开放 API Token | 未使用 | 本应用不调用 `/api/v1/trimapp`，无 `TRIM_API_TOKEN` 参与 |
| 下游端口 | `3010`（上游默认端口），仅 `/v1/*` + `/health` | 其余路径一律拒绝，避免控制台从外部裸奔 |
| 身份判定 | 取网关注入的 `X-Trim-Isadmin` | 不信任请求体/查询参数中的身份标记 |

---

## 7. 上游与许可

上游仓库：[`caigee-cmd/cli2api`](https://github.com/caigee-cmd/cli2api)，当前锁定 **v0.6.13**。
版本升级时需同步修改 `build.py` 的 `UPSTREAM_TAG`，并复核 worker 的 CLI 兼容探针。

随包组件的许可随包附带（见打包产物中的 `UPSTREAM.txt` 与 `LICENSE.upstream`）：

- cli2api / fngateway：MIT
- Qoder CLI 组件（`@qoder-ai/qodercli`、`@qodercn-ai/qoderclicn`）：Apache-2.0
- sharp / libvips：Apache-2.0 / LGPL-3.0

> 本应用仅用于私有环境下的个人账号管理，需自备已获授权的 Qoder 账号，请遵守上游服务条款。
