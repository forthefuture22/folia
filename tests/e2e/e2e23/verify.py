#!/usr/bin/env python3
"""验证深色模式适配（/tmp/e2e23）。

跑两趟无头 Chrome（`?case=light` / `?case=dark`），把同一批选择器在
两套主题下**算出来的样式**抄回来比对。深色模式是纯 CSS 特性，没有"行为"可测 ——
能测的是「两套主题确实不一样，且各自等于预期值」。

关键点：主题类由 index.html 里那段**真实的**同步脚本挂上。
无头 Chrome 切不了系统偏好，所以 verify.py 在主题脚本**之前**塞了一段
`window.matchMedia` 桩，把偏好钉成当前用例要的那个值（light 钉 light、dark 钉 dark），
让真实脚本自己去读、自己去挂类 —— 测的是真实链路。
两个用例都钉死是必须的：宿主机入夜会自动转深色，只钉 dark 的话，
`case=light` 那趟读到的就是宿主的真实偏好，系统一变颜色用例就凭空红。

做法同 e2e17~22：拷 `dist/`、只桩 Tauri IPC、无头 Chrome 跑真实 wasm。
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

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 在主题脚本之前插入：把 `prefers-color-scheme` **钉死**成当前用例要的那个值。
#
# 两个用例都要钉，不能只钉 dark：宿主机的系统外观是会变的
# （macOS 日落自动转深色 —— 白天跑绿、晚上跑红的用例就是这么来的）。
# 只钉 dark 的话，`case=light` 那趟读到的就是宿主的真实偏好，
# 系统一旦入夜，"浅色那趟不该挂 theme-dark" 这条断言就凭空红了。
THEME_STUB = """    <script>
      (function () {
        var search = location.search;
        var forceDark = /(?:^|[?&])case=dark(?:&|$)/.test(search);
        var forceLight = /(?:^|[?&])case=light(?:&|$)/.test(search);
        if (!forceDark && !forceLight) return;
        var real = window.matchMedia.bind(window);
        // 顺手把宿主的**真实**偏好记下来，方便排查"这条怎么红了"。
        try { window.__HOST_MQ_DARK__ = real("(prefers-color-scheme: dark)").matches; } catch (_) {}
        window.matchMedia = function (query) {
          if (/prefers-color-scheme:\\s*dark/.test(query)) {
            return {
              matches: forceDark,
              media: query,
              onchange: null,
              addEventListener: function () {},
              removeEventListener: function () {},
              addListener: function () {},
              removeListener: function () {},
            };
          }
          return real(query);
        };
      })();
    </script>
