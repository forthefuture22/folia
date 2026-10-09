#!/usr/bin/env python3
"""验证「选中文字的底色」不再写死品牌紫，而是跟着当前所选批注颜色走。

修复前：`.textLayer ::selection { background: rgba(108,92,231,.28) }` ——
        叠在白纸上 ≈ (214,209,248)，也就是用户截图里那抹淡紫。
修复后：`background: var(--hl-sel)`，而 `--hl-sel` 由 app.rs 写成 `var(--hl-<color>)`，
        和高亮矩形共用 `:root` 里的 `--hl-<color>` 定义。

判据：
  A. 点哪个色板，根元素上的 `--hl-sel` 就等于 `var(--hl-<该色>)`；
  B. `--hl-<该色>` 的取值 == 同色高亮矩形算出来的 backgroundColor（预览 == 结果）；
  C. 截图上选中区域的像素 ≈ 该色叠在白纸上的结果，且**不等于**旧的淡紫。
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
PORT = 8746
PAGE_W, PAGE_H = 612.0, 792.0
NUM_PAGES = 60
COLORS = ["yellow", "green", "blue", "pink"]

# 期望：token 值 / 叠在纯白上的实际像素
TOKEN = {"yellow": "rgba(255, 214, 0, 0.42)", "green": "rgba(52, 199, 89, 0.36)",
         "blue": "rgba(0, 122, 255, 0.3)", "pink": "rgba(255, 45, 120, 0.28)"}
ALPHA = {"yellow": 0.42, "green": 0.36, "blue": 0.3, "pink": 0.28}
RGB = {"yellow": (255, 214, 0), "green": (52, 199, 89),
       "blue": (0, 122, 255), "pink": (255, 45, 120)}
OLD_PURPLE = (214, 209, 248)   # rgba(108,92,231,.28) 叠白

DOCS = {"/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha.pdf",
                           "numPages": NUM_PAGES, "size": 1048576}}
RECENT = [{"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha.pdf",
           "size": 1048576, "pages": NUM_PAGES, "last_page": 1,
           "opened_at": 2000.0, "favorite": False}]
ANNOTS = [{"id": f"hl-{c}", "page": 1, "color": c, "text": c, "note": "",
           "rects": [{"x": 0.10, "y": 0.72 + i * 0.04, "w": 0.4, "h": 0.03}],
           "createdAt": 1.0} for i, c in enumerate(COLORS)]

# 驱动 A：逐个点色板，把变量链 dump 出来
DRIVER_DUMP = """
window.addEventListener('load', function () {
  var out = [];
  setTimeout(function () { document.querySelector('.file-card').click(); }, 300);
  setTimeout(function () {
    var list = %COLORS%, i = 0;
    function step() {
      if (i >= list.length) { dump(); return; }
      var c = list[i];
      document.querySelector('.swatch.folia-hl-' + c).click();
      setTimeout(function () {
        var root = getComputedStyle(document.documentElement);
        var rect = document.querySelector('.hl-layer .folia-hl-' + c);
        out.push({ color: c,
                   sel: root.getPropertyValue('--hl-sel').trim(),
                   token: root.getPropertyValue('--hl-' + c).trim(),
                   rectBg: rect ? getComputedStyle(rect).backgroundColor : null,
                   swatchBg: getComputedStyle(document.querySelector('.swatch.folia-hl-' + c)).backgroundColor });
        i++; step();
      }, 220);
    }
    step();
  }, 1500);
  function dump() {
    var pre = document.createElement('pre');
    pre.id = 'shot';
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
});
"""

# 驱动 B：按 URL 上的 ?color= 选一个色板，然后选中文字层里的 span
DRIVER_SHOT = """
window.addEventListener('load', function () {
  var color = (location.search.match(/color=(\\w+)/) || [])[1] || 'yellow';
  setTimeout(function () { document.querySelector('.file-card').click(); }, 300);
  setTimeout(function () {
    var sw = document.querySelector('.swatch.folia-hl-' + color);
    if (sw) sw.click();
  }, 1400);
  setTimeout(function () {
    var layer = document.querySelector('.textLayer');
    var spans = layer.querySelectorAll('span');
    var range = document.createRange();
    range.setStartBefore(spans[0]);
    range.setEndAfter(spans[spans.length - 1]);
    var sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    window.focus();
    // 不滚动 —— 文字本来就在页面顶部，坐标两遍必然一致
    var r = spans[1].getBoundingClientRect();
    var pre = document.createElement('pre');
    pre.id = 'shot';
    pre.textContent = JSON.stringify({ color: color, spans: spans.length,
      x: r.left, y: r.top, w: r.width, h: r.height,
      selText: String(sel).slice(0, 40) });
    document.body.appendChild(pre);
  }, 2000);
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
  renderTextLayer: async function (containerId, page, scale) {
    // 造几行可选中的文字，放在**画布没有黑条**的区域，方便干净地采样底色
    var el = document.getElementById(containerId);
    if (!el) return;
    el.innerHTML = '';
    // 放在页面**顶部**：不滚动就可见，两遍跑的坐标必然一致
    for (var i = 0; i < 5; i++) {
      var s = document.createElement('span');
      s.textContent = 'SELECTABLE LINE ' + i;
      s.style.cssText = 'position:absolute;left:' + Math.round(60 * scale) + 'px;'
        + 'top:' + Math.round((60 + i * 34) * scale) + 'px;'
        + 'font-size:' + Math.round(15 * scale) + 'px;color:transparent;';
      el.appendChild(s);
    }
  },
  renderPage: async function (canvasId, page, scale) {
    var c = document.getElementById(canvasId);
    if (!c) return JSON.stringify({ ok: false });
    var w = %PW% * scale, h = %PH% * scale;
    c.width = w; c.height = h; c.style.width = w + 'px'; c.style.height = h + 'px';
    var g = c.getContext('2d');
    g.fillStyle = '#fff'; g.fillRect(0, 0, w, h);
    // 黑条画在**下半部分**，顶部留白给选区采样（不与可选文字重叠）
    g.fillStyle = '#333333';
    for (var i = 0; i < 8; i++) g.fillRect(40 * scale, (300 + i * 30) * scale, (400 + (i % 3) * 50) * scale, 11 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};
//DRIVER//
</script>
"""


def build(driver, name):
    stub = (STUB.replace("//DRIVER//", driver)
            .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
            .replace("%ANNOTS%", json.dumps(ANNOTS, ensure_ascii=False))
            .replace("%STATE%", json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False)))
            .replace("%PW%", repr(PAGE_W)).replace("%PH%", repr(PAGE_H))
            .replace("%COLORS%", json.dumps(COLORS)))
    dest = ROOT / name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(SRC, dest)
    html = (dest / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    html = html.replace("</head>", stub + "\n</head>", 1)
    (dest / "index.html").write_text(html, encoding="utf-8")
    left = [t for t in ("%DOCS%", "%ANNOTS%", "%STATE%", "%PW%", "%PH%", "%COLORS%") if t in html]
    assert not left, f"unreplaced: {left}"
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


def chrome(extra, budget, query=""):
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--force-device-scale-factor=1", "--window-size=1280,900",
           f"--virtual-time-budget={budget}", *extra,
           f"http://127.0.0.1:{PORT}/index.html{query}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout


def parse(dom):
    m = re.search(r'<pre id="shot">(.*?)</pre>', dom, re.S)
    assert m, "no dump"
    return json.loads(m.group(1).replace("&quot;", '"').replace("&amp;", "&"))


dump_dir = build(DRIVER_DUMP, "app-dump")
shot_dir = build(DRIVER_SHOT, "app-shot")

fails = []

# 静态断言：产物里的 ::selection 确实读的是 --hl-sel（而不是写死的紫）
css = next(SRC.glob("styles-*.css")).read_text(encoding="utf-8")
m = re.search(r'\.textLayer ::selection\s*\{[^}]*?background:\s*var\(--hl-sel\)', css, re.S)
print("产物 CSS：.textLayer ::selection { background: var(--hl-sel) } ->",
      "OK" if m else "FAIL")
if not m:
    fails.append("::selection 没读 --hl-sel")
sel_block = re.search(r'\.textLayer ::selection\s*\{[^}]*\}', css, re.S)
if sel_block and "108, 92, 231" in sel_block.group(0):
    fails.append("::selection 块里还留着写死的品牌紫")
print()
print("=" * 76)
print("A/B. 变量链：点哪个色板，--hl-sel 指向哪个 token，token 是否等于高亮矩形")
print("=" * 76)
with Server(dump_dir):
    rows = parse(chrome(["--dump-dom"], 12000))
for r in rows:
    c = r["color"]
    # 注意：`getComputedStyle` 对自定义属性返回的是**替换后**的值，
    # 所以这里拿到的已经是 `var(--hl-sel)` → `var(--hl-<c>)` → rgba 的最终结果。
    want_sel = TOKEN[c]
    ok_sel = r["sel"] == want_sel
    ok_tok = r["token"] == TOKEN[c]
    ok_rect = r["rectBg"] == TOKEN[c]
    ok_sw = r["swatchBg"] == f"rgb{RGB[c]}"
    print(f"{c:7s} --hl-sel={r['sel']:<16s} 期望={want_sel:<16s} {'OK' if ok_sel else 'FAIL'}"
          f" | token={r['token']:<26s} {'OK' if ok_tok else 'FAIL'}"
          f" | 矩形bg={r['rectBg']:<26s} {'OK' if ok_rect else 'FAIL'}")
    if not ok_sel:
        fails.append(f"{c}:--hl-sel 不对")
    if not ok_tok:
        fails.append(f"{c}:token 值不对")
    if not ok_rect:
        fails.append(f"{c}:token 和高亮矩形不一致（预览≠结果）")
    if not ok_sw:
        fails.append(f"{c}:色板底色被改坏")

print()
print("=" * 76)
print("C. 像素：选中文字那层底色的实际颜色")
print("=" * 76)
from PIL import Image


def sample_selection(png, meta):
    """取选中区域里出现次数最多的那个颜色。"""
    im = Image.open(png).convert("RGB")
    x0 = int(round(meta["x"] + 4))
    x1 = int(round(meta["x"] + meta["w"] - 4))
    y0 = int(round(meta["y"] + 3))
    y1 = int(round(meta["y"] + meta["h"] - 3))
    counts = {}
    for x in range(x0, x1 + 1, 2):
        for y in range(y0, y1 + 1):
            p = im.getpixel((x, y))
            counts[p] = counts.get(p, 0) + 1
    return max(counts, key=counts.get)


# --- A/B：把旧的写死紫装回去，量化「headless 未聚焦时选中色的淡化系数」
#
# Chrome 在没有输入焦点的窗口里会把 `::selection` 画淡。
# 系数不是 1，所以不能直接拿"标称 alpha 叠白"当期望值 ——
# 用同一套渲染管线跑一遍旧代码，把这个系数测出来，再拿它去校验新代码。
ab_dir = build(DRIVER_SHOT, "app-ab")
css_file = next(ab_dir.glob("styles-*.css"))
txt = css_file.read_text(encoding="utf-8")
patched = re.sub(r'(\.textLayer ::selection\s*\{[^}]*?background:\s*)var\(--hl-sel\)',
                 r'\1rgba(108, 92, 231, 0.28)', txt, flags=re.S)
assert patched != txt, "A/B 补丁没打上"
css_file.write_text(patched, encoding="utf-8")

# ⚠️ trunk 生成的 <link> 带 `integrity="sha384-..."`。改了 CSS 却不更新它，
# 浏览器会**直接拒绝应用整份样式表** —— 页面变成裸 HTML，
# 于是两遍跑的布局对不上，采样采到别的地方去。必须把 integrity 摘掉。
ab_html = ab_dir / "index.html"
h = ab_html.read_text(encoding="utf-8")
h2 = re.sub(r'(<link[^>]*rel="stylesheet"[^>]*?)\s+integrity="[^"]*"', r'\1', h)
assert h2 != h, "A/B 的 integrity 没摘掉"
ab_html.write_text(h2, encoding="utf-8")

with Server(ab_dir):
    meta_ab = parse(chrome(["--dump-dom"], 12000, query="?color=yellow"))
    png_ab = ROOT / "ab-old-purple.png"
    chrome([f"--screenshot={png_ab}"], 12000, query="?color=yellow")
ab_main = sample_selection(png_ab, meta_ab)
# 旧的写死紫 rgba(108,92,231,.28)：R = 255 - 147*ka  →  解出 k
ka = (255 - ab_main[0]) / 147.0
k = ka / 0.28
print(f"A/B 旧紫采样={ab_main}  有效 alpha={ka:.4f}  →  淡化系数 k={k:.3f}")
print()

shots = {}
with Server(shot_dir):
    for c in COLORS:
        png = ROOT / f"sel-{c}.png"
        # `--dump-dom` 与 `--screenshot` 不能同跑，分两遍；布局确定，坐标一致
        meta = parse(chrome(["--dump-dom"], 12000, query=f"?color={c}"))
        chrome([f"--screenshot={png}"], 12000, query=f"?color={c}")
        main = sample_selection(png, meta)
        a = ALPHA[c]
        expect = tuple(round(RGB[c][i] * k * a + 255 * (1 - k * a)) for i in range(3))
        dev = max(abs(main[i] - expect[i]) for i in range(3))
        # 和旧的淡紫比：必须明显更接近自己那一色
        d_self = max(abs(main[i] - expect[i]) for i in range(3))
        old_dim = tuple(round(OLD_PURPLE[i] * k + 255 * (1 - k)) for i in range(3))
        d_old = max(abs(main[i] - old_dim[i]) for i in range(3))
        shots[c] = (png, meta)
        print(f"{c:7s} 选中区主色={main}  期望={expect}  偏差={d_self}"
              f"  与旧紫距离={d_old}  {'← 更像旧紫，没修好' if d_old < d_self else 'OK'}"
              f"  selText={meta['selText']!r}")
        if max(main) - min(main) < 6:
            fails.append(f"{c}:采样区是纯白/纯灰，说明坐标没落在选区上")
        if d_self > 4:
            fails.append(f"{c}:选中底色不是该色的淡色版")
        if d_old < d_self:
            fails.append(f"{c}:选中底色更像旧的品牌紫")

print()
print("=" * 76)
print("结论:", "全部通过" if not fails else "失败 -> " + "; ".join(fails))
print("=" * 76)
