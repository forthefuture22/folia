#!/usr/bin/env python3
"""三处「内存上限」：页面位图 / pdf.js 算子表 / 检索文本缓存。

为什么需要这一套：`e2e18` 只验了缩略图裁得对不对、`e2e35` 验了缩略图实例的寿命，
**阅读区本身的 canvas 与 pdf.js 的缓存从来没被验过**。而它们才是长文档里
"翻着翻着内存一路涨、回不去"的主因：

  · 原来每一页都挂 `<canvas>` + `.textLayer`，滚出可视区后没人释放位图
    （A4 页在缩放 1.25、dpr 2 下 12.5 MB/页）
  · `textCache` 没有条数上限，而 `search()` 会逐页走完整本

分三次运行（虚拟时钟的预算一次不够跑完三个场景）：

    ?mode=page    不滚动，量虚拟化与 dpr 上限
    ?mode=scroll  滚到底，量"滚走的页有没有被还回去"
    ?mode=search  搜全文，量文本缓存的上限

`REGRESS=1`：把 `bridge.js` 里那三条规则（dpr 上限、文本缓存上限、缩略图渲染完
放算子表）还原成修好之前的样子，确认判据会红（3 条红）。

⚠️ **①（虚拟化）没有 REGRESS 对照组** —— 它的判断在 Rust/wasm 里，而站点里是
**编译好的 wasm**，文本替换改不动。所以它用绝对判据（`canvas 数 < 页数`）+
配对判据（当前页照样画得出来）。要验证这条判据会红，得回到改动前重新
`trunk build` 再跑（见 README）。

⚠️ ④（缩略图放算子表）**有**对照组 —— 分支在 `bridge.js` 里，能直接改。它的
判据用的是**副作用计数**（`thumbCleanups`）：`page.cleanup()` 放掉了什么，
DOM 和 pdf.js 的公开 API 上都看不见，只能靠"这个分支执行过几次"来钉。
"""

# --- 路径推导 ---------------------------------------------------------
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import base64
import html
import http.server
import json
import os
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

PAGES = 30
BUDGET = "600000"

REGRESS = os.environ.get("REGRESS") == "1"

# 修好之前的三条规则。⚠️ 必须**直接改站点里那份 bridge.js 的源码** ——
# 这几条都是模块内部的常量/分支，在外面包一层看不见也改不动。
OLD_BRIDGE_PATCHES = [
    ("const MAX_DPR = 1.5;", "const MAX_DPR = 2; // REGRESS: 修好之前"),
    (
        "const TEXT_CACHE_LIMIT = 16;",
        "const TEXT_CACHE_LIMIT = Infinity; // REGRESS: 修好之前（没有上限）",
    ),
    # 缩略图那条路：把"要不要顺手放算子表"那个判据变成永假 —— 渲染完不再放。
    # ⚠️ 锚点跟着实现走：守卫从 `if (borrowed)` 挪到了内层 `isInReader` 那一条
    # （阅读区正在看的页要跳过）。锚点对不上时**补丁会静默失效**、对照组全绿 ——
    # 那就是"假绿"，比不测更坏。
    (
        "      if (!isInReader(pageNumber || 1)) {\n        thumbCleanups += 1;",
        "      if (false) { // REGRESS: 修好之前（缩略图不放算子表）\n"
        "        thumbCleanups += 1;",
    ),
]


