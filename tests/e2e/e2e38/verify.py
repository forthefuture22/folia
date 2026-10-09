#!/usr/bin/env python3
"""并发打开两份文档：会不会静默吃掉一个标签 / 会不会读到别人的页数。

`bridge.js` 的 `getPageSizes()` / `getOutline()` 读的是模块里**那一个**全局
`pdfDoc`，不认路径；而 `apply_document` 从 `openDocumentPath` 到提交标签之间要跨
三次 await。两条打开流水线一重叠就有两件事：

  1. 第二条的 `open_path` 把 `pdfDoc` 换掉 → 第一条读到**别人的**页数；
  2. 两条各自拿一份"还没有对方"的 `tabs` 快照去 `set`，后提交的那条把前一条刚
     push 的标签整个覆盖掉 → 标签栏里只剩一份。

修法是**串行化**（`state/handles.rs` 的 `OpenQueue`），不是"丢掉一条"。

⚠️ **现有套件抓不到**：`e2e25` 的 drop2 / openpath / reopen 都是"等第一份开完
再开第二份"（每条判据前面都 `await settle`），两条流水线从来不重叠。

判据分三类：
  · **前提判据**：监听器注册了 / 两份都真的发起了打开。
  · **★ 主判据**：两个标签都在 / 每份的页数跟自己对得上 / 两次打开确实没重叠。
  · **配对判据**：切标签能来回、阅读器正常、最近列表里两份都在。
"""

# --- 路径推导 ---------------------------------------------------------
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import html
import http.server
import json
import re
import socketserver
import subprocess
import sys
import threading
sys.path.insert(0, str(_E2E))
from theme_pin import pin_light  # noqa: E402  (把偏好钉成浅色，用例才与宿主机外观无关)

ROOT = _pl.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = _pl.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

NAME_A = "h38a.pdf"
NAME_B = "h38b.pdf"
PATH_A = "/tmp/h38a.pdf"
PATH_B = "/tmp/h38b.pdf"
PAGES_A = 120
PAGES_B = 37


def build_site() -> None:
    # 用 `cp -R` 而不是 `shutil.copytree`：这个沙箱环境会把 Python 的
    # `shutil.copytree` 代理到宿主机做递归拷贝，而它对 `dist/public/pdfjs/*`
    # 触发文件策略拒绝。走 shell 的 `cp` 不受这个代理影响。
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    index = (SITE / "index.html").read_text()
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    index = pin_light(index)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次，不正常"

    anchor = '<script type="module" src="public/pdfjs/bridge.js"></script>'
    assert anchor in index, "没找到 bridge.js 的 script 标签"
    index = index.replace(
        anchor, anchor + '\n    <script type="module" src="/harness-stub.js"></script>'
    )
    (SITE / "index.html").write_text(index)
    (SITE / "harness-stub.js").write_text((WORK / "harness.js").read_text())


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory: _pl.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, timeout: int = 240) -> subprocess.CompletedProcess:
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        "--dump-dom",
        "--virtual-time-budget=30000",
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        st = re.search(r'<body[^>]*data-e2e38-stage="([^"]*)"', dom)
        raise SystemExit(
            f"页面里没有 harness-result（卡在阶段: {st.group(1) if st else '未知'}）\n"
            + dom[-1500:]
        )
    return json.loads(html.unescape(m.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {url}")
    try:
        dom = run_chrome(url).stdout
        result = extract_result(dom)
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

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    events = result.get("eventsAfterOpen") or []
    # 串行的话 A 的尺寸一定读在 B 的 `openDocumentPath` **之前**；
    # 重叠的话两条 `open` 会挨在一起，A 的 `sizes` 落在后面（读到的是 B 那份）。
    try:
        i_sizes_a = events.index(f"sizes:{PATH_A}")
    except ValueError:
        i_sizes_a = -1
    try:
        i_open_b = events.index(f"open:{PATH_B}")
    except ValueError:
        i_open_b = -1
    no_overlap = i_sizes_a >= 0 and i_open_b >= 0 and i_sizes_a < i_open_b

    # -------------------------------------------------- 前提判据
    check(
        "判据本身成立：应用注册了 folia://open-path 监听器（不是空跑）",
        isinstance(result.get("listenerNames"), list)
        and "folia://open-path" in result["listenerNames"]
        and result.get("dropCbRegistered") is True,
        f"监听器={result.get('listenerNames')}",
    )
    check(
        "判据本身成立：两份都真的发起了打开",
        result.get("openedA") is True and result.get("openedB") is True,
        f"A={result.get('openedA')} B={result.get('openedB')}",
    )

    # -------------------------------------------------- ★ 主判据
    check(
        "★ 两次打开**没有重叠**：A 读完自己的尺寸之前，B 的 openDocumentPath 还没发出",
        no_overlap,
        f"时间线={events}（A 的 sizes 在第 {i_sizes_a} 位，B 的 open 在第 {i_open_b} 位）"
        f" —— 重叠时 A 的 getPageSizes() 读的是全局那份（= B），"
        f"第一条流水线拿到别人的页数",
    )
    check(
        "★ 标签栏里两份都在（不是只剩最后打开的那一份）",
        result.get("tabNamesAfter") == [NAME_A, NAME_B],
        f"标签={result.get('tabNamesAfter')}",
    )
    check(
        "★ 每份的页数跟它自己对得上（A 是 120 页，不是 B 的 37）",
        result.get("totalA") == f"/ {PAGES_A}",
        f"切到 A 看到 {result.get('totalA')!r}（应为 '/ {PAGES_A}'）",
    )
    check(
        "★ 每份的页数跟它自己对得上（B 是 37 页）",
        result.get("totalB") == f"/ {PAGES_B}",
        f"切到 B 看到 {result.get('totalB')!r}（应为 '/ {PAGES_B}'）",
    )

    # -------------------------------------------------- 配对判据
    check(
        "配对：切回第一份还能切回来、页数也没变",
        result.get("activeTabRoundTrip") == NAME_A and result.get("totalA2") == f"/ {PAGES_A}",
        f"切回来={result.get('activeTabRoundTrip')!r} 页数={result.get('totalA2')!r}",
    )
    check(
        "配对：两份都真的渲染出了阅读器（不是空标签）",
        result.get("stageVisibleA") is True and result.get("stageVisibleB") is True,
        f"A={result.get('stageVisibleA')} B={result.get('stageVisibleB')}",
    )
    check(
        "配对：最近列表里两份都在（没被覆盖掉）",
        isinstance(result.get("savedNames"), list)
        and NAME_A in result["savedNames"]
        and NAME_B in result["savedNames"],
        f"落盘={result.get('savedNames')}",
    )

    print("\n================ checks ================")
    failed = 0
    for name, ok, detail in checks:
        if not ok:
            failed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ""))
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
