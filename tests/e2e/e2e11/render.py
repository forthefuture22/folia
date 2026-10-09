#!/usr/bin/env python3
"""删掉那几块 UI 之后的回归验证。

一次跑三种：DOM dump（断言元素真的没了）+ 两张截图（书架 / 双标签阅读器）。
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

DOCS = {
    "/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha 报告.pdf", "numPages": 6, "size": 1048576},
    "/tmp/beta.pdf": {"id": "doc-beta", "name": "Beta 手册.pdf", "numPages": 10, "size": 2097152},
}
RECENT = [
    {"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha 报告.pdf", "size": 1048576,
     "pages": 6, "last_page": 1, "opened_at": 2000.0, "favorite": False},
    {"id": "doc-beta", "path": "/tmp/beta.pdf", "name": "Beta 手册.pdf", "size": 2097152,
     "pages": 10, "last_page": 1, "opened_at": 1000.0, "favorite": True},
]

STUB = """
<script>
window.__DOCS__ = %DOCS%;
window.__CUR__ = null;
window.__CALLS__ = [];
window.__TAURI_INTERNALS__ = { invoke: function (c) { window.__CALLS__.push(c); return Promise.resolve(null); } };
window.__TAURI__ = { core: { invoke: async function (cmd) {
  if (cmd === 'load_app_state') return %STATE%;
  if (cmd === 'load_annotations') return '[]';
  return null;
}}};
window.FoliaBridge = {
  openDocumentDialog: async function () { return JSON.stringify({ ok: false, cancelled: true }); },
  openDocumentPath: async function (path) {
    var d = window.__DOCS__[path];
    if (!d) return JSON.stringify({ ok: false, error: 'no such doc' });
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
    var g = c.getContext('2d');
    var cur = window.__CUR__ || { id: '?' };
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);
    g.fillStyle = cur.id === 'doc-alpha' ? '#1d4ed8' : '#15803d';
    g.fillRect(40 * scale, 40 * scale, 260 * scale, 30 * scale);
    g.fillStyle = '#c9cfe0';
    for (var i = 0; i < 14; i++) g.fillRect(40 * scale, (110 + i * 26) * scale, (420 + (i % 4) * 40) * scale, 9 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};
function click(sel, idx) {
  var els = document.querySelectorAll(sel);
  var el = els[idx === undefined ? 0 : idx];
  if (el) el.click();
}
window.addEventListener('load', function () {
  // `#geo` = 停在书架并把关键盒模型打出来，用来量「面板底部到文档底部」的间隔
  if (location.hash === '#geo') {
    setTimeout(function () {
      var lib = document.querySelector('.library');
      var panel = document.querySelector('.panel');
      var pre = document.createElement('pre');
      pre.id = 'geo';
      pre.textContent = JSON.stringify({
        libClient: lib.clientHeight,
        libScroll: lib.scrollHeight,
        panelBottom: panel.getBoundingClientRect().bottom,
        libBottom: lib.getBoundingClientRect().bottom,
        libPadBottom: getComputedStyle(lib).paddingBottom,
        panelMargin: getComputedStyle(panel).margin,
      });
      document.body.appendChild(pre);
    }, 900);
    return;
  }
  // `#lib` = 停在书架视图，不打开任何文档（用来断言书架自己的控件还在）
  if (location.hash === '#lib') return;
  setTimeout(function () { click('.file-card', 0); }, 300);
  setTimeout(function () { click('.tabbar-home'); }, 1400);
  setTimeout(function () { click('.file-card', 1); }, 2000);
});
</script>
"""

state_json = json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False))
stub = STUB.replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False)).replace("%STATE%", state_json)

out = pathlib.Path(str(_HERE) + "/app")
if out.exists():
    shutil.rmtree(out)
out.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(SRC, out)
html = (out / "index.html").read_text(encoding="utf-8")
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", stub + "\n</head>", 1)
(out / "index.html").write_text(html, encoding="utf-8")
left = [t for t in ("%DOCS%", "%STATE%") if t in html]
assert not left, f"unreplaced: {left}"
print("built ->", out)
