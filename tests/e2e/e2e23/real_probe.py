#!/usr/bin/env python3
"""真实 pdf.js 管线下，正文 canvas 到底被谁弄黑了（/tmp/e2e23/real_probe.py）。"""

import html
import json
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import verify  # noqa: E402
import real_shot  # noqa: E402

PROBE = r"""
(function () {
  const settle = async (n) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 16)); };
  const waitFor = (p, n = 12000) => new Promise((res, rej) => {
    let t = 0;
    const tick = () => { let v = null; try { v = p(); } catch (_) {} if (v) return res(v); if (++t > n) return rej(new Error("timeout")); setTimeout(tick, 10); };
    tick();
  });
  (async () => {
    const out = {};
    try {
      await waitFor(() => document.querySelector(".page-canvas"));
      await settle(200);
      out.htmlClass = document.documentElement.className || "(none)";
      out.pages = document.querySelectorAll(".page").length;
      const c = document.querySelector(".page-canvas");
      const cs = getComputedStyle(c);
      out.canvas = {
        filter: cs.filter, mixBlendMode: cs.mixBlendMode, opacity: cs.opacity,
        background: cs.backgroundColor, w: c.width, h: c.height,
        rect: (() => { const r = c.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height) }; })(),
      };
      const p = c.parentElement;
      const pcs = getComputedStyle(p);
      out.parent = { cls: p.className, filter: pcs.filter, mixBlendMode: pcs.mixBlendMode, background: pcs.backgroundColor, opacity: pcs.opacity };
      const page = document.querySelector(".page");
      out.page = { cls: page.className, background: getComputedStyle(page).backgroundColor };
      const t = document.querySelector(".thumb-item img");
      out.thumbImgFilter = t ? getComputedStyle(t).filter : null;
      try {
        const ctx = c.getContext("2d");
        const sample = (x, y) => { const d = ctx.getImageData(x, y, 1, 1).data; return [d[0], d[1], d[2], d[3]]; };
        out.pxCenter = sample(Math.floor(c.width / 2), Math.floor(c.height / 2));
        out.pxCorner = sample(6, 6);
        out.pxLower = sample(Math.floor(c.width / 2), Math.floor(c.height * 0.8));
      } catch (e) { out.pxErr = String(e); }
    } catch (e) { out.err = String(e); }
    const pre = document.createElement("pre");
    pre.id = "probe-result";
    pre.textContent = JSON.stringify(out, null, 2);
    document.body.appendChild(pre);
  })();
})();
"""


def main() -> int:
    real_shot.build_real_site()
    stub = (real_shot.REALSITE / "harness-stub.js").read_text()
    (real_shot.REALSITE / "harness-stub.js").write_text(PROBE + stub)
    httpd, port = verify.serve(real_shot.REALSITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        for case in ("light", "dark"):
            url = f"{base}?mode=reader&case={case}"
            print(f"\n================ {case} ================")
            dom = verify.run_chrome(url, ["--dump-dom", "--virtual-time-budget=120000"]).stdout
            m = re.search(r'<pre id="probe-result">(.*?)</pre>', dom, flags=re.S)
            if not m:
                print("没有 probe-result；页面尾部：\n" + dom[-2000:])
                continue
            print(json.dumps(json.loads(html.unescape(m.group(1))), indent=2, ensure_ascii=False))
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
