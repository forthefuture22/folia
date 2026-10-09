#!/usr/bin/env python3
"""探针 2：虚拟时钟下 requestAnimationFrame 会不会跑？scroll 事件会不会派发？"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib, re, sys

src = pathlib.Path(str(_HERE) + "/verify_det.py").read_text(encoding="utf-8")
head = src.split("# ================================================================ A：徽标颜色")[0]
ns = {}
exec(compile(head, "verify-head", "exec"), ns)

DRIVER = """
var out = [];
function log(s) { out.push(s); }
function q(s) { return document.querySelector(s); }
window.addEventListener('load', function () {
  setTimeout(function () { q('.file-card').click(); }, 300);
  setTimeout(function () {
    var stage = q('.stage'), body = q('.side-body');

    // ① rAF 计数
    var raf = 0, t0 = Date.now();
    function tick() {
      raf++;
      if (Date.now() - t0 < 600) requestAnimationFrame(tick);
      else log('P1 rafTicks=' + raf + ' over 600ms');
    }
    requestAnimationFrame(tick);

    // ② scroll 事件计数
    var sc = 0;
    stage.addEventListener('scroll', function () { sc++; });
    stage.scrollTop = 3000;
    log('P2 set scrollTop=3000 -> ' + stage.scrollTop);

    setTimeout(function () {
      log('P3 scrollEvents=' + sc + ' stageTop=' + stage.scrollTop + ' page=' + q('.page-input').value);
      log('P4 bodyTop=' + body.scrollTop);

      // ③ 手动派发一个 scroll 事件，看 app / sidebar 的监听器在不在
      stage.dispatchEvent(new Event('scroll'));
      setTimeout(function () {
        log('P5 after synthetic scroll: page=' + q('.page-input').value + ' bodyTop=' + body.scrollTop);
        var pre = document.createElement('pre'); pre.id = 'e2e';
        pre.textContent = out.join('\\n'); document.body.appendChild(pre);
      }, 300);
    }, 300);
  }, 1500);
});
"""

d = ns["build"](DRIVER, "app-probe2")
with ns["Server"](d):
    dom = ns["chrome"](d, ["--dump-dom"], 9000)
m = re.search(r'<pre id="e2e">(.*?)</pre>', dom, re.S)
print(m.group(1) if m else "NO DUMP\n" + dom[-800:])
