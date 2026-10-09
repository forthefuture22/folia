#!/usr/bin/env python3
"""五项改动的一次性验证：

1. 打开 PDF 后自动适宽
2. 缩略图跟随正文滚动
3. 阅读器右上角不再有「回主页」圆按钮
4. 搜索框从工具栏搬进左侧栏「搜索结果」面板（并验证 ⌘F / 放大镜能把焦点送进去）
5. 批注徽标颜色跟随所选颜色

stub 掉 pdf.js 桥接层，用 DOM 断言 + 数值测量，不靠肉眼看截图。
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import functools, http.server, json, pathlib, re, shutil, socketserver, subprocess, sys, threading

SRC = pathlib.Path(str(_REPO) + "/dist")
OUT = pathlib.Path(str(_HERE) + "/app")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PAGE_W, PAGE_H = 612.0, 792.0
NUM_PAGES = 60
QUERY = "reader"

DOCS = {
    "/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha 报告.pdf", "numPages": NUM_PAGES, "size": 1048576},
}
RECENT = [
    {"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha 报告.pdf", "size": 1048576,
     "pages": NUM_PAGES, "last_page": 1, "opened_at": 2000.0, "favorite": False},
]

# 四种颜色各一条，都落在第 1 页 —— 用来量徽标的计算背景色。
ANNOTS = [
    {"id": f"hl-{c}", "page": 1, "color": c, "text": f"{c} text", "note": "",
     "rects": [{"x": 0.1, "y": 0.10 + i * 0.06, "w": 0.3, "h": 0.02}], "createdAt": 1.0}
    for i, c in enumerate(["yellow", "green", "blue", "pink"])
]

HITS = [
    {"page": 1 + i % 4, "itemIndex": i, "charIndex": 12, "length": len(QUERY),
     "key": f"{1 + i % 4}-{i}-0", "y": 0.1 + 0.08 * (i % 4),
     "snippet": f"…the {QUERY.capitalize() if i % 3 == 0 else QUERY} must open several documents, part {i + 1}…"}
    for i in range(9)
]

STUB = """
<script>
window.__DOCS__ = %DOCS%;
window.__HITS__ = %HITS%;
window.__ANNOTS__ = %ANNOTS%;
window.__CUR__ = null;
window.__TAURI_INTERNALS__ = { invoke: function () { return Promise.resolve(null); } };
window.__TAURI__ = { core: { invoke: async function (cmd) {
  if (cmd === 'load_app_state') return %STATE%;
  if (cmd === 'load_annotations') return JSON.stringify(window.__ANNOTS__);
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
    for (var i = 0; i < n; i++) out.push({ width: %PW%, height: %PH% });
    return JSON.stringify(out);
  },
  getOutline: async function () { return '[]'; },
  search: async function () { return JSON.stringify(window.__HITS__); },
  getSelectionInfo: function () { return 'null'; },
  clearSelection: function () {},
  onFileDrop: function () {},
  renderTextLayer: async function () {},
  renderPage: async function (canvasId, page, scale) {
    var c = document.getElementById(canvasId);
    if (!c) return JSON.stringify({ ok: false });
    var w = %PW% * scale, h = %PH% * scale;
    c.width = w; c.height = h; c.style.width = w + 'px'; c.style.height = h + 'px';
    var g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);
    g.fillStyle = '#e7e9f2';
    for (var i = 0; i < 12; i++) g.fillRect(40 * scale, (80 + i * 30) * scale, (400 + (i % 3) * 50) * scale, 10 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};

// ---------------------------------------------------------------- 驱动
var out = [];
function q(sel) { return document.querySelector(sel); }
function qa(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
function log(s) { out.push(s); }

// 量一次「徽标颜色」：拿四种颜色徽标的计算背景色。
function badgeColors() {
  return ["yellow", "green", "blue", "pink"].map(function (c) {
    var el = q('.folia-hl-badge.folia-hl-' + c);
    if (!el) return c + '=<none>';
    return c + '=' + getComputedStyle(el).backgroundColor;
  }).join(' ');
}

function snapOpen() {
  var stage = q('.stage');
  var label = q('.zoom-label');
  var fitPct = Math.round((stage.clientWidth - 64) / %PW% * 100);
  log('01-open  stageW=' + stage.clientWidth
      + ' | zoom=' + (label ? label.textContent.trim() : '-')
      + ' | page=' + (q('.page-input') ? q('.page-input').value : '-')
      + ' | expect_fit=' + fitPct + '%'
      + ' | match=' + (label && label.textContent.trim() === fitPct + '%' ? 'YES' : 'NO'));
  log('01-open  reader-top-right: buttons=' + qa('.reader-top-right button').length
      + ' round-accent=' + qa('.round-accent').length
      + ' || toolbar search-box=' + qa('.reader-top .search-box').length
      + ' toolbar search-input=' + qa('.reader-top .search-input').length);
  log('01-open  badges: ' + badgeColors());
  log('01-open  side-search(mounted before opening panel)=' + qa('.side-search').length);
}

function snapRail() {
  var input = q('.side-search .search-input');
  log('02-rail  side-search=' + qa('.side-search').length
      + ' input=' + (input ? 1 : 0)
      + ' focused=' + (input && document.activeElement === input ? 'YES' : 'NO')
      + ' panel=' + (q('.side-tab.active') ? q('.side-tab.active').textContent.trim() : '-'));
}

function snapSearch() {
  log('03-search hits=' + qa('.hit-item').length
      + ' marks=' + qa('.hit-mark').length
      + ' count=' + (q('.hit-count') ? q('.hit-count').textContent.trim() : '-'));
}

function snapThumbs() {
  var body = q('.side-body');
  var act = q('.thumb-item.active');
  if (!body || !act) { log('04-thumbs MISS body=' + !!body + ' active=' + !!act); return; }
  var bv = body.getBoundingClientRect(), ar = act.getBoundingClientRect();
  var inView = ar.top >= bv.top - 1 && ar.bottom <= bv.bottom + 1;
  log('04-thumbs page=' + (q('.page-input') ? q('.page-input').value : '-')
      + ' activeThumb=' + act.querySelector('.thumb-num').textContent.trim()
      + ' scrollTop=' + Math.round(body.scrollTop)
      + ' scrollable=' + (body.scrollHeight > body.clientHeight)
      + ' inView=' + (inView ? 'YES' : 'NO')
      + ' items=' + qa('.thumb-item').length);
}

function click(sel, idx) {
  var el = qa(sel)[idx === undefined ? 0 : idx];
  if (el) el.click(); else log('MISS click ' + sel + '[' + idx + ']');
}
function typeInto(sel, value) {
  var el = q(sel);
  if (!el) { log('MISS type ' + sel); return; }
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
}
function submitPage(value) {
  var el = q('.page-input');
  if (!el) { log('MISS page-input'); return; }
  // 缩略图跟随现在是监听 `.stage` 的 scroll 事件做的。
  // 这个 headless 环境（虚拟时钟）**不产生帧**，程序化赋 scrollTop 不会派发 scroll 事件，
  // 而 `scroll-behavior: smooth` 的动画也永远走不完 ——
  // 所以这里手动补一个 scroll 事件、并把平滑临时关掉，才能测到跟随逻辑本身。
  var body = q('.side-body');
  if (body) body.style.scrollBehavior = 'auto';
  el.value = String(value);
  el.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  setTimeout(function () { q('.stage').dispatchEvent(new Event('scroll')); }, 120);
}
function dump() {
  var pre = document.createElement('pre');
  pre.id = 'e2e';
  pre.textContent = out.join('\\n');
  document.body.appendChild(pre);
}

window.addEventListener('load', function () {
  setTimeout(function () { click('.file-card', 0); }, 300);
  setTimeout(snapOpen, 1600);
  // 放大镜（图标轨第 4 个）→ 应当把搜索面板顶出来并把焦点送进输入框
  setTimeout(function () { click('.rail-btn', 3); }, 1800);
  setTimeout(snapRail, 2200);
  setTimeout(function () { typeInto('.side-search .search-input', %Q%); }, 2400);
  setTimeout(snapSearch, 2900);
  // 切回缩略图，跳到第 45 页，验证缩略图自动跟随
  setTimeout(function () { click('.rail-btn', 0); }, 3100);
  setTimeout(function () { submitPage(45); }, 3500);
  setTimeout(snapThumbs, 4300);
  setTimeout(dump, 4900);
});
</script>
"""

# `restore` 场景：上次关掉的正是这份、且缩放是 200% ——
# 打开它必须沿用 200%，不能被「自动适宽」冲掉。
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "fresh"
if SCENARIO == "restore":
    PERSISTED = {"recentFiles": RECENT, "lastPath": "/tmp/alpha.pdf",
                 "lastPage": 30, "scale": 2.0}
    OUT = pathlib.Path(str(_HERE) + "/app-restore")
    # 续读场景只关心「缩放没被适宽冲掉、页码续上了」，后面的步骤全部短路。
    STUB = STUB.replace(
        "  setTimeout(function () { click('.rail-btn', 3); }, 1800);\n"
        "  setTimeout(snapRail, 2200);\n"
        "  setTimeout(function () { typeInto('.side-search .search-input', %Q%); }, 2400);\n"
        "  setTimeout(snapSearch, 2900);\n"
        "  // 切回缩略图，跳到第 45 页，验证缩略图自动跟随\n"
        "  setTimeout(function () { click('.rail-btn', 0); }, 3100);\n"
        "  setTimeout(function () { submitPage(45); }, 3500);\n"
        "  setTimeout(snapThumbs, 4300);\n",
        "  setTimeout(function () { log('02..04 skipped (restore scenario)'); }, 2200);\n")
else:
    PERSISTED = {"recentFiles": RECENT}

state_json = json.dumps(json.dumps(PERSISTED, ensure_ascii=False))
stub = (STUB
        .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
        .replace("%HITS%", json.dumps(HITS, ensure_ascii=False))
        .replace("%ANNOTS%", json.dumps(ANNOTS, ensure_ascii=False))
        .replace("%STATE%", state_json)
        .replace("%Q%", json.dumps(QUERY))
        .replace("%PW%", repr(PAGE_W))
        .replace("%PH%", repr(PAGE_H)))

if OUT.exists():
    shutil.rmtree(OUT)
shutil.copytree(SRC, OUT)
html = (OUT / "index.html").read_text(encoding="utf-8")
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", stub + "\n</head>", 1)
(OUT / "index.html").write_text(html, encoding="utf-8")
left = [t for t in ("%DOCS%", "%HITS%", "%ANNOTS%", "%STATE%", "%Q%", "%PW%", "%PH%") if t in html]
assert not left, f"unreplaced: {left}"
print("built ->", OUT)

# ---------------------------------------------------------------- 起本地服务
# 必须走 http：`<script type="module">` 在 file:// 下会被 CORS 拦掉，wasm 根本不会加载。
PORT = 8731
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(OUT))
httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
httpd.allow_reuse_address = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# ---------------------------------------------------------------- 跑 Chrome
cmd = [
    CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
    "--window-size=1280,900",
    "--virtual-time-budget=9000",
    "--dump-dom",
    f"http://127.0.0.1:{PORT}/index.html",
]
res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
httpd.shutdown()
dom = res.stdout
m = re.search(r'<pre id="e2e">(.*?)</pre>', dom, re.S)
if not m:
    print("!! no #e2e dump; chrome stderr tail:")
    print(res.stderr[-2000:])
    sys.exit(1)
print(m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))
