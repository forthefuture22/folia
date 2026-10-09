#!/usr/bin/env python3
"""收藏会不会被「最近 10 条」的上限挤掉？（e2e32）

`apply_document` 里的 `list.truncate(10)` 不看 `favorite`：
被挤到第 11 位的条目（哪怕用户亲手标了星）直接消失，下一句 `persist_state`
就把它从磁盘上抹掉。结果「收藏」实际上只是「最近 10 条之内的标记」。

本套件：先收藏一份，再打开 10 份别的，看那份收藏还在不在。
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

FAV_NAME = "keep-fav.pdf"


def build_site() -> None:
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
        dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=90000"]).stdout
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

    # ---- 判据成立的前置
    check(
        "前置：一开始书架上就是那份被收藏的文档",
        res.get("initialShelf") == [FAV_NAME],
        f"初始书架={res.get('initialShelf')}",
    )
    check(
        "前置：10 份别的文档都真的打开了（不是空跑）",
        res.get("tabCount") == 10,
        f"标签数={res.get('tabCount')}",
    )
    check(
        "前置：确实落过盘",
        (res.get("saveCount") or 0) >= 1,
        f"落盘次数={res.get('saveCount')}",
    )
    check(
        "判据本身成立：最近列表确实被截断了（书架只剩 10 张）",
        len(res.get("shelfAfter") or []) == 10,
        f"书架={res.get('shelfAfter')}",
    )

    # ---- ★ 核心
    check(
        "★ 被收藏的那份**还在**书架上（没被 10 条上限挤掉）",
        FAV_NAME in (res.get("shelfAfter") or []),
        f"书架={res.get('shelfAfter')}",
    )
    check(
        "★ 它仍然出现在「收藏」分页里",
        FAV_NAME in (res.get("favTabAfter") or []),
        f"收藏分页={res.get('favTabAfter')}",
    )
    check(
        "★ 落盘里它的 favorite 仍是 true（没被静默抹掉）",
        f"{FAV_NAME}:true" in (res.get("savedNames") or []),
        f"落盘={res.get('savedNames')}",
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
