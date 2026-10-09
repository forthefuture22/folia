#!/usr/bin/env python3
"""探针（/tmp/e2e20）：查「点不动」卡在哪一层。

和 e2e19 的区别只有一处：不往按钮上直接 dispatchEvent，而是先 `elementFromPoint()`
命中测试，再点那个"最上面的元素" —— 复刻真实鼠标的路径。
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

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def build_site() -> None:
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    index = (SITE / "index.html").read_text()
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次"

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
        raise SystemExit("页面里没有 harness-result\n" + dom[-3000:])
    return json.loads(html.unescape(match.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"probe url: {url}")

    try:
        dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=60000"]).stdout
        result = extract_result(dom)
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n================ probe result ================")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
