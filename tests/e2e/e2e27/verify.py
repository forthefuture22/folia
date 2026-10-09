#!/usr/bin/env python3
"""e2e27：书架卡片的 hover 提示 + 右键菜单（/tmp/e2e27）。

两个用例：

- `?case=probe` —— 卡片 hover 时"有时候看不到文件名"的根因。
  原生 `title` 提示框由**光标下那个元素**决定（浏览器往上找第一个非空 title），
  所以沿卡片横扫采样 `elementFromPoint`，就能定位哪块区域给出的提示不是文件名。

- `?case=menu` —— 右键菜单的每一项行为（打开/关闭/夹取/五个动作/二次确认/落盘）。
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

LONG_NAME = "Java 热门面试题 200 道速记通关版 _ 面试刷题 mianshiya.com_加水印_最终版_v3.pdf"


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


def extract_result(dom: str) -> dict:
    match = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not match:
        raise SystemExit("页面里没有 harness-result —— 驱动脚本没跑完\n" + dom[-3000:])
    return json.loads(html.unescape(match.group(1)))


# ------------------------------------------------------------------ 用例

FAILURES: list[str] = []


def report_probe(res: dict) -> None:
    print("=============== probe：hover 提示 ===============")
    if "error" in res:
        FAILURES.append(f"probe 驱动脚本报错: {res['error']}")
        print("!! 驱动脚本报错:", res["error"], res.get("stack", ""))
        return

    # 曾经和文件名行重叠的那两颗悬浮按钮，现在应该整个不存在。
    print("卡片上还有悬浮操作区吗:", res.get("actionsExists"), "| 卡片里的 button 数:", res.get("cardButtons"))
    if res.get("actionsExists"):
        FAILURES.append("probe: 卡片上还有 .file-actions —— 悬浮按钮应该已经删掉了")
    if res.get("cardButtons"):
        FAILURES.append(f"probe: 卡片里还有 {res.get('cardButtons')} 个 button，应该一个都不剩")

    bad = res.get("titleSamplesBad")
    total = res.get("titleSampleCount")
    print(f"标题行采样 {bad}/{total} 处给出的不是文件名")
    for s in res.get("titleSamples") or []:
        flag = "OK  " if s["title"] == "<完整文件名>" else "BAD "
        print(f"  {flag} {s['at']:<16} 命中 {s['hit']:<28} title={s['title']!r}")

    if bad:
        FAILURES.append(f"probe: 标题行有 {bad}/{total} 处 hover 给出的不是文件名")

    print("缩略图 / 元信息行的提示:")
    for s in res.get("otherSamples") or []:
        print(f"  {s['at']:<6} 命中 {s['hit']:<24} title={s['title']!r}")


def report_menu(res: dict) -> None:
    print()
    print("=============== menu：右键菜单 ===============")
    if "error" in res:
        FAILURES.append(f"menu 驱动脚本报错: {res['error']}")
        print("!! 驱动脚本报错:", res["error"], res.get("stack", ""))
        return

    tests = res.get("tests") or []
    if not tests:
        FAILURES.append("menu: 一条判据都没跑")
        return

    for t in tests:
        flag = "OK  " if t["pass"] else "FAIL"
        extra = "" if t["pass"] or t["detail"] is None else f"   实际={t['detail']!r}"
        print(f"  {flag} {t['name']}{extra}")

    failed = res.get("failed", 0)
    print(f"\n{len(tests) - failed}/{len(tests)} 条通过")
    if failed:
        FAILURES.append(f"menu: {failed} 条判据失败")


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        probe = extract_result(run_chrome(f"{base}?case=probe"))
        menu = extract_result(run_chrome(f"{base}?case=menu"))
    finally:
        httpd.shutdown()
        httpd.server_close()

    report_probe(probe)
    report_menu(menu)

    print()
    if FAILURES:
        print("================ 失败 ================")
        for f in FAILURES:
            print(" ✗", f)
        return 1
    print("================ 全绿 ================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
