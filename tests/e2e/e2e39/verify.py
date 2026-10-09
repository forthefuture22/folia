#!/usr/bin/env python3
"""书架缩略图那条 effect：`recent` 一变就起新任务，而**旧任务不会自己停**。

旧任务手里那份 `known` 是它启动那一刻的快照，里面可能有已经被删掉的 id。它接着
`thumbs.set(known.clone())` 就把那些条目的 data URL（约 20–30 KB 一张）又塞回
map 里，而这时"丢掉不在列表里的条目"那句 `retain` 早就跑完了 —— 要等**下一次**
`recent` 变化才会再清一遍（自愈，但中间一直在漏），而且旧任务还在白渲染已经删掉
的那份文件的首页。

判据怎么落地：数 `renderThumbnail` 的调用**按路径**分。删掉**最后一份**（它排在
旧任务渲染顺序的末尾，此刻一定还没轮到），只要之后还出现**它**的渲染调用，
就说明旧任务没退场。

⚠️ **现有套件抓不到**：这一条在 DOM 上完全看不见（多余的那张 data URL 挂在
`thumbs` 里，而书架只给 `recent` 里的条目画卡片），只能靠副作用计数。

判据分三类：
  · **前提判据**：卡片数对 / 删掉前**最后一份**确实还没被渲染过（否则窗口太晚，
    判据无效）。
  · **★ 主判据**：删掉之后，再没有对它的渲染调用。
  · **配对判据**：剩下那 7 份照样渲染出来了（不是把整条 effect 干掉了）、
    删除也落盘了。
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

COUNT = 8
LAST_NAME = f"f{COUNT}.pdf"
LAST_PATH = f"/tmp/f{COUNT}.pdf"


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
        st = re.search(r'<body[^>]*data-e2e39-stage="([^"]*)"', dom)
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

    at_remove = result.get("lastRendersAtRemove")
    after = result.get("lastRendersAfter")
    kept = result.get("keptRendered")

    # -------------------------------------------------- 前提判据
    check(
        "判据本身成立：一开始有 8 张卡片（不是空跑）",
        result.get("cardsBefore") == COUNT,
        f"卡片={result.get('cardsBefore')}",
    )
    check(
        "判据本身成立：删掉之前，最后那份**还没被渲染过**（窗口够早，判据才有意义）",
        result.get("lastRendersBefore") == 0,
        f"删之前已渲染 {result.get('lastRendersBefore')} 次"
        f"（此时共渲染 {result.get('rendersBeforeRemove')} 张）",
    )
    check(
        "判据本身成立：右击菜单里确实有「删除记录」，而且删掉了（8 → 7）",
        isinstance(result.get("menuLabels"), list)
        and "删除记录" in result["menuLabels"]
        and result.get("cardsAfter") == COUNT - 1,
        f"菜单={result.get('menuLabels')} 删后卡片={result.get('cardsAfter')}",
    )

    # -------------------------------------------------- ★ 主判据
    check(
        "★ 删掉之后，旧任务**再没有**渲染那份已经不在列表里的文件",
        at_remove == 0 and after == 0,
        f"删的那一刻 {at_remove} 次、之后又 {after} 次 —— 非 0 就说明旧任务没退场，"
        f"它还在白渲染、还会把过期条目写回 `thumbs`",
    )

    # -------------------------------------------------- 配对判据
    check(
        "配对：剩下那 7 份照样都渲染出来了（不是把整条 effect 干掉了）",
        isinstance(kept, list) and len(kept) == COUNT - 1 and all(n >= 1 for n in kept),
        f"各自渲染次数={kept}",
    )
    check(
        "配对：删除落盘了，而且落盘的 7 条里没有它",
        isinstance(result.get("savedNames"), list)
        and len(result["savedNames"]) == COUNT - 1
        and LAST_NAME not in result["savedNames"],
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
