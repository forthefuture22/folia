#!/usr/bin/env python3
"""把「批注」面板打开、并让一条批注处于选中态，截一张图。

目的：用户说「批注徽标还是淡紫色」，但产物里 `.folia-hl-badge` 已经是纯色。
所以要把**所有**跟批注沾边、又可能显淡紫的元素一次性摆到同一张图里：
  .side-tab.active（批注页签）  .ann-item.active（选中条目底色）
  .ann-dot（列表里的颜色点）    .swatch.active（色板选中环）
  .pill-tool（工具按钮）        .thumb-item.active .thumb-num（页码胶囊）
  .folia-hl-badge（页面上的圆点）
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import functools, http.server, json, pathlib, re, shutil, socketserver, subprocess, threading

SRC = pathlib.Path(str(_REPO) + "/dist")
ROOT = pathlib.Path(str(_HERE))
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8743
PAGE_W, PAGE_H = 612.0, 792.0
NUM_PAGES = 60
COLORS = ["yellow", "green", "blue", "pink"]

DOCS = {"/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha.pdf",
                           "numPages": NUM_PAGES, "size": 1048576}}
RECENT = [{"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha.pdf",
           "size": 1048576, "pages": NUM_PAGES, "last_page": 1,
           "opened_at": 2000.0, "favorite": False}]

# 和用户磁盘上那条一模一样：yellow、page 1（放第 1 页好看清）
ANNOTS = [
    {"id": "hl-yellow", "page": 1, "color": "yellow", "text": "但会影响区分度，可能增加扫描行数。",
     "note": "", "rects": [{"x": 0.20, "y": 0.16, "w": 0.5, "h": 0.05}], "createdAt": 1.0},
    {"id": "hl-blue", "page": 1, "color": "blue", "text": "第二条，用来看列表里的圆点。",
     "note": "", "rects": [{"x": 0.20, "y": 0.30, "w": 0.5, "h": 0.05}], "createdAt": 2.0},
]

DRIVER = """
var out = [];
function log(s) { out.push(s); }
window.addEventListener('load', function () {
  setTimeout(function () { document.querySelector('.file-card').click(); }, 300);
  setTimeout(function () {
    var rail = document.querySelectorAll('.rail-btn');
    for (var i = 0; i < rail.length; i++) {
      if ((rail[i].title || '') === '批注') { rail[i].click(); }
    }
  }, 1300);
  setTimeout(function () {
    var head = document.querySelector('.ann-head');
    if (head) { head.click(); }
  }, 1900);
  setTimeout(function () {
    var sels = ['.side-tab.active', '.ann-item', '.ann-item.active', '.ann-dot',
                '.swatch.active', '.pill-tool', '.thumb-item.active .thumb-num',
                '.folia-hl-badge', '.textLayer ::selection'];
    var info = {};
    sels.forEach(function (sel) {
      var el = document.querySelector(sel);
      if (!el) { info[sel] = null; return; }
      var r = el.getBoundingClientRect(), cs = getComputedStyle(el);
      info[sel] = { x: Math.round(r.left), y: Math.round(r.top),
                    w: Math.round(r.width), h: Math.round(r.height),
                    bg: cs.backgroundColor, border: cs.borderTopColor, color: cs.color };
    });
    info['_annItems'] = document.querySelectorAll('.ann-item').length;
    var pre = document.createElement('pre');
    pre.id = 'shot';
    pre.textContent = JSON.stringify(info, null, 1);
    document.body.appendChild(pre);
  }, 2700);
});
"""

STUB = """
<script>
window.__DOCS__ = %DOCS%;
window.__CUR__ = null;
window.__ANNOTS__ = %ANNOTS%;
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
  search: async function () { return '[]'; },
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
    g.fillStyle = '#333333';
    for (var i = 0; i < 24; i++) g.fillRect(40 * scale, (40 + i * 30) * scale, (400 + (i % 3) * 50) * scale, 11 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};
//DRIVER//
</script>
"""


def build(name: str) -> pathlib.Path:
    # 注意：driver 自己带 %COLORS% 之类的占位符，必须先插入 driver 再统一 replace。
    stub = (STUB.replace("//DRIVER//", DRIVER)
            .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
            .replace("%ANNOTS%", json.dumps(ANNOTS, ensure_ascii=False))
            .replace("%STATE%", json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False)))
            .replace("%PW%", repr(PAGE_W))
            .replace("%PH%", repr(PAGE_H)))
    dest = ROOT / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest)
    html = (dest / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    html = html.replace("</head>", stub + "\n</head>", 1)
    (dest / "index.html").write_text(html, encoding="utf-8")
    left = [t for t in ("%DOCS%", "%ANNOTS%", "%STATE%", "%PW%", "%PH%") if t in html]
    assert not left, f"unreplaced: {left}"
    return dest


class Server:
    def __init__(self, directory: pathlib.Path):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def chrome(directory, extra, budget):
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--force-device-scale-factor=1", "--window-size=1280,900",
           f"--virtual-time-budget={budget}", *extra,
           f"http://127.0.0.1:{PORT}/index.html"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout


dest = build("app")
with Server(dest):
    dom = chrome(dest, ["--dump-dom"], 9000)
    png = ROOT / "panel.png"
    chrome(dest, [f"--screenshot={png}"], 9000)

m = re.search(r'<pre id="shot">(.*?)</pre>', dom, re.S)
print(m.group(1).replace("&quot;", '"').replace("&amp;", "&") if m else "NO DUMP")
print("png:", png, png.exists())
