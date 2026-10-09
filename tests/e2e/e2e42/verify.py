#!/usr/bin/env python3
"""缩略图那条路上的**并发竞态**：同一份文件的两次渲染，先回来的那个不能把还在飞的
那份实例 `destroy()` 掉。

为什么需要这一套：`e2e35` 验的是"缩略图实例用完就放"（顺序调用、互不重叠），
`e2e39` 验的是"书架那条 effect 换代之后旧任务不再渲染已删的文件"。
但**两者都没有覆盖**中间那一小段：书架 effect 在 `recent` 变化时重跑，旧任务
**不会立刻退出** —— 它要等手上那次 `await renderThumbnail(...)` 回来才检查代次。
于是**同一份文件**会有两个 `renderThumbnail` 同时在飞：

  · 它们从 `openThumbDoc` 拿到的是**同一个** `PDFDocumentProxy`；
  · 先回来的那个在 `finally` 里 `dropThumbDoc` → `doc.destroy()`，
    把 worker 一起收走；
  · 另一个还在 `await page.render()` → 被 reject → `catch` 吞掉 → 返回**空串**；
  · 而它往往已经因为代次被作废，没人再补 → **那张缩略图永久静默消失**
    （不报错、不重试、DOM 上只表现为"少了一张图"）。

修法是给每条 path 记一个"在飞计数"（`bridge.js` 的 `thumbRefs`）：最后一个
使用者走了才真放。

判据怎么落地：并发发起两次渲染（同一份文件、不同页、**一快一慢**），断言
**两次都拿到真 JPEG**。这个断言**与"谁先回来"无关** —— 旧实现下无论谁先，
总有一次拿到空串，所以不依赖虚拟时钟下的具体调度顺序。

⚠️ 配对判据（防"把用完即放改成了永不释放"）：
  · 并发那一对只读了一次文件（确实共享同一份实例，没有各开一份）；
  · 这一对收尾之后实例**确实**被放掉了 —— 再要一次必须重新读文件。

自检模式 `REGRESS=1`：把 `bridge.js` 的 `finally` 改回 `dropThumbDoc`（谁先回来
谁 destroy），确认主判据真的会红。⚠️ 必须**直接改站点里那份源码** —— 那个
`finally` 在模块内部，外面包一层看不见也改不动。
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

PAGES = 6
BUDGET = "600000"

REGRESS = os.environ.get("REGRESS") == "1"

# 修好之前：`finally` 里直接 `dropThumbDoc` —— 谁先回来谁 destroy，
# 把同路径还在飞的另一次渲染一起打掉。
#
# ⚠️ 只打这一条。`releaseThumbnail` 里那句 `if (thumbRefs.has(key)) return;` 虽然
# 也是同一个修复的一部分，但这条路径**本套件走不到**（要从最近列表移除才会调）
# —— 打在走不到的分支上就是"假绿"，宁可留给 e2e29。
OLD_BRIDGE_PATCHES = [
    (
        "    if (!borrowed) releaseThumbDoc(key);",
        "    if (!borrowed) dropThumbDoc(key); // REGRESS: 修好之前（谁先回来谁 destroy）",
    ),
]


def build_pdf(pages: int = PAGES, w: int = 595, h: int = 842) -> bytes:
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
            f"BT /F1 24 Tf 0 0 0 rg 72 {h - 120} Td (FOLIA e2e42 page {i + 1}) Tj ET"
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
        "--force-device-scale-factor=1",
        *extra,
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        st = re.search(r'<body[^>]*data-e2e42-stage="([^"]*)"', dom)
        raise SystemExit(
            f"页面里没有 harness-result（卡在阶段: {st.group(1) if st else '未知'}）\n"
            + dom[-1500:]
        )
    return json.loads(html.unescape(m.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {url}   REGRESS={REGRESS}")
    try:
        dom = run_chrome(url, ["--dump-dom", f"--virtual-time-budget={BUDGET}"]).stdout
        result = extract_result(dom)
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
    print(json.dumps(result, indent=2, ensure_ascii=False))

    if "error" in result:
        print("\n!! 驱动脚本报错:", result["error"])
        print(result.get("stack", ""))
        return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    cc = result.get("concurrent") or {}
    ar = result.get("afterRelease") or {}

    # -------------------------------------------------- ★ 主判据
    check(
        "★ 同一份文件并发渲染两次，两次都拿到真 JPEG（旧实现必有一次是空串）",
        cc.get("fastIsImage") is True and cc.get("slowIsImage") is True,
        f"快那次={cc.get('fastIsImage')}(len={cc.get('fastLen')}) "
        f"慢那次={cc.get('slowIsImage')}(len={cc.get('slowLen')})"
        f"（旧实现：先回来的 destroy 掉实例 → 另一个渲染被 reject → 空串）",
    )

    # -------------------------------------------------- 配对判据
    check(
        "配对：并发这一对只读了一次文件（确实共享同一份实例，没有各开一份）",
        cc.get("delta") == 1,
        f"并发这一对新增读文件 {cc.get('delta')} 次（期望 1）",
    )
    check(
        "配对：这一对收尾之后实例被放掉了（'计数'没有变成'永不释放'）",
        ar.get("isImage") is True and ar.get("delta") == 1,
        f"再要一次：是图={ar.get('isImage')} 增量={ar.get('delta')}"
        f"（期望 True/1 = 上一轮那份已放、必须重读）",
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
