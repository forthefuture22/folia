#!/usr/bin/env python3
"""验证「拖入 / 直接打开」的 PDF 会不会进历史记录（/tmp/e2e25）。

四个场景：
  drop      停在首页时拖一个 PDF 进来
  drop2     已经开着一份文档时再拖一份进来（顺带查标签有没有被吃掉）
  openpath  热启动的 `folia://open-path`（双击 / Dock 图标 / 打开方式）
  pending   冷启动：后端已经存好了待打开路径

判据同时看两处：**首页卡片**（内存里的列表）和 **save_app_state 的载荷**（落盘的那份）。
只查一处会漏 —— 内存对了但没落盘，重启就丢。
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

DROPPED = "dropped-in.pdf"
PENDING = "from-dock.pdf"


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
        for case in ("drop", "drop2", "openpath", "pending", "reopen"):
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

    # ---------------------------------------------------------------- 前置
    drop_r = results["drop"]
    check(
        "首页一开始有两份历史（判据不是空验证）",
        drop_r.get("cardNamesBefore") == ["old1.pdf", "old2.pdf"],
        f"卡片={drop_r.get('cardNamesBefore')}",
    )
    check(
        "应用确实注册了拖拽回调",
        drop_r.get("dropCbRegistered") is True,
        f"注册={drop_r.get('dropCbRegistered')}",
    )
    check(
        "应用确实监听了 folia://open-path",
        "folia://open-path" in (drop_r.get("listeners") or []),
        f"监听器={drop_r.get('listeners')}",
    )

    # ---------------------------------------------------------------- drop
    check(
        "【拖入】文档真的被打开了",
        drop_r.get("tabNamesAfter") == [DROPPED],
        f"标签={drop_r.get('tabNamesAfter')}",
    )
    check(
        "【拖入】回到首页能看到它，且排在最前",
        (drop_r.get("cardNamesAfter") or [None])[0] == DROPPED,
        f"卡片={drop_r.get('cardNamesAfter')}",
    )
    check(
        "【拖入】历史里的旧条目还在（没被冲掉）",
        set(drop_r.get("cardNamesAfter") or []) >= {"old1.pdf", "old2.pdf"},
        f"卡片={drop_r.get('cardNamesAfter')}",
    )
    check(
        "【拖入】真的落盘了（save_app_state 的载荷里有它）",
        (drop_r.get("savedNames") or [None])[0] == DROPPED,
        f"落盘={drop_r.get('savedNames')}",
    )
    check(
        "【拖入】落盘的列表里旧条目也在",
        set(drop_r.get("savedNames") or []) >= {"old1.pdf", "old2.pdf"},
        f"落盘={drop_r.get('savedNames')}",
    )

    # ---------------------------------------------------------------- drop2
    d2 = results["drop2"]
    check(
        "【拖入·已开着文档】原来那份没被吃掉（标签变成 2 个）",
        len(d2.get("tabNamesAfter") or []) == 2,
        f"拖之前={d2.get('tabNamesBeforeDrop')} 拖之后={d2.get('tabNamesAfter')}",
    )
    check(
        "【拖入·已开着文档】新拖进来的排在最前",
        (d2.get("cardNamesAfter") or [None])[0] == DROPPED,
        f"卡片={d2.get('cardNamesAfter')}",
    )
    check(
        "【拖入·已开着文档】也落盘了",
        (d2.get("savedNames") or [None])[0] == DROPPED,
        f"落盘={d2.get('savedNames')}",
    )

    # ---------------------------------------------------------------- openpath（热启动）
    op = results["openpath"]
    check(
        "【直接打开】文档真的被打开了",
        op.get("tabNamesAfter") == [PENDING],
        f"标签={op.get('tabNamesAfter')}",
    )
    check(
        "【直接打开】回到首页能看到它，且排在最前",
        (op.get("cardNamesAfter") or [None])[0] == PENDING,
        f"卡片={op.get('cardNamesAfter')}",
    )
    check(
        "【直接打开】历史里的旧条目还在",
        set(op.get("cardNamesAfter") or []) >= {"old1.pdf", "old2.pdf"},
        f"卡片={op.get('cardNamesAfter')}",
    )
    check(
        "【直接打开】真的落盘了",
        (op.get("savedNames") or [None])[0] == PENDING,
        f"落盘={op.get('savedNames')}",
    )

    # ---------------------------------------------------------------- pending（冷启动）
    pd = results["pending"]
    check(
        "【冷启动】待打开的那份自己开了",
        pd.get("openedPending") is True,
        f"标签={pd.get('tabNames')}",
    )
    check(
        "【冷启动】落盘的列表 = 待打开 + 原有历史（不是只剩 1 条）",
        len(pd.get("savedNames") or []) == 3
        and (pd.get("savedNames") or [None])[0] == PENDING
        and set(pd.get("savedNames") or []) >= {"old1.pdf", "old2.pdf"},
        f"落盘={pd.get('savedNames')}",
    )
    check(
        "【冷启动】回首页三张卡片都在",
        len(pd.get("cardNamesAfterHome") or []) == 3,
        f"卡片={pd.get('cardNamesAfterHome')}",
    )

    # ---------------------------------------------------------------- reopen
    #
    # 把一份**已经开着**的 PDF 再打开一次。判据全部读**状态派生**的 DOM
    # （侧栏缩略图上的 `.thumb-item.active`），不读 `.page-input` ——
    # 那个 input 被脚本打过字之后就"脏"了，状态 1 → 1 时 Yew 不会写回 DOM，
    # 判据会变成永远通过的假绿（踩过）。
    rp = results["reopen"]
    check(
        "【重开】判据本身成立：侧栏画出了 12 张缩略图（否则下面全是在量空气）",
        rp.get("thumbCount") == 12,
        f"缩略图={rp.get('thumbCount')}",
    )
    check(
        "【重开】先把第一份打开、并跳到第 7 页（判据不是空验证）",
        rp.get("pageBeforeReopen") == "7",
        f"标签={rp.get('tabNamesAfterOpen')} 页码={rp.get('pageBeforeReopen')}",
    )
    check(
        "【重开】再打开**同一份**不会多出一个标签",
        len(rp.get("tabNamesAfterReopen") or []) == 1,
        f"重开前={rp.get('tabNamesAfterOpen')} 重开后={rp.get('tabNamesAfterReopen')}",
    )
    check(
        "【重开】同一份重新打开后，阅读位置**没有**退回第 1 页",
        rp.get("pageAfterReopen") == rp.get("pageBeforeReopen")
        and rp.get("pageAfterReopen") != "1",
        f"重开前={rp.get('pageBeforeReopen')} 重开后={rp.get('pageAfterReopen')} "
        f"滚动={rp.get('stageAfterReopen')}",
    )
    # 这条和上面那条命中的是**同一份 `tabs` 快照**（重开时若把快照冲成第 1 页，
    # 切回来自然也就读到第 1 页），所以它不是"另一条独立的路"，而是同一个根因的
    # 第二个出口 —— 当年正是它先红，才顺着查到 `set()` 排队那个坑。
    check(
        "【重开·连带】切到另一份再切回来，阅读位置也还在",
        rp.get("pageAfterTabRoundTrip") == rp.get("pageBeforeReopen")
        and rp.get("pageAfterTabRoundTrip") != "1"
        and rp.get("activeTabAfterRoundTrip") is not None,
        f"切之前={rp.get('pageAfterOpenSecond')} 切回来={rp.get('pageAfterTabRoundTrip')} "
        f"当前标签={rp.get('activeTabAfterRoundTrip')} 滚动={rp.get('stageAfterRoundTrip')}",
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
