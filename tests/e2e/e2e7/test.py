#!/usr/bin/env python3
"""干净地重建 worst 用例并注入宽度探针。"""
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

BREAK = ".cover-meta{align-self:flex-start}.name-wrap{align-self:flex-start}"

PROBE = """
<script>
function __probe(){
  var card = document.querySelectorAll('.cover')[1];
  if (!card) return;
  var out = [];
  [['.cover', card],
   ['.cover-btn', card.querySelector('.cover-btn')],
   ['.cover-meta', card.querySelector('.cover-meta')],
   ['.name-wrap', card.querySelector('.name-wrap')],
   ['.cover-name', card.querySelector('.cover-name')],
   ['.cover-tip', card.querySelector('.cover-tip')],
   ['.cover-sub', card.querySelector('.cover-sub')]].forEach(function(pair){
    var el = pair[1];
    if (!el) { out.push(pair[0] + ' = MISSING'); return; }
    var cs = getComputedStyle(el);
    out.push(pair[0] + ' | w=' + el.getBoundingClientRect().width.toFixed(1)
      + ' scrollW=' + el.scrollWidth + ' | display=' + cs.display
      + ' cssW=' + cs.width + ' alignSelf=' + cs.alignSelf
      + ' overflowX=' + cs.overflowX + ' opacity=' + cs.opacity
      + ' whiteSpace=' + cs.whiteSpace + ' textOverflow=' + cs.textOverflow);
  });
  var g = document.querySelector('.shelf-grid');
  if (g) out.push('.shelf-grid | cols=' + getComputedStyle(g).gridTemplateColumns
    + ' w=' + g.getBoundingClientRect().width.toFixed(1));
  var pre = document.getElementById('probe');
  if (!pre) { pre = document.createElement('pre'); pre.id = 'probe'; document.body.appendChild(pre); }
  pre.textContent = out.join('\\n');
}
setInterval(__probe, 150);
</script>
"""


def build(name, extra_css):
    out = pathlib.Path(str(_HERE)) / name
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(SRC, out)
    html = (out / "index.html").read_text(encoding="utf-8")
    html = re.sub(r'\s*<script type="module" src="public/pdfjs/bridge\.js"></script>', "", html)
    inject = STUB + (f"\n<style>{extra_css}</style>" if extra_css else "") + PROBE
    html = html.replace("</head>", inject + "\n</head>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    print("built", out)


build("worst", BREAK)
build("plain", "")
