#!/usr/bin/env python3
"""渲染新版书架 / 阅读器，用用户真实的 state.json。
renderThumbnail 用一张假的「页面」SVG 代替，方便看布局。"""
# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import json, pathlib, re, shutil, urllib.parse

SRC = pathlib.Path(str(_REPO) + "/dist")
REAL = json.loads((pathlib.Path.home() / "Library/Application Support/com.anemone.folia/state.json").read_text())
for f in REAL.get("recentFiles", []):
    f.setdefault("favorite", False)

# 假的「页面预览」：白底 + 灰条，模拟真实缩略图
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
<rect x="158" y="178" width="100" height="9" rx="3" fill="#d7dcea"/>
<rect x="26" y="238" width="268" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="256" width="248" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="274" width="262" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="300" width="200" height="9" rx="3" fill="#c9cfe0"/>
<rect x="26" y="330" width="268" height="56" rx="5" fill="#eef1f8"/>
</svg>"""
FAKE_URL = "data:image/svg+xml;utf8," + urllib.parse.quote(FAKE, safe="")


def build(outdir, state, mode):
    out = pathlib.Path(outdir)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)

    stub = (
        "<script>\n"
        "window.__FAKE__ = " + json.dumps(FAKE_URL) + ";\n"
        "window.__TAURI__={core:{invoke:async function(cmd){"
        "if(cmd==='load_app_state')return " + json.dumps(json.dumps(state, ensure_ascii=False)) + ";"
        "if(cmd==='load_annotations')return '[]';"
        "return null;}}};\n"
        "window.FoliaBridge={openDocumentDialog:async function(){return '{}';},"
        "openDocumentPath:async function(){return '{}';},closeDocument:function(){},"
        "documentInfo:function(){return 'null';},getPageSizes:async function(){return '[]';},"
        "renderPage:async function(){return '{}';},renderTextLayer:async function(){},"
        "getOutline:async function(){return '[]';},search:async function(){return '[]';},"
        "getSelectionInfo:function(){return 'null';},clearSelection:function(){},"
        "onFileDrop:function(){},releaseThumbnail:function(){},"
        "renderThumbnail:async function(){return window.__FAKE__;}};\n"
        "</script>"
    )
    html = html.replace("</head>", stub + "\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    print("built", mode, "->", out)


build(str(_HERE) + "/library", REAL, "library")
