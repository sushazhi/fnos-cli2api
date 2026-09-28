#!/usr/bin/env python3
"""把官方图标 assets/apple-touch-icon.svg 渲染成 fnOS 应用需要的各尺寸 PNG。

用法（在仓库根目录执行）:

    python assets/render-icons.py

图标真源是上游仓库的官方 SVG（frontend/public/apple-touch-icon.svg，随仓库
提交一份副本），本脚本负责把它栅格化成 fnOS 要求的 64/256 两种尺寸：

    ICON.PNG                    (64)   包根图标
    ICON_256.PNG                (256)  包根高清图标
    app/ui/images/icon_64.png   (64)   桌面入口图标
    app/ui/images/icon_256.png  (256)  桌面入口高清图标
    assets/icon-master.png      (964)  设计源文件（仅供再生成，不进包）

渲染交给本机 Chrome/Edge 的 headless 截图：每个尺寸都按目标像素**直接**
栅格化（不先渲大图再缩小），64px 小图标上的圆弧因此仍是干净的抗锯齿。
SVG 的根元素带 width/height="180"，会被 HTML 里的 CSS 尺寸覆盖，这里先删掉
这两个属性，让 viewBox 决定缩放；窗口尺寸与 SVG 尺寸严格相等，截图即成品。
"""
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
SVG = os.path.join(HERE, "apple-touch-icon.svg")

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
]

# (输出路径, 边长)
TARGETS = (
    ("ICON.PNG", 64),
    ("ICON_256.PNG", 256),
    ("app/ui/images/icon_64.png", 64),
    ("app/ui/images/icon_256.png", 256),
    ("assets/icon-master.png", 964),
)

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>CLI2API icon</title>
<style>
  html,body{{margin:0;padding:0;background:transparent;overflow:hidden}}
  svg{{display:block;width:{s}px;height:{s}px}}
</style></head>
<body>{svg}</body></html>
"""


def browser():
    for b in BROWSERS:
        if os.path.exists(b):
            return b
    sys.exit("ERROR: 未找到 Chrome/Edge，无法渲染 SVG（图标随仓库提交，"
             "没有浏览器时可沿用已有的 PNG）")


def render(exe, svg_text, size, out_path, workdir):
    html_path = os.path.join(workdir, f"icon-{size}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(HTML.format(s=size, svg=svg_text))
    if os.path.exists(out_path):
        os.remove(out_path)
    profile = os.path.join(workdir, f"profile-{size}")
    cmd = [
        exe, "--headless=new", "--disable-gpu", "--hide-scrollbars",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--force-device-scale-factor=1", "--default-background-color=00000000",
        f"--window-size={size},{size}", f"--user-data-dir={profile}",
        f"--screenshot={out_path}", "file:///" + html_path.replace("\\", "/"),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if not os.path.exists(out_path):
        sys.exit(f"ERROR: 渲染 {size}px 失败: "
                 f"{proc.stderr.decode('utf-8', 'replace')[:800]}")


def verify(out_path, size):
    """尺寸/透明角/官方配色三项自检 —— 渲染歪了必须当场发现。"""
    from PIL import Image
    im = Image.open(out_path)
    rgba = im.convert("RGBA")
    if rgba.size != (size, size):
        sys.exit(f"ERROR: {out_path} 尺寸应为 {size}x{size}，实际 {rgba.size}")
    corners = [rgba.getpixel(xy) for xy in
               ((0, 0), (size - 1, 0), (0, size - 1), (size - 1, size - 1))]
    if any(px[3] != 0 for px in corners):
        sys.exit(f"ERROR: {out_path} 四角必须透明（圆角外的留白），实际 {corners}")
    colors = {c for _, c in rgba.convert("RGB").getcolors(maxcolors=1 << 24)}
    for want in ((24, 24, 27), (252, 252, 252), (34, 211, 238)):  # #18181B / #FCFCFC / #22D3EE
        if want not in colors:
            sys.exit(f"ERROR: {out_path} 缺少官方配色 #{''.join(f'{c:02X}' for c in want)}")


def main():
    if not os.path.exists(SVG):
        sys.exit(f"ERROR: 缺少图标真源 {SVG}")
    with open(SVG, encoding="utf-8") as f:
        # 根元素上的 width/height 会压过 CSS 尺寸，去掉后由 viewBox 决定缩放。
        svg_text = re.sub(r'\s(width|height)="[^"]*"', "", f.read(), count=2)
    exe = browser()
    print(f"  渲染器: {exe}")
    workdir = os.path.join(PROJECT, ".local-build", "icon-render")
    os.makedirs(workdir, exist_ok=True)
    for rel, size in TARGETS:
        out = os.path.join(PROJECT, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        render(exe, svg_text, size, out, workdir)
        verify(out, size)
        print(f"  {rel}: {size}x{size}")


if __name__ == "__main__":
    main()
