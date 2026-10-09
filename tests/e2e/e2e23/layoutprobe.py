#!/usr/bin/env python3
"""一次性布局探针：书架右上角「设为默认」按钮为什么被压窄。"""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import verify  # noqa: E402

PROBE = """
const result = {};
const waitFor = (p, label, n = 3000) =>
  new Promise((res, rej) => {
    let t = 0;
    const tick = () => {
      let v = null;
      try { v = p(); } catch (_) {}
      if (v) return res(v);
      if (++t > n) return rej(new Error("timeout " + label));
      setTimeout(tick, 10);
    };
    tick();
  });
const settle = async (n) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 16)); };
const box = (el) => {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  const s = getComputedStyle(el);
  return {
    w: Math.round(r.width), h: Math.round(r.height),
    x: Math.round(r.left), y: Math.round(r.top),
    display: s.display, flex: s.flex, minWidth: s.minWidth, maxWidth: s.maxWidth,
    whiteSpace: s.whiteSpace, overflow: s.overflow, flexShrink: s.flexShrink,
    width: s.width, flexBasis: s.flexBasis, padding: s.padding, gap: s.gap,
    text: (el.textContent || "").trim().slice(0, 20),
  };
};
(async () => {
  try {
    await waitFor(() => document.querySelector(".file-card"), "shelf");
    await settle(20);
    const btn = [...document.querySelectorAll(".lib-actions .btn")].find((b) => b.textContent.includes("设为默认"));
    result.btn = box(btn);
    result.btnSvg = box(btn && btn.querySelector("svg"));
    result.actions = box(document.querySelector(".lib-actions"));
    result.head = box(document.querySelector(".lib-head"));
    result.segmented = box(document.querySelector(".segmented"));
    result.library = box(document.querySelector(".library"));
    result.panel = box(document.querySelector(".panel"));
    result.primary = box(document.querySelector(".lib-actions .btn-primary"));
    result.headStyle = (() => { const s = getComputedStyle(document.querySelector(".lib-head")); return { display: s.display, gap: s.gap, justify: s.justifyContent, padding: s.padding }; })();
    // 祖先链上谁在缩？
    const chain = [];
    let el = btn;
    while (el && el !== document.body) {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      chain.push({ cls: el.className || el.tagName, w: Math.round(r.width), display: s.display, flex: s.flex, overflow: s.overflow, minWidth: s.minWidth });
      el = el.parentElement;
    }
    result.chain = chain;
  } catch (e) { result.error = String(e); }
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
})();
"""


def main() -> int:
    verify.build_site()
    # 探针要自带 Tauri IPC 桩，否则页面起不来 —— 直接复用 harness.js 的前半段。
    harness = (pathlib.Path(__file__).parent / "harness.js").read_text()
    stub = harness.split("// ---------------------------------------------------------------- 驱动")[0]
    (verify.SITE / "harness-stub.js").write_text(stub + PROBE)
    httpd, port = verify.serve(verify.SITE)
    try:
        url = f"http://127.0.0.1:{port}/index.html"
        dom = verify.run_chrome(url, ["--dump-dom", "--virtual-time-budget=30000"]).stdout
        import html as _html, json as _json
        m = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
        print(_json.dumps(_json.loads(_html.unescape(m.group(1))), indent=2, ensure_ascii=False))
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
