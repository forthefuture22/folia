#!/usr/bin/env python3
"""按真机窗口尺寸 (1080x753, DPR=2) 复现封面下的名字溢出。"""
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

# 真机里两份都已是 130 页
for f in REAL.get("recentFiles", []):
    f["pages"] = 130
    f["last_page"] = 1


def render(outdir, extra_css=""):
    out = pathlib.Path(outdir)
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    stub = (
        "<script>\n"
        "window.__TAURI__={core:{invoke:async function(cmd){"
        "if(cmd==='load_app_state')return " + json.dumps(json.dumps(REAL, ensure_ascii=False)) + ";"
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
    )
    html = html.replace("</head>", stub + "\n</head>", 1)
    if extra_css:
        html = html.replace("</head>", f"<style>{extra_css}</style>\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")


render(str(_HERE) + "/normal")
render(str(_HERE) + "/hover", """
/* 模拟鼠标停在第二张卡片上 */
.cover:nth-of-type(3) .name-wrap .cover-tip { opacity: 1 !important; transform: translateX(-50%) translateY(0) !important; }
""")
print("ok")
