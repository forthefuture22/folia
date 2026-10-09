#!/usr/bin/env python3
"""e2e41 —— 图片层（深色模式"保持原色"那一层）的内存不变量 + 算子表解析的单测。

这一套是 e2e40 的续集。e2e40 证明了"一张图能保住原色"，但它是**单页、单张图**，
既没碰"一页里有好几块位图"，也没碰"翻完一遍之后该还的还了没有"。这一套补这两块。

## ① 主角：`quadsFromOps` 的单测（`?mode=units`）

`quadsFromOps` 把算子表翻成"图片四边形"。它的正确性**全靠算子表语义**，
靠真 PDF 凑样本很难凑全 —— 所以抽成纯函数、直接喂**合成算子表**。

⚠️ **一段被否证的历史，别再走回去**：这套用例最早的三条判据是"数第 1 页同一行
上有几块"，本意是覆盖 pdf.js 的图片合并算子 `paintImageXObjectRepeat`(88)。
实测发现：

  · 本版本 pdf.js 的 `getOperatorList()` **永远拿不到合并过的算子表** ——
    `OperatorList` 构造时 `streamSink && !(intent & RenderingIntentFlag.OPLIST)`
    才用 `QueueOptimizer`，而 `getOperatorList()` 硬编码传 `isOpList = true`
    （`pdf.mjs`：`getRenderingIntent(intent, …, isEditing, true)`），
    `render()` 那条路才传 `false`。→ 走 `NullOptimizer`，**不合并**。
  · 于是那 6 个戳记在算子表里就是 **6 条独立的 `paintImageXObject`(85)**，
    "关掉 88 号分支"这个补丁**打不到它们** → 主跑绿、对照也绿 → **假绿**。

所以现在改成两条腿：**88/87 分支由 `?mode=units` 喂合成算子表覆盖**
（见下面那 8 条判据），真 PDF 那边只负责"多块位图"和"内存账"。
教训一句话：**补丁打在一条走不到的分支上，对照组就失去了意义。**

## ② 浅色下不该收四边形

收一次四边形 = `page.getOperatorList()` 走**另一个 intent**（比 `render()` 多一个
`OPLIST` 位）→ worker **再解析一遍内容流**、并**多留一份算子表**直到 `page.cleanup()`。
**这不是缓存命中**（`cacheKey` 含 intent，是两条独立记录）。浅色是大多数人的默认
状态，这份钱不该付 → 只在深色下收集；浅色期间渲染的页靠 `pageCanvases` 记下
transform，切到深色时由 `syncDarkImages` **补收**。

判据：浅色开局 `imageQuads == 0` 但 `canvases >= 1`；切深色后 `imageQuads > 0`。

⚠️ **切回浅色时 `pageImageQuads` 不删是设计，不是漏**：`syncDarkImages` 在浅色那支
只 `clearImageLayer`（DOM 里的位图小 canvas 一块不留），但把四边形记录**留着** ——
再切回深色时直接 `paintImageLayer` 就够，**不用**为每一页重跑一遍 worker 的
`getOperatorList`。记录本身是几十个 float，且照样受 `forgetPageImages`（页离开保留
窗口）与 `resetCaches`（换文档）约束，是有界的。所以浅色各拍只判 `layers == 0`。

## ③ 内存不变量：图片层的账必须随页离开保留窗口一起还回去

`pageCanvases` / `pageImageQuads` 的条目在两种情况下清掉：页离开保留窗口
（`releasePage` → `forgetPageImages`）、换文档（`resetCaches`）。
三种压力各验一遍：滚一遍全书 / 反复缩放 / 反复切主题。任何一项**随次数增长**就是漏。

`cacheStats()` 的计数就是为这个加的（见 `public/pdfjs/bridge.js`）：
`imageQuads`（记了几页）/ `quads`（一共几个四边形）/ `canvases`（记了几个页的 canvas）
/ `layers`（DOM 里几块小 canvas）/ `thumbDocs`（缩略图独立实例剩几份）。

## REGRESS 对照组

四条补丁，都能文本替换：

  · 关掉 `paintImageXObjectRepeat`(88) 分支 → `units.repeat` 归零（**1 条红**）
    —— 这条正是"单测真覆盖了 88 分支"的证明（旧版打在这上面却是绿的）。
  · 关掉 `paintInlineImageXObjectGroup`(87) 分支 → `units.group` 归零（**1 条红**）
  · 把 `forgetPageImages` 调用改成空操作 → 滚一遍之后 `imageQuads` / `canvases`
    涨到 24（全书页数）、`quads` 涨到 30 → 内存判据红（**3 条红**）
  · 让浅色下也去收四边形（`if (darkImagesOn)` → `if (true)`）→ 浅色开局
    `imageQuads == 0` 变红（**1 条红**）

预期 **23/29**（实测：主跑 29/29、对照 23/29）。两条**故意不红**的：

  · 滚动的 `layers` 那条 —— DOM 节点是跟着 `.page` 子树一起被 Yew 摘掉的，
    和 map 里那条记录是**两回事**（对照里 `imageQuads=24` 而 `layers=4`）。
    这正是要把它们分开量的原因。
  · 浅色各拍的 `layers == 0` —— 切回浅色时 `syncDarkImages` 会 `clearImageLayer`，
    所以哪怕浅色下也收了（补丁④），切到浅色之后 DOM 照样是空的。补丁④的红只出在
    `浅色开局 imageQuads` 那一条上。

分四次运行（虚拟时钟的预算一次不够跑完四个场景），见 `tests/e2e/README.md`。
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
from theme_pin import pin_light  # noqa: E402

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PAGES = 24
BUDGET = "600000"
# 第 1 页那组重复戳记的个数（与 make_pdf.py 的 STAMP_N 一致）。
STAMP_N = 6
# 保留窗口：`KEEP_BAND = 2` 每侧 + 可视区那几页 → 大约 5~7 页。
# 实测主跑滚到底后是 4（`imageQuads` / `quads` / `canvases` / `layers` 都是 4），
# 留一点余量取 10；对照组会涨到 24，分得很开。
KEEP_WINDOW = 10

REGRESS = os.environ.get("REGRESS") == "1"

REGRESS_PATCHES = [
    # ① 单测确实覆盖了 88 号合并算子分支（旧版这条补丁打上去是**绿的**，因为
    #    真 PDF 的戳记根本不走这条分支 —— 现在由 `?mode=units` 的合成算子表覆盖）。
    (
        "    } else if (fn === OPS.paintImageXObjectRepeat) {",
        "    } else if (false) { // REGRESS: 关掉 88 号合并算子分支",
    ),
    # ② 同理，87 号 inline group 分支。
    (
        "    } else if (fn === OPS.paintInlineImageXObjectGroup) {",
        "    } else if (false) { // REGRESS: 关掉 87 号 inline group 分支",
    ),
    # ③ 页离开保留窗口时不删记录（DOM 那一侧照旧被 Yew 摘掉，所以 `layers` 不会涨）。
    (
        "  forgetPageImages(pageNumber);",
        "  // REGRESS: 不清理",
    ),
    # ④ 浅色下也去收四边形 —— 那条"浅色下 imageQuads == 0"的判据就是钉这个的。
    (
        "      if (darkImagesOn) {",
        "      if (true) { // REGRESS: 浅色下也收",
    ),
]

# 单测期望值（`base = [1,0,0,1,0,0]`，即"PDF 用户空间 = 设备像素"）。
# 单位正方形 `[0,0]–[1,1]` 经 `unitSquareQuad` 摊成 8 个数（四角，顺时针）。
EXPECT = {
    # save / transform([2,0,0,3,10,20]) / paintImageXObject / restore
    "single": [[10, 20, 12, 20, 12, 23, 10, 23]],
    # 裸 inline image：没有 transform → 就是基矩阵下的单位正方形
    "inline": [[0, 0, 1, 0, 1, 1, 0, 1]],
    # form XObject：`paintFormXObjectBegin` 自带 matrix [3,0,0,3,1,1]
    "form": [[1, 1, 4, 1, 4, 4, 1, 4]],
}
# 88 号：5 个位置，只有平移不同 → 5 个四边形，首尾两个点对一下
EXPECT_REPEAT = (5, [0, 0, 2, 0, 2, 3, 0, 3], [40, 0, 42, 0, 42, 3, 40, 3])
# 87 号：3 个 entry → 3 个四边形
EXPECT_GROUP = (3, [5, 5, 6, 5, 6, 6, 5, 6], [9, 9, 10, 9, 10, 10, 9, 10])
# 嵌套 save/restore：第二个 PIXO 必须回到**外层**矩阵，不能被内层污染
EXPECT_NESTED = (2, [0, 0, 2, 0, 2, 2, 0, 2])
# 上限：200 张单图 → 收够 `IMAGE_LAYER_MAX * 4`（= 48 × 4）就停
EXPECT_CAPPED = 192


def build_pdf() -> bytes:
    """直接调用同目录的 make_pdf.py，保持"判据与样本同源"。"""
    out = subprocess.run(
        [sys.executable, str(WORK / "make_pdf.py"), str(WORK / "sample.pdf")],
        check=True,
        capture_output=True,
        text=True,
    )
    print("  " + out.stdout.strip())
    return (WORK / "sample.pdf").read_bytes()


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

    if REGRESS:
        f = SITE / "public" / "pdfjs" / "bridge.js"
        src = f.read_text()
        for old, new in REGRESS_PATCHES:
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
        # dpr 上限那条（`MAX_DPR = 1.5`）要生效，devicePixelRatio 得 > 1.5。
        "--force-device-scale-factor=2",
        *extra,
        url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not m:
        st = re.search(r'<body[^>]*data-e2e41-stage="([^"]*)"', dom)
        raise SystemExit(
            f"页面里没有 harness-result（卡在阶段: {st.group(1) if st else '未知'}）\n"
            + dom[-1200:]
        )
    return json.loads(html.unescape(m.group(1)))


PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    tail = f"   ({detail})" if detail else ""
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{tail}")


def num(v, default=-1):
    return v if isinstance(v, (int, float)) else default


def quads(v):
    """把 harness 里拿到的四边形数组规范化成 `[[float, ...], ...]`。"""
    if not isinstance(v, list):
        return None
    out = []
    for q in v:
        if not isinstance(q, list):
            return None
        out.append([float(x) for x in q])
    return out


def same_quad(a, b, eps=1e-6):
    return a is not None and len(a) == len(b) and all(abs(x - y) <= eps for x, y in zip(a, b))


def main() -> int:
    if not DIST.exists():
        print(f"!! 没有 {DIST}，先跑 `trunk build`")
        return 2

    build_site()
    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {base}   REGRESS={REGRESS}")

    results: dict[str, dict] = {}
    try:
        for mode in ("units", "scroll", "zoom", "theme"):
            dom = run_chrome(
                f"{base}?mode={mode}", ["--dump-dom", f"--virtual-time-budget={BUDGET}"]
            ).stdout
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

    un = results["units"].get("units") or {}
    sc, zm, th = results["scroll"], results["zoom"], results["theme"]
    sc_before, sc_after = sc.get("before") or {}, sc.get("after") or {}
    zm_before, zm_after = zm.get("before") or {}, zm.get("after") or {}
    th_before, th_after = th.get("before") or {}, th.get("after") or {}

    # ------------------------------------------------ 判据本身成立（否则在量空气）
    print("\n---------------- 判据本身成立 ----------------")
    check(
        "四个场景都跑到了 done（没有半路卡住）",
        all(r.get("stage") == "done" for r in results.values()),
        ", ".join(f"{m}={r.get('stage')}" for m, r in results.items()),
    )
    ops = un.get("opsAvailable") or {}
    check(
        "★ OPS 枚举的型号没变（85/86/87/88/83/90）—— 变了说明 pdf.js 升级了，判据要重核",
        ops.get("p85") == 85
        and ops.get("p86") == 86
        and ops.get("p87") == 87
        and ops.get("p88") == 88
        and ops.get("p83") == 83
        and ops.get("p90") == 90,
        f"实测 {ops}",
    )

    # ------------------------------- ① 单测：算子表 → 四边形（纯函数，喂合成算子表）
    print("\n---------------- ① `quadsFromOps` 单测（合成算子表） ----------------")
    for name, want in EXPECT.items():
        got = quads(un.get(name))
        check(
            f"★ `{name}`：{len(want)} 个四边形，坐标对得上",
            got is not None and len(got) == len(want) and same_quad(got[0], want[0]),
            f"实测={got} 期望={want}",
        )

    n, first, last = EXPECT_REPEAT
    got = quads(un.get("repeat"))
    check(
        f"★ `repeat`（**88 号合并算子**）：{n} 个四边形（每个位置一块）",
        got is not None
        and len(got) == n
        and same_quad(got[0], first)
        and same_quad(got[-1], last),
        f"实测={got} 期望 {n} 个，首={first} 尾={last}"
        "（本版本真 PDF 走不到这条分支，只能这样测）",
    )

    n, first, last = EXPECT_GROUP
    got = quads(un.get("group"))
    check(
        f"★ `group`（**87 号 inline group**）：{n} 个四边形",
        got is not None
        and len(got) == n
        and same_quad(got[0], first)
        and same_quad(got[-1], last),
        f"实测={got} 期望 {n} 个",
    )

    got = quads(un.get("masks"))
    check(
        "★ `masks`：4 种遮罩算子一个都不收（单色遮罩是正文/图标，不是位图）",
        got is not None and len(got) == 0,
        f"实测={got} 期望 []",
    )

    n, second = EXPECT_NESTED
    got = quads(un.get("nested"))
    check(
        f"★ `nested`：{n} 个四边形，第二个回到**外层**矩阵（`restore` 真生效）",
        got is not None and len(got) == n and same_quad(got[1], second),
        f"实测={got} 期望第 2 个={second}（内层矩阵 [2,0,0,2,10,10] 没被清掉就是"
        f" [10,10,12,10,12,12,10,12]，那就错了）",
    )

    check(
        f"★ `capped`：200 张单图只收到上限 {EXPECT_CAPPED} 个（收够就停，不白占内存）",
        num(un.get("capped")) == EXPECT_CAPPED,
        f"实测={num(un.get('capped'))} 期望={EXPECT_CAPPED}",
    )

    # ------------------------------------------- ② 浅色下不该收四边形
    print("\n---------------- ② 浅色下不收四边形（切深色时补收） ----------------")
    tb_cache = th_before.get("cache") or {}
    check(
        "★ 浅色开局 `imageQuads == 0`（浅色下不付「再解析一遍内容流」的代价）",
        num(tb_cache.get("imageQuads")) == 0,
        f"imageQuads={num(tb_cache.get('imageQuads'))} 期望 0",
    )
    check(
        "★ 但浅色开局 `canvases >= 1`（记了页与 transform，切深色才有依据补收）",
        num(tb_cache.get("canvases")) >= 1,
        f"canvases={num(tb_cache.get('canvases'))} 期望 ≥1",
    )
    th_samples = th.get("samples") or []
    light = [s for s in th_samples if s.get("dark") is False]
    dark = [s for s in th_samples if s.get("dark") is True]
    light_ly = [num((s.get("cache") or {}).get("layers")) for s in light]
    dark_iq = [num((s.get("cache") or {}).get("imageQuads")) for s in dark]
    dark_ly = [num((s.get("cache") or {}).get("layers")) for s in dark]
    check(
        "★ 浅色各拍图片层 `layers == 0`（DOM 里的位图小 canvas 一块不占）",
        bool(light) and all(v == 0 for v in light_ly),
        f"浅色各拍 layers={light_ly}",
    )
    check(
        "★ 深色各拍 `imageQuads > 0` 且 `layers > 0`（补收 / 重画都成功了）",
        bool(dark) and all(v > 0 for v in dark_iq) and all(v > 0 for v in dark_ly),
        f"深色各拍 imageQuads={dark_iq} layers={dark_ly}",
    )
    check(
        "★ 深色各拍 `imageQuads` 恒定（来回切**不累积**，是覆盖不是追加）",
        bool(dark) and len(set(dark_iq)) == 1,
        f"深色各拍 imageQuads={dark_iq}",
    )

    # ---------------------------------------------------- ③ 内存：滚一遍全书
    print("\n---------------- ③ 内存 · 滚一遍全书 ----------------")
    b_cache, a_cache = sc_before.get("cache") or {}, sc_after.get("cache") or {}
    check(
        "深色开局第 1 页真的记下了四边形（否则后面是在量空气）",
        num(b_cache.get("imageQuads")) >= 1 and num(b_cache.get("canvases")) >= 1,
        f"imageQuads={num(b_cache.get('imageQuads'))} canvases={num(b_cache.get('canvases'))}",
    )
    check(
        "★ 滚到底之后 `imageQuads` 回落到保留窗口量级（不是全书页数）",
        num(a_cache.get("imageQuads")) <= KEEP_WINDOW,
        f"滚前={num(b_cache.get('imageQuads'))} 滚后={num(a_cache.get('imageQuads'))}"
        f" 期望 ≤{KEEP_WINDOW}（不清理时≈{PAGES}）",
    )
    check(
        "★ 滚到底之后四边形总数同样回落（`quads` 不跟着翻过的页数涨）",
        num(a_cache.get("quads")) <= KEEP_WINDOW * 2,
        f"滚前={num(b_cache.get('quads'))} 滚后={num(a_cache.get('quads'))}"
        f" 期望 ≤{KEEP_WINDOW * 2}",
    )
    check(
        "★ 滚到底之后 `canvases` 也回落（`pageCanvases` 和 `pageImageQuads` 一起清）",
        num(a_cache.get("canvases")) <= KEEP_WINDOW,
        f"滚前={num(b_cache.get('canvases'))} 滚后={num(a_cache.get('canvases'))}"
        f" 期望 ≤{KEEP_WINDOW}（不清理时≈{PAGES}）",
    )
    check(
        "★ 滚到底之后图片层的 canvas 也回落（DOM 不累积）",
        num(a_cache.get("layers")) <= KEEP_WINDOW * 3,
        f"滚前={num(b_cache.get('layers'))} 滚后={num(a_cache.get('layers'))}"
        f" 期望 ≤{KEEP_WINDOW * 3}",
    )
    check(
        "★ `releasePage` 真被调用过（滚走的页确实走了释放这条路）",
        num(sc.get("released")) > 0,
        f"调用次数={sc.get('released')} 样本={sc.get('releasedSample')}",
    )
    last_row = sc_after.get("lastRow") or {}
    check(
        "配对：最后一页的图片层照样建起来了（释放没把功能一起清掉）",
        num(last_row.get("rects")) >= 1,
        f"最后一页图片层块数={last_row.get('rects')}",
    )

    # ---------------------------------------------------- ③ 内存：反复缩放
    print("\n---------------- ③ 内存 · 反复缩放 6 次 ----------------")
    samples = zm.get("samples") or []
    peak = max((num(s.get("imageQuads")) for s in samples), default=-1)
    peak_layers = max((num(s.get("layers")) for s in samples), default=-1)
    check(
        "★ 6 次缩放中 `imageQuads` 从没超过保留窗口量级（不随次数累积）",
        0 <= peak <= KEEP_WINDOW,
        f"各次={[num(s.get('imageQuads')) for s in samples]} 峰值={peak} 期望 ≤{KEEP_WINDOW}",
    )
    check(
        "★ 6 次缩放中图片层 canvas 数同样不累积",
        0 <= peak_layers <= KEEP_WINDOW * 3,
        f"各次={[num(s.get('layers')) for s in samples]} 峰值={peak_layers}"
        f" 期望 ≤{KEEP_WINDOW * 3}",
    )
    check(
        "★ 缩放收敛后 `quads` 也没涨（每次重渲染都是覆盖，不是追加）",
        num((zm_after.get("cache") or {}).get("quads"))
        <= max(1, num((zm_before.get("cache") or {}).get("quads"))) + 2,
        f"缩放前={num((zm_before.get('cache') or {}).get('quads'))}"
        f" 缩放后={num((zm_after.get('cache') or {}).get('quads'))}",
    )

    # ---------------------------------------------------- ③ 内存：反复切主题
    print("\n---------------- ③ 内存 · 反复切主题 6 次 ----------------")
    check(
        "★ 来回切之后 `imageQuads` 仍是保留窗口量级",
        num((th_after.get("cache") or {}).get("imageQuads")) <= KEEP_WINDOW,
        f"imageQuads={num((th_after.get('cache') or {}).get('imageQuads'))} 期望 ≤{KEEP_WINDOW}",
    )

    # ---------------------------------------------------- 配对：功能没被搞坏
    print("\n---------------- 配对：多块位图照样被保护 ----------------")
    # ⚠️ 只对**第 1 页**判这个：那 6 个戳记在第 1 页。滚到底之后量的是第 24 页，
    # 它只有一张大图（那条单独判 `lastRow.rects`）。
    for label, res in (
        ("深色开局", sc_before),
        ("缩放后", zm_after),
        ("切主题后", th_after),
    ):
        row = res.get("row") or {}
        check(
            f"第 1 页图片层里同一行上有 {STAMP_N} 块（6 个戳记各自成块）· {label}",
            num(row.get("rowMax")) >= STAMP_N,
            f"rowMax={row.get('rowMax')} 总块数={row.get('rects')}",
        )

    print()
    print(f"{PASS}/{PASS + FAIL} 通过" + ("（REGRESS 对照）" if REGRESS else ""))
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
