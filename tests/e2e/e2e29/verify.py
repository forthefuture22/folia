#!/usr/bin/env python3
"""验证「被最近列表挤掉的 PDF，缩略图缓存会不会被释放」（e2e29）。

这是一个**内存泄露**的回归套件。

背景：书架给每个「最近」条目渲染首页缩略图，`bridge.js` 把每份 PDF 的
`PDFDocumentProxy` 按 path 缓存进 `thumbDocs` —— 那个代理扣着整份 PDF 的字节
与解析结果，只有 `releaseThumbnail(path)` 会放掉它。

原先只有「删除记录」和「清空最近打开」两处记得放，而 `record_open` 里那句
`list.truncate(10)` 把第 11 条挤出去时**没人放** —— 那份代理就永久常驻了。
而且 Rust 侧那份 `thumbs` 快照还留着它的 id，以后也不会再请求它，等于再也
回收不掉：缓存按"本次会话出现过的所有文件"增长，书架却只显示 10 个。

修法把「前后差集」放进 `write_recent`（最近列表唯一的读写入口），
消失的 path 一律释放。这个套件钉两件事：

  ★ 被挤出去的那两份**调了** releaseThumbnail
  ★ 还在列表里的那十份**没被误伤**

只桩 Tauri IPC 与 bridge 的两个方法，其余（含真实 wasm）全走真实现。
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

N = 12  # 打开多少份（要 > 10，否则挤不掉任何东西）
CAP = 10  # 最近列表上限，对齐 handles.rs 里的 truncate(10)
ALL = [f"evict-{i}.pdf" for i in range(1, N + 1)]
EVICTED = ALL[: N - CAP]  # 被挤出去的（最早打开的那几份）
KEPT = ALL[N - CAP :]  # 还在列表里的


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


def run_chrome(url: str, timeout: int = 240) -> str:
    cmd = [
        CHROME,
        "--headless=new",
        "--no-sandbox",
        "--disable-gpu",
        "--hide-scrollbars",
        "--window-size=1200,900",
        "--force-device-scale-factor=1",
        "--dump-dom",
        # 这个套件要顺序打开 12 份文档，每份都等一次渲染 —— 预算给足。
        "--virtual-time-budget=240000",
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
    try:
        res = extract_result(run_chrome(f"http://127.0.0.1:{port}/index.html"))
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(json.dumps(res, indent=2, ensure_ascii=False))

    if "error" in res:
        print("\n!! 驱动脚本报错:", res["error"])
        print(res.get("stack", ""))
        return 1

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    rendered = res.get("rendered") or []
    released = res.get("released") or []
    card_names = res.get("cardNames") or []
    saved_names = res.get("savedNames") or []

    # ⚠️ 口径要统一：harness 记的是**完整路径**（`/tmp/evict-1.pdf`），
    # 而书架卡片上显示的是**文件名**（`evict-1.pdf`）。第一版忘了这茬，
    # 拿文件名去比路径，于是"修复明明生效了"却报了两条 FAIL ——
    # 断言写错方向，比没有断言更浪费时间。
    def base(p: str) -> str:
        return str(p).rsplit("/", 1)[-1]

    released_base = {base(p) for p in released}
    rendered_base = {base(p) for p in rendered}

    # ---------------------------------------------------------------- 判据成立的前置
    #
    # 少了这几条，下面那些"没被误伤"的断言在一个"什么都没发生"的页面上**照样会绿**。
    check(
        f"判据本身成立：{N} 份都真的打开了（不是空跑）",
        res.get("tabCount") == N,
        f"标签数={res.get('tabCount')}",
    )
    check(
        "判据本身成立：书架真的请求过缩略图（否则下面是在量空气）",
        len(rendered) >= CAP,
        f"renderThumbnail 请求过 {len(rendered)} 份",
    )
    check(
        f"最近列表确实被截断了：书架只剩 {CAP} 张卡片",
        len(card_names) == CAP,
        f"卡片={card_names}",
    )
    check(
        "落盘的列表也是 10 条，且和书架一致",
        len(saved_names) == CAP and saved_names == card_names,
        f"落盘={saved_names}",
    )

    # ---------------------------------------------------------------- ★ 核心断言
    check(
        f"★ 被挤出去的那 {len(EVICTED)} 份**调了** releaseThumbnail",
        released_base >= set(EVICTED),
        f"期望 ⊇ {EVICTED}；实际释放={released}",
    )
    check(
        "★ 还在列表里的那 10 份**没有被误伤**（一个都没释放）",
        not (released_base & set(KEPT)),
        f"被误伤的={sorted(released_base & set(KEPT))}；实际释放={released}",
    )
    check(
        "释放的总数正好等于被挤掉的份数（没多放也没少放）",
        len(released) == len(EVICTED),
        f"释放 {len(released)} 次，期望 {len(EVICTED)} 次；实际={released}",
    )
    check(
        "★ 被释放的那几份**此前确实被请求过**缩略图（'释放'不是空动作）",
        # ⚠️ 必须带上 `released_base and` 这一半。只写 `EVICTED <= rendered_base`
        # 的话，当 released 是空的时候它**照样绿** —— 因为它压根没看 released。
        # 这条本来是"证明释放的不是空气"，结果自己变成了空气（关掉修复实测过）。
        bool(released_base) and released_base <= rendered_base,
        f"释放={released}；请求过={rendered}",
    )
    check(
        "书架上剩下的正是最后打开的那 10 份（顺序也对）",
        card_names == KEPT[::-1],
        f"卡片={card_names} 期望={KEPT[::-1]}",
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
