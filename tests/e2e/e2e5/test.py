#!/usr/bin/env python3
"""验证假设：WKWebView 里 <button> 内的 flex item 没有被 stretch，
导致 .cover-meta 变成 max-content 宽度 -> .cover-name 不裁剪。

在 Chrome 里用 align-self: flex-start 人为取消 stretch 来复现。
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
REAL = json.loads((pathlib.Path.home() / "Library/Application Support/com.anemone.folia/state.json").read_text())
for f in REAL.get("recentFiles", []):
    f["pages"] = 130
    f["last_page"] = 1

STUB = (
    "<script>\n"
    "window.__TAURI__={core:{invoke:async function(cmd){"
    "if(cmd==='load_app_state')return __STATE__;"
    "if(cmd==='load_annotations')return '[]';"
    "return null;}}};\n"
    "window.FoliaBridge={openDocumentDialog:async function(){return '{}';},"
    "openDocumentPath:async function(){return '{}';},closeDocument:function(){},"
    "documentInfo:function(){return 'null';},getPageSizes:async function(){return '[]';},"
    "renderPage:async function(){return '{}';},renderTextLayer:async function(){},"
    "getOutline:async function(){return '[]';},search:async function(){return '[]';},"
    "getSelectionInfo:function(){return 'null';},clearSelection:function(){},"
    "onFileDrop:function(){}};\n"
    "</script>"
).replace("__STATE__", json.dumps(json.dumps(REAL, ensure_ascii=False)))

CASES = {
    # A: 取消 stretch —— 模拟 WebKit 的行为，预期复现溢出
    "A_nostretch": ".cover-meta{align-self:flex-start}",
    # B: 取消 stretch + 显式 width:100% —— 预期修好
    "B_nostretch_width": ".cover-meta{align-self:flex-start;width:100%}",
}

for name, css in CASES.items():
    out = pathlib.Path(str(_HERE)) / name
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    html = html.replace("</head>", STUB + f"\n<style>{css}</style>\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    print("ok", name)