"""


def build_site() -> None:
    # 沙箱会把 Python 的 `shutil.copytree` 代理到宿主机并拒绝 —— 用 `cp -R`。
    if SITE.exists():
        subprocess.run(["rm", "-rf", str(SITE)], check=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    index = (SITE / "index.html").read_text()
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次，不正常"

    # ① matchMedia 桩插在主题脚本正前方。
    anchor_theme = "    <script>\n      (function () {\n        try {\n          var mq = window.matchMedia"
    assert anchor_theme in index, "没找到主题脚本，插桩位置失效"
    index = index.replace(anchor_theme, THEME_STUB + anchor_theme, 1)

    # ② 驱动脚本挂在 bridge.js 之后。
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
        for case in ("light", "dark"):
            url = f"{base}?case={case}"
            print(f"[{case}] {url}")
            dom = run_chrome(url, ["--dump-dom", "--virtual-time-budget=60000"]).stdout
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

    light = results["light"]
    dark = results["dark"]

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    def tok(res: dict, name: str) -> str:
        return (res.get("tokens") or {}).get(name) or ""

    # ---------------------------------------------------------------- 主题开关链路
    check(
        "浅色那趟：主题脚本没挂 theme-dark",
        light.get("themeClass") is False,
        f"themeClass={light.get('themeClass')}",
    )
    check(
        "深色那趟：index.html 里那段真实脚本把 theme-dark 挂上了",
        dark.get("themeClass") is True,
        f"themeClass={dark.get('themeClass')}（桩在主题脚本之前，测的是真实链路；"
        f"宿主机真实偏好=深色? {dark.get('hostPrefersDark')}）",
    )

    # ---------------------------------------------------------------- tokens 真的换了一套
    check(
        "两套主题的 tokens 确实不同（不是空验证）",
        tok(light, "--bg") != tok(dark, "--bg") and tok(light, "--text") != tok(dark, "--text"),
        f"浅色 bg={tok(light, '--bg')} / 深色 bg={tok(dark, '--bg')}",
    )
    check(
        "深色底色是中性深灰、不是纯黑",
        tok(dark, "--bg") == "#17171a",
        f"--bg={tok(dark, '--bg')}",
    )
    check(
        "深色文字是浅色",
        tok(dark, "--text") == "#ececf2",
        f"--text={tok(dark, '--text')}",
    )
    check(
        "深色强调色提亮了一档（浅色 #6c5ce7 → 深色 #9b8cff）",
        tok(light, "--accent") == "#6c5ce7" and tok(dark, "--accent") == "#9b8cff",
        f"{tok(light, '--accent')} → {tok(dark, '--accent')}",
    )
    check(
        "批注淡色在深色下加浓了（否则 screen 叠在近黑底上看不见）",
        tok(dark, "--hl-yellow") == "rgba(255, 214, 0, 0.62)",
        f"--hl-yellow={tok(dark, '--hl-yellow')}",
    )
    check(
        "批注实色在深色下也提亮了",
        tok(dark, "--mk-yellow") == "#ffe066",
        f"--mk-yellow={tok(dark, '--mk-yellow')}",
    )
    check(
        "危险色在深色下更亮（认得出来是红）",
        tok(dark, "--danger") == "#ff6b6f",
        f"--danger={tok(dark, '--danger')}",
    )

    # ---------------------------------------------------------------- 外壳
    check(
        "外壳底色跟着换（浅色 rgb(241,241,244) → 深色 rgb(23,23,26)）",
        light.get("bodyBg") == "rgb(241, 241, 244)" and dark.get("bodyBg") == "rgb(23, 23, 26)",
        f"{light.get('bodyBg')} → {dark.get('bodyBg')}",
    )
    check(
        "外壳字色跟着换（深字 → 浅字）",
        light.get("bodyColor") == "rgb(28, 28, 32)"
        and dark.get("bodyColor") == "rgb(236, 236, 242)",
        f"{light.get('bodyColor')} → {dark.get('bodyColor')}",
    )

    # ---------------------------------------------------------------- 正文页面
    check("两趟都真的渲染出了正文 canvas（判据不是空验证）", dark.get("canvasExists") is True)
    check(
        "浅色下正文 canvas 不反相",
        (light.get("pageCanvasFilter") or "none") == "none",
        f"filter={light.get('pageCanvasFilter')!r}",
    )
    check(
        "深色下正文 canvas 走智能反相 invert(1) hue-rotate(180deg)",
        "invert(1)" in (dark.get("pageCanvasFilter") or "")
        and "hue-rotate(180deg)" in (dark.get("pageCanvasFilter") or ""),
        f"filter={dark.get('pageCanvasFilter')!r}",
    )
    check(
        "深色下页面兜底底色不是白的",
        dark.get("pageBg") == "rgb(16, 16, 19)",
        f"page bg={dark.get('pageBg')}（浅色是 {light.get('pageBg')}）",
    )

    # ---------------------------------------------------------------- 批注 / 检索层
    check(
        "浅色下批注走 multiply（黑字透出来）",
        (light.get("foliaMarkBlend") or "") == "multiply",
        f"mix-blend-mode={light.get('foliaMarkBlend')!r}",
    )
    check(
        "深色下批注翻成 screen（否则 multiply 把批注压成黑）",
        (dark.get("foliaMarkBlend") or "") == "screen",
        f"mix-blend-mode={dark.get('foliaMarkBlend')!r}",
    )
    check(
        "正文检索命中框在深色下换了暗琥珀底（白字才透得出来）",
        light.get("markShBg") == "rgba(255, 214, 0, 0.42)"
        and dark.get("markShBg") == "rgba(255, 190, 40, 0.4)",
        f"{light.get('markShBg')} → {dark.get('markShBg')}",
    )
    check(
        "缩略图在深色下跟着一起反相",
        (light.get("thumbImgFilter") or "none") == "none"
        and "invert(1)" in (dark.get("thumbImgFilter") or ""),
        f"书架缩略图 {light.get('thumbImgFilter')!r} → {dark.get('thumbImgFilter')!r}",
    )
    check(
        "侧栏缩略图也反相了",
        "invert(1)" in (dark.get("thumbFrameImgFilter") or ""),
        f"filter={dark.get('thumbFrameImgFilter')!r}",
    )

    # ---------------------------------------------------------------- 散件
    check(
        "检索片段高亮：深色下换成琥珀底 + 琥珀字（浅黄底 + 浅字会看不见）",
        light.get("hitMarkBg") == "rgb(255, 229, 138)"
        and dark.get("hitMarkBg") == "rgba(255, 214, 0, 0.26)"
        and dark.get("hitMarkColor") == "rgb(255, 233, 168)",
        f"bg {light.get('hitMarkBg')} → {dark.get('hitMarkBg')}；"
        f"color {light.get('hitMarkColor')} → {dark.get('hitMarkColor')}",
    )
    check(
        "兜底封面：深色下改成浅字（深墨字配深底会糊）",
        light.get("fallbackColor") == "rgb(44, 43, 38)"
        and dark.get("fallbackColor") == "rgb(217, 217, 224)",
        f"{light.get('fallbackColor')} → {dark.get('fallbackColor')}",
    )
    check(
        "兜底封面角标也换了",
        dark.get("fallbackBadgeColor") == "rgba(255, 255, 255, 0.42)",
        f"color={dark.get('fallbackBadgeColor')}（浅色 {light.get('fallbackBadgeColor')}）",
    )
    # `.ctx-menu` 的底色走的是 `--surface` token（#ffffff / #232328），
    # 和 `.po-panel` 的 `--bg` 不是同一个 —— 这里比的是 surface 那一对。
    check(
        "右键菜单面板底色跟着主题走（走 --surface token）",
        light.get("ctxMenuBg") == "rgb(255, 255, 255)" and dark.get("ctxMenuBg") == "rgb(35, 35, 40)",
        f"{light.get('ctxMenuBg')} → {dark.get('ctxMenuBg')}",
    )
    check(
        "右键菜单里的危险项：深红 → 亮红（否则深底上糊成一片）",
        light.get("ctxMenuDangerColor") == "rgb(192, 57, 43)"
        and dark.get("ctxMenuDangerColor") == "rgb(255, 107, 94)",
        f"{light.get('ctxMenuDangerColor')} → {dark.get('ctxMenuDangerColor')}",
    )

    # ---------------------------------------------------------------- 总览浮层
    check(
        "总览浮层面板底色跟着主题走",
        light.get("poPanelBg") == "rgb(246, 246, 248)" and dark.get("poPanelBg") == "rgb(29, 29, 33)",
        f"{light.get('poPanelBg')} → {dark.get('poPanelBg')}",
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
