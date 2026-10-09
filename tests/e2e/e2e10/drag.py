#!/usr/bin/env python3
"""标签栏拖拽区端到端验证。

关键点：把 **Tauri 自己那份 drag.js** 原样注入到 headless Chrome 里
（只把构建期的 `__TEMPLATE_os_name__` 换成 'macos'），
再 stub 掉 `window.__TAURI_INTERNALS__.invoke` 记录调用。
然后用 `elementFromPoint` + 合成 mousedown 在若干个坐标上探一遍，
看哪些位置真的会触发 `plugin:window|start_dragging`。

这样验证的是「真实判定逻辑」，而不是我复述的规则。
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import json, pathlib, re, shutil

SRC = pathlib.Path(str(_REPO) + "/dist")
DRAG_JS = pathlib.Path(
    "/Users/anemone/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/"
    "tauri-2.12.1/src/window/scripts/drag.js"
)

DOCS = {
    "/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha 报告.pdf", "numPages": 6, "size": 1048576},
    "/tmp/beta.pdf": {"id": "doc-beta", "name": "Beta 手册.pdf", "numPages": 10, "size": 2097152},
}
RECENT = [
    {"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha 报告.pdf", "size": 1048576,
     "pages": 6, "last_page": 1, "opened_at": 2000.0, "favorite": False},
    {"id": "doc-beta", "path": "/tmp/beta.pdf", "name": "Beta 手册.pdf", "size": 2097152,
     "pages": 10, "last_page": 1, "opened_at": 1000.0, "favorite": False},
]

# 真 drag.js：只替换构建期模板变量
drag_src = DRAG_JS.read_text(encoding="utf-8")
assert "__TEMPLATE_os_name__" in drag_src, "drag.js 模板变量不见了，去看上游是不是改了"
drag_src = drag_src.replace("__TEMPLATE_os_name__", "'macos'")
assert "start_dragging" in drag_src

STUB = """
<script>
window.__DOCS__ = %DOCS%;
window.__CUR__ = null;
window.__CALLS__ = [];

// drag.js 走的是这个全局
window.__TAURI_INTERNALS__ = {
  invoke: function (cmd) { window.__CALLS__.push(cmd); return Promise.resolve(null); }
};

// 应用自己读状态走这个全局
window.__TAURI__ = { core: { invoke: async function (cmd) {
  if (cmd === 'load_app_state') return %STATE%;
  if (cmd === 'load_annotations') return '[]';
  return null;
}}};

