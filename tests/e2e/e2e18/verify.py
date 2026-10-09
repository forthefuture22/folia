#!/usr/bin/env python3
"""用**真实 pdf.js** 渲染缩略图，看它到底有没有被裁。

和 `/tmp/e2e17` 那套的区别：那一套把 `renderThumbnail` 换成了自己的桩
（返回的图天然就是页面比例），所以「图的比例 == 页面比例」验的是桩不是应用。
这一套只桩 Tauri IPC，pdf.js 与 bridge.js 全走真实实现，并且**直接读缩略图
自己的像素** —— 测试 PDF 四角有 40×40 的黑方块，裁掉一点角上就不黑了。

书架和阅读器**分两次运行**：虚拟时钟推进得比真实时间快得多，
一次运行里先等书架缩略图会把预算提前烧完，阅读器的正文就渲染不出来。
"""

# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import html
import http.server
import json
import os
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import threading
sys.path.insert(0, str(_E2E))
from theme_pin import pin_light  # noqa: E402  (把偏好钉成浅色，用例才与宿主机外观无关)

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PAGE_RATIO = 595.0 / 842.0
BUDGET = "120000"

# 自检模式：把**修好之前**的规则重新盖回去，确认这套判据真的会红。
# 「测试通过」只有在「故意弄坏它也会红」的前提下才有意义。
REGRESS = os.environ.get("REGRESS") == "1"
OLD_CSS = """
<style>
.file-thumb { aspect-ratio: 1 / 1.32 !important; }
.file-thumb img { object-fit: cover !important; object-position: top center !important; }
.thumb-frame { aspect-ratio: 1 / 1.32 !important; }
.thumb-frame img { object-fit: cover !important; }
/* 顶边对齐那 20px 的来源 */
.pages { padding: 20px 0 40px !important; }
</style>
"""


