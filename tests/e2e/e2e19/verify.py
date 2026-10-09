#!/usr/bin/env python3
"""验证顶栏改动（/tmp/e2e19，新设计）。

1. 「工具」pill 里的 sparkles（星星）去掉了；
2. 它右边那个按钮改成「展开全部缩略图」—— 点击盖出**页面总览**浮层；
3. 「打开其它 PDF」搬到了标签栏右端的 + 号；
4. 批注面板空态那句「选中正文后会浮出一排工具…」删掉了。

做法和 /tmp/e2e17 一样：拷一份 `dist/`、往 index.html 里插一段桩（只桩 Tauri IPC），
然后用无头 Chrome 把真实 wasm 应用跑起来，模拟点击，核对结果。
点击走 `elementFromPoint()` 命中测试，和真实鼠标一致。
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
import pathlib
import re
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

PAGES = 30
THUMB_TITLE = "展开全部缩略图（快速跳页）"


def build_site() -> None:
    # 用 `cp -R` 而不是 `shutil.copytree`：沙箱会把 Python 的 copytree 代理到宿主机，
    # 对 `dist/public/pdfjs/*` 触发文件策略拒绝。
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

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
    (SITE / "index.html").write_text(index)
    subprocess.run(["cp", str(WORK / "harness.js"), str(SITE / "harness-stub.js")], check=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, extra: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
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
    match = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not match:
        raise SystemExit("页面里没有 harness-result —— 驱动脚本没跑完\n" + dom[-3000:])
    return json.loads(html.unescape(match.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {url}")

    try:
        dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=60000"]).stdout
        result = extract_result(dom)
        run_chrome(url, [f"--screenshot={WORK / 'toolbar.png'}", "--virtual-time-budget=60000"])
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n================ harness result ================")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if "error" in result:
        print("\n!! 驱动脚本报错:", result["error"])
        print(result.get("stack", ""))
        return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ---------------------------------------------------------------- 顶栏左侧
    check(
        "「工具」pill 里的星星图标删掉了",
        result.get("pillSvgCount") == 0,
        f"pill 内 svg 数={result.get('pillSvgCount')} 文案={result.get('pillText')!r}",
    )
    check(
        "「工具」pill 文案还在（不是把整个 pill 删了）",
        result.get("pillText") == "工具",
        repr(result.get("pillText")),
    )
    check(
        "工具右边那个按钮是「展开全部缩略图」",
        result.get("thumbBtnTitle") == THUMB_TITLE,
        f"title={result.get('thumbBtnTitle')!r}",
    )
    check(
        "它的图标是 pages（4 个矩形），不再是 cards（2 个）",
        result.get("thumbBtnHasSvg") is True and result.get("thumbBtnRects") == 4,
        f"svg={result.get('thumbBtnHasSvg')} rect 数={result.get('thumbBtnRects')}",
    )
    check(
        "阅读器工具栏里不再有「打开其它 PDF」",
        result.get("leftStillHasOpenOther") is False,
        f"还在={result.get('leftStillHasOpenOther')}",
    )
    check(
        "按钮能被真实点击命中（elementFromPoint 落在它身上）",
        result.get("thumbBtnTopIsSelf") is True,
        f"命中的是={result.get('thumbBtnTopIsSelf')}",
    )

    # ---------------------------------------------------------------- 标签栏的 +
    check("标签栏右端有 + 按钮", result.get("addBtnExists") is True)
    check(
        "+ 按钮就是「打开其它 PDF」",
        result.get("addBtnTitle") == "打开其它 PDF",
        f"title={result.get('addBtnTitle')!r}",
    )
    check(
        "+ 按钮挂在标签栏里、而且是最后一个元素（在最右端）",
        result.get("addBtnInTabbar") is True and result.get("addBtnIsLast") is True,
        f"在 tabbar 内={result.get('addBtnInTabbar')} 是最后一个={result.get('addBtnIsLast')}",
    )

    # ---------------------------------------------------------------- 批注空态
    check("图标轨上有「批注」按钮", result.get("railCommentExists") is True)
    check(
        "切到批注面板后确实停在批注上（这一步不是空验证）",
        result.get("tabAfterRail") == "批注",
        f"active={result.get('tabAfterRail')!r}",
    )
    check(
        "批注空态那句提示删掉了",
        result.get("annHasHint") is False,
        f"正文={result.get('annBodyText')!r}",
    )
    check(
        "批注空态现在什么都不显示",
        result.get("annBodyText") == "" and result.get("annEmptyEls") == 0,
        f"正文={result.get('annBodyText')!r} 空态元素数={result.get('annEmptyEls')}",
    )

    # ---------------------------------------------------------------- 点之前的状态
    # 这条是"为什么用户会觉得点不动"的证据：侧栏默认就开着、默认就在缩略图页。
    check(
        "点之前侧栏本来就是开着的、本来就在缩略图页（所以才显得按钮没反应）",
        result.get("sidebarOpenAtStart") is True
        and result.get("activeTabAtStart") == "页面缩略图",
        f"侧栏开着={result.get('sidebarOpenAtStart')} 页签={result.get('activeTabAtStart')!r}",
    )
    check(
        "点之前总览浮层是关着的（判据不是空验证）",
        result.get("overviewBeforeClick") is False,
        f"浮层={result.get('overviewBeforeClick')}",
    )

    # ---------------------------------------------------------------- 页面总览浮层
    check(
        "点一下盖出「页面总览」浮层",
        result.get("overviewAfterClick") is True,
        f"浮层={result.get('overviewAfterClick')}",
    )
    check(
        "总览的格子数 = 文档页数（30），一屏排全",
        result.get("poItemsAfterClick") == PAGES,
        f"格子数={result.get('poItemsAfterClick')} / {PAGES}",
    )
    check(
        "标题写着总页数",
        result.get("poTitle") == f"全部页面 · 共 {PAGES} 页",
        f"标题={result.get('poTitle')!r}",
    )
    check(
        "刚打开时缩略图还没全好 —— 但有进度文案（不是一片空白）",
        isinstance(result.get("poImgsRightAfterClick"), int)
        and result["poImgsRightAfterClick"] <= PAGES
        and isinstance(result.get("poProgress"), str)
        and "生成" in (result.get("poProgress") or ""),
        f"刚打开已填={result.get('poImgsRightAfterClick')} 进度={result.get('poProgress')!r}",
    )
    check(
        "30 页的缩略图最后全都填进网格",
        result.get("poImgsAfterWait") == PAGES,
        f"填好={result.get('poImgsAfterWait')} / {PAGES}",
    )
    check(
        "填完后进度文案变成「全部已生成」",
        result.get("poProgressAfter") == "全部已生成",
        f"进度={result.get('poProgressAfter')!r}",
    )
    check(
        "「当前页」那一格被标出来了（正好一格）",
        result.get("poCurrentCount") == 1,
        f"标出的格数={result.get('poCurrentCount')}",
    )

    # ---------------------------------------------------------------- 交互
    check(
        "点网格里的第 5 页 → 跳到第 5 页",
        result.get("pageAfterPick") == "5",
        f"页码框={result.get('pageAfterPick')!r}",
    )
    check(
        "跳完浮层自己收起来",
        result.get("overviewAfterPick") is False,
        f"浮层={result.get('overviewAfterPick')}",
    )
    check(
        "再点一次能重新打开（不是一次性的）",
        result.get("overviewReopened") is True,
        f"浮层={result.get('overviewReopened')}",
    )
    check(
        "点遮罩空白处会关闭",
        result.get("overviewAfterBackdrop") is False,
        f"浮层={result.get('overviewAfterBackdrop')}",
    )
    check(
        "Esc 会关闭总览",
        result.get("overviewAfterEsc") is False,
        f"浮层={result.get('overviewAfterEsc')}",
    )
    check(
        "关掉总览后**没有**顺手退回书架（Esc 只关最上面那层）",
        result.get("stillInReaderAfterEsc") is True and result.get("backAtLibrary") is False,
        f"还在阅读器={result.get('stillInReaderAfterEsc')} 回书架了={result.get('backAtLibrary')}",
    )

    print("\n================ checks ================")
    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ""))

    print(f"\n{len(checks) - failed}/{len(checks)} 通过；截图: {WORK / 'toolbar.png'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
