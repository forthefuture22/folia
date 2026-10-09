#!/usr/bin/env python3
"""端到端验证 Folia 的三件事：

1. 选中浮层上的四种标注样式（高亮 / 下划线 / 删划线 / 波浪线）+ 复制，
   并且浮层里**不再有**颜色选择；
2. 缩略图与页面比例对齐（框的比例 == 图的比例 == 页面的比例）；
3. 右上角的「撤回 / 保存」。

做法：把 `dist/` 拷一份出来，往 index.html 里插一段桩（替换掉需要真 pdf.js /
真 Tauri 的那几个桥接方法，`getSelectionInfo` 仍用真实实现），然后用无头 Chrome
把整个真实 wasm 应用跑起来，模拟「选文字 → 松开鼠标 → 点按钮」，最后核对结果。

位置核对用的是浏览器自己给的几何（`range.getBoundingClientRect()`），
所以它验证的是**真实坐标换算**，不是我对换算的复述。
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
import os
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import threading
sys.path.insert(0, str(_E2E))
from theme_pin import pin_light  # noqa: E402  (把偏好钉成浅色，用例才与宿主机外观无关)

from PIL import Image

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# selbar.rs 里的常量（实测标定值），用来核对浮层落点。
# 工具栏上是 4 个样式按钮 + 1 个复制按钮，所以比只有单个按钮时宽不少。
SEL_BAR_W = 173.0
SEL_BAR_H = 34.0
SEL_BAR_GAP = 8.0

# 浮层上五个按钮的 title，顺序即源码顺序。
BAR_TITLES = ["高亮", "下划线", "删划线", "波浪线", "复制选中的文字"]
# 四种样式写进 DOM 的类名，顺序即点按顺序。
MARK_CLASSES = ["folia-mark-highlight", "folia-mark-underline", "folia-mark-strike", "folia-mark-wavy"]
KIND_LABELS = ["高亮", "下划线", "删划线", "波浪线"]

HARNESS_CSS = """
<style>
/* 真实应用里字是画在 canvas 上的，文本层是透明的。
   这里反过来：canvas 只画白底，让文本层的字显出来，截图才看得清选区。
   ⚠️ 这里**不能**写 `.textLayer { opacity: 1 }` —— 那正好会把
   「选中底色被容器 opacity 压暗」这个 bug 一起盖掉，判据就永远绿了。 */
