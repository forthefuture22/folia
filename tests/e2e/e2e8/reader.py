#!/usr/bin/env python3
"""渲染新版阅读器：stub 掉打开流程与页面绘制，自动打开最近一份文档。

注意：占位符必须与 JS 里的 window.__FAKE__ 之类的全局名**不重叠**，
否则 str.replace 会把 `window.__FAKE__` 也一起替换掉，产生语法错误。
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import json, pathlib, re, shutil, sys, urllib.parse

SRC = pathlib.Path(str(_REPO) + "/dist")
REAL = json.loads((pathlib.Path.home() / "Library/Application Support/com.anemone.folia/state.json").read_text())
for f in REAL.get("recentFiles", []):
    f.setdefault("favorite", False)

PAGES = 12
FAKE = """<svg xmlns="http://www.w3.org/2000/svg" width="320" height="420">
<rect width="320" height="420" fill="#ffffff"/>
<rect x="26" y="30" width="150" height="16" rx="3" fill="#2f3b8f"/>
<rect x="26" y="58" width="268" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="76" width="240" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="94" width="256" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="124" width="120" height="90" rx="4" fill="#e8ecf6"/>
<rect x="158" y="124" width="136" height="9" rx="3" fill="#d7dcea"/>
<rect x="158" y="142" width="120" height="9" rx="3" fill="#d7dcea"/>
<rect x="158" y="160" width="132" height="9" rx="3" fill="#d7dcea"/>
<rect x="26" y="238" width="268" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="256" width="248" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="274" width="262" height="9" rx="3" fill="#c9cfe0"/>
</svg>"""
FAKE_URL = "data:image/svg+xml;utf8," + urllib.parse.quote(FAKE, safe="")

OUTLINE = [
    {"title": "第一部分 基础篇", "pageNumber": 1, "bold": True, "italic": False, "children": [
        {"title": "1. 集合与泛型", "pageNumber": 2, "bold": False, "italic": False, "children": []},
        {"title": "2. 并发与线程池", "pageNumber": 5, "bold": False, "italic": False, "children": []},
    ]},
    {"title": "第二部分 JVM 篇", "pageNumber": 8, "bold": True, "italic": False, "children": []},
]

STUB = """
<script>
window.__FAKE__ = %FAKE%;
window.__PAGES__ = %PAGES%;
window.__OUTLINE__ = %OUTLINE%;

window.__TAURI__ = { core: { invoke: async function (cmd) {
  if (cmd === 'load_app_state') return %STATE%;
  if (cmd === 'load_annotations') return '[]';
  if (cmd === 'save_annotations') return null;
  if (cmd === 'save_app_state') return null;
  return null;
}}};

window.FoliaBridge = {
  openDocumentDialog: async function () { return JSON.stringify({ ok: true, meta: %META% }); },
  openDocumentPath: async function (path) { return JSON.stringify({ ok: true, meta: %META% }); },
  closeDocument: function () {},
  documentInfo: function () { return 'null'; },
  releaseThumbnail: function () {},
  renderThumbnail: async function () { return window.__FAKE__; },
  getPageSizes: async function () {
    var out = [];
    for (var i = 0; i < window.__PAGES__; i++) out.push({ width: 612, height: 792 });
    return JSON.stringify(out);
  },
  getOutline: async function () { return JSON.stringify(window.__OUTLINE__); },
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
    g.fillStyle = '#ffffff'; g.fillRect(0, 0, w, h);
    g.fillStyle = '#2f3b8f';
    g.fillRect(64 * scale, 72 * scale, 300 * scale, 24 * scale);
    g.fillStyle = '#c9cfe0';
    for (var i = 0; i < 20; i++) {
      g.fillRect(64 * scale, (132 + i * 24) * scale, (380 + (i % 5) * 30) * scale, 9 * scale);
    }
    g.fillStyle = '#e8ecf6';
    g.fillRect(64 * scale, 630 * scale, 220 * scale, 110 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};

// 自动打开最近的一份，进入阅读器
window.addEventListener('load', function () {
  setTimeout(function () {
    var tab = document.querySelector('.tab-label');
    if (tab) { tab.click(); return; }
    var card = document.querySelector('.file-card');
    if (card) card.click();
  }, 300);
  %AFTER%
});
</script>
"""

state_json = json.dumps(json.dumps(REAL, ensure_ascii=False))
meta = {
    "id": "demo-doc",
    "path": REAL["recentFiles"][0]["path"],
    "name": REAL["recentFiles"][0]["name"],
    "size": REAL["recentFiles"][0]["size"],
    "numPages": PAGES,
}


def build(outdir, after=""):
    out = pathlib.Path(outdir)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)

    stub = (
        STUB.replace("%FAKE%", json.dumps(FAKE_URL))
        .replace("%PAGES%", str(PAGES))
        .replace("%OUTLINE%", json.dumps(OUTLINE, ensure_ascii=False))
        .replace("%STATE%", state_json)
        .replace("%META%", json.dumps(meta, ensure_ascii=False))
        .replace("%AFTER%", after)
    )
    html = html.replace("</head>", stub + "\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")

    # 占位符必须全部被替换掉，否则就是 bug
    left = [t for t in ("%FAKE%", "%PAGES%", "%OUTLINE%", "%STATE%", "%META%", "%AFTER%") if t in html]
    assert not left, f"unreplaced placeholders in {outdir}: {left}"
    print("built ->", out)


# 变体 1：默认阅读器（侧栏收起）
build(str(_HERE) + "/reader")

# 变体 2：切到「目录」面板
AFTER = """
  setTimeout(function () {
    var btns = document.querySelectorAll('.rail-btn');
    if (btns[1]) btns[1].click();
  }, 1200);
"""
build(str(_HERE) + "/reader_side", AFTER)