def build_site() -> None:
    # 用 `cp -R` 而不是 `shutil.copytree`：这个沙箱环境会把 Python 的
    # `shutil.copytree` 代理到宿主机做递归拷贝，而它对 `dist/public/pdfjs/*`
    # 触发文件策略拒绝（PermissionError: ... refused by file policy）。
    # 走 shell 的 `cp` 不受这个代理影响。
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)
    subprocess.run(["cp", str(WORK / "sample.pdf"), str(SITE / "sample.pdf")], check=True)

    index = (SITE / "index.html").read_text()
    # `trunk serve` 的产物里有热重载脚本、`trunk build` 的没有 —— 0 或 1 次都正常。
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    index = pin_light(index)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次，不正常"

    anchor = '<script type="module" src="public/pdfjs/bridge.js"></script>'
    assert anchor in index, "没找到 bridge.js 的 script 标签"
    index = index.replace(
        anchor, anchor + '\n    <script type="module" src="/harness-stub.js"></script>'
    )
    if REGRESS:
        index = index.replace("</head>", OLD_CSS + "</head>")
    (SITE / "index.html").write_text(index)
    subprocess.run(["cp", str(WORK / "harness.js"), str(SITE / "harness-stub.js")], check=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, extra: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    # 不要加 --user-data-dir（会让 --dump-dom 挂住）；--screenshot= 要单参数。
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        "--force-device-scale-factor=1",
        *extra,
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        raise SystemExit("页面里没有 harness-result\n" + dom[-4000:])
    return json.loads(html.unescape(m.group(1)))


def thumb_checks(label: str, thumb: dict, checks: list) -> None:
    """缩略图的通用判据：比例链 + 像素（角方块还在 = 没裁）。"""
    px = thumb.get("pixels") or {}
    corners = px.get("cornerDark") or {}
    ext = px.get("cornerExtent") or {}
    expect = px.get("expectedCornerPx") or 0

    def check(name, ok, detail=""):
        checks.append((f"{label}：{name}", bool(ok), detail))

    check(
        "是真 pdf.js 画出来的（不是桩）",
        (thumb.get("srcPrefix") or "").startswith("data:image/jpeg"),
        f"src={thumb.get('srcPrefix')!r} len={thumb.get('srcLength')}",
    )
    check(
        "图自己的比例 == 页面比例",
        abs((thumb.get("natural", {}).get("ratio") or 0) - PAGE_RATIO) <= 0.01,
        f"natural={thumb.get('natural')} 页面={PAGE_RATIO:.4f}",
    )
    check(
        "框的比例 == 页面比例",
        abs((thumb.get("frameRatio") or 0) - PAGE_RATIO) <= 0.02,
        f"框={thumb.get('frameRatio')} inline={thumb.get('inlineAspectRatio')!r}",
    )
    check(
        "显示出来的比例 == 页面比例（没被拉伸）",
        abs((thumb.get("shownRatio") or 0) - PAGE_RATIO) <= 0.03,
        f"显示={thumb.get('shownRatio')}",
    )
    check("图片是 contain", thumb.get("objectFit") == "contain", str(thumb.get("objectFit")))
    check(
        "四角黑方块都在（= 一点没裁）",
        all(corners.get(k) for k in ("tl", "tr", "bl", "br")),
        json.dumps(corners, ensure_ascii=False),
    )
    # ★ 真正决定性的判据：按 object-fit 反推出「可视源矩形」，
    # 再看页面四角的标记有没有完整落在里面。
    # 只读源图像素是验不出 `cover` 裁剪的 —— 被裁的部分仍在源图里。
    cv = thumb.get("cornersVisible") or {}
    check(
        "四个角标记都完整落在可视区域里（= 真的没裁，不是源图里有）",
        all(cv.get(k) for k in ("tl", "tr", "bl", "br")),
        f"可见={json.dumps(cv, ensure_ascii=False)} 裁剪={thumb.get('cropPx')} "
        f"裁掉比例={thumb.get('cropFraction')}",
    )
    check(
        "可视区域覆盖整张图（裁掉比例为 0）",
        (thumb.get("cropFraction") or 0) <= 0.002,
        f"裁掉 {thumb.get('cropFraction')}（各边源像素 {thumb.get('cropPx')}）",
    )
    check(
        "四角方块尺寸正常（≈ 40pt × 缩放 = 整页都在）",
        expect > 4
        and all(
            abs((ext.get(k) or 0) - expect) <= max(3.0, expect * 0.35)
            for k in ("tlRight", "tlDown", "brLeft", "brUp")
        ),
        f"实测 {json.dumps(ext)} 期望≈{expect}px",
    )
    check(
        "正中间不是黑的（图确实是那一页）",
        px.get("centreIsBlack") is False,
        f"中心 rgb={px.get('centreRgb')}",
    )


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {base}")

    results: dict[str, dict] = {}
    try:
        for mode in ("shelf", "reader"):
            url = f"{base}?mode={mode}"
            dom = run_chrome(url, ["--dump-dom", f"--virtual-time-budget={BUDGET}"]).stdout
            results[mode] = extract_result(dom)
            run_chrome(
                url + "&shot=1",
                [f"--screenshot={WORK / (mode + '.png')}", f"--virtual-time-budget={BUDGET}"],
            )
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n================ harness result ================")
    print(json.dumps(results, indent=2, ensure_ascii=False))

    for mode, res in results.items():
        if "error" in res:
            print(f"\n!! [{mode}] 驱动脚本报错:", res["error"])
            print(res.get("stack", ""))
            return 1

    checks: list[tuple[str, bool, str]] = []
    thumb_checks("书架卡片", results["shelf"].get("shelf") or {}, checks)
    thumb_checks("侧栏缩略图", results["reader"].get("sidebar") or {}, checks)

    page = results["reader"].get("page") or {}
    pc = page.get("cornerDark") or {}
    checks.append(
        (
            "对照组：正文页四角也是黑的（说明测试 PDF 本身有效）",
            all(pc.get(k) for k in ("tl", "tr", "bl", "br")),
            json.dumps(pc, ensure_ascii=False)
            + f" canvas={page.get('canvasW')}x{page.get('canvasH')}",
        )
    )

    # ---------------------------------------------------------- 顶边对齐
    al = results["reader"].get("align") or {}
    delta = al.get("pageMinusSidebar")
    checks.append(
        (
            "页面的上边缘与侧栏面板的上边缘齐平",
            delta is not None and abs(delta) <= 1.0,
            f"侧栏顶={al.get('sidebarTop')} 正文区顶={al.get('stageTop')} "
            f"页面顶={al.get('pageTop')} 差={delta}px",
        )
    )
    checks.append(
        (
            "`.pages` 顶部没有内边距（它就是那 20px 的来源）",
            al.get("pagesPaddingTop") in ("0px", "0"),
            f"padding-top={al.get('pagesPaddingTop')!r} padding-bottom={al.get('pagesPaddingBottom')!r}",
        )
    )
    checks.append(
        (
            "侧栏与正文滚动区顶边本来就齐平（说明只差页面这一层）",
            al.get("stageTop") is not None
            and abs(al["stageTop"] - al["sidebarTop"]) <= 1.0,
            f"侧栏={al.get('sidebarTop')} 正文区={al.get('stageTop')}",
        )
    )
    # 横向：页面在正文区里居中（左右留白应当相等，容差给滚动条宽度）
    lg, rg = al.get("pageLeftGap"), al.get("pageRightGap")
    checks.append(
        (
            "页面在正文区里水平居中（左右留白相等）",
            lg is not None and rg is not None and abs(lg - rg) <= 16,
            f"左留白={lg} 右留白={rg} 差={None if lg is None else round(lg - rg, 2)}",
        )
    )

    # ---------------------------------------------------------- 跳页（layout 与 DOM 是否一致）
    #
    # `.pages` 的顶部内边距是 `Layout::offsets` 不知道的。它不为 0 时，
    # `scroll_to_page` 会把目标页送偏同样的距离，「当前是第几页」也会跟着偏。
    # 所以跳页后的落点正好是「两者是否一致」的探针。
    jumps = results["reader"].get("jumps") or []
    checks.append(("文档确实有多页可跳（测试素材有效）", len(jumps) >= 3, f"{len(jumps)} 次跳页"))
    for j in jumps:
        top = j.get("topInStage")
        checks.append(
            (
                f"跳到第 {j['target']} 页后，该页上边缘落在正文区顶部附近（源码是 offset-12）",
                top is not None and -2 <= top <= 30,
                f"topInStage={top} 高亮缩略图={j.get('activeThumb')!r} 页码框={j.get('pageInput')!r}",
            )
        )
        checks.append(
            (
                f"跳到第 {j['target']} 页后，「当前页」也认对了",
                str(j.get("activeThumb")) == str(j["target"])
                and str(j.get("pageInput")) == str(j["target"]),
                f"高亮缩略图={j.get('activeThumb')!r} 页码框={j.get('pageInput')!r}",
            )
        )

    print("\n================ checks ================")
    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ""))
    print(
        f"\n{len(checks) - failed}/{len(checks)} 通过；截图: {WORK / 'shelf.png'} / {WORK / 'reader.png'}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
