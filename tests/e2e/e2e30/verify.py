#!/usr/bin/env python3
"""重新打开一份**已收藏**的文档，会不会把「收藏」标记清掉？（e2e30）

`apply_document` 登记「最近打开」时用的是 `RecentFile::from(&meta)`，
而那个转换里 `favorite` 写死为 `false`（`DocMeta` 不带收藏信息）。
于是「打开一份已收藏的文档」= 「retain 掉带 true 的旧条目 + 插一条 false 的新条目」。

这条路只在文档**不在标签里**时走到（已开着的话 `apply_document` 开头就 `return` 了），
所以「收藏 → 重启 → 从书架再打开」正是命中的那一步。

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
    # ⚠️ 用 `shutil.rmtree(ignore_errors=True)` 而不是 `rm -rf` + `check=True`：
    # `site/` 落在仓库里（用户的 Documents 下），**Finder 会往里丢 `.DS_Store`** ——
    # 它和 `rm -rf` 抢同一棵树时，rm 会以「目录非空」失败（exit 1），
    # 整个套件还没开始跑就崩。实测撞过一次。
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

    # ---- 判据成立的前置：没有这些，下面那条「还在」可能在空跑里也绿
    check(
        "前置：书架上有那张卡片",
        res.get("shelfNames") == ["fav.pdf"],
        f"卡片={res.get('shelfNames')}",
    )
    # ⚠️ 这一条才是「一开始确实是收藏」的**有效**判据：收藏分页是按
    # `item.favorite` 过滤出来的，它在初始状态就列出了这张卡片，说明应用内存里
    # 那份 `RecentFile.favorite` 确实是 true。
    # （原先这里还写了一条读「落盘 payload」的 —— 那是错的：应用启动只 *读* 不 *写*，
    #  那时 `__saved` 还是空的，判据永远拿不到值。别拿"还没发生的事"当前置。）
    check(
        "前置：收藏分页里一开始看得到它（= 应用内存里 favorite 确实为 true）",
        (res.get("favTabNames") or []) == ["fav.pdf"],
        f"收藏分页={res.get('favTabNames')}",
    )
    check(
        "前置：那份文档确实被打开了",
        res.get("opened") is True,
        f"opened={res.get('opened')}",
    )
    check(
        "前置：打开后确实落了一次盘（判据读的是它）",
        (res.get("saveCount") or 0) >= 1,
        f"落盘次数={res.get('saveCount')}",
    )

    # ---- ★ 核心
    check(
        "★ 重新打开后，收藏标记**还在**（favorite 仍为 true）",
        res.get("finalSavedFavorite") is True,
        f"落盘={res.get('finalSavedNames')}",
    )
    check(
        "★ 重新打开后，它仍然出现在「收藏」分页里",
        (res.get("favTabNamesAfter") or []) == ["fav.pdf"],
        f"收藏分页={res.get('favTabNamesAfter')}",
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