window.FoliaBridge = {
  openDocumentDialog: async function () { return JSON.stringify({ ok: false, cancelled: true }); },
  openDocumentPath: async function (path) {
    var d = window.__DOCS__[path];
    if (!d) return JSON.stringify({ ok: false, error: 'no such doc: ' + path });
    window.__CUR__ = d;
    return JSON.stringify({ ok: true, meta: { id: d.id, path: path, name: d.name, size: d.size, numPages: d.numPages } });
  },
  closeDocument: function () { window.__CUR__ = null; },
  documentInfo: function () { return 'null'; },
  releaseThumbnail: function () {},
  renderThumbnail: async function () { return ''; },
  getPageSizes: async function () {
    var out = [], n = window.__CUR__ ? window.__CUR__.numPages : 0;
    for (var i = 0; i < n; i++) out.push({ width: 612, height: 792 });
    return JSON.stringify(out);
  },
  getOutline: async function () { return '[]'; },
  search: async function () { return '[]'; },
  getSelectionInfo: function () { return 'null'; },
  clearSelection: function () {},
  onFileDrop: function () {},
  renderTextLayer: async function () {},
  renderPage: async function (canvasId, page, scale) {
    var c = document.getElementById(canvasId);
    if (!c) return JSON.stringify({ ok: false });
    var w = 612 * scale, h = 792 * scale;
    c.width = w; c.height = h; c.style.width = w + 'px'; c.style.height = h + 'px';
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};
</script>
<script>
%DRAG%
</script>
<script>
var out = [];
function describe(el) {
  if (!el) return 'null';
  var cn = (el.className && typeof el.className === 'string')
      ? '.' + el.className.trim().split(/\\s+/).join('.') : '';
  return el.tagName.toLowerCase() + cn;
}
function rect(sel, idx) {
  var els = document.querySelectorAll(sel);
  var el = els[idx === undefined ? 0 : idx];
  return el ? el.getBoundingClientRect() : null;
}
// 在 (x,y) 上找真实元素、合成一次 mousedown，看有没有触发 start_dragging
function probe(name, x, y) {
  var el = document.elementFromPoint(x, y);
  var before = window.__CALLS__.length;
  if (el) {
    el.dispatchEvent(new MouseEvent('mousedown', {
      bubbles: true, composed: true, cancelable: true,
      button: 0, detail: 1, clientX: x, clientY: y
    }));
  }
  var drag = window.__CALLS__.slice(before).some(function (c) {
    return c.indexOf('start_dragging') >= 0;
  });
  out.push('PROBE ' + name.padEnd(14) + '| xy=' + (x | 0) + ',' + (y | 0)
           + ' | hit=' + describe(el).padEnd(26) + ' | drag=' + (drag ? 1 : 0));
  return drag;
}
function click(sel, idx) {
  var els = document.querySelectorAll(sel);
  var el = els[idx === undefined ? 0 : idx];
  if (el) el.click(); else out.push('MISS ' + sel + '[' + idx + ']');
}
function dump() {
  var pre = document.createElement('pre');
  pre.id = 'e2e';
  pre.textContent = out.join('\\n');
  document.body.appendChild(pre);
}
// 把关键盒模型导出来，供 zones.py 画「哪些地方能拖」的示意图
function dumpRects() {
  function r(sel, idx) {
    var els = document.querySelectorAll(sel);
    var el = els[idx === undefined ? 0 : idx];
    if (!el) return null;
    var b = el.getBoundingClientRect();
    return { x: b.left, y: b.top, w: b.width, h: b.height };
  }
  var tabsN = document.querySelectorAll('.tab').length;
  var data = {
    scale: window.devicePixelRatio,
    viewport: { w: window.innerWidth, h: window.innerHeight },
    bar: r('.tabbar'),
    home: r('.tabbar-home'),
    strip: r('.tab-strip'),
    add: r('.tab-add'),
    right: r('.tabbar-right'),
    tabs: [],
  };
  for (var i = 0; i < tabsN; i++) data.tabs.push(r('.tab', i));
  var pre = document.createElement('pre');
  pre.id = 'rects';
  pre.textContent = JSON.stringify(data);
  document.body.appendChild(pre);
}

function runProbes() {
  out.push('--- probes (window ' + window.innerWidth + 'x' + window.innerHeight + ') ---');
  var strip = rect('.tab-strip');
  var lastTab = rect('.tab', document.querySelectorAll('.tab').length - 1);
  var add = rect('.tab-add');
  var home = rect('.tabbar-home');
  var bar = rect('.tabbar');
  var label = rect('.tab-label', 0);
  var close = rect('.tab-close', 0);
  var firstTab = rect('.tab', 0);

  // 1) 标签条右侧的空白 —— 用户要的就是这里
  probe('strip-blank', strip.right - 40, strip.top + strip.height / 2);
  // 2) `.tabs` 里 tab-strip 与 + 之间的缝
  var gapX = (strip.right + add.left) / 2;
  probe('tabs-gap', gapX, bar.top + bar.height / 2);
  // 3) 最左端 macOS 红绿灯留白（header 自己的 padding）
  probe('header-gutter', 40, bar.top + bar.height / 2);
  // 4~7) 交互控件必须**不**拖
  probe('tab-label', label.left + label.width / 2, label.top + label.height / 2);
  probe('tab-close', close.left + close.width / 2, close.top + close.height / 2);
  probe('tab-add', add.left + add.width / 2, add.top + add.height / 2);
  probe('home-btn', home.left + home.width / 2, home.top + home.height / 2);
  // 8) 标签容器自己的 padding（信息项：deep 下会拖，surgical 下不会）
  probe('tab-padding', firstTab.left + 2, firstTab.top + firstTab.height / 2);
  // 9) 标签标题下方一点点的 tab 容器空白
  out.push('lastTab.right=' + (lastTab.right | 0) + ' add.left=' + (add.left | 0)
           + ' gap=' + ((add.left - lastTab.right) | 0));
}

window.addEventListener('load', function () {
  setTimeout(function () { click('.file-card', 0); }, 300);
  setTimeout(function () { click('.tabbar-home'); }, 1500);
  setTimeout(function () { click('.file-card', 1); }, 2100);
  setTimeout(function () { runProbes(); dump(); dumpRects(); }, 3400);
});
</script>
"""

state_json = json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False))
stub = (STUB
        .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
        .replace("%STATE%", state_json)
        .replace("%DRAG%", drag_src))

out = pathlib.Path(str(_HERE) + "/drag")
if out.exists():
    shutil.rmtree(out)
out.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(SRC, out)

html = (out / "index.html").read_text(encoding="utf-8")
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", stub + "\n</head>", 1)
(out / "index.html").write_text(html, encoding="utf-8")

left = [t for t in ("%DOCS%", "%STATE%", "%DRAG%") if t in html]
assert not left, f"unreplaced: {left}"
assert "start_dragging" in html
print("built ->", out)
