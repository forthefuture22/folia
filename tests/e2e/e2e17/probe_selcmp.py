# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import json
import sys

sys.path.insert(0, str(_HERE))
import verify as V  # noqa: E402
from PIL import Image  # noqa: E402

V.build_site()
httpd, port = V.serve(V.SITE)
url = f"http://127.0.0.1:{port}/index.html"
try:
    dom = V.run_chrome(url + "?shot=selcmp", ["--dump-dom", "--virtual-time-budget=20000"]).stdout
    res = V.extract_result(dom)
    V.run_chrome(
        url + "?shot=selcmp",
        [f"--screenshot={V.WORK / 'selcmp.png'}", "--virtual-time-budget=20000"],
    )
finally:
    httpd.shutdown()
    httpd.server_close()

sc = res["selcmp"]
im = Image.open(str(_HERE) + "/selcmp.png").convert("RGB")
sel = im.getpixel((sc["sel"]["x"], sc["sel"]["y"]))
mk = im.getpixel((sc["mark"]["x"], sc["mark"]["y"]))
print("selcmp:", json.dumps(sc, ensure_ascii=False))
print("sel px", sel, " mark px", mk, " match:", sel == mk)
print("--- vertical scan through selection ---")
for dy in range(-12, 14, 2):
    print(sc["sel"]["y"] + dy, im.getpixel((sc["sel"]["x"], sc["sel"]["y"] + dy)))
