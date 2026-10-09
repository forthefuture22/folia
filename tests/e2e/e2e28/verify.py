#!/usr/bin/env python3
"""e2e28：正文里右击一条批注 → 删除它（/tmp/e2e28）。

和 e2e17 分工不同：那边验的是「选字 → 浮层按钮 → 造出批注」，
这边直接让 `load_annotations` 返回四条现成的（四种样式各一条），
专心验右击菜单 —— 命中测试、文案、删除、落盘、编辑批注、关闭时机、常量守卫。
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
from theme_pin import pin_light  # noqa: E402

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
    def log_message(self, *args):
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, extra=None, timeout: int = 180) -> str:
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        "--force-device-scale-factor=1",
        *(extra or ["--dump-dom", "--virtual-time-budget=90000"]),
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def extract(dom: str, tag: str):
    match = re.search(rf'<pre id="{tag}">(.*?)</pre>', dom, flags=re.S)
    return html.unescape(match.group(1)) if match else None


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        dom = run_chrome(base)
    finally:
        httpd.shutdown()
        httpd.server_close()

    raw = extract(dom, "harness-result")
    if raw is None:
        print("!! 页面里没有 harness-result —— 驱动脚本没跑完")
        err = extract(dom, "harness-error")
        if err:
            print("驱动脚本的报错轨迹:")
            print(err)
        print(dom[-2500:])
        return 1

    res = json.loads(raw)
    if "error" in res:
        print("!! 驱动脚本报错:", res["error"])
        print(res.get("stack", ""))
        return 1

    print("viewport:", res.get("viewport"))
    print("四条批注的类名:", res.get("markClasses"))
    print()

    tests = res.get("tests") or []
    for t in tests:
        flag = "OK  " if t["pass"] else "FAIL"
        extra = "" if t["pass"] or t["detail"] is None else f"   实际={t['detail']!r}"
        print(f"  {flag} {t['name']}{extra}")

    failed = res.get("failed", 0)
    print()
    print("落盘前:", res.get("savedBefore"))
    print("落盘后:", res.get("savedAfter"))
    print("侧栏选中:", res.get("activeAnn"))
    print(f"\n{len(tests) - failed}/{len(tests)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
