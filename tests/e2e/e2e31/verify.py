#!/usr/bin/env python3
"""从书架打开一份「不是最后关掉的那份」文档，阅读进度会不会被重置？（e2e31）

`apply_document` 只看**一个** `restore` 槽位来决定从第几页打开：
对不上就回第 1 页，**并且把这个 1 写回 `RecentFile.last_page`**。
而 `restore` 只在「启动时」和「关掉正在看的那份标签时」写 —— 只有一个槽位。

于是「上次关掉的不是这份」时，这份文档被当成从没读过：
既从第 1 页打开，又把记录在案的进度覆盖成 1（卡片上的 `30/100` 变 `1/100`）。

只桩 Tauri IPC 与 bridge，其余（含真实 wasm）全走真实现。
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
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import threading

sys.path.insert(0, str(_E2E))
from theme_pin import pin_light  # noqa: E402

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def build_site() -> None:
    # 用 `shutil.rmtree(ignore_errors=True)` 而不是 `rm -rf` + `check=True`：
    # `site/` 在仓库里，Finder 会往里丢 `.DS_Store`，和 `rm -rf` 抢同一棵树时
    # rm 会以「目录非空」exit 1（e2e30 撞过）。
    if SITE.exists():
        shutil.rmtree(SITE, ignore_errors=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    index = (SITE / "index.html").read_text()
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次"
    index = pin_light(index)

    anchor = '<script type="module" src="public/pdfjs/bridge.js"></script>'
    assert anchor in index, "没找到 bridge.js 的 script 标签"
    index = index.replace(
        anchor, anchor + '\n    <script type="module" src="/harness-stub.js"></script>'
    )
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


def run_chrome(url: str, extra: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = [
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        "--window-size=1200,900", "--force-device-scale-factor=1", *extra, url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    match = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not match:
        raise SystemExit("页面里没有 harness-result\n" + dom[-3000:])
    return json.loads(html.unescape(match.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    try:
        url = f"http://127.0.0.1:{port}/index.html"
        dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=60000"]).stdout
        res = extract_result(dom)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(json.dumps(res, indent=2, ensure_ascii=False))

    if "error" in res:
        print("\n!! 驱动脚本报错:", res["error"])
        print(res.get("stack", ""))
        return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    metas = res.get("cardMetas") or []
    meta_a = next((m.get("meta") for m in metas if m.get("name") == "prog-a.pdf"), None)

    # ---- 判据成立的前置
    check(
        "前置：书架上两张卡片都在",
        sorted(m.get("name") for m in metas) == ["prog-a.pdf", "prog-b.pdf"],
        f"卡片={metas}",
    )
    check(
        "前置：卡片上确实记着 A 的进度（30/100）",
        (meta_a or "").startswith("30/100"),
        f"A 的卡片={meta_a!r}",
    )
    check(
        "前置：还没进阅读器（打开前没有页码框）",
        res.get("pageInputBefore") in (None, ""),
        f"打开前 page-input={res.get('pageInputBefore')!r}",
    )
    check("前置：文档确实被打开了", res.get("opened") is True, f"opened={res.get('opened')}")
    check(
        "前置：打开后确实落了一次盘",
        (res.get("saveCount") or 0) >= 1,
        f"落盘次数={res.get('saveCount')}",
    )

    # ---- ★ 核心
    check(
        "★ 打开后停在第 30 页（不是被拽回第 1 页）",
        res.get("pageInputAfter") == "30",
        f"页码框={res.get('pageInputAfter')!r}",
    )
    check(
        "★ 落盘里 A 的 last_page 还是 30（没被覆盖成 1）",
        res.get("savedLastPageOfA") == 30,
        f"落盘={res.get('savedAll')}",
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
