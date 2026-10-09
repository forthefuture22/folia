#!/usr/bin/env python3
"""多标签端到端验证：开两份文档 → 切标签 → 关标签 → 关最后一个回书架。

把真 WASM 跑在 headless Chrome 里，stub 掉 FoliaBridge，
每步用 `snap()` 记一行状态，最后写进 <pre id="e2e"> 供 --dump-dom 读取。
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

# 两份「文档」：页数不同，这样页码总数就是区分当前文档的信号。
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

STUB = """
<script>
window.__DOCS__ = %DOCS%;
window.__CUR__ = null;

window.__TAURI__ = { core: { invoke: async function (cmd) {
  if (cmd === 'load_app_state') return %STATE%;
  if (cmd === 'load_annotations') return '[]';
  if (cmd === 'save_annotations') return null;
  if (cmd === 'save_app_state') return null;
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
    var out = [];
    var n = window.__CUR__ ? window.__CUR__.numPages : 0;
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
    c.width = w; c.height = h;
    c.style.width = w + 'px'; c.style.height = h + 'px';
    var g = c.getContext('2d');
    var cur = window.__CUR__ || { id: '?' };
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);
    // 每份文档一个底色，肉眼也能一眼看出切没切过去
    g.fillStyle = cur.id === 'doc-alpha' ? '#1d4ed8' : '#15803d';
    g.fillRect(40 * scale, 40 * scale, 260 * scale, 30 * scale);
    g.fillStyle = '#c9cfe0';
    for (var i = 0; i < 14; i++) {
      g.fillRect(40 * scale, (110 + i * 26) * scale, (420 + (i % 4) * 40) * scale, 9 * scale);
    }
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};

// ---------------------------------------------------------------- 步骤驱动
var out = [];
function snap(tag) {
  var tabs = document.querySelectorAll('.tab');
  var active = -1;
  var labels = [];
  tabs.forEach(function (t, i) {
    if (t.classList.contains('active')) active = i;
    var l = t.querySelector('.tab-label');
    labels.push(l ? l.textContent : '?');
  });
  var pager = document.querySelector('.page-total');
  out.push([
    tag,
    'tabs=' + tabs.length,
    'labels=' + labels.join(','),
    'active=' + active,
    'pager=' + (pager ? pager.textContent.trim() : '-'),
    'reader=' + (document.querySelector('.reader-top') ? 1 : 0),
    'library=' + (document.querySelector('.library') ? 1 : 0),
  ].join(' | '));
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

window.addEventListener('load', function () {
  setTimeout(function () { snap('01-start'); click('.file-card', 0); }, 300);
  setTimeout(function () { snap('02-open-A'); click('.tabbar-home'); }, 1400);
  setTimeout(function () { snap('03-home'); click('.file-card', 1); }, 2000);
  setTimeout(function () { snap('04-open-B'); click('.tab-label', 0); }, 3200);
  setTimeout(function () { snap('05-switch-A'); click('.tab-close', 0); }, 4400);
  setTimeout(function () { snap('06-after-close-A'); click('.tab-close', 0); }, 5600);
  setTimeout(function () { snap('07-after-close-last'); dump(); }, 6800);
});
</script>
"""

state_json = json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False))
stub = STUB.replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False)).replace("%STATE%", state_json)

out = pathlib.Path(str(_HERE) + "/tabs")
if out.exists():
    shutil.rmtree(out)
shutil.copytree(SRC, out)
html = (out / "index.html").read_text(encoding="utf-8")
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", stub + "\n</head>", 1)
(out / "index.html").write_text(html, encoding="utf-8")
left = [t for t in ("%DOCS%", "%STATE%") if t in html]
assert not left, f"unreplaced: {left}"
print("built ->", out)
