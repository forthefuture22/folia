#!/usr/bin/env python3
"""全文检索改版验证：工具栏搜索框 + 左侧栏「搜索结果」列表。

stub 的 search 返回 9 条带 snippet 的命中，驱动往搜索框里打字，
然后断言 DOM（hit-list 条数 / active 项 / 关键词 <mark>）并截图。
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
QUERY = "reader"

# 9 条命中：分布在 4 页，snippet 里都含关键词（大小写混着来，验证 <mark> 的匹配）
HITS = []
for i in range(9):
    page = 1 + i % 4
    HITS.append({
        "page": page,
        "itemIndex": i,
        "charIndex": 12,
        "length": len(QUERY),
        "key": f"{page}-{i}-0",
        "y": 0.1 + 0.08 * (i % 4),
        "snippet": f"…the {QUERY.capitalize() if i % 3 == 0 else QUERY} must be able to open several documents at once, part {i + 1}…",
    })

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
window.__HITS__ = %HITS%;
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
  search: async function (q) { return JSON.stringify(window.__HITS__); },
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
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);
    g.fillStyle = '#e7e9f2';
    for (var i = 0; i < 12; i++) g.fillRect(40 * scale, (80 + i * 30) * scale, (400 + (i % 3) * 50) * scale, 10 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};

// ---------------------------------------------------------------- 驱动
var out = [];
function snap(tag) {
  var hits = document.querySelectorAll('.hit-item');
  var active = -1;
  hits.forEach(function (h, i) { if (h.classList.contains('active')) active = i; });
  var marks = document.querySelectorAll('.hit-mark');
  var bar = document.querySelector('.hit-count');
  var title = document.querySelector('.side-tab.active');
  out.push([
    tag,
    'hits=' + hits.length,
    'active=' + active,
    'marks=' + marks.length,
    'count=' + (bar ? bar.textContent.trim() : '-'),
    'panel=' + (title ? title.textContent.trim() : '-'),
    'sidebar=' + (document.querySelector('.sidebar') ? 1 : 0),
    'searchbox=' + (document.querySelector('.search-box') ? 1 : 0),
    'oldcount=' + (document.querySelector('.search-count') ? 1 : 0),
  ].join(' | '));
}
function click(sel, idx) {
  var els = document.querySelectorAll(sel);
  var el = els[idx === undefined ? 0 : idx];
  if (el) el.click(); else out.push('MISS ' + sel);
}
function type(sel, value) {
  var el = document.querySelector(sel);
  if (!el) { out.push('MISS ' + sel); return; }
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}
function dump() {
  var pre = document.createElement('pre');
  pre.id = 'e2e';
  pre.textContent = out.join('\\n');
  document.body.appendChild(pre);
}

window.addEventListener('load', function () {
  setTimeout(function () { click('.file-card', 0); }, 300);
  setTimeout(function () { snap('01-reader'); type('.search-box .search-input', %Q%); }, 1600);
  setTimeout(function () { snap('02-typed'); click('.hit-item', 4); }, 2800);
  setTimeout(function () { snap('03-picked'); dump(); }, 3400);
});
</script>
"""

state_json = json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False))
stub = (STUB
        .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
        .replace("%HITS%", json.dumps(HITS, ensure_ascii=False))
        .replace("%STATE%", state_json)
        .replace("%Q%", json.dumps(QUERY)))

out = pathlib.Path(str(_HERE) + "/app")
if out.exists():
    shutil.rmtree(out)
out.parent.mkdir(parents=True, exist_ok=True)
shutil.copytree(SRC, out)
html = (out / "index.html").read_text(encoding="utf-8")
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", stub + "\n</head>", 1)
(out / "index.html").write_text(html, encoding="utf-8")
left = [t for t in ("%DOCS%", "%HITS%", "%STATE%", "%Q%") if t in html]
assert not left, f"unreplaced: {left}"
print("built ->", out)
