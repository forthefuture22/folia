#!/usr/bin/env python3
"""真实 PDF 内容下的深色模式截图（/tmp/e2e23/real_shot.py）。

e2e23 的主体用的是自己画的"纯白页"桩，反相后必然是一片纯黑 —— 只能证明
"滤镜挂上了"，证明不了"真实内容反得对不对"。这里借 e2e18 那套真实 pdf.js
管线（`e2e18/sample.pdf`，四角带黑方块，正文是黑字白底），
再叠加本目录的 matchMedia 桩，出浅色/深色两张正文图，肉眼比对。
"""

import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import verify  # noqa: E402

# e2e18 那套真实 pdf.js 管线：harness.js 就在仓库里，sample.pdf 由
# `python tests/e2e/e2e18/make_pdf.py` 现场生成（*.pdf 不入库，见 .gitignore）。
# 以前这两个东西在 /tmp/e2e18，现在按本文件位置推。
E2E18 = pathlib.Path(__file__).resolve().parent.parent / "e2e18"
REALSITE = verify.WORK / "realsite"

# 真实 pdf.js 的正文 canvas 在无头 `--screenshot` 下会合成成一片黑
# （**与本次改动无关**：把旧 CSS 注回去照样黑，而直接读 canvas 像素是正常的白页，
#  e2e18 的 32 条断言全过）。所以正文截图证明不了反相对不对。
# 换成「页面总览」浮层 —— 它把每页画成 `<img>`，而 `<img>` 的 filter 截图能正常合成，
# 于是同一批真实页面内容在深色下到底反成什么样，一眼可查。
OVERVIEW_PRE = r"""
(function () {
  const settle = async (n) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 16)); };
  const waitFor = (p, n = 12000) => new Promise((res, rej) => {
    let t = 0;
    const tick = () => { let v = null; try { v = p(); } catch (_) {} if (v) return res(v); if (++t > n) return rej(new Error("timeout")); setTimeout(tick, 10); };
    tick();
  });
  if (!/overview=1/.test(location.search)) return;
  (async () => {
    try {
      await waitFor(() => document.querySelector(".page-canvas"));
      await settle(120);
      const btn = document.querySelector(".reader-top-left .icon-btn");
      if (btn) btn.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
      await settle(200);
    } catch (_) {}
  })();
})();
"""


def build_real_site() -> None:
    if REALSITE.exists():
        subprocess.run(["rm", "-rf", str(REALSITE)], check=True)
    REALSITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(verify.DIST) + "/.", str(REALSITE)], check=True)
    subprocess.run(["cp", str(E2E18 / "sample.pdf"), str(REALSITE / "sample.pdf")], check=True)

    index = (REALSITE / "index.html").read_text()
    index, _ = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)

    anchor_theme = "    <script>\n      (function () {\n        try {\n          var mq = window.matchMedia"
    assert anchor_theme in index, "没找到主题脚本，插桩位置失效"
    index = index.replace(anchor_theme, verify.THEME_STUB + anchor_theme, 1)

    anchor = '<script type="module" src="public/pdfjs/bridge.js"></script>'
    index = index.replace(
        anchor, anchor + '\n    <script type="module" src="/harness-stub.js"></script>'
    )
    (REALSITE / "index.html").write_text(index)
    harness = (E2E18 / "harness.js").read_text()
    (REALSITE / "harness-stub.js").write_text(OVERVIEW_PRE + harness)


def main() -> int:
    build_real_site()
    httpd, port = verify.serve(REALSITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        for case in ("light", "dark"):
            out = verify.WORK / f"real-reader-{case}.png"
            url = f"{base}?mode=reader&shot=reader&case={case}"
            print(f"[{case}] -> {out}")
            verify.run_chrome(
                url,
                [
                    "--screenshot=" + str(out),
                    "--virtual-time-budget=120000",
                    "--run-all-compositor-stages-before-draw",
                ],
            )
        for case in ("light", "dark"):
            out = verify.WORK / f"real-overview-{case}.png"
            url = f"{base}?mode=reader&shot=reader&overview=1&case={case}"
            print(f"[{case}/overview] -> {out}")
            verify.run_chrome(
                url,
                [
                    "--screenshot=" + str(out),
                    "--virtual-time-budget=120000",
                    "--run-all-compositor-stages-before-draw",
                ],
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
