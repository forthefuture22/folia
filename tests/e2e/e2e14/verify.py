#!/usr/bin/env python3
"""两项修复的验证：

A. 批注徽标颜色 —— 用**像素采样**证明徽标内部是纯色，且等于工具栏色板的颜色。
   旧实现的徽标挂着 `.folia-hl`（`mix-blend-mode: multiply`），
   又正好压在高亮框左上角，于是左半边跟淡色高亮相乘、右半边跟白页相乘 ——
   同一个圆点两种深浅。所以这里同时检查「主色对不对」和「内部是否只有一种颜色」。

B. 缩略图跟随滚动 —— 让正文每帧匀速滚 55px，逐帧记录缩略图列表的 scrollTop：
   看它是不是**每帧都在小步移动**（连续平滑），而不是长时间不动再猛跳一格。
   再验一下「手动滚列表后的冷却期」确实拦住了跟随。

跑两遍 Chrome：一遍出截图给 PIL 采样，一遍只 dump DOM 拿行为数据。
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
ROOT = pathlib.Path(str(_HERE))
OUT = ROOT / "app"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8741
PAGE_W, PAGE_H = 612.0, 792.0
NUM_PAGES = 60

# 期望值 = styles.css 里 `.swatch.folia-hl-*` / `.folia-hl-badge.folia-hl-*` 的颜色
EXPECTED = {"yellow": (0xFF, 0xD6, 0x00), "green": (0x34, 0xC7, 0x59),
            "blue": (0x00, 0x7A, 0xFF), "pink": (0xFF, 0x2D, 0x78)}
COLORS = ["yellow", "green", "blue", "pink"]

DOCS = {"/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha.pdf", "numPages": NUM_PAGES, "size": 1048576}}
RECENT = [{"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha.pdf", "size": 1048576,
           "pages": NUM_PAGES, "last_page": 1, "opened_at": 2000.0, "favorite": False}]

# 四种颜色各一条，纵向错开，方便逐条采样
ANNOTS = [
    {"id": f"hl-{c}", "page": 1, "color": c, "text": f"{c} text", "note": "",
     "rects": [{"x": 0.20, "y": 0.16 + i * 0.14, "w": 0.5, "h": 0.05}], "createdAt": 1.0}
    for i, c in enumerate(COLORS)
]

# ---------------------------------------------------------------- 驱动 A：截图 + 徽标坐标
DRIVER_SHOT = """
var out = [];
function log(s) { out.push(s); }
window.addEventListener('load', function () {
  setTimeout(function () { document.querySelector('.file-card').click(); }, 300);
  setTimeout(function () {
    // ?multiply → 人为把旧的 bug 复现出来（徽标挂着 .folia-hl 时的效果）
    var force = location.search.indexOf('multiply') >= 0;
    var badges = Array.prototype.slice.call(document.querySelectorAll('.folia-hl-badge')).map(function (el) {
      if (force) { el.style.mixBlendMode = 'multiply'; }
      var r = el.getBoundingClientRect();
      var cls = Array.prototype.slice.call(el.classList);
      return { cls: cls.join(' '), x: r.left, y: r.top, w: r.width, h: r.height,
               blend: getComputedStyle(el).mixBlendMode };
    });
    var swatches = {};
    %COLORS%.forEach(function (c) {
      var el = document.querySelector('.swatch.folia-hl-' + c);
      if (!el) { swatches[c] = null; return; }
      var r = el.getBoundingClientRect();
      swatches[c] = { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    });
    var pre = document.createElement('pre');
    pre.id = 'shot';
    pre.textContent = JSON.stringify({
      dpr: window.devicePixelRatio, iw: window.innerWidth, ih: window.innerHeight,
      badges: badges, swatches: swatches
    });
    document.body.appendChild(pre);
  }, 1700);
});
"""

# ---------------------------------------------------------------- 驱动 B：跟随逻辑
#
# 这个 headless 环境（--headless=new + 虚拟时钟）**不产生帧**：
# 探针实测 requestAnimationFrame 一次都不回调，程序化赋 scrollTop 也不会派发 scroll 事件，
# 所以 `scroll-behavior: smooth` 的动画永远走不完 —— `set_scroll_top` 看起来"没生效"。
#
# 因此这里把 `scroll-behavior` 临时压成 auto，把**跟随逻辑本身**
# （目标换算 / 连续性 / 居中 / 冷却）单独隔离出来测，
# 并且用合成 scroll 事件代替浏览器派发。
# 「平滑」那一半由 CSS 的 `scroll-behavior: smooth` 负责，用静态断言查它确实写进了产物。
DRIVER_BEHAVE = """
var out = [];
function log(s) { out.push(s); }
function q(s) { return document.querySelector(s); }
function dump() {
  var pre = document.createElement('pre');
  pre.id = 'e2e';
  pre.textContent = out.join('\\n');
  document.body.appendChild(pre);
}

var STEP = 150, N = 26, SETTLE = 60;

function run() {
  var stage = q('.stage'), body = q('.side-body');
  log('B0-start items=' + document.querySelectorAll('.thumb-item').length
      + ' scrollBehavior(真身)=' + getComputedStyle(body).scrollBehavior
      + ' stageScrollH=' + Math.round(stage.scrollHeight)
      + ' bodyScrollH=' + Math.round(body.scrollHeight)
      + ' bodyClientH=' + body.clientHeight);
  // 隔离测试：关掉平滑动画，见上面的注释
  body.style.scrollBehavior = 'auto';

  var bodySteps = [], i = 0;
  function step() {
    if (i > N) { analyse(bodySteps); return; }
    stage.scrollTop = i * STEP;
    stage.dispatchEvent(new Event('scroll'));   // 虚拟时钟下浏览器不会自己派发
    setTimeout(function () {
      bodySteps.push(Math.round(body.scrollTop));
      i++;
      step();
    }, SETTLE);
  }
  step();
}

function analyse(bodySteps) {
  var moved = 0, still = 0, maxJump = 0, minJump = 1e9, stillAt = [];
  for (var i = 1; i < bodySteps.length; i++) {
    var d = bodySteps[i] - bodySteps[i - 1];
    if (Math.abs(d) > 2) { moved++; maxJump = Math.max(maxJump, d); minJump = Math.min(minJump, d); }
    else { still++; stillAt.push(i); }
  }
  log('B1-continuity steps=' + (bodySteps.length - 1)
      + ' movedSteps=' + moved
      + ' stillSteps=' + still
      + ' jumpRange=[' + (moved ? minJump : 0) + ',' + maxJump + ']'
      + ' distinct=' + new Set(bodySteps).size
      + ' first=' + bodySteps[0] + ' last=' + bodySteps[bodySteps.length - 1]);
  log('B1b-stillAt=' + (stillAt.length ? stillAt[0] + '..' + stillAt[stillAt.length - 1] : '-')
      + '  (开头几页的落点被 clamp 到列表顶部，不动是应该的)');

  // 居中断言：视口中心落在第 N 页的 t 处时，
  // 第 N 页的缩略图就该在列表中心**上方 t*pitch** 的位置 ——
  // 这正好证明它是按比例插值，而不是「翻页就吸附到中间」。
  var body = q('.side-body'), stage = q('.stage'), act = q('.thumb-item.active');
  if (!act) { log('B2-center MISS no active thumb'); cooldown(); return; }
  var items = document.querySelectorAll('.thumb-item');
  var num = parseInt(act.querySelector('.thumb-num').textContent.trim(), 10);
  var pitch = 0;
  if (items.length > num) {
    pitch = items[num].getBoundingClientRect().top - items[num - 1].getBoundingClientRect().top;
  }
  var pages = document.querySelectorAll('.page');
  var center = stage.getBoundingClientRect().top + stage.clientHeight / 2;
  var frac = 0;
  for (var i = 0; i < pages.length; i++) {
    var r = pages[i].getBoundingClientRect();
    if (center < r.top) break;
    frac = Math.max(0, Math.min(1, (center - r.top) / r.height));
    if (center <= r.bottom) break;
  }
  var bv = body.getBoundingClientRect(), ar = act.getBoundingClientRect();
  var offset = (ar.top + ar.height / 2) - (bv.top + bv.height / 2);
  log('B2-center activeThumb=' + num + ' page=' + q('.page-input').value
      + ' 页内比例=' + frac.toFixed(3)
      + ' pitch=' + Math.round(pitch)
      + ' offCenterPx=' + Math.round(offset)
      + ' 预期≈-' + Math.round(frac * pitch)
      + ' 差=' + Math.round(Math.abs(offset + frac * pitch)));
  cooldown();
}

function cooldown() {
  var stage = q('.stage'), body = q('.side-body');
  var before = body.scrollTop;
  // 合成一个滚动手势（我们只监听事件本身，不做默认滚动）
  body.dispatchEvent(new WheelEvent('wheel', { bubbles: true, deltaY: 120 }));
  stage.scrollTop = stage.scrollTop + 1500;
  stage.dispatchEvent(new Event('scroll'));
  setTimeout(function () {
    log('B3-cooldown bodyBefore=' + Math.round(before)
        + ' bodyAfter=' + Math.round(body.scrollTop)
        + ' followed=' + (Math.abs(body.scrollTop - before) > 3));
    resume();
  }, 120);
}

// 冷却期内不动，过了冷却期再滚正文应该恢复跟随
function resume() {
  var stage = q('.stage'), body = q('.side-body');
  var before = body.scrollTop;
  setTimeout(function () {
    stage.scrollTop = stage.scrollTop + 1500;
    stage.dispatchEvent(new Event('scroll'));
    setTimeout(function () {
      log('B4-resume bodyBefore=' + Math.round(before)
          + ' bodyAfter=' + Math.round(body.scrollTop)
          + ' followed=' + (Math.abs(body.scrollTop - before) > 3));
      dump();
    }, 120);
  }, 1800);   // 等过 PAUSE_MS(1600)
}

window.addEventListener('load', function () {
  setTimeout(function () { q('.file-card').click(); }, 300);
  setTimeout(run, 1800);
});
"""


def build(driver: str, name: str) -> pathlib.Path:
    stub = """
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
    // 画成**深色**的假正文：徽标正好压在一行字上，这是 multiply 最惨的情况
    g.fillStyle = '#333333';
    for (var i = 0; i < 24; i++) g.fillRect(40 * scale, (40 + i * 30) * scale, (400 + (i % 3) * 50) * scale, 11 * scale);
    return JSON.stringify({ ok: true, width: w, height: h });
  },
};
//DRIVER//
</script>
"""

    # 注意顺序：driver 自己也带 %COLORS% 占位符，必须先插进来再统一替换。
    stub = (stub
            .replace("//DRIVER//", driver)
            .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
            .replace("%ANNOTS%", json.dumps(ANNOTS, ensure_ascii=False))
            .replace("%STATE%", json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False)))
            .replace("%PW%", repr(PAGE_W))
            .replace("%PH%", repr(PAGE_H))
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


def chrome(directory: pathlib.Path, extra, budget, query=""):
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--force-device-scale-factor=1", "--window-size=1280,900",
           f"--virtual-time-budget={budget}", *extra,
           f"http://127.0.0.1:{PORT}/index.html{query}"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    return res.stdout


class Server:
    """同一进程里起/停测试服务；每次都要 server_close，否则端口会 TIME_WAIT 住。"""

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


def sample_badges(img, meta, scale):
    """返回 {颜色: (主色, 最暗像素, 不同色数, blend)}。"""
    out = {}
    for b in meta["badges"]:
        color = next((c for c in COLORS if f"folia-hl-{c}" in b["cls"]), None)
        if color is None:
            continue
        x0 = int(round((b["x"] + 3.5) * scale))
        y0 = int(round((b["y"] + 3.5) * scale))
        x1 = int(round((b["x"] + b["w"] - 3.5) * scale))
        y1 = int(round((b["y"] + b["h"] - 3.5) * scale))
        pixels = [img.getpixel((x, y)) for x in range(x0, x1 + 1) for y in range(y0, y1 + 1)]
        if not pixels:
            continue
        counts = {}
        for p in pixels:
            counts[p] = counts.get(p, 0) + 1
        modal = max(counts.items(), key=lambda kv: kv[1])[0]
        # 亮度最低的那个像素：mix-blend-mode: multiply 会把一部分压暗
        darkest = min(pixels, key=lambda p: sum(p))
        out[color] = (modal, darkest, len(counts), b["blend"], (x0, y0, x1, y1))
    return out


# ================================================================ A：徽标颜色
from PIL import Image

shot_dir = build(DRIVER_SHOT, "app-shot")
png = ROOT / "shot.png"
png_bad = ROOT / "shot_multiply.png"
with Server(shot_dir) as _:
    # `--dump-dom` 和 `--screenshot` 在 --headless=new 下不能一起用，
    # 所以跑多遍：布局是确定的，各遍的 getBoundingClientRect 必然一致。
    dom = chrome(shot_dir, ["--dump-dom"], 6000)
    chrome(shot_dir, [f"--screenshot={png}"], 6000)
    chrome(shot_dir, [f"--screenshot={png_bad}"], 6000, query="?multiply")

m = re.search(r'<pre id="shot">(.*?)</pre>', dom, re.S)
if not m:
    print("!! 拿不到 #shot，Chrome 输出尾部：")
    print(dom[-1500:])
    sys.exit(1)
meta = json.loads(m.group(1))

img = Image.open(png).convert("RGB")
img_bad = Image.open(png_bad).convert("RGB")
scale = img.width / meta["iw"]
print(f"[A] 截图 {img.width}x{img.height}  innerWidth={meta['iw']}  scale={scale:.3f}  徽标数={len(meta['badges'])}")

now = sample_badges(img, meta, scale)
old = sample_badges(img_bad, meta, scale)

ok = True
print("[A] 修好后（徽标不再挂 .folia-hl）：")
for c in COLORS:
    if c not in now:
        print(f"[A]   {c:6s} ✗ 没采到")
        ok = False
        continue
    modal, darkest, distinct, blend, _ = now[c]
    expected = EXPECTED[c]
    dist = max(abs(modal[i] - expected[i]) for i in range(3))
    # 「一个圆点里最深和最浅差多少」—— multiply 会让一半明显变暗
    spread = max(abs(darkest[i] - modal[i]) for i in range(3))
    good = dist <= 6 and blend == "normal"
    ok = ok and good
    print(f"[A]   {c:6s} 主色={str(modal):18s} 期望={expected} 偏差={dist} "
          f"最深像素={str(darkest):18s} 明暗差={spread:3d} blend={blend} {'✓' if good else '✗'}")

print("[A] 对照：人为把旧 bug 装回去（徽标 mix-blend-mode: multiply）：")
for c in COLORS:
    if c not in old:
        continue
    modal, darkest, distinct, blend, _ = old[c]
    spread = max(abs(darkest[i] - modal[i]) for i in range(3))
    print(f"[A]   {c:6s} 主色={str(modal):18s} 最深像素={str(darkest):18s} 明暗差={spread:3d} blend={blend}")
print(f"[A] 徽标颜色：{'全部通过' if ok else '有失败'}")

def pixel_map(img, meta, scale, color, label):
    """把徽标中心的像素网格打成 hex，直接看出「一个圆点里有没有两种颜色」。"""
    b = next((x for x in meta["badges"] if f"folia-hl-{color}" in x["cls"]), None)
    if not b:
        return
    cx = int(round((b["x"] + b["w"] / 2) * scale))
    cy = int(round((b["y"] + b["h"] / 2) * scale))
    print(f"[A] {color} 徽标像素图（{label}），中心 ±6（每格 RGB hex）：")
    for dy in range(-6, 7):
        print("      " + " ".join(
            "%02x%02x%02x" % img.getpixel((cx + dx, cy + dy)) for dx in range(-6, 7)))


pixel_map(img, meta, scale, "yellow", "修好后")
pixel_map(img_bad, meta, scale, "yellow", "旧 multiply")

# 放大裁剪出来直接看（四个徽标各 44x44，8 倍放大后横向拼起来）
crops = []
for c in COLORS:
    if c not in now:
        continue
    x0, y0, x1, y1 = now[c][4]
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    box = (cx - 22, cy - 22, cx + 22, cy + 22)
    crops.append((img.crop(box), img_bad.crop(box)))
if crops:
    w, h = 44, 44
    sheet = Image.new("RGB", (w * len(crops) * 2 + 8 * len(crops), h * 2 + 6), (255, 255, 255))
    for i, (a, b) in enumerate(crops):
        sheet.paste(a, (i * (w * 2 + 8), 0))
        sheet.paste(b, (i * (w * 2 + 8) + w, 0))
    sheet = sheet.resize((sheet.width * 6, sheet.height * 6), Image.NEAREST)
    sheet.save(ROOT / "badges.png")
    print(f"[A] 放大对照图 -> {ROOT / 'badges.png'}（每对：左=修好后，右=旧 multiply）")

# ================================================================ B：平滑跟随
behave_dir = build(DRIVER_BEHAVE, "app-behave")
with Server(behave_dir) as _:
    dom = chrome(behave_dir, ["--dump-dom"], 20000)
m = re.search(r'<pre id="e2e">(.*?)</pre>', dom, re.S)
if not m:
    print("!! 拿不到 #e2e，Chrome 输出尾部：")
    print(dom[-1500:])
    sys.exit(1)
print()
print(m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&"))

# 平滑那一半：查产物 CSS 里 `.side-body` 确实声明了 scroll-behavior: smooth
css = next(SRC.glob("styles-*.css")).read_text(encoding="utf-8")
rule = re.search(r'\.side-body\s*\{([^}]*)\}', css, re.S)
declared = "scroll-behavior: smooth" in (rule.group(1) if rule else "")
print(f"\n[B] 产物 CSS 里 .side-body 有 scroll-behavior: smooth —— {'是' if declared else '否 ✗'}")
