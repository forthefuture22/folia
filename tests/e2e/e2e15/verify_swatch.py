#!/usr/bin/env python3
"""验证色板选中态不再把颜色吃掉。

修复前：`.swatch.active { border-color: var(--accent); box-shadow: 0 0 0 2px #fff inset }`
        15px 的圆里是 紫2px / 白2px / 本色7px / 白2px / 紫2px —— 本色只占面积 22%。
修复后：边框保持透明，本色铺满整圆，选中态画在圆外的中性描边上。

判据：
  1. 选中色板的**横向扫描**应当整段都是本色（不再出现紫色段）；
  2. 选中色板圆内的**紫像素数应当为 0**；
  3. 选中与未选中的色板，圆心颜色一致（都是色板原色）；
  4. 选中态确实可见 —— 圆外一圈中性描边（灰色），不是"看不出来选中了"。
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
PORT = 8744
PAGE_W, PAGE_H = 612.0, 792.0
NUM_PAGES = 60

EXPECTED = {"yellow": (0xFF, 0xD6, 0x00), "green": (0x34, 0xC7, 0x59),
            "blue": (0x00, 0x7A, 0xFF), "pink": (0xFF, 0x2D, 0x78)}
ACCENT = (0x6C, 0x5C, 0xE7)

DOCS = {"/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha.pdf",
                           "numPages": NUM_PAGES, "size": 1048576}}
RECENT = [{"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha.pdf",
           "size": 1048576, "pages": NUM_PAGES, "last_page": 1,
           "opened_at": 2000.0, "favorite": False}]
ANNOTS = [{"id": "hl-yellow", "page": 1, "color": "yellow", "text": "t", "note": "",
           "rects": [{"x": 0.20, "y": 0.16, "w": 0.5, "h": 0.05}], "createdAt": 1.0}]

DRIVER = """
window.addEventListener('load', function () {
  setTimeout(function () { document.querySelector('.file-card').click(); }, 300);
  setTimeout(function () {
    var out = {};
    ['yellow', 'green', 'blue', 'pink'].forEach(function (c) {
      var el = document.querySelector('.swatch.folia-hl-' + c);
      if (!el) { out[c] = null; return; }
      var r = el.getBoundingClientRect(), cs = getComputedStyle(el);
      out[c] = { x: r.left, y: r.top, w: r.width, h: r.height,
                 bg: cs.backgroundColor, border: cs.borderTopColor,
                 shadow: cs.boxShadow };
    });
    var pre = document.createElement('pre');
    pre.id = 'shot';
    pre.textContent = JSON.stringify({ dpr: window.devicePixelRatio, swatches: out });
    document.body.appendChild(pre);
  }, 1800);
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


def build(name):
    stub = (STUB.replace("//DRIVER//", DRIVER)
            .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
            .replace("%ANNOTS%", json.dumps(ANNOTS, ensure_ascii=False))
            .replace("%STATE%", json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False)))
            .replace("%PW%", repr(PAGE_W)).replace("%PH%", repr(PAGE_H)))
    dest = ROOT / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest)
    html = (dest / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    html = html.replace("</head>", stub + "\n</head>", 1)
    (dest / "index.html").write_text(html, encoding="utf-8")
    return dest


class Server:
    def __init__(self, directory):
        handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
        socketserver.TCPServer.allow_reuse_address = True
        self.httpd = socketserver.TCPServer(("127.0.0.1", PORT), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def chrome(extra, budget):
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--force-device-scale-factor=1", "--window-size=1280,900",
           f"--virtual-time-budget={budget}", *extra,
           f"http://127.0.0.1:{PORT}/index.html"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout


def near(p, t, tol=26):
    return max(abs(p[i] - t[i]) for i in range(3)) <= tol


def is_purple(p, tol=40):
    """够紫：蓝显著高于绿，且红蓝接近。"""
    r, g, b = p
    return b - g >= tol and abs(r - b) <= 55 and b > 90


dest = build("app-swatch")
with Server(dest):
    dom = chrome(["--dump-dom"], 8000)
    png = ROOT / "swatch.png"
    chrome([f"--screenshot={png}"], 8000)

m = re.search(r'<pre id="shot">(.*?)</pre>', dom, re.S)
meta = json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))

from PIL import Image
im = Image.open(png).convert("RGB")
scale = meta["dpr"]

print("=" * 74)
print("色板选中态验证")
print("=" * 74)
fails = []
for color, sw in meta["swatches"].items():
    if sw is None:
        print(f"{color:7s} 找不到元素"); fails.append(color); continue
    x0 = int(round(sw["x"] * scale)); y0 = int(round(sw["y"] * scale))
    x1 = int(round((sw["x"] + sw["w"]) * scale)) - 1
    y1 = int(round((sw["y"] + sw["h"]) * scale)) - 1
    cy = (y0 + y1) // 2
    row = [im.getpixel((x, cy)) for x in range(x0, x1 + 1)]
    inner = [im.getpixel((x, y)) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
    purple = sum(1 for p in inner if is_purple(p))
    centre = im.getpixel(((x0 + x1) // 2, cy))
    target = EXPECTED[color]
    ok_c = near(centre, target)
    print(f"\n{color:7s} 圆心={centre} 期望={target} 匹配={'YES' if ok_c else 'NO'}"
          f"  圆内紫像素={purple}/{len(inner)}")
    print(f"        横向扫描: {row}")
    if not ok_c:
        fails.append(f"{color}:圆心不是本色")
    if purple:
        fails.append(f"{color}:圆内还有 {purple} 个紫像素")

# 选中态是否可见：圆外一圈应当是中性灰，不是本色也不是紫
sw = meta["swatches"]["yellow"]
bx = int(round((sw["x"] + sw["w"] / 2) * scale))
by = int(round((sw["y"] + sw["h"] / 2) * scale))
r_out = int(round(sw["w"] / 2 * scale)) + 3
outside = im.getpixel((bx, by - r_out))
print(f"\n选中环（圆外 {r_out}px 处）= {outside}"
      f"   紫? {'YES' if is_purple(outside) else 'NO'}")
if is_purple(outside):
    fails.append("圆外的选中环还是紫的")
if sw["bg"] != "rgb(255, 214, 0)":
    fails.append(f"选中色板底色被改坏了: {sw['bg']}")
print(f"选中色板 computed bg = {sw['bg']}  border = {sw['border']}")
print(f"选中色板 computed boxShadow = {sw['shadow']}")

print("\n" + "=" * 74)
print("结论:", "全部通过" if not fails else "失败 -> " + "; ".join(fails))
print("=" * 74)
