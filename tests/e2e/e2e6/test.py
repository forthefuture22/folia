#!/usr/bin/env python3
"""最坏情况验证：应用了 width:100% 修复，同时人为取消 .cover-meta / .name-wrap
的 flex stretch（模拟 WKWebView 按钮内的行为）。名字仍应正常省略、不重叠。
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

# 人为破坏 stretch —— 最坏情况
BREAK = ".cover-meta{align-self:flex-start}.name-wrap{align-self:flex-start}"

CASES = {
    "worst": BREAK,                              # 修复 + 破坏 stretch
    "plain": "",                                 # 修复 + 正常
}

for name, css in CASES.items():
    out = pathlib.Path(str(_HERE)) / name
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    inject = STUB + (f"\n<style>{css}</style>" if css else "")
    html = html.replace("</head>", inject + "\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    print("ok", name)