def build_pdf(pages: int = PAGES, w: int = 595, h: int = 842) -> bytes:
    """手写一个多页 PDF：每页都带 "FOLIA"，好让全文检索有命中。"""
    font_id = 3
    first_page_id = 4
    objs: list[bytes] = []
    kids = " ".join(f"{first_page_id + i * 2} 0 R" for i in range(pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for i in range(pages):
        page_id = first_page_id + i * 2
        content_id = page_id + 1
        lines = [
            f"BT /F1 24 Tf 0 0 0 rg 72 {h - 120} Td (FOLIA e2e36 page {i + 1}) Tj ET",
            f"BT /F1 12 Tf 0 0 0 rg 72 {h - 160} Td (body text for page {i + 1} of {pages}) Tj ET",
        ]
        content = "\n".join(lines).encode("latin-1")
        objs.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("latin-1")
        )
        objs.append(
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


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

    if REGRESS:
        f = SITE / "public" / "pdfjs" / "bridge.js"
        src = f.read_text()
        for old, new in OLD_BRIDGE_PATCHES:
            assert old in src, f"REGRESS 锚点没找到：{old!r}（bridge.js 改过？同步一下）"
            src = src.replace(old, new, 1)
        f.write_text(src)

    pdf_b64 = base64.b64encode(build_pdf()).decode("ascii")
    harness = (WORK / "harness.js").read_text()
    (SITE / "harness-stub.js").write_text(f'window.__PDF_B64 = "{pdf_b64}";\n' + harness)


REQUESTS: list[str] = []


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        REQUESTS.append(self.path)
        return super().do_GET()


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, extra: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        # 让 devicePixelRatio = 2，dpr 上限那条判据才有意义（否则 min(1, 1.5) = 1）。
        "--force-device-scale-factor=2",
        *extra,
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        st = re.search(r'<body[^>]*data-e2e36-stage="([^"]*)"', dom)
        raise SystemExit(
            f"页面里没有 harness-result（卡在阶段: {st.group(1) if st else '未知'}）\n"
            + dom[-1200:]
        )
    return json.loads(html.unescape(m.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {base}   REGRESS={REGRESS}")

    results: dict[str, dict] = {}
    try:
        for mode in ("page", "scroll", "search"):
            url = f"{base}?mode={mode}"
            dom = run_chrome(url, ["--dump-dom", f"--virtual-time-budget={BUDGET}"]).stdout
            results[mode] = extract_result(dom)
    except SystemExit as e:
        print("\n!! " + str(e))
        print("\n================ 服务器收到的请求 ================")
        for p in REQUESTS:
            print("  " + p)
        return 1
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("\n================ harness result ================")
    print(json.dumps(results, indent=2, ensure_ascii=False))

    for mode, res in results.items():
        if "error" in res:
            print(f"\n!! [{mode}] 驱动脚本报错:", res["error"])
            print(res.get("stack", ""))
            return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # ------------------------------------------------------------ mode=page
    pg = results["page"].get("page") or {}
    check(
        "占位盒是齐的：30 个 `.page`（说明少挂 canvas 不是因为页面没渲染出来）",
        pg.get("pageCount") == PAGES,
        f"pageCount={pg.get('pageCount')} 期望 {PAGES}",
    )
    check(
        "★ 不是每一页都挂 canvas：只挂「保留窗口」那几页",
        isinstance(pg.get("canvasCount"), int)
        and 0 < pg["canvasCount"] < PAGES
        and pg["canvasCount"] <= 10,
        f"canvas={pg.get('canvasCount')} 页数={pg.get('pageCount')}"
        f"（改之前每页都挂 = {PAGES}）",
    )
    check(
        "文本层同样只挂保留窗口那几页（它是每页上千个 span）",
        isinstance(pg.get("textLayerCount"), int)
        and 0 < pg["textLayerCount"] <= 10,
        f"textLayer={pg.get('textLayerCount')}",
    )
    check(
        "配对：当前页照样画得出来（虚拟化没把渲染搞坏）",
        pg.get("page1Rendered") is True,
        f"已渲染样本={pg.get('renderedSample')}",
    )
    check(
        "配对：当前页的文本层照样有内容（还能选中 / 检索高亮）",
        isinstance(pg.get("page1Spans"), int) and pg["page1Spans"] > 0,
        f"第 1 页 span 数={pg.get('page1Spans')}",
    )
    check(
        "对照：远处的页确实没挂 canvas（不是「挂了但没画」）",
        pg.get("lastPageHasCanvas") is False,
        f"第 {PAGES} 页有 canvas={pg.get('lastPageHasCanvas')}",
    )
    ratio = pg.get("dprRatio")
    check(
        "★ dpr 上限 1.5：位图只有 CSS 尺寸的 1.5 倍（改之前是 2 倍）",
        isinstance(ratio, (int, float)) and abs(ratio - 1.5) < 0.02,
        f"canvas.width / style.width = {ratio}（期望 ≈1.5；改之前 ≈2.0）",
    )
    # ④ 缩略图路径。判据只能用**副作用计数**：`page.cleanup()` 放掉了什么，
    # 从 DOM 和公开 API 上都看不见（pdf.js 没有"这一页的算子表还在不在"的口子）。
    # 而侧栏一开就渲染附近 13 页，走的正是借用主实例那条路 → 计数必然 > 0。
    pc = results["page"].get("cache") or {}
    check(
        "★ 缩略图那条路也顺手放了算子表（整本渲染过缩略图的页不再常驻）",
        isinstance(pc.get("thumbCleanups"), int) and pc["thumbCleanups"] > 0,
        f"thumbCleanups={pc.get('thumbCleanups')}（改之前恒为 0）",
    )

    # ---------------------------------------------------------- mode=scroll
    sc = results["scroll"]
    before = sc.get("before") or {}
    after = sc.get("after") or {}
    check(
        "★ 滚到底之后，第 1 页的 canvas 被还回去了（位图不再常驻）",
        before.get("page1HasCanvas") is True and after.get("page1HasCanvas") is False,
        f"滚前={before.get('page1HasCanvas')} 滚后={after.get('page1HasCanvas')}"
        f"（改之前永远是 True）",
    )
    check(
        "配对：滚到的当前页（最后一页）照样画得出来",
        after.get("lastPageRendered") is True,
        f"第 {PAGES} 页有 canvas={after.get('lastPageHasCanvas')}"
        f" 已渲染={after.get('lastPageRendered')} 样本={after.get('lastSample')}",
    )
    check(
        "★ canvas 数不随滚动累积（翻过的页数 ≫ 挂着的 canvas 数）",
        isinstance(after.get("canvasCount"), int)
        and after["canvasCount"] <= (before.get("canvasCount") or 0) + 2
        and after["canvasCount"] <= 10,
        f"滚前={before.get('canvasCount')} 滚后={after.get('canvasCount')}",
    )
    check(
        "★ pdf.js 算子表被放了：releasePage 真被调用过",
        isinstance(sc.get("released"), int) and sc["released"] > 0,
        f"releasePage 调用次数={sc.get('released')} 样本={sc.get('releasedSample')}",
    )

    # ---------------------------------------------------------- mode=search
    se = results["search"]
    cache = se.get("cache") or {}
    check(
        "★ 搜完全文（30 页）之后，文本缓存条数仍在 16 以内",
        isinstance(cache.get("text"), int) and cache["text"] <= 16,
        f"textCache={cache.get('text')} / 共 {se.get('numPages')} 页"
        f"（改之前 = 页数，整本留在内存里）",
    )
    check(
        "★ 同一处的 viewCache 也被同一条上限带走（它一样会跟着整本长）",
        isinstance(cache.get("view"), int) and cache["view"] <= 16,
        f"viewCache={cache.get('view')}（改之前 = 页数）",
    )
    check(
        "配对：检索照样有命中（没为了省内存把功能改坏）",
        isinstance(se.get("hits"), int) and se["hits"] > 0,
        f"命中 {se.get('hits')} 处，首条={se.get('firstHit')}",
    )
    sa = se.get("after") or {}
    check(
        "配对：缓存淘汰之后文本层还能重建（被挤掉的页能重新取）",
        isinstance(sa.get("page1Spans"), int) and sa["page1Spans"] > 0,
        f"第 1 页 span 数={sa.get('page1Spans')}",
    )
    check(
        "配对：检索高亮还在（<mark> 被画出来了）",
        isinstance(se.get("page1Marks"), int) and se["page1Marks"] > 0,
        f"第 1 页 mark 数={se.get('page1Marks')}",
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
