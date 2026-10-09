#!/usr/bin/env python3
"""生成「选中文字底色」的三联对照页（用 Chrome 渲染，中文字体才正常）。

左：用户自己的截图（修复前，真实文档上的淡紫选区）
中：修复后 —— 选中「蓝」
右：修复后 —— 选中「黄」
"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import base64, functools, http.server, json, pathlib, re, shutil, socketserver, subprocess, threading

ROOT = pathlib.Path(str(_HERE))
SRC = pathlib.Path(str(_REPO) + "/dist")
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
USER_SHOT = pathlib.Path("/Users/anemone/.workbuddy-ai/clipboard-images/"
                         "clipboard-2026-10-05T03-54-47-770Z-c780cf5d.png")
from PIL import Image

# ---------------------------------------------------------------- 1) 用户截图里那抹淡紫
u = Image.open(USER_SHOT).convert("RGB")
xs, ys = [], []
for y in range(u.height):
    for x in range(u.width):
        r, g, b = u.getpixel((x, y))
        if b - r >= 14 and b > 230 and abs(r - g) <= 10:   # 那抹淡紫
            xs.append(x); ys.append(y)
print("用户截图尺寸", u.size, "淡紫 bbox", (min(xs), min(ys), max(xs), max(ys)))
pad = 30
panel_user = u.crop((max(0, min(xs) - pad), max(0, min(ys) - 46),
                     min(u.width, max(xs) + pad), min(u.height, max(ys) + 34)))

# ---------------------------------------------------------------- 2) 修复后的渲染
# 复用验证脚本的驱动，加一个 `opaque` 开关让文字显形（只为好看，不影响已完成的像素验证）
src = (ROOT / "verify_selection.py").read_text(encoding="utf-8")
driver = re.search(r'DRIVER_SHOT = """(.*?)"""', src, re.S).group(1)
# ⚠️ 从**文件文本**里抠出来的 driver 带着 Python 源码的转义：`\\w` 会变成
# JS 里的「字面反斜杠 + w」，正则匹配不上 `?color=blue`，于是每次都退回黄色。
driver = driver.replace("\\\\w", "\\w")
driver = driver.replace(
    "var r = spans[1].getBoundingClientRect();",
    "if (location.search.indexOf('opaque') >= 0) {\n"
    "      for (var i = 0; i < spans.length; i++) spans[i].style.color = '#1c1c20';\n"
    "    }\n"
    "    var r = spans[1].getBoundingClientRect();")
assert "opaque" in driver

STUB = re.search(r'STUB = """(.*?)"""', src, re.S).group(1)
COLORS = ["yellow", "green", "blue", "pink"]
DOCS = {"/tmp/alpha.pdf": {"id": "doc-alpha", "name": "Alpha.pdf",
                           "numPages": 60, "size": 1048576}}
RECENT = [{"id": "doc-alpha", "path": "/tmp/alpha.pdf", "name": "Alpha.pdf",
           "size": 1048576, "pages": 60, "last_page": 1,
           "opened_at": 2000.0, "favorite": False}]
ANNOTS = [{"id": f"hl-{c}", "page": 1, "color": c, "text": c, "note": "",
           "rects": [{"x": 0.10, "y": 0.72 + i * 0.04, "w": 0.4, "h": 0.03}],
           "createdAt": 1.0} for i, c in enumerate(COLORS)]

stub = (STUB.replace("//DRIVER//", driver)
        .replace("%DOCS%", json.dumps(DOCS, ensure_ascii=False))
        .replace("%ANNOTS%", json.dumps(ANNOTS, ensure_ascii=False))
        .replace("%STATE%", json.dumps(json.dumps({"recentFiles": RECENT}, ensure_ascii=False)))
        .replace("%PW%", "612.0").replace("%PH%", "792.0")
        .replace("%COLORS%", json.dumps(COLORS)))

dest = ROOT / "app-pres"
if dest.exists():
    shutil.rmtree(dest)
shutil.copytree(SRC, dest)
html = (dest / "index.html").read_text(encoding="utf-8")
html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
html = html.replace("</head>", stub + "\n</head>", 1)
(dest / "index.html").write_text(html, encoding="utf-8")


class Server:
    def __init__(self, directory):
        h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
        socketserver.TCPServer.allow_reuse_address = True
        self.s = socketserver.TCPServer(("127.0.0.1", 8751), h)
        threading.Thread(target=self.s.serve_forever, daemon=True).start()

    def __enter__(self): return self

    def __exit__(self, *a):
        self.s.shutdown(); self.s.server_close()


def chrome(extra, budget, query=""):
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
           "--force-device-scale-factor=1", "--window-size=1280,900",
           f"--virtual-time-budget={budget}", *extra,
           f"http://127.0.0.1:8751/index.html{query}"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=240).stdout


def crop_sel(png):
    im = Image.open(png).convert("RGB")
    xs, ys = [], []
    for y in range(150, 520):
        for x in range(240, 1000):
            p = im.getpixel((x, y))
            if max(p) - min(p) > 6:
                xs.append(x); ys.append(y)
    b = (min(xs) - 18, min(ys) - 12, max(xs) + 18, max(ys) + 12)
    return im.crop(b)


panels = {}
with Server(dest):
    for c in ("blue", "yellow"):
        meta = re.search(r'<pre id="shot">(.*?)</pre>',
                         chrome(["--dump-dom"], 12000, query=f"?color={c}&opaque=1"), re.S)
        png = ROOT / f"pres-{c}.png"
        chrome([f"--screenshot={png}"], 12000, query=f"?color={c}&opaque=1")
        panels[c] = crop_sel(png)
        print(c, panels[c].size)


def b64(img):
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<style>
  body {{ margin:0; padding:22px 24px 26px; background:#fff;
         font:14px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif; color:#1c1c20; }}
  h1 {{ margin:0 0 4px; font-size:18px; font-weight:600; }}
  p.sub {{ margin:0 0 18px; font-size:13px; color:#55565f; }}
  .row {{ display:flex; gap:18px; align-items:flex-start; }}
  .card {{ flex:1 1 0; min-width:0; border:1px solid #ececf1; border-radius:12px;
           overflow:hidden; background:#fff; box-shadow:0 2px 8px rgba(22,22,40,.06); }}
  .cap {{ padding:8px 11px; font-size:12.5px; border-bottom:1px solid #ececf1; }}
  .bad {{ color:#c0392b; }} .good {{ color:#1e8449; }}
  .card img {{ display:block; width:100%; height:auto; }}
</style></head><body>
<h1>选中文字的底色</h1>
<p class="sub">同一处「选中待批注的文字」，修复前后对比。以前无论选哪个颜色，底色都是品牌紫。</p>
<div class="row">
  <div class="card"><div class="cap bad">修复前 · 你的截图：写死品牌紫</div>
    <img src="data:image/png;base64,{b64(panel_user)}"></div>
  <div class="card"><div class="cap good">修复后 · 选中「蓝」</div>
    <img src="data:image/png;base64,{b64(panels['blue'])}"></div>
  <div class="card"><div class="cap good">修复后 · 选中「黄」</div>
    <img src="data:image/png;base64,{b64(panels['yellow'])}"></div>
</div>
</body></html>"""

(ROOT / "compare.html").write_text(page, encoding="utf-8")

# 用 Chrome 渲染这页，拿到带正常中文字体的成图
shot = ROOT / "compare_sel.png"
subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                "--force-device-scale-factor=2", "--window-size=1000,560",
                "--virtual-time-budget=3000", f"--screenshot={shot}",
                f"file://{ROOT/'compare.html'}"], capture_output=True, text=True, timeout=120)
print("saved", shot, Image.open(shot).size)
