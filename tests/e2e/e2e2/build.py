#!/usr/bin/env python3
"""Deterministic E2E harness for the Folia 'pages: 0' regression.

Replaces pdf.js + Tauri IPC with stubs, so the REAL compiled WASM (Rust/Yew)
runs its real open -> apply_document -> persist -> back-to-shelf path.
The bug lived in Rust, so this is exactly the right layer to test.
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib
import re
import shutil

SRC = pathlib.Path(str(_REPO) + "/dist")
OUT = pathlib.Path(str(_HERE) + "/site")

if OUT.exists():
    shutil.rmtree(OUT)
shutil.copytree(SRC, OUT)

html = (OUT / "index.html").read_text(encoding="utf-8")

STUB = r"""
<script>
window.__log = [];   // save_app_state payloads
window.__err = [];   // errors surfaced from the driver
window.__stage = "boot";

window.__TAURI__ = { core: { invoke: async function (cmd, args) {
  try {
    if (cmd === 'load_app_state') return JSON.stringify({recentFiles:[],lastPath:null,lastPage:null,scale:null});
    if (cmd === 'load_annotations') return '[]';
    if (cmd === 'save_annotations') return null;
    if (cmd === 'save_app_state') { window.__log.push(args.data); return null; }
    return null;
  } catch (e) { window.__err.push(cmd + ': ' + e); return null; }
}}};

// 2 pages in reality, but we claim 286 to mimic the user's real file.
var PAGES = 286;
window.FoliaBridge = {
  openDocumentDialog: async function () {
    return JSON.stringify({ ok: true, meta: {
      id: 'e2etest', path: '/tmp/e2e2/sample.pdf',
      name: 'Java 热门面试题与核心知识点详解（2024 修订版）.pdf',
      size: 13841203, numPages: PAGES } });
  },
  openDocumentPath: async function (p) {
    return JSON.stringify({ ok: true, meta: {
      id: 'e2etest', path: p, name: 'sample.pdf', size: 1000, numPages: PAGES } });
  },
  closeDocument: function () {},
  documentInfo: function () { return 'null'; },
  getPageSizes: async function () {
    var out = []; for (var i = 0; i < PAGES; i++) out.push({ width: 612, height: 792 });
    return JSON.stringify(out);
  },
  renderPage: async function () { return JSON.stringify({ ok: true, width: 612, height: 792 }); },
  renderTextLayer: async function () {},
  getOutline: async function () { return '[]'; },
  search: async function () { return '[]'; },
  getSelectionInfo: function () { return 'null'; },
  clearSelection: function () {},
  onFileDrop: function () {},
};

function __report() {
  var pre = document.getElementById('e2e');
  if (!pre) { pre = document.createElement('pre'); pre.id = 'e2e'; document.body.appendChild(pre); }
  pre.textContent = 'E2E_STAGE=' + window.__stage +
    '\nE2E_SAVES=' + window.__log.length +
    '\nE2E_PAYLOADS=' + JSON.stringify(window.__log) +
    '\nE2E_ERRORS=' + JSON.stringify(window.__err) +
    '\nE2E_PROBE=' + JSON.stringify(window.__probe || null);
}
window.addEventListener('load', function () {
  // Stage 1: click the empty-state drop zone -> triggers the real open flow.
  setTimeout(function () {
    window.__stage = 'clicked-open';
    var zone = document.querySelector('.drop-zone') || document.querySelector('.cover-add');
    if (!zone) { window.__err.push('no drop-zone found'); __report(); return; }
    zone.click();
  }, 300);

  // Stage 2: once a document is open, click the back button -> homepage again.
  setTimeout(function () {
    window.__stage = 'clicked-back';
    var back = document.querySelector('.back-btn');
    if (!back) { window.__err.push('no .back-btn found'); __report(); return; }
    back.click();
    setTimeout(function () {
      window.__stage = 'done';
      window.__probe = {
        hasProgressBar: !!document.querySelector('.cover-progress-fill'),
        hasResumeCard: !!document.querySelector('.resume'),
        subtitle: (document.querySelector('.cover-sub') || {}).textContent || null,
        shelfCount: (document.querySelector('.shelf-count') || {}).textContent || null,
      };
      __report();
    }, 900);
  }, 2200);

  setTimeout(__report, 4000);
});
</script>
"""

# Drop the real bridge.js module tag (pdf.js is stubbed out) and inject ours.
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", STUB + "\n</head>")
(OUT / "index.html").write_text(html, encoding="utf-8")
print("wrote", OUT / "index.html")
