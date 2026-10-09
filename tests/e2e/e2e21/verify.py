#!/usr/bin/env python3
"""验证全局键盘快捷键（/tmp/e2e21）。

回归对象：`src/app.rs` 里那个 keydown effect。
它原来是 `use_effect_with((), …)` —— 只注册一次，闭包里的 `UseStateHandle` /
`UseMemo` 是注册那一帧的快照。第一帧文档还没载入，`meta` 是 `None`、
`layout` 是空的，于是**所有翻页快捷键都按不动**。
修法是给 effect 补上依赖（`meta` / `scale` / `current_page` / `layout` / `overview_open`）。

做法同 e2e17/18/19：拷一份 `dist/`、只桩 Tauri IPC，无头 Chrome 跑真实 wasm。
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


def build_site() -> None:
    # 沙箱会把 Python 的 `shutil.copytree` 代理到宿主机并拒绝 —— 用 `cp -R`。
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

    check("打开文档后停在第 1 页", result.get("startPage") == "1", f"页码={result.get('startPage')!r}")

    seq = result.get("rightSeq") or []
    check(
        "按 3 次 → 能一路走到第 4 页（不是卡在第 1 页）",
        seq == ["2", "3", "4"],
        f"页码序列={seq}",
    )
    check(
        "PageDown 也能翻页",
        result.get("afterPageDown") == "5",
        f"页码={result.get('afterPageDown')!r}",
    )
    check(
        "空格也能翻页",
        result.get("afterSpace") == "6",
        f"页码={result.get('afterSpace')!r}",
    )
    check(
        "End 跳到最后一页",
        result.get("afterEnd") == str(PAGES),
        f"页码={result.get('afterEnd')!r}",
    )
    check("Home 跳回第 1 页", result.get("afterHome") == "1", f"页码={result.get('afterHome')!r}")
    check(
        "第 1 页按 ← 不会退到第 0 页",
        result.get("afterLeft") == "1" and result.get("afterLeftAgain") == "1",
        f"页码={result.get('afterLeft')!r} / {result.get('afterLeftAgain')!r}",
    )

    # 光看页码框不够：End 必须真的把正文滚下去。
    top1 = result.get("scrollTopAtPage1")
    top_end = result.get("scrollTopAtEnd")
    check(
        "End 真的把正文滚下去了（不是只改了页码框）",
        isinstance(top1, (int, float))
        and isinstance(top_end, (int, float))
        and top_end > top1 + 100,
        f"第1页 scrollTop={top1} 末页 scrollTop={top_end}",
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