.textLayer span { color: #1c1c20 !important; }
</style>
"""

# 自检模式：把**修好之前**的规则重新盖回去，确认判据真的会红。
REGRESS = os.environ.get("REGRESS") == "1"
OLD_CSS = "<style>.textLayer { opacity: 0.25 !important; }</style>" if REGRESS else ""


def build_site() -> None:
    # 用 `cp -R` 而不是 `shutil.copytree`：这个沙箱环境会把 Python 的
    # `shutil.copytree` 代理到宿主机做递归拷贝，而它对 `dist/public/pdfjs/*`
    # 触发文件策略拒绝（PermissionError: ... refused by file policy）。
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    index = (SITE / "index.html").read_text()

    # Trunk 的热重载脚本在 dist 里还留着未替换的占位符，连上去会抛异常，去掉更干净。
    # 注意：`trunk serve` 的产物里有这段、`trunk build` 的产物里没有，
    # 所以是 0 或 1 次都算正常，>1 才是真的异常。
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    index = pin_light(index)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次，不正常"

    # 桩必须排在真实 bridge.js **之后**、应用模块**之前**。
    anchor = '<script type="module" src="public/pdfjs/bridge.js"></script>'
    assert anchor in index, "没找到 bridge.js 的 script 标签"
    index = index.replace(anchor, anchor + '\n    <script type="module" src="/harness-stub.js"></script>')

    assert "</head>" in index
    index = index.replace("</head>", HARNESS_CSS + OLD_CSS + "</head>")

    (SITE / "index.html").write_text(index)
    subprocess.run(["cp", str(WORK / "harness.js"), str(SITE / "harness-stub.js")], check=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd, port


def run_chrome(url: str, extra: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    # 注意：**不要**加 `--user-data-dir` —— 指定之后 `--dump-dom` 会一直挂着不返回
    # （连一个只有 setTimeout 的空白页都跑不完）。不带时 Chrome 自己用临时 profile，正常。
    # 另外 `--screenshot` 必须写成 `--screenshot=路径`（单参数），分开写不报错但不出文件。
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
    url = f"http://127.0.0.1:{port}/index.html"
    print(f"harness url: {url}")

    try:
        dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=20000"]).stdout
        result = extract_result(dom)

        # 截图各跑一遍：一张停在「浮层刚浮出来」，一张停在「四种样式都画完」，
        # 一张停在「悬停在标注上、提示框正显示」。
        # 注意 `--screenshot=` 必须是一个参数（分开写不报错但不出文件）。
        for suffix, name in (
            ("?shot=1", "selbar.png"),
            ("?shot=marks", "marks.png"),
            ("?shot=tip", "tip.png"),
        ):
            run_chrome(
                url + suffix,
                [f"--screenshot={WORK / name}", "--virtual-time-budget=20000"],
            )

        # 选中底色 vs 高亮底色：`::selection` 读不到计算样式，只能读截图像素。
        # 先跑一趟 `--dump-dom` 拿到两个取样点的坐标（布局是确定的，两趟一致）。
        selcmp_dom = run_chrome(
            url + "?shot=selcmp", ["--dump-dom", "--virtual-time-budget=20000"]
        ).stdout
        selcmp = extract_result(selcmp_dom).get("selcmp") or {}
        run_chrome(
            url + "?shot=selcmp",
            [f"--screenshot={WORK / 'selcmp.png'}", "--virtual-time-budget=20000"],
        )
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

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    # ---------------------------------------------------------------- 2. 缩略图对齐
    thumb = result.get("thumb") or {}
    img = thumb.get("img") or {}
    page_ratio = thumb.get("pageRatio")
    check(
        "缩略图框按每页比例内联设定（不再是写死的 1/1.32）",
        bool(thumb.get("inlineAspectRatio")),
        f"style.aspect-ratio={thumb.get('inlineAspectRatio')!r}",
    )
    check(
        "缩略图框的比例 == 页面比例",
        page_ratio is not None
        and thumb.get("frameRatio") is not None
        and abs(thumb["frameRatio"] - page_ratio) <= 0.02,
        f"frame={thumb.get('frameRatio')} page={page_ratio}",
    )
    check(
        "缩略图本身的比例 == 页面比例（没被裁）",
        page_ratio is not None
        and img.get("naturalRatio")
        and abs(img["naturalRatio"] - page_ratio) <= 0.02,
        f"img={img.get('naturalRatio')} page={page_ratio}",
    )
    check(
        "缩略图是 contain（整页缩进来，不是 cover 裁掉一截）",
        img.get("objectFit") == "contain",
        str(img.get("objectFit")),
    )

    # ---------------------------------------------------------------- 1. 浮层工具栏
    bar = result.get("barRect")
    sel = result.get("selection")

    check("松开鼠标后浮层出现", result.get("barPresent") is True)
    check("浮层挂在 .stage 里（内容坐标定位）", result.get("barParentIsStage") is True)
    check("浮层的定位祖先是 .stage", result.get("barOffsetParentIsStage") is True)
    check(
        "浮层完全落在正文可视区内",
        result.get("barInsideStage") is True,
        f"bar={bar} stage={result.get('stageRect')}",
    )
    check("浮层盖在文本层之上", result.get("barAboveTextLayer") is True)

    if bar and sel:
        bar_centre = (bar["left"] + bar["right"]) / 2
        sel_centre = (sel["left"] + sel["right"]) / 2
        check(
            "横向以选区中心对齐",
            abs(bar_centre - sel_centre) <= 1.5,
            f"bar_centre={bar_centre:.2f} sel_centre={sel_centre:.2f} 差={bar_centre - sel_centre:.2f}",
        )
        gap = sel["top"] - bar["bottom"]
        check(
            "贴在选区上方（间隔 = SEL_BAR_GAP）",
            abs(gap - SEL_BAR_GAP) <= 2.0,
            f"实测间隔={gap:.2f} 期望={SEL_BAR_GAP}",
        )

    check(
        "浮层上 5 个按钮：四种样式 + 复制",
        result.get("barButtons") == BAR_TITLES,
        str(result.get("barButtons")),
    )
    check(
        "浮层里没有颜色选择（色板已删除）",
        result.get("barSwatchCount") == 0,
        f"浮层内 .swatch 数 = {result.get('barSwatchCount')}",
    )
    check(
        "颜色选择搬到了顶部工具栏（4 个色板还在）",
        result.get("topSwatchCount") == 4,
        str(result.get("topSwatchCount")),
    )
    check(
        "顶部工具栏按钮从「注释」改名为「高亮」",
        result.get("topMarkButtonText") == "高亮",
        f"文案={result.get('topMarkButtonText')!r} title={result.get('topMarkButtonTitle')!r}",
    )

    # ---- 四种样式：类名
    expected_after = {
        "afterHighlight": MARK_CLASSES[:1],
        "afterUnderline": MARK_CLASSES[:2],
        "afterStrike": MARK_CLASSES[:3],
        "afterWavy": MARK_CLASSES[:4],
    }
    for field, prefix in expected_after.items():
        got = result.get(field) or []
        ok = len(got) == len(prefix) and all(
            cls.split()[1] == want for cls, want in zip(got, prefix)
        )
        check(
            f"{field} 后页面上的批注类名依次为 {'/'.join(c.split('-')[-1] for c in prefix)}",
            ok,
            " | ".join(got),
        )

    check(
        "四条批注都写进了正文层",
        result.get("annotationCount") == 4,
        str(result.get("annotationCount")),
    )
    check(
        "四条批注都带侧栏色点",
        result.get("badgeCount") == 4,
        str(result.get("badgeCount")),
    )
    check(
        "侧栏批注列表标出了样式名",
        sorted(result.get("annKinds") or []) == sorted(KIND_LABELS),
        str(result.get("annKinds")),
    )

    # ---- 四种样式：CSS 真的画出四样东西
    styles = {s["cls"].split()[1]: s for s in (result.get("markStyles") or [])}
    hl = styles.get("folia-mark-highlight", {})
    ul = styles.get("folia-mark-underline", {})
    st = styles.get("folia-mark-strike", {})
    wv = styles.get("folia-mark-wavy", {})

    check(
        "高亮画的是底色（半透明填充）",
        hl.get("bg", "rgba(0, 0, 0, 0)") != "rgba(0, 0, 0, 0)" and hl.get("mask", "none") == "none",
        f"bg={hl.get('bg')} mask={hl.get('mask')}",
    )
    check(
        "下划线画的是下边框",
        ul.get("borderBottom") not in (None, "0px") and ul.get("bg") == "rgba(0, 0, 0, 0)",
        f"border-bottom={ul.get('borderBottom')} bg={ul.get('bg')}",
    )
    check(
        "删划线画在 ::after 中线上",
        st.get("afterHeight") not in (None, "auto", "0px")
        and st.get("afterBg", "rgba(0, 0, 0, 0)") != "rgba(0, 0, 0, 0)",
        f"::after height={st.get('afterHeight')} bg={st.get('afterBg')}",
    )
    check(
        "波浪线用 SVG 遮罩铺出来",
        wv.get("mask", "none") != "none",
        f"mask={(wv.get('mask') or '')[:60]}",
    )
    check(
        "四种样式都走 multiply 混色（不遮住底下的字）",
        {hl.get("blend"), ul.get("blend"), st.get("blend"), wv.get("blend")} == {"multiply"},
        str([hl.get("blend"), ul.get("blend"), st.get("blend"), wv.get("blend")]),
    )

    # ---------------------------------------------------------------- 复制
    spy = result.get("copySpy") or []
    spy_forced = result.get("copySpyForced") or []
    expected = result.get("copyExpected") or ""

    check(
        "点复制时调用了 document.execCommand（且只调一次）",
        len(spy) == 1,
        json.dumps(spy, ensure_ascii=False),
    )
    check(
        "复制的是当前选中的那段文字",
        bool(spy) and spy[0].get("cmd") == "copy" and spy[0].get("value") == expected,
        f"value={spy[0].get('value') if spy else None!r} expected={expected!r}",
    )
    returned = spy[0].get("returned") if spy else None
    check(
        "按 execCommand 的真实返回值如实汇报（不谎报成功）",
        (result.get("copyToastReal") == "复制失败")
        if returned is False
        else (result.get("copyToastReal") or "").startswith("已复制"),
        f"execCommand 返回 {returned} → 提示 {result.get('copyToastReal')!r}",
    )
    check(
        "成功分支的提示带上字数",
        (result.get("copyToastForced") or "") == f"已复制 {len(expected)} 个字",
        f"{result.get('copyToastForced')!r}（期望「已复制 {len(expected)} 个字」）",
    )
    check(
        "强制成功那一趟也把选中的文字放进了临时框",
        bool(spy_forced) and spy_forced[0].get("value") == expected,
        f"value={spy_forced[0].get('value') if spy_forced else None!r}",
    )
    check(
        "复制后临时 textarea 被摘掉了（没留幽灵节点）",
        result.get("textareasAfter") == result.get("textareasBefore"),
        f"前={result.get('textareasBefore')} 后={result.get('textareasAfter')}",
    )
    check("复制后浮层收起（选区已被临时 textarea 顶掉）", result.get("barAfterCopy") is False)

    # ---------------------------------------------------------------- 3. 撤回 / 保存
    check("右上角有撤回按钮", result.get("undoInTopRight") is True)
    check("右上角有保存按钮", result.get("saveInTopRight") is True)
    check(
        "还没改过批注时撤回按钮是灰的",
        result.get("undoDisabledAtStart") is True,
        f"disabled={result.get('undoDisabledAtStart')}",
    )
    check(
        "点撤回删掉了正好一条批注",
        result.get("undoRemoved") == 1,
        f"减少了 {result.get('undoRemoved')} 条",
    )
    check(
        "撤回后给了提示",
        (result.get("undoToast") or "").startswith("已撤回"),
        str(result.get("undoToast")),
    )
    check(
        "一路撤到底后批注清空",
        result.get("afterUndoAll") == 0,
        str(result.get("afterUndoAll")),
    )
    check(
        "栈空后撤回按钮自己变灰",
        result.get("undoDisabledEmpty") is True,
        str(result.get("undoDisabledEmpty")),
    )
    check(
        "点保存真的调了 save_annotations",
        (result.get("saveCalls") or 0) >= 1,
        f"调用 {result.get('saveCalls')} 次",
    )
    check(
        "保存的负载就是当前批注（1 条）",
        result.get("savePayload") == 1,
        str(result.get("savePayload")),
    )
    check(
        "保存后给了提示",
        (result.get("saveToast") or "").startswith("已保存"),
        str(result.get("saveToast")),
    )

    # ---------------------------------------------------------------- 点空白收起
    check("点空白前浮层确实在（这一步不是空验证）", result.get("barBeforeClickAway") is True)
    check("点正文空白处浮层收起", result.get("barAfterClickAway") is False)

    # ---------------------------------------------------------------- 选中底色 vs 高亮底色
    #
    # `::selection` 的底色读不到计算样式，只能读截图像素。
    # `?shot=selcmp` 那趟里：第 0 行标了一条高亮、第 3 行是选区，
    # 两处都在白底上，像素应当**完全一致**。
    sel_px = mark_px = None
    try:
        shot = Image.open(WORK / "selcmp.png").convert("RGB")
        if selcmp.get("sel"):
            sel_px = shot.getpixel((selcmp["sel"]["x"], selcmp["sel"]["y"]))
        if selcmp.get("mark"):
            mark_px = shot.getpixel((selcmp["mark"]["x"], selcmp["mark"]["y"]))
    except FileNotFoundError:
        pass

    check(
        "文本层没有被容器 opacity 压暗（选中底色变淡的根因）",
        selcmp.get("textLayerOpacity") == "1",
        f"computed opacity={selcmp.get('textLayerOpacity')!r}",
    )
    check(
        "底色确实画出来了（不是白页，判据不是空验证）",
        sel_px is not None and sel_px != (255, 255, 255),
        f"选中像素={sel_px}",
    )
    check(
        "选中底色 == 高亮底色（截图里像素级一致）",
        sel_px is not None and sel_px == mark_px,
        f"选中={sel_px} 高亮={mark_px}",
    )

    # ---------------------------------------------------------------- 批注：失焦即保存
    check("批注条目上有「批注」按钮", result.get("noteEditButtonExists") is True)
    check("点「批注」后输入框出现", result.get("noteEditorOpened") is True)
    check(
        "输入框一出现就自动获得焦点（否则用户点开又点走，永远等不到 blur）",
        result.get("noteEditorFocused") is True,
        f"activeElement 就是输入框={result.get('noteEditorFocused')}",
    )
    check(
        "编辑态里没有「保存 / 取消」按钮了",
        result.get("noteEditorHasButtons") is False,
        f".ann-edit .ann-actions 数={result.get('noteEditorHasButtons')}",
    )
    check("失焦后编辑态自动退出", result.get("noteEditorClosed") is True)
    check(
        "失焦就把批注内容存下来了",
        result.get("noteText") == "这是一条测试批注",
        f"列表里显示={result.get('noteText')!r}",
    )
    check(
        "存下来的内容也落了盘（save_annotations 的负载里有它）",
        result.get("noteSavedPayload") == "这是一条测试批注",
        f"负载里的 note={result.get('noteSavedPayload')!r}",
    )

    # ---------------------------------------------------------------- 悬停提示框
    check("悬停前没有提示框", result.get("tipBefore") is False)
    check("悬停在标注上出现提示框", result.get("tipShown") is True)
    check(
        "提示框显示的是批注内容",
        result.get("tipText") == "这是一条测试批注",
        f"文案={result.get('tipText')!r}",
    )
    tip_style = result.get("tipStyle") or {}
    check(
        "提示框是黑底白字",
        tip_style.get("background") == "rgb(28, 28, 32)"
        and tip_style.get("color") == "rgb(255, 255, 255)",
        f"bg={tip_style.get('background')} color={tip_style.get('color')}",
    )
    check(
        "提示框不吃鼠标事件（否则一出现就 mouseleave，闪一下就没）",
        tip_style.get("pointerEvents") == "none",
        str(tip_style.get("pointerEvents")),
    )
    tip_rect = result.get("tipRect")
    mark_rect = result.get("tipMarkRect")
    check(
        "提示框挂在标注上方",
        bool(tip_rect and mark_rect) and tip_rect["bottom"] <= mark_rect["top"] + 1,
        f"tip.bottom={tip_rect.get('bottom') if tip_rect else None} "
        f"mark.top={mark_rect.get('top') if mark_rect else None}",
    )
    check("移开鼠标后提示框收起", result.get("tipAfterMoveAway") is False)

    # ------------------------------------------------ 没批注的标注：悬停**不显示**
    #
    # 用户的要求就一句：「没有批注，hover 则不显示」。
    # 两个分支都要钉：整页一条批注都没有（悬停路径被短路），
    # 以及页面上**有**批注、只有这一条没有（悬停路径在跑）。
    check(
        "找到了一条没批注的标注（下面两条断言才有意义）",
        result.get("bareMarkFound") is True,
        f"标注数={result.get('bareMarkCount')}",
    )
    check(
        "没批注的标注：悬停**不显示**提示框",
        result.get("tipOnNoteLessMark") is False,
        f".mark-tip 存在={result.get('tipOnNoteLessMark')}",
    )
    check(
        "没批注时色点上的原生提示说「添加批注」（不是「查看批注」）",
        result.get("noteLessBadgeTitle") == "添加批注",
        f"title={result.get('noteLessBadgeTitle')!r}",
    )
    check(
        "没批注的标注：右击**仍然**弹菜单（悬停与右击是两套判据）",
        result.get("menuOnNoteLessMark") is True,
        f"[data-ctx-menu=mark] 存在={result.get('menuOnNoteLessMark')}",
    )
    check(
        "右击没批注的标注 → 菜单是「编辑批注 / 删除高亮」",
        result.get("menuOnNoteLessLabels") == ["编辑批注", "删除高亮"],
        str(result.get("menuOnNoteLessLabels")),
    )
    check(
        "右击没批注的标注时 contextmenu 被拦下（不叠系统菜单）",
        result.get("menuEvPrevented") is True,
        f"defaultPrevented={result.get('menuEvPrevented')}",
    )
    check(
        "点别处后菜单收起",
        result.get("menuClosedAfterNoteLess") is False,
    )
    check(
        "页面上已有带批注的标注时，划过没批注的标注依然不显示",
        result.get("tipOnSecondBareMark") is False,
        f"标注数={result.get('marksBeforeBareProbe')} .mark-tip 存在={result.get('tipOnSecondBareMark')}",
    )
    check(
        "反向对照：同一次运行里带批注的那条**照样**弹框（上一条不是空验证）",
        result.get("tipOnNotedMarkAgain") == "这是一条测试批注",
        f"文案={result.get('tipOnNotedMarkAgain')!r}",
    )

    width = (result.get("barOffset") or {}).get("width")
    height = (result.get("barOffset") or {}).get("height")

    # 常量漂移守卫：`selbar.rs` 的 SEL_BAR_W/H 是「实测值」，改了工具栏样式
    # 就会失准 —— 失准的表现是贴边时溢出几个像素，肉眼很难发现。
    # 所以这里拿浏览器量到的真实尺寸反过来卡常量。
    check(
        "selbar.rs 的尺寸常量与浏览器实测一致",
        bool(width and height)
        and abs(width - SEL_BAR_W) <= 1
        and abs(height - SEL_BAR_H) <= 1,
        f"实测 {width}x{height} vs 常量 {SEL_BAR_W:.0f}x{SEL_BAR_H:.0f}",
    )

    print("\n================ checks ================")
    failed = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {name}" + (f"   ({detail})" if detail else ""))

    print("\n================ 常量标定 ================")
    if width and height:
        print(f"实测 .sel-bar 尺寸: {width} x {height}   常量: {SEL_BAR_W} x {SEL_BAR_H}")
        print(f"宽度偏差 {width - SEL_BAR_W:+.0f}px，高度偏差 {height - SEL_BAR_H:+.0f}px")
    print(f"\n{len(checks) - failed}/{len(checks)} 通过；截图: {WORK / 'selbar.png'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
