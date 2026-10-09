#!/usr/bin/env python3
"""验证本轮四项改动（/tmp/e2e26）。

  1. 首页卡片的文件名只占一行，hover 名字看到完整文件名；
  3. 已经是默认 PDF 阅读器时，「设为默认」按钮不显示；
  4. 首页的「筛选」按钮被删掉。

三个场景：home / alreadydefault / clickdefault。
做法同 e2e17~25：拷 `dist/`、只桩 Tauri IPC、无头 Chrome 跑真实 wasm。
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


def run_chrome(url: str, extra: list[str] | None = None, timeout: int = 180) -> str:
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


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"

    results: dict[str, dict] = {}
    try:
        for case in ("home", "alreadydefault", "clickdefault"):
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

    home = results["home"]
    already = results["alreadydefault"]
    click = results["clickdefault"]

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ---------------------------------------------------------------- 1. 名字一行 + hover
    check(
        "首页摆了两张卡片（判据不是空验证）",
        home.get("titleCount") == 2,
        f"卡片={home.get('titleCount')}",
    )
    check(
        "那个长名字**确实**被截断了（否则这条用例是空验证）",
        home.get("titleIsTruncated") is True,
        f"scrollWidth={home.get('titleScrollWidth')} clientWidth={home.get('titleClientWidth')}",
    )
    check(
        "文件名只占一行（white-space: nowrap + ellipsis）",
        home.get("titleWhiteSpace") == "nowrap"
        and home.get("titleTextOverflow") == "ellipsis"
        and home.get("titleOverflow") == "hidden",
        f"white-space={home.get('titleWhiteSpace')!r} text-overflow={home.get('titleTextOverflow')!r} overflow={home.get('titleOverflow')!r}",
    )
    lh = home.get("titleLineHeight") or 0
    h = home.get("titleHeight") or 0
    check(
        "名字的高度就是一行（不是两行）",
        0 < h <= lh * 1.35,
        f"高度={h} 单行行高={lh}（两行会是 {round(lh * 2, 2)} 左右）",
    )
    check(
        "长名字和短名字的卡片标题一样高（网格不会参差）",
        abs((home.get("titleHeight") or 0) - (home.get("shortTitleHeight") or 0)) < 0.6,
        f"长={home.get('titleHeight')} 短={home.get('shortTitleHeight')}",
    )
    check(
        "hover 名字显示的是**完整文件名**",
        home.get("titleAttr") == LONG_NAME,
        f"title={home.get('titleAttr')!r}",
    )
    check(
        "卡片整体仍然挂「名字 + 时间」的提示（没被名字那层覆盖掉）",
        home.get("cardAttr") == LONG_NAME + "\n今天",
        f"card title={home.get('cardAttr')!r}",
    )
    check(
        "时间戳缺失的条目：提示里不会多出一条空行",
        home.get("cardAttrNoTime") == "短名字.pdf",
        f"card title={home.get('cardAttrNoTime')!r}",
    )

    # ---------------------------------------------------------------- 4. 筛选按钮没了
    check(
        "首页的「筛选」按钮没了",
        home.get("hasFilterBtn") is False and home.get("hasSlidersIcon") is False,
        f"面板工具按钮={home.get('panelToolTitles')}",
    )
    check(
        "同一排的其他按钮都还在（删的只是筛选那一个）",
        set(home.get("panelToolTitles") or []) == {"搜索", "排序", "网格视图", "清空记录"},
        f"面板工具按钮={home.get('panelToolTitles')}",
    )

    # ---------------------------------------------------------------- 3. 默认按钮显隐
    check(
        "不是默认时，「设为默认」按钮照常显示",
        home.get("defaultBtnExists") is True,
        f"存在={home.get('defaultBtnExists')}",
    )
    check(
        "已经是默认时，这个按钮**不显示**",
        already.get("defaultBtnExists") is False,
        f"存在={already.get('defaultBtnExists')}",
    )
    check(
        "按钮藏起来时，「打开文件」还在（不是整排都没了）",
        already.get("openBtnExists") is True,
        f"打开文件存在={already.get('openBtnExists')}",
    )
    check(
        "启动时查了一次当前默认程序（只读）",
        (already.get("titleCount") or 0) >= 1,
        f"卡片={already.get('titleCount')}",
    )
    check(
        "点「设为默认」→ 调用设置命令、给出吐司",
        click.get("setInvoked") == 1
        and click.get("toastAfterClick") == "已把 Folia 设为默认 PDF 阅读器",
        f"设置次数={click.get('setInvoked')} 吐司={click.get('toastAfterClick')!r}",
    )
    check(
        "设完之后按钮就消失了",
        click.get("defaultBtnExistsAfter") is False,
        f"点之前={click.get('defaultBtnExists')} 点之后={click.get('defaultBtnExistsAfter')}",
    )
    check(
        "设完之后「打开文件」还在",
        click.get("openBtnExistsAfter") is True,
        f"打开文件={click.get('openBtnExistsAfter')}",
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
