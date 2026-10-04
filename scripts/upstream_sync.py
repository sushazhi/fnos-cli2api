#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上游版本同步：检测 caigee-cmd/cli2api 的新 Release，并把版本落进本仓库。

只依赖标准库，供 .github/workflows/check-upstream.yml 调用（check / bump /
trim-changelog 都在那边跑），也可以本地单跑。

子命令
------
check
    读 build.py 里的 ``UPSTREAM_REPO`` / ``UPSTREAM_TAG``，查上游最新正式
    Release，判断是否需要出新包。结果打印到 stdout，并可用 ``--github-output``
    追加到 GitHub Actions 的 ``$GITHUB_OUTPUT``。输出键（单行，可直接当 step
    output 用）::

        changed=true|false     是否需要进行构建/发布
        tag=v0.6.14            上游 tag（无更新时是当前锁定的 tag）
        version=0.6.14-1       fnOS 包版本（无更新时是当前 manifest 版本）
        reason=upstream|release-missing|forced|none
                               需要构建的原因：上游有新版本 / 当前版本还没有
                               Release（上次发布中断的补发场景）/ 手动 --force /
                               无需构建

    ``changed`` 不只看上游：本仓库若还没有当前版本的 Release（例如上一次运行
    改好了文件、构建或发布却失败了），也会置为 true，好让构建发布流程补上。

    ``--notes-out FILE`` 可把上游 Release 正文原文写到文件，供 ``bump
    --notes-file`` 当 changelog 素材（拿不到就写空文件，不阻塞流程）。

bump
    把指定上游 tag 落进仓库文件：

    * ``build.py``   ``UPSTREAM_TAG`` 及两处说明性注释
    * ``manifest``   ``version = <tag 去掉 v>-1``，并把 ``changelog`` 换成该版本的
      新记录（**只留最新一版**，不累加旧记录）
    * ``README.md``  构建命令示例、安装示例、上游锁定版本

    每条替换都要求**恰好命中 1 处**，且命中后内容必须真的变化；对不上就报错退出。
    不做全局替换 —— README 里还有描述历史事实的版本号（例如「自 0.6.13-1 起，
    网关会给每个 worker 注入 V8 老生代上限」），它记录的是该能力**引入**的版本，
    不是当前版本，改掉它就变成假话。所以只认「当前版本」语义的那三处。

trim-changelog
    把 manifest 的 ``changelog`` 收敛成「只留最新一版」。旧行为是累加（最多留
    10 条），存量仓库里已经攒了历史记录；``bump`` 改成整条替换只对**新**版本
    生效，收敛旧数据要靠这个子命令。幂等，可以每次运行都跑。

    以 ``<br><br>`` 切分后只保留最前面那条；本来就只有一条时原样返回，不改文件。

用法
----
    python scripts/upstream_sync.py check
    python scripts/upstream_sync.py check --github-output "$GITHUB_OUTPUT"
    python scripts/upstream_sync.py bump --tag v0.6.14
    python scripts/upstream_sync.py bump --tag v0.6.14 --notes-file notes.md
    python scripts/upstream_sync.py bump --tag v0.6.14 --root /tmp/copy --dry-run
    python scripts/upstream_sync.py check --force --notes-out notes.md
    python scripts/upstream_sync.py trim-changelog
    python scripts/upstream_sync.py trim-changelog --dry-run

``--root`` 写在子命令**前**或**后**都可以，两种写法等价。
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
API_BASE = "https://api.github.com"

RE_UPSTREAM_REPO = re.compile(r'(?m)^UPSTREAM_REPO\s*=\s*"([^"]+)"')
RE_UPSTREAM_TAG = re.compile(r'(?m)^UPSTREAM_TAG\s*=\s*"([^"]+)"')
RE_MANIFEST_VERSION = re.compile(r'(?m)^(version[ \t]*=[ \t]*)([^\n]*)')
RE_MANIFEST_CHANGELOG = re.compile(r'(?m)^(changelog[ \t]*=[ \t]*)([^\n]*)')
RE_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
RE_CN_HEADING = re.compile(r"(?im)^#{1,6}[ \t]*中文[ \t]*$")
RE_BULLET = re.compile(r"^[-*+][ \t]+")

