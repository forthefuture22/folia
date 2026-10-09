#!/usr/bin/env python3
"""探针：headless 虚拟时钟下，给 .stage 赋 scrollTop 到底生不生效？"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib, re, sys
sys.path.insert(0, str(_HERE))
import importlib.util
spec = importlib.util.spec_from_file_location("v", str(_HERE) + "/verify.py")

# 直接复用 verify.py 里的 build/Server/chrome，但不跑它的主流程
src = pathlib.Path(str(_HERE) + "/verify.py").read_text(encoding="utf-8")
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
    var stage = q('.stage');
    log('T1 before=' + stage.scrollTop + ' clientH=' + stage.clientHeight + ' scrollH=' + stage.scrollHeight);
    stage.scrollTop = 3000;
    log('T2 right-after-set=' + stage.scrollTop);
    setTimeout(function () {
      log('T3 after-300ms=' + stage.scrollTop + ' page=' + q('.page-input').value);
      var body = q('.side-body');
      log('T4 body.scrollTop=' + body.scrollTop + ' bodyScrollH=' + body.scrollHeight + ' bodyClientH=' + body.clientHeight);
      var pre = document.createElement('pre'); pre.id = 'e2e';
      pre.textContent = out.join('\\n'); document.body.appendChild(pre);
    }, 300);
  }, 1500);
});
"""

d = ns["build"](DRIVER, "app-probe")
with ns["Server"](d):
    dom = ns["chrome"](d, ["--dump-dom"], 8000)
m = re.search(r'<pre id="e2e">(.*?)</pre>', dom, re.S)
print(m.group(1) if m else "NO DUMP\n" + dom[-800:])
