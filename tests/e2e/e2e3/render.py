#!/usr/bin/env python3
"""用用户真实的 state.json 渲染首页，对比 pages=0 与 pages=286 两种数据。"""
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


def render(outdir, state, tag):
    out = pathlib.Path(outdir)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)

    stub = (
        "<script>\n"
        "window.__TAURI__={core:{invoke:async function(cmd){"
        "if(cmd==='load_app_state')return " + json.dumps(json.dumps(state, ensure_ascii=False)) + ";"
        "if(cmd==='load_annotations')return '[]';"
        "if(cmd==='save_annotations')return null;"
        "if(cmd==='save_app_state')return null;"
        "return null;}}};\n"
        "window.FoliaBridge={openDocumentDialog:async function(){return '{}';},"
        "openDocumentPath:async function(){return '{}';},closeDocument:function(){},"
        "documentInfo:function(){return 'null';},getPageSizes:async function(){return '[]';},"
        "renderPage:async function(){return '{}';},renderTextLayer:async function(){},"
        "getOutline:async function(){return '[]';},search:async function(){return '[]';},"
        "getSelectionInfo:function(){return 'null';},clearSelection:function(){},"
        "onFileDrop:function(){}};\n"
        "</script>"
    )
    html = html.replace("</head>", stub + "\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    print(f"  [{tag}] -> {out}")


print("真实数据（pages=0，即你现在的状态）:")
render(str(_HERE) + "/real", REAL, "real")

fixed = json.loads(json.dumps(REAL, ensure_ascii=False))
for f in fixed.get("recentFiles", []):
    f["pages"] = 286
    f["last_page"] = 137
print("修正数据（重开一次 PDF 之后的样子）:")
render(str(_HERE) + "/fixed", fixed, "fixed")
