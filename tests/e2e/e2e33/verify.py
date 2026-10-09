#!/usr/bin/env python3
"""e2e33 —— 打开两份规格相同的 PDF 后按 Esc 回书架，第二个标签会不会被吃掉。

为什么专门写这一套：`src/app.rs` 的 keydown 监听器是**按依赖重注册**的，
依赖里含 `layout`（= `use_memo((page_sizes, scale))`）。两份页面尺寸、页数
都相同的 PDF 会让 `layout` 完全相等 → 打开第二份时依赖元组不变 →
**effect 不重注册** → 闭包里的 `handles` 停在「只开着第一份」那一帧。
而它按 Esc 调的是 `go_library(&handles)`，第一步就是
`sync_tab_from_live(handles, *handles.active)`，会以**旧帧的 `handles.tabs`**
为基准改完再 `set` 回去 —— 第二个标签被静默抹掉。

两个 case：
  esc   主角：按 Esc 回书架
  home  对照：点标签栏的 home 按钮回书架（走每帧新建的回调，handles 是新的）

对照绿、主角红，才说明根因是「闭包读到旧帧」而不是「回书架本身会掉标签」。
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

NAME_A = "dup-a.pdf"
NAME_B = "dup-b.pdf"


def build_site() -> None:
    # ⚠️ 别用 `rm -rf` + check=True：`site/` 在仓库里，Finder 会往里丢
    # `.DS_Store`，它和 rm 抢同一棵树时 rm 会以「目录非空」exit 1（踩过）。
    shutil.rmtree(SITE, ignore_errors=True)
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


def run_chrome(url: str, timeout: int = 180) -> str:
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        "--force-device-scale-factor=1",
        "--dump-dom",
        "--virtual-time-budget=90000",
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout


def extract_result(dom: str) -> dict:
    match = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not match:
        raise SystemExit("页面里没有 harness-result —— 驱动脚本没跑完\n" + dom[-3000:])
    return json.loads(html.unescape(match.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"

    results: dict[str, dict] = {}
    try:
        for case in ("esc", "home"):
            url = f"{base}?case={case}"
            print(f"[{case}] {url}")
            results[case] = extract_result(run_chrome(url))
    finally:
        httpd.shutdown()
        httpd.server_close()

    for case, res in results.items():
        print(f"\n================ {case} ================")
        print(json.dumps(res, indent=2, ensure_ascii=False))

    for case, res in results.items():
        if "error" in res:
            print(f"\n!! [{case}] 驱动脚本报错:", res["error"])
            print(res.get("stack", ""))
            return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def both_names(names) -> bool:
        return sorted(names or []) == sorted([NAME_A, NAME_B])

    # ---------------------------------------------------------------- 前置
    for case in ("esc", "home"):
        r = results[case]
        check(
            f"[{case}] 前置：两份文档都真的开成了两个标签",
            both_names(r.get("tabsBefore")),
            f"标签={r.get('tabsBefore')}",
        )

    # ---------------------------------------------------------------- esc（主角）
    esc = results["esc"]
    check(
        "[esc] ★ 动作真的发生了：Esc 确实退回了书架（否则下面那条是空验证）",
        esc.get("backInLibrary") is True,
        f"在书架={esc.get('backInLibrary')}",
    )
    check(
        "[esc] ★ 退回书架后**两个标签都还在**（第二个没被静默吃掉）",
        both_names(esc.get("tabsAfter")),
        f"Esc 前={esc.get('tabsBefore')} Esc 后={esc.get('tabsAfter')}",
    )

    # ---------------------------------------------------------------- home（对照）
    home = results["home"]
    check(
        "[home·对照] 动作真的发生了：点 home 确实退回了书架",
        home.get("backInLibrary") is True,
        f"在书架={home.get('backInLibrary')}",
    )
    check(
        "[home·对照] 走同一条路（回书架）但用新帧的回调 → 两个标签都还在",
        both_names(home.get("tabsAfter")),
        f"点前={home.get('tabsBefore')} 点后={home.get('tabsAfter')}",
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
