#!/usr/bin/env python3
"""验证本轮六项改动（/tmp/e2e22）。

1. 中键点标签栏 → 关掉那个标签；
2. 「收藏」分页空着时什么都不显示；
3. 拖到 Dock 图标 / Finder「打开方式」：冷启动靠 `take_pending_open`；
4. 「设为默认 PDF 阅读器」按钮 + 话术；
5. 页面总览浮层变小 + 弹出动效；
6. 吐司 2 秒后自动收起。

三个场景用 `?case=` 跑三趟 Chrome（main / pending / alreadydefault）。
做法同 e2e17~21：拷 `dist/`、只桩 Tauri IPC、无头 Chrome 跑真实 wasm。
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

PAGES = 12


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
    base = f"http://127.0.0.1:{port}/index.html"

    results: dict[str, dict] = {}
    try:
        for case in ("main", "pending", "alreadydefault"):
            url = base if case == "main" else f"{base}?case={case}"
            print(f"[{case}] {url}")
            dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=90000"]).stdout
            results[case] = extract_result(dom)
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

    main_r = results["main"]
    pend_r = results["pending"]
    already_r = results["alreadydefault"]

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ---------------------------------------------------------------- 1. 中键关标签
    check(
        "准备了两个标签（判据不是空验证）",
        main_r.get("tabCountBefore") == 2,
        f"标签={main_r.get('tabNamesBefore')}",
    )
    check(
        "左键点标签只切换、不会关掉它",
        main_r.get("tabCountAfterLeftClick") == 2,
        f"左键后标签数={main_r.get('tabCountAfterLeftClick')}",
    )
    check(
        "左键点的那个标签确实被激活了",
        main_r.get("activeTabAfterLeftClick") == main_r.get("middleTarget"),
        f"激活={main_r.get('activeTabAfterLeftClick')!r} 目标是={main_r.get('middleTarget')!r}",
    )
    check(
        "中键点标签 → 关掉它（标签数 -1）",
        main_r.get("tabCountAfterMiddle") == 1,
        f"中键前={main_r.get('tabCountBefore')} 中键后={main_r.get('tabCountAfterMiddle')}",
    )
    check(
        "关掉的正是被中键点的那个标签",
        main_r.get("middleTarget") not in (main_r.get("tabNamesAfterMiddle") or []),
        f"被点的={main_r.get('middleTarget')!r} 剩下={main_r.get('tabNamesAfterMiddle')}",
    )

    # ---------------------------------------------------------------- 5. 总览浮层
    check("总览浮层还在（没被改坏）", main_r.get("panelExists") is True)
    check(
        "浮层格子数 = 文档页数",
        main_r.get("poItems") == PAGES,
        f"格子={main_r.get('poItems')} / {PAGES}",
    )
    w, h = main_r.get("panelWidth") or 0, main_r.get("panelHeight") or 0
    check(
        "浮层变小了（宽度 ≤ 900、高度 ≤ 640）",
        0 < w <= 900 and 0 < h <= 640,
        f"实测 {w}×{h}（原来是 1060×780）",
    )
    check(
        "面板有弹出动效",
        (main_r.get("panelAnimation") or "none") != "none",
        f"animation-name={main_r.get('panelAnimation')!r} 时长={main_r.get('panelAnimationMs')!r}",
    )
    check(
        "遮罩也有淡入动效",
        (main_r.get("overlayAnimation") or "none") != "none",
        f"animation-name={main_r.get('overlayAnimation')!r}",
    )
    check(
        "面板缩小后四周仍然留出遮罩（点空白关闭还能点）",
        main_r.get("backdropTopLeftHitsOverlay") is True,
        f"左上角命中遮罩={main_r.get('backdropTopLeftHitsOverlay')}",
    )
    check(
        "Esc 仍然能关掉总览",
        main_r.get("overviewClosedByEsc") is True,
        f"关掉了={main_r.get('overviewClosedByEsc')}",
    )

    # ---------------------------------------------------------------- 2. 收藏空态
    check("书架上能找到「收藏」分页", main_r.get("favSegExists") is True)
    check(
        "收藏空着时不再渲染空态整块（提示 + 图标都没了）",
        main_r.get("favEmptyEls") == 0 and main_r.get("favEmptyIconEls") == 0,
        f"空态块={main_r.get('favEmptyEls')} 图标={main_r.get('favEmptyIconEls')}",
    )
    check(
        "收藏空着时也没有卡片网格",
        main_r.get("favGridEls") == 0,
        f"网格={main_r.get('favGridEls')}",
    )
    check(
        "收藏分页面板里一个字都没有",
        (main_r.get("favPanelText") or "") == "",
        f"正文={main_r.get('favPanelText')!r}",
    )
    check(
        "那句「还没有收藏…」彻底消失",
        main_r.get("favStillMentionsHint") is False,
        f"还提到={main_r.get('favStillMentionsHint')}",
    )
    check(
        "切回「最近」卡片照旧（删的只是收藏那一支）",
        main_r.get("recentCardCount") == 2,
        f"最近卡片数={main_r.get('recentCardCount')}",
    )

    # ---------------------------------------------------------------- 4. 设为默认
    check("不是默认时，书架上有「设为默认」按钮", main_r.get("defaultBtnExists") is True)
    check(
        "按钮的说明写清了它是干嘛的",
        "默认" in (main_r.get("defaultBtnTitle") or ""),
        f"title={main_r.get('defaultBtnTitle')!r}",
    )
    check(
        "点它会先查当前默认程序、再设",
        (main_r.get("defaultCheckedInvoked") or 0) >= 1
        and (main_r.get("defaultSetInvoked") or 0) >= 1,
        f"查询={main_r.get('defaultCheckedInvoked')} 设置={main_r.get('defaultSetInvoked')}",
    )
    check(
        "设完的吐司文案正确",
        main_r.get("toastAtStart") == "已把 Folia 设为默认 PDF 阅读器",
        f"吐司={main_r.get('toastAtStart')!r}",
    )
    check(
        "已经是默认时，按钮**不显示**（点了什么都不会变的按钮比没有更糟）",
        already_r.get("defaultBtnExists") is False,
        f"存在={already_r.get('defaultBtnExists')}",
    )
    check(
        "已经是默认时不会去重复设置",
        already_r.get("defaultSetInvoked") == 0,
        f"设置次数={already_r.get('defaultSetInvoked')}",
    )
    check(
        "已经是默认时启动仍然查过一次（只读，用来决定按钮显不显示）",
        (already_r.get("defaultCheckedInvoked") or 0) >= 1,
        f"查询次数={already_r.get('defaultCheckedInvoked')}",
    )

    # ---------------------------------------------------------------- 6. 吐司自动收起
    check(
        "吐司 1.2 秒时还在（不是一出来就没了）",
        main_r.get("toastAt1200ms") == "已把 Folia 设为默认 PDF 阅读器",
        f"1.2s={main_r.get('toastAt1200ms')!r}",
    )
    check(
        "吐司 2.6 秒时已经自动收起",
        main_r.get("toastAt2600ms") is None,
        f"2.6s={main_r.get('toastAt2600ms')!r}",
    )

    # ---------------------------------------------------------------- 3. Dock 拖入（冷启动）
    check(
        "冷启动时前端会主动取一次「待打开文件」",
        (pend_r.get("pendingInvokeCount") or 0) >= 1,
        f"调用次数={pend_r.get('pendingInvokeCount')}",
    )
    check(
        "什么都没点，文档就自己打开了（拖到 Dock 图标上的那份）",
        pend_r.get("pendingOpenedDirectly") is True,
        f"直接打开={pend_r.get('pendingOpenedDirectly')} 标签={pend_r.get('pendingTabName')!r}",
    )
    check(
        "打开的是待打开的那份，而不是书架上的「上次在读」",
        pend_r.get("pendingIsDockFile") is True,
        f"标签={pend_r.get('pendingTabName')!r}",
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
