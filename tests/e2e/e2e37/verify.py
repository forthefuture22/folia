#!/usr/bin/env python3
"""总览（全部页面）在**超过缩略图缓存上限**之后，按需补齐还成不成立。

为什么需要这一套：缩略图缓存有条数上限（`PAGE_THUMB_LIMIT = 60`），淘汰规则是
「挤掉**离焦点页最远**的那张」，而焦点必须取**用户正在看的地方**。总览却是一张
**所有页**的网格 —— 用户会在网格里滚到远离阅读位置的地方。焦点要是取阅读位置，
这两个事实叠在一起就是这一套要钉的东西：

    在网格里滚到第 127 页（阅读位置还在第 1 页）→ 第 127 页的缩略图刚渲染完
    就被当成「离第 1 页最远」淘汰掉 → 网格看得见的格子永远没有图，
    而「补齐」那条 effect 又挂在版本号上 → 渲染 → 淘汰 → 再渲染 …… 转不停。

⚠️ **现有套件抓不到**：`e2e19` 用的是 30 页文档（< 60），上限根本不触发；
它的判据甚至是「总览里 30 张缩略图全部填好」—— 那在小文档上是对的。

⚠️ **必须分档滚到底**（见 harness 的 `scroll-grid`）：打开时只请求可视区那一片，
一步跳到底的请求量到不了 60，淘汰不参与 → 主判据假绿。所以下面专门有一条
「缓存确实被推到了上限」的前置判据。

判据分三类：
  · **前提判据**：格子数是全的 / 渲染过 / 滚远了 / **缓存推到了上限**。
  · **★ 主判据**（会红的）：可视区格子有图 / 静止时渲染次数不再涨 / 等一会儿也不会好。
  · **配对判据**：总览照样能跳页、格子数照样是全的 —— 证明不是"把功能改坏了
    所以不涨了"。
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
import re
import socketserver
import subprocess
import sys
import threading
sys.path.insert(0, str(_E2E))
from theme_pin import pin_light  # noqa: E402  (把偏好钉成浅色，用例才与宿主机外观无关)

ROOT = _pl.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = _pl.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PAGES = 150
# 跟 `state/handles.rs` 的 `PAGE_THUMB_LIMIT` 同步（跟 harness 里的 `BAND` 一样，
# 照抄一份常量，免得探针和应用各算各的）。
PAGE_THUMB_LIMIT = 60


def build_site() -> None:
    # 用 `cp -R` 而不是 `shutil.copytree`：这个沙箱环境会把 Python 的
    # `shutil.copytree` 代理到宿主机做递归拷贝，而它对 `dist/public/pdfjs/*`
    # 触发文件策略拒绝。走 shell 的 `cp` 不受这个代理影响。
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

    harness = (WORK / "harness.js").read_text()
    (SITE / "harness-stub.js").write_text(harness)


REQUESTS: list[str] = []


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        REQUESTS.append(self.path)
        return super().do_GET()


def serve(directory: _pl.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        "--dump-dom",
        # ⚠️ 别调大。补齐那条 effect 挂在版本号上，一旦主判据红了就是
        # 「渲染→淘汰→再渲染」转不停 —— 预算有多大就烧多大（600000 实测
        # 跑满 300s 真实超时，harness 连结果都出不来）。60000 够跑完全部阶段。
        "--virtual-time-budget=60000",
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        st = re.search(r'<body[^>]*data-e2e37-stage="([^"]*)"', dom)
        raise SystemExit(
            f"页面里没有 harness-result（卡在阶段: {st.group(1) if st else '未知'}）\n"
            + dom[-1200:]
        )
    return json.loads(html.unescape(m.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {url}   PAGES={PAGES}")
    try:
        dom = run_chrome(url).stdout
        result = extract_result(dom)
    except SystemExit as e:
        print("\n!! " + str(e))
        for p in REQUESTS:
            print("  " + p)
        return 1
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n================ harness result ================")
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if "error" in result:
        print("\n!! 驱动脚本报错:", result["error"])
        print(result.get("stack", ""))
        return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # -------------------------------------------------- 判据本身成立
    check(
        "判据本身成立：网格格子数 = 页数（不是空跑）",
        result.get("poItems") == PAGES,
        f"格子={result.get('poItems')} 期望 {PAGES}",
    )
    check(
        "判据本身成立：打开总览后确实渲染过缩略图（否则下面是在量空气）",
        isinstance(result.get("rendersAfterOpen"), int) and result["rendersAfterOpen"] > 0,
        f"renderThumbnail 调用 {result.get('rendersAfterOpen')} 次"
        f"，不同页 {result.get('uniquePagesRendered')} 页",
    )
    check(
        "判据本身成立：网格确实滚动到了远离阅读位置的地方",
        isinstance(result.get("visibleSample"), list)
        and result["visibleSample"]
        and min(result["visibleSample"]) > 60,
        f"可视区样本={result.get('visibleSample')}（阅读位置仍在第 1 页，"
        f"滚动 {result.get('scrollTop')} / {result.get('scrollHeight')}）",
    )
    # ⚠️ 这条是**主判据的前提**，不是陪衬。`open_all_thumbs` 不再逐页渲染整本
    # 之后，打开时只请求可视区那一片（约 24 张）；一步跳到底也只有 48 张 ——
    # 没到上限，淘汰不参与，主判据就成了假绿（改回 `current_page` 也能过）。
    # 分档滚动把 store 推满到上限，才真的在测"淘汰焦点取哪儿"。
    check(
        "判据本身成立：缓存确实被推到了上限（否则淘汰没参与，主判据是假绿）",
        isinstance(result.get("poImgsAtBottom"), int)
        and result["poImgsAtBottom"] >= PAGE_THUMB_LIMIT,
        f"网格里有图的格子 {result.get('poImgsAtBottom')} 个 / 上限 {PAGE_THUMB_LIMIT}",
    )

    # -------------------------------------------------- ★ 主判据
    check(
        "★ 滚到底之后，网格**看得见**的格子全部有缩略图",
        result.get("visibleMissing") == 0,
        f"缺图 {result.get('visibleMissing')} / {result.get('visibleAtScroll')} 个"
        f"（样本 {result.get('visibleMissingSample')}）"
        f"—— 缩略图刚渲染完就被当成「离第 1 页最远」淘汰了",
    )
    check(
        "★ 静止不动时渲染次数不再增长（没有「渲染→淘汰→再渲染」的循环）",
        isinstance(result.get("rendersIdle"), int) and result["rendersIdle"] <= 2,
        f"静止窗口内又渲染了 {result.get('rendersIdle')} 次"
        f"（累计 {result.get('totalRenders')} 次）",
    )
    check(
        "★ 等待之后仍然缺图（不是「再等等就好了」）",
        result.get("visibleMissingAfter") == 0,
        f"等待后仍缺 {result.get('visibleMissingAfter')} 个",
    )

    # -------------------------------------------------- 配对判据
    check(
        "配对：总览照样能跳页（没为了省内存把功能改坏）",
        isinstance(result.get("jumpTarget"), int)
        and result.get("pageAfterJump") == str(result["jumpTarget"]),
        f"点第 {result.get('jumpTarget')} 页 → 页码框={result.get('pageAfterJump')}"
        f"（命中自己={result.get('jumpHitIsSelf')}）"
        f"，浮层收起={not result.get('overviewAfterJump')}",
    )
    check(
        "配对：有图的格子数不超过页数（缓存没有无限膨胀）",
        isinstance(result.get("poImgsAfterScroll"), int)
        and 0 < result["poImgsAfterScroll"] <= PAGES,
        f"有图 {result.get('poImgsAfterScroll')} / {PAGES}",
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
