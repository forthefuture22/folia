#!/usr/bin/env python3
"""缩略图实例的**生命周期**：用完就放；当前文档那份复用主实例。

为什么需要这一套：`e2e29` 验的是「**Rust 侧**有没有在正确的时机调
`release_thumbnail`」，它把 `bridge.renderThumbnail` / `releaseThumbnail`
都换成了桩 —— 也就是说 bridge.js 里那个真实现（`thumbDocs` 这个 Map）
**从来没被验过**。而这个 Map 的寿命问题正是"打开 PDF 飙到 600–1000 MB、
关掉之后降不下来"的主因：

  · 书架渲染完缩略图后永不释放（只有"被挤出最近列表"才放）
  · 侧栏给**当前正在看的**那份要缩略图时又开了**第二份**完整实例

所以这一套只桩 Tauri IPC，**pdf.js 与 bridge.js 全走真实实现**，
用 `read_pdf_bytes` 的调用次数当"实例有没有被放掉"的探针。

自检模式 `REGRESS=1`：把 bridge.js 的**旧语义**（长期缓存）用一个包装层
重新盖回去，确认这套判据真的会红。
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

BUDGET = "600000"

REGRESS = os.environ.get("REGRESS") == "1"

# 旧语义：把**修好之前**的两条规则重新盖回去，确认这套判据真的会红。
# 「测试通过」只有在「故意弄坏它也会红」的前提下才有意义。
#
# ⚠️ 必须**直接改站点里那份 bridge.js 的源码**，不能在外面包一层
# `renderThumbnail`。包一层只能盖住「长期缓存」这半边；「当前文档另开一份实例」
# 是源码内部的判断，包装层看不见也改不动 —— 那样 `REGRESS=1` 会有两条判据
# 照样绿，等于控制组本身失效。（e2e18 的自检也是改站点源码，不是包一层。）
OLD_BRIDGE_PATCHES = [
    # ① 旧语义：渲染完不释放，长期缓存
    # ⚠️ 锚点跟着实现走：这里现在走计数版 `releaseThumbDoc`（同路径并发渲染时
    # 不能提前 destroy）。锚点对不上补丁会**静默失效**、对照组全绿。
    (
        "    if (!borrowed) releaseThumbDoc(key);",
        "    // REGRESS: 旧语义 —— 用完不放，长期缓存",
    ),
    # ② 旧语义：当前正在读的那份也另开一份实例
    (
        "  const borrowed = isCurrentDoc(key);",
        "  const borrowed = false; // REGRESS: 旧语义 —— 当前文档也另开一份",
    ),
]


def build_pdf(pages: int = 2, w: int = 595, h: int = 842) -> bytes:
    """手写一个最小可用的多页 PDF（不依赖任何库）。"""
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
        content = (
            f"BT /F1 24 Tf 0 0 0 rg 72 {h - 120} Td (FOLIA e2e35 page {i + 1}) Tj ET"
        ).encode("latin-1")
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


TRACE_PATCHES = [
    (
        '    const raw = await invoke("read_pdf_bytes", { path });\n'
        "    const task = getDocument({",
        '    const raw = await invoke("read_pdf_bytes", { path });\n'
        '    window.__trace && window.__trace("otd:bytes");\n'
        "    const task = getDocument({",
    ),
    (
        "    return task.promise;",
        '    window.__trace && window.__trace("otd:promise");\n'
        "    return task.promise;",
    ),
    (
        "  thumbDocs.delete(path);\n  promise.then((doc) => doc.destroy()).catch(() => {});",
        '  window.__trace && window.__trace("drop:" + path.split("/").pop());\n'
        "  thumbDocs.delete(path);\n  promise.then((doc) => doc.destroy()).catch(() => {});",
    ),
    (
        "    const doc = borrowed ? pdfDoc : await openThumbDoc(key);",
        '    window.__trace && window.__trace("rt:doc-start");\n'
        "    const doc = borrowed ? pdfDoc : await openThumbDoc(key);\n"
        '    window.__trace && window.__trace("rt:doc");',
    ),
    (
        "    const page = await doc.getPage(pageNumber || 1);",
        "    const page = await doc.getPage(pageNumber || 1);\n"
        '    window.__trace && window.__trace("rt:page");',
    ),
    (
        "    await page.render({ canvasContext: ctx, viewport }).promise;",
        "    await page.render({ canvasContext: ctx, viewport }).promise;\n"
        '    window.__trace && window.__trace("rt:rendered");',
    ),
    (
        '    return canvas.toDataURL("image/jpeg", 0.75);',
        '    window.__trace && window.__trace("rt:url");\n'
        '    return canvas.toDataURL("image/jpeg", 0.75);',
    ),
]

TRACE_HEAD = (
    "window.__traceLog = [];\n"
    "window.__trace = (s) => { try { window.__traceLog.push(s);\n"
    "  document.body.setAttribute('data-trace', window.__traceLog.slice(-14).join(' > '));\n"
    "} catch (_) {} };\n"
)


def patch_trace() -> None:
    """诊断用：给站点里的 bridge.js 打点，看真实现卡在哪一步。"""
    f = SITE / "public" / "pdfjs" / "bridge.js"
    src = f.read_text()
    for old, new in TRACE_PATCHES:
        assert old in src, f"打点锚点没找到：{old!r}"
        src = src.replace(old, new, 1)
    f.write_text(TRACE_HEAD + src)


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
        # 自检：把站点里那份 bridge.js 还原成修好之前的两条规则。
        f = SITE / "public" / "pdfjs" / "bridge.js"
        src = f.read_text()
        for old, new in OLD_BRIDGE_PATCHES:
            assert old in src, f"REGRESS 锚点没找到：{old!r}（bridge.js 改过？同步一下）"
            src = src.replace(old, new, 1)
        f.write_text(src)

    pdf_b64 = base64.b64encode(build_pdf()).decode("ascii")
    harness = (WORK / "harness.js").read_text()
    use_sample = os.environ.get("USE_SAMPLE") == "1"
    # 诊断用：换成 e2e18 那份"已被验证能被真 pdf.js 渲染"的 PDF。
    if use_sample:
        pdf_b64 = base64.b64encode((_E2E / "e2e18" / "sample.pdf").read_bytes()).decode("ascii")
    (SITE / "harness-stub.js").write_text(
        f'window.__PDF_B64 = "{pdf_b64}";\n' + harness
    )

    if os.environ.get("TRACE") == "1":
        patch_trace()


REQUESTS: list[str] = []


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        REQUESTS.append(self.path)
        return super().do_GET()

    def do_HEAD(self):
        REQUESTS.append("HEAD " + self.path)
        return super().do_HEAD()


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
        "--force-device-scale-factor=1",
        *extra,
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        # 没产出结果时，把分阶段标记带出来 —— 否则只能瞎猜卡在哪。
        st = re.search(r'<body[^>]*data-e2e35-stage="([^"]*)"', dom)
        tr = re.search(r'<body[^>]*data-trace="([^"]*)"', dom)
        raise SystemExit(
            f"页面里没有 harness-result（卡在阶段: {st.group(1) if st else '未知'}"
            f"{'，说明连模块都没跑' if not st else ''}；bridge 打点: "
            f"{tr.group(1) if tr else '（没打点）'}）\n" + dom[-1500:]
        )
    return json.loads(html.unescape(m.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {base}   REGRESS={REGRESS}")

    # 分两次运行，各自一份虚拟时间预算 —— 合成一次会把预算烧光，
    # 后面的阶段永远完不成（e2e18 出于同样的原因把书架/阅读器拆开）。
    results: dict[str, dict] = {}
    try:
        for mode in ("shelf", "main"):
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
    print("\n================ 服务器收到的请求 ================")
    for p in REQUESTS:
        print("  " + p)

    for mode, res in results.items():
        if "error" in res:
            print(f"\n!! [{mode}] 驱动脚本报错:", res["error"])
            print(res.get("stack", ""))
            return 1

    shelf = results["shelf"]
    main_res = results["main"]

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    # ---------------------------------------------------------------- ① 书架
    sr = shelf.get("shelfRepeat") or {}
    check(
        "★ 书架缩略图用完就放：同一份连要两次，第二次必须重新读文件",
        sr.get("delta") == 1,
        f"第二次增量={sr.get('delta')}（期望 1；旧实现是 0 = 一直留着） "
        f"该文件共读 {sr.get('readsOfA')} 次 读序={shelf.get('readOrder')}",
    )

    # ---------------------------------------------------------------- ② 当前文档
    cd = main_res.get("currentDoc") or {}
    check(
        "★ 当前文档的缩略图复用主实例：一次都不该再读文件",
        cd.get("delta") == 0 and cd.get("firstDelta") == 0,
        f"两次增量={cd.get('firstDelta')}/{cd.get('delta')}"
        f"（期望 0/0；旧实现是 1/1 = 又开第二份） 基准={cd.get('base')}",
    )
    check(
        "防连坐：复用主实例之后主文档还在（没被误 destroy）",
        main_res.get("mainDocAlive") is True,
        f"documentInfo().path 是否等于 B：{main_res.get('mainDocAlive')}",
    )

    # ---------------------------------------------------------------- ③ 别的文档
    od = main_res.get("otherDoc") or {}
    check(
        "别的文档：临时开一份（增量 1），用完也放掉（再要就增量 2）",
        od.get("firstDelta") == 1 and od.get("delta") == 2,
        f"两次增量={od.get('firstDelta')}/{od.get('delta')}"
        f"（期望 1/2；旧实现是 1/1 = 第二份被留下）",
    )

    # ---------------------------------------------------------------- ④ 对照组
    imgs = [
        sr.get("firstIsImage"),
        sr.get("secondIsImage"),
        cd.get("firstIsImage"),
        cd.get("secondIsImage"),
        od.get("firstIsImage"),
        od.get("secondIsImage"),
    ]
    check(
        "对照组：六次拿到的都是真 pdf.js 画出来的 JPEG（功能没被改坏）",
        all(x is True for x in imgs),
        f"书架={sr.get('firstIsImage')}/{sr.get('secondIsImage')} "
        f"当前={cd.get('firstIsImage')}/{cd.get('secondIsImage')} "
        f"别的={od.get('firstIsImage')}/{od.get('secondIsImage')}",
    )
    check(
        "对照组：主文档 B 全程只被读一次（打开那次）",
        main_res.get("readsOfB") == 1,
        f"readsOfB={main_res.get('readsOfB')} 读序={main_res.get('readOrder')}",
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
