#!/usr/bin/env python3
"""冷启动 + 待打开文件时，书架的「最近」列表会不会被冲掉（/tmp/e2e24）。

用户报告「拖到 Dock 图标打开 PDF」不生效，排查时发现 state.json 的
recentFiles 从 6 条掉到 1 条。这个套件把那种情况钉成判据：
桩给 3 条历史 + 1 个待打开路径，断言**最终落盘的 recentFiles 是 4 条**
（3 条历史一条不少 + 新打开的那条），而不是 1 条。

做法同 e2e17~23：拷 dist/、只桩 Tauri IPC、无头 Chrome 跑真实 wasm。
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
        return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    check("待打开的文件确实被自动打开了", res.get("pendingOpened") is True, f"标签={res.get('tabName')!r}")
    check(
        "落盘的 recentFiles 不是空的（判据本身有效）",
        (res.get("savedRecentCounts") or []) and max(res["savedRecentCounts"]) >= 1,
        f"每次写入的条数={res.get('savedRecentCounts')}",
    )
    check(
        "★ 3 条历史一条没丢（最终落盘是 4 条，不是 1 条）",
        (res.get("lastSavedPaths") or []).__len__() == 4,
        f"最终落盘={res.get('lastSavedPaths')}",
    )
    paths = res.get("lastSavedPaths") or []
    check(
        "★ 三条历史路径都还在",
        all(p in paths for p in ("/tmp/a.pdf", "/tmp/b.pdf", "/tmp/c.pdf")),
        f"落盘={paths}",
    )
    check(
        "★ 新打开的那份也在最前面",
        paths[:1] == ["/tmp/from-dock.pdf"],
        f"第一条={paths[:1]}",
    )
    check(
        "★ 收藏标记没被抹掉（b.pdf 是收藏）",
        "b.pdf:true" in (res.get("lastSavedFavorite") or []),
        f"落盘={res.get('lastSavedFavorite')}",
    )
    check(
        "书架上也看得到历史卡片（不是只剩一张）",
        len(res.get("shelfCardNames") or []) >= 4,
        f"书架卡片={res.get('shelfCardNames')}",
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