# manifest 的 changelog 是**一条** <br> 连接的整行，只写最新一版的更新日志。
#
# 不累加历史：manifest 是给应用中心读的展示字段，不是变更日志归档。越攒越长
# 只会让包详情页难读，还会逼近 fnpack 的取值长度；真正的历史在 git 与各版
# Release 里。要查「上一版改了什么」就翻上一个 Release 的说明，不靠这个字段。
# 所以 bump 是**整条替换**，而不是「前插一条、再截掉最旧的几条」。
#
# 这个分隔符只在**处理历史遗留**时用到：统计旧记录条数（写进日志），以及
# trim-changelog 子命令按它切分、只保留最前面那一条。新写入的 changelog 里
# 不会再出现它。
LEGACY_CHANGELOG_SEP = "<br><br>"


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _force_utf8_streams():
    """Windows 控制台默认 GBK，打印 changelog 里的 emoji / 中文会崩。

    只改本进程的流编码，不动 locale；Linux 上本来就是 UTF-8，等于空操作。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def log(msg):
    print(msg, flush=True)


def strip_v(tag):
    return tag[1:] if tag.startswith("v") else tag


def parse_semver(tag):
    """'v0.6.13' → (0, 6, 13)；不匹配返回 None。"""
    m = RE_SEMVER.match(tag.strip()) if tag else None
    return tuple(int(g) for g in m.groups()) if m else None


def read_text(path):
    """按原样读（newline='' 不做换行翻译），保证替换后字节级只差我们改的地方。"""
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write_text(path, text):
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def read_manifest_version(root):
    with open(Path(root) / "manifest", "r", encoding="utf-8", newline="") as f:
        for line in f:
            if line.startswith("version") and "=" in line:
                return line.split("=", 1)[1].strip()
    return ""


def read_upstream_pin(root):
    """从 build.py 读上游仓库与当前锁定的 tag（唯一真源）。"""
    text = read_text(Path(root) / "build.py")
    repo = RE_UPSTREAM_REPO.search(text)
    tag = RE_UPSTREAM_TAG.search(text)
    if not repo:
        die("build.py 里找不到 UPSTREAM_REPO")
    if not tag:
        die("build.py 里找不到 UPSTREAM_TAG")
    return repo.group(1), tag.group(1)


def map_line(text, anchor, fn, what):
    """在**唯一**含 anchor 的行上应用 fn，并要求该行确实发生了变化。

    anchor 不唯一 → 报错（说明文件结构变了，需要人工确认）；
    命中后内容没变 → 也报错（说明版本字面量已经漂移，替换其实是空操作）。
    两种都是「静默改错文件」的前兆，宁可硬失败。
    """
    lines = text.split("\n")
    hits = [i for i, l in enumerate(lines) if anchor in l]
    if len(hits) != 1:
        die(f"{what}: 期望恰好 1 行含锚点 {anchor!r}，实际 {len(hits)} 行")
    i = hits[0]
    new = fn(lines[i])
    if new == lines[i]:
        die(f"{what}: 锚点行未发生变化，版本字面量可能已漂移 —— {lines[i].strip()!r}")
    lines[i] = new
    return "\n".join(lines)


def sub_once(text, pattern, repl, what):
    new, n = pattern.subn(repl, text, count=1)
    if n != 1:
        die(f"{what}: 期望恰好命中 1 处，实际 {n} 处")
    return new


def write_github_output(path, kv):
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write(f"{k}={v}\n")


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------

class ApiError(Exception):
    """GitHub API 调用失败；status 为 0 表示网络层失败（超时/不可达）。"""

    def __init__(self, message, status=0):
        super().__init__(message)
        self.status = status


def api_get(path, token=None):
    """GET 一个 GitHub API 路径，返回解析后的 JSON。

    失败时抛 ApiError（由调用方决定 404 是「正常答案」还是「致命错误」），
    不在这里直接退出 —— 404 在 Release 查询里是常规结果，不该刷一行 ERROR。
    """
    req = urllib.request.Request(
        API_BASE + path,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "fnos-cli2api-upstream-sync",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        raise ApiError(f"HTTP {e.code} {e.reason} {detail}".strip(), e.code) from e
    except Exception as e:
        raise ApiError(str(e), 0) from e


def api_get_or_die(path, token=None):
    try:
        return api_get(path, token)
    except ApiError as e:
        die(f"请求 {path} 失败: {e}")


def latest_release_or_none(repo, token):
    """尽力取上游最新 Release；网络不通等情况返回 None（用于 --force 这种不依赖它的路径）。"""
    try:
        return latest_release(repo, token)
    except SystemExit:
        return None


def latest_release(repo, token):
    """上游最新**正式** Release（draft / prerelease 都排除）。"""
    try:
        return api_get(f"/repos/{repo}/releases/latest", token)
    except ApiError as e:
        if e.status != 404:
            die(f"查询 {repo} 最新 Release 失败: {e}")
        # /releases/latest 在「一个正式 Release 都没有」时是 404，退回列表里挑
    rels = api_get_or_die(f"/repos/{repo}/releases?per_page=100", token)
    cands = [
        r for r in rels
        if not r.get("draft") and not r.get("prerelease") and parse_semver(r.get("tag_name", ""))
    ]
    if not cands:
        die(f"{repo} 没有可用于比较的正式 Release（tag 需形如 vX.Y.Z）")
    return max(cands, key=lambda r: parse_semver(r["tag_name"]))


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

def repo_release_exists(repo, tag, token):
    """本仓库是否已有该 tag 的 Release。

    发布中断（构建失败、Release 创建失败）时，版本已经落进 main，上游却不再
    有「更新」，光看上游就会永远跳过发布。这里补一道检查，让下一次运行把缺的
    Release 补上。查不到仓库信息（本地跑）时返回 None，表示「不判断」。
    """
    if not repo:
        return None
    try:
        api_get(f"/repos/{repo}/releases/tags/{tag}", token)
        return True
    except ApiError as e:
        if e.status == 404:
            return False
        # 仓库查不到 / 网络不通 / 无权限：不判断，交给上游比较结果决定
        log(f"  [提示] 无法查询本仓库 Release（{e}），跳过补发检查")
        return None


def _write_release_notes(repo, tag, out_path):
    """把上游 Release 正文原文写到文件，供创建本仓库 Release 当发布说明。

    拿不到就写空文件：发布说明不是构建的必要条件，不该因此中断流程。
    """
    body = ""
    try:
        rel = api_get(f"/repos/{repo}/releases/tags/{tag}",
                      os.environ.get("GITHUB_TOKEN"))
        body = (rel.get("body") or "").strip()
    except ApiError as e:
        log(f"  [提示] 未能取到上游 Release 正文（{e}），发布说明将为空")
    write_text(Path(out_path), body + "\n" if body else "")
    log(f"发布说明已写入 {out_path}（{len(body)} 字符）")


def cmd_check(args):
    root = Path(args.root).resolve()
    repo, cur_tag = read_upstream_pin(root)
    cur_version = read_manifest_version(root)
    if not cur_version:
        die("无法从 manifest 读取当前版本号")

    cur = parse_semver(cur_tag)
    if not cur:
        die(f"build.py 的 UPSTREAM_TAG={cur_tag!r} 不是 vX.Y.Z 形式，无法比较")

    log(f"上游仓库: {repo}")
    log(f"当前锁定: {cur_tag}（包版本 {cur_version}）")

    # --force 是「重新出一次包」，但若上游正好有更新的版本，就顺手带上新版本，
    # 免得手动触发反而把旧版本又发一遍。查不到上游信息也不影响重建。
    if args.force:
        rel = latest_release_or_none(repo, os.environ.get("GITHUB_TOKEN"))
        latest = parse_semver((rel or {}).get("tag_name", ""))
        if latest and latest > cur:
            out_tag = rel["tag_name"]
            out_version = f"{strip_v(out_tag)}-1"
            log(f"→ --force：上游已有 {out_tag}，顺带构建新版本")
        else:
            out_tag, out_version = cur_tag, cur_version
            log(f"→ --force：按当前锁定版本 {cur_tag} 重建并重新发布")
        if args.notes_out:
            _write_release_notes(repo, out_tag, args.notes_out)
        if args.github_output:
            write_github_output(args.github_output, {
                "changed": "true",
                "reason": "forced",
                "tag": out_tag,
                "version": out_version,
            })
            log(f"已写入 {args.github_output}")
        return 0

    rel = latest_release(repo, os.environ.get("GITHUB_TOKEN"))
    latest_tag = rel.get("tag_name", "")
    latest = parse_semver(latest_tag)
    if not latest:
        die(f"上游最新 Release 的 tag {latest_tag!r} 不是 vX.Y.Z 形式，"
            f"发布规则可能已变，需要人工确认")
    log(f"上游最新: {latest_tag}（发布于 {rel.get('published_at', '?')}）")

    if latest > cur:
        changed, reason, out_tag = "true", "upstream", latest_tag
        out_version = f"{strip_v(latest_tag)}-1"
        log(f"→ 上游有新版本：{cur_tag} → {latest_tag}，将打包 {out_version}")
    elif latest == cur:
        out_tag, out_version = cur_tag, cur_version
        log("→ 上游无新版本")
        exists = repo_release_exists(args.repo or os.environ.get("GITHUB_REPOSITORY"),
                                     out_tag, os.environ.get("GITHUB_TOKEN"))
        if exists is False:
            changed, reason = "true", "release-missing"
            log(f"→ 但本仓库还没有 {out_tag} 的 Release，补一次构建发布")
        else:
            changed, reason = "false", "none"
            log("→ 本仓库已有对应 Release，本次空跑（不构建、不提交、不发布）")
    else:
        # 上游把旧 tag 重发成 Release，或删了新 Release —— 都不是自动该处理的情形
        changed, reason, out_tag, out_version = "false", "none", cur_tag, cur_version
        log(f"→ 上游最新 Release（{latest_tag}）比当前锁定的（{cur_tag}）更旧，"
            f"按无更新处理（如需回退请人工操作）")

    if args.notes_out:
        _write_release_notes(repo, out_tag, args.notes_out)

    if args.github_output:
        write_github_output(args.github_output, {
            "changed": changed,
            "reason": reason,
            "tag": out_tag,
            "version": out_version,
        })
        log(f"已写入 {args.github_output}")
    return 0


# ---------------------------------------------------------------------------
# bump：各文件的落地
# ---------------------------------------------------------------------------

def bump_build_py(root, cur_tag, new_tag, old_version, new_version):
    path = Path(root) / "build.py"
    text = read_text(path)
    old_base, new_base = strip_v(cur_tag), strip_v(new_tag)

    # 1) 真正的功能改动：UPSTREAM_TAG
    text = sub_once(
        text,
        re.compile(r'(?m)^UPSTREAM_TAG[ \t]*=[ \t]*"[^"]*"'),
        f'UPSTREAM_TAG = "{new_tag}"',
        "build.py: UPSTREAM_TAG",
    )

    # 2) 说明性注释，跟着一起改，免得文档和代码对不上
    text = map_line(
        text, "上游（版本与 manifest 的",
        lambda l: re.sub(r"(\d+\.\d+\.\d+)-N", f"{new_base}-N", l),
        "build.py: 上游版本注释",
    )
    text = map_line(
        text, "版本号用上游 tag",
        lambda l: l.replace(old_version, new_version).replace(old_base, new_base),
        "build.py: 二进制版本注释",
    )
    write_text(path, text)
    return f"UPSTREAM_TAG {cur_tag} → {new_tag}"


def sanitize_fragment(s):
    """把上游 release 里的一段文字收拾成能放进 manifest 取值的样子。

    manifest 的取值由 fnpack 解析，ASCII ';' 会被当成终止符截断（build.py 的
    check_manifest_values 会硬失败），所以这里先把 ';' 换成全角；'<br>' 是
    changelog 的结构分隔符，出现在条目里会打乱层级，也一并换掉。
    """
    s = s.replace("<br>", "、").replace("<BR>", "、")
    s = s.replace(";", "；")
    s = re.sub(r"\s+", " ", s).strip()
    s = RE_BULLET.sub("", s)
    return s.strip()


def extract_cn_bullets(body):
    """取上游 Release 说明里「## 中文」小节的项目符号条目。

    上游 Release body 是双语惯例：先 '## English' 再 '## 中文'，各是一组 '- ' 条目。
    """
    if not body:
        return []
    m = RE_CN_HEADING.search(body)
    section = body[m.end():] if m else body
    out = []
    for line in section.splitlines():
        t = line.strip()
        if RE_BULLET.match(t):
            out.append(RE_BULLET.sub("", t))
    return out


def build_entry(version, upstream_tag, bullets):
    """组装 changelog 记录（整条，不含历史）。

    这是 manifest ``changelog`` 的**全部**内容 —— 不拼接旧记录。
    """
    items = [f"上游 cli2api 同步至 {upstream_tag}（零源码改动）"] + list(bullets)
    body = "<br>".join(f"{i}.{t}" for i, t in enumerate(items, 1))
    return f"v{version}<br>{body}"


def bump_manifest(root, version, entry):
    path = Path(root) / "manifest"
    text = read_text(path)
    notes = []

    # 1) version
    def set_version(m):
        old = m.group(2)
        cr = "\r" if old.endswith("\r") else ""
        if old.rstrip("\r") == version:
            die(f"manifest: version 已经是 {version}，无需 bump")
        return m.group(1) + version + cr

    text = sub_once(text, RE_MANIFEST_VERSION, set_version, "manifest: version")

    # 2) changelog **整条替换**成新记录：只保留最新一版，不累加历史。
    #    幂等：已经是本条就原样返回（release-missing 补发场景会走到这里）。
    def set_changelog(m):
        old = m.group(2)
        cr = "\r" if old.endswith("\r") else ""
        old_val = old.rstrip("\r")
        if old_val == entry:
            notes.append("changelog 已是该版本记录，未改动")
            return m.group(1) + old_val + cr
        dropped = old_val.count(LEGACY_CHANGELOG_SEP) + 1 if old_val else 0
        if dropped:
            notes.append(f"changelog 替换为最新一版（移除 {dropped} 条旧记录）")
        else:
            notes.append("changelog 写入最新一版记录")
        return m.group(1) + entry + cr

    text = sub_once(text, RE_MANIFEST_CHANGELOG, set_changelog, "manifest: changelog")

    # 3) fnpack 会在 ASCII ';' 处截断取值，落地前先自查一遍
    for line in text.split("\n"):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, val = line.split("=", 1)
        i = val.find(";")
        if i >= 0:
            die(f"manifest: {key.strip()} 的取值里有 ASCII ';'（第 {i} 个字符），"
                f"fnpack 会在此截断，请改写该值")

    write_text(path, text)
    return f"version → {version}，changelog 更新为最新一版（只保留该条）"


def bump_readme(root, old_version, new_version, cur_tag, new_tag):
    """只改「当前版本」语义的三处；历史性版本号（如「自 X 起」）保持不动。"""
    path = Path(root) / "README.md"
    text = read_text(path)

    text = map_line(
        text, "python build.py --version",
        lambda l: l.replace(old_version, new_version),
        "README: 构建命令示例",
    )
    text = map_line(
        text, "appcenter-cli install-fpk cli2api-",
        lambda l: l.replace(old_version, new_version),
        "README: 安装命令示例",
    )
    text = map_line(
        text, "当前锁定",
        lambda l: l.replace(cur_tag, new_tag),
        "README: 上游锁定版本",
    )
    write_text(path, text)
    return f"版本引用 {old_version} → {new_version}，锁定 tag {cur_tag} → {new_tag}"


def trim_manifest_changelog(root):
    """把 manifest 的 changelog 收敛成「只留最新一版」，返回一句人类可读的结果描述。

    存量仓库的 changelog 是历史累加出来的（旧行为会一直前插、最多留 10 条）。
    光把 bump 改成整条替换，只对新版本生效，已写进去的旧记录会永远留在那儿。
    这个子命令用来做一次性（以及之后每次运行都幂等）的收敛。

    只动「最前面那一条」：以 <br><br> 切分，保留 parts[0]。切分前先确认它真的
    有 ≥2 条，否则原样返回不动 —— 万一将来改成别的分隔方式，宁可什么都不做
    也不要乱切。返回的字符串里会写明移除了几条旧记录，方便看日志。
    """
    path = Path(root) / "manifest"
    text = read_text(path)

    state = {"changed": False, "dropped": 0, "noop": ""}

    def fix(m):
        old = m.group(2)
        cr = "\r" if old.endswith("\r") else ""
        old_val = old.rstrip("\r")
        parts = old_val.split(LEGACY_CHANGELOG_SEP)
        if len(parts) < 2:
            state["noop"] = "changelog 已只有一条，无需收敛"
            return m.group(1) + old_val + cr
        state["changed"] = True
        state["dropped"] = len(parts) - 1
        return m.group(1) + parts[0] + cr

    text = sub_once(text, RE_MANIFEST_CHANGELOG, fix, "manifest: changelog")

    if not state["changed"]:
        return state["noop"] or "changelog 无需收敛"

    write_text(path, text)
    return f"changelog 收敛为最新一版（移除 {state['dropped']} 条旧记录）"


def cmd_trim_changelog(args):
    root = Path(args.root).resolve()
    log(f"收敛 {root / 'manifest'} 的 changelog 为「只留最新一版」")
    log(f"  → {trim_manifest_changelog(root)}")
    return 0


def read_notes(args, repo, tag):
    """取新版本的 changelog 素材：优先 --notes-file，否则读上游 Release body。"""
    if args.notes_file:
        body = read_text(args.notes_file)
        src = args.notes_file
    else:
        rel = api_get_or_die(f"/repos/{repo}/releases/tags/{tag}", os.environ.get("GITHUB_TOKEN"))
        body = rel.get("body") or ""
        src = f"{repo} Release {tag}"

    bullets = [sanitize_fragment(b) for b in extract_cn_bullets(body)]
    bullets = [b for b in bullets if b]
    if not bullets:
        log(f"  [提示] 未能从 {src} 提取到中文条目，changelog 只写同步说明")
    else:
        log(f"  从 {src} 提取到 {len(bullets)} 条中文条目")
    return bullets


def cmd_bump(args):
    root = Path(args.root).resolve()
    repo, cur_tag = read_upstream_pin(root)
    new_tag = args.tag.strip()

    if not parse_semver(new_tag):
        die(f"--tag 必须是 vX.Y.Z 形式，收到 {new_tag!r}")
    cur, new = parse_semver(cur_tag), parse_semver(new_tag)
    if new == cur:
        log(f"上游 tag 已经是 {new_tag}，无需更新")
        return 0
    if new < cur:
        die(f"新 tag {new_tag} 比当前锁定的 {cur_tag} 更旧，拒绝回退")

    old_version = read_manifest_version(root)
    new_version = f"{strip_v(new_tag)}-1"
    if not old_version:
        die("无法从 manifest 读取当前版本号")

    log(f"上游 {cur_tag} → {new_tag}")
    log(f"包版本 {old_version} → {new_version}")

    bullets = read_notes(args, repo, new_tag)
    entry = build_entry(new_version, new_tag, bullets)

    targets = [
        ("build.py", bump_build_py(root, cur_tag, new_tag, old_version, new_version)),
        ("manifest", bump_manifest(root, new_version, entry)),
        ("README.md", bump_readme(root, old_version, new_version, cur_tag, new_tag)),
    ]

    log("")
    log("已更新：")
    for name, what in targets:
        log(f"  - {name}: {what}")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_root_arg(parser, suppress_default=False):
    """给主解析器和各子命令都挂上 --root，让「写在子命令前」和「写在子命令后」都成立。

    子命令上的那一份用 SUPPRESS 当默认值：argparse 只在属性不存在时才填默认值，
    而主解析器已经把 root 放进 namespace 了，所以「子命令没给 --root」时不会把
    主解析器的值覆盖掉。两种写法因此等价，不再是「写错位置就 unrecognized
    arguments」。
    """
    parser.add_argument(
        "--root",
        default=argparse.SUPPRESS if suppress_default else str(DEFAULT_ROOT),
        help="仓库根目录（默认按脚本位置推断；写在子命令前或后都可以）",
    )


def main():
    _force_utf8_streams()
    ap = argparse.ArgumentParser(
        description="上游版本同步（检测 + 落地）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_root_arg(ap)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="查上游是否有新版本")
    _add_root_arg(p_check, suppress_default=True)
    p_check.add_argument("--github-output", default="",
                         help="把 changed/reason/tag/version 追加写入该文件（$GITHUB_OUTPUT）")
    p_check.add_argument("--repo", default="",
                         help="本仓库（owner/name），用于检查 Release 是否已存在；"
                              "缺省读 $GITHUB_REPOSITORY")
    p_check.add_argument("--notes-out", default="",
                         help="把上游 Release 正文写入该文件（供创建 Release 时当发布说明）")
    p_check.add_argument("--force", action="store_true",
                         help="忽略上游比较，按当前锁定版本重新构建发布（手动触发用）")
    p_check.set_defaults(func=cmd_check)

    p_bump = sub.add_parser("bump", help="把指定上游 tag 落进仓库文件")
    _add_root_arg(p_bump, suppress_default=True)
    p_bump.add_argument("--tag", required=True, help="新的上游 tag，例如 v0.6.14")
    p_bump.add_argument("--notes-file", default="",
                        help="changelog 素材（上游 Release body 或纯条目）；缺省时自行拉取")
    p_bump.add_argument("--dry-run", action="store_true", help="只打印将要做的改动，不写文件")
    p_bump.set_defaults(func=cmd_bump)

    p_trim = sub.add_parser("trim-changelog",
                            help="把 manifest 的 changelog 收敛成只留最新一版（幂等）")
    _add_root_arg(p_trim, suppress_default=True)
    p_trim.add_argument("--dry-run", action="store_true", help="只打印将要做的改动，不写文件")
    p_trim.set_defaults(func=cmd_trim_changelog)

    args = ap.parse_args()

    if getattr(args, "dry_run", False):
        # 在临时副本上跑一遍，把 diff 打出来 —— 避免「先改了才想起来要预览」
        import shutil
        import tempfile
        src_root = Path(args.root).resolve()
        tmp = Path(tempfile.mkdtemp(prefix="upstream-sync-dry-"))
        try:
            for rel in ("build.py", "manifest", "README.md"):
                shutil.copy2(src_root / rel, tmp / rel)
            args.root = str(tmp)
            args.dry_run = False
            args.func(args)
            for rel in ("build.py", "manifest", "README.md"):
                a = read_text(src_root / rel).splitlines(keepends=True)
                b = read_text(tmp / rel).splitlines(keepends=True)
                diff = list(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}", n=2))
                if diff:
                    log("")
                    sys.stdout.writelines(diff)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return 0

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
