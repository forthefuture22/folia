#!/usr/bin/env python3
"""给「页面总览」浮层截一张图（/tmp/e2e19）。

复用 verify.py 的建站/起服务逻辑，只是把 URL 换成 `?shot=overview`，
让 harness 停在"浮层开着、30 张缩略图都填好"的那一刻。
"""

# --- 路径推导 ---------------------------------------------------------
# 原先这里硬编码了 /tmp/e2eNN 和仓库的绝对路径。搬进仓库后改成从本文件
# 位置推导，这样仓库放哪儿都能跑。
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import pathlib
import subprocess
import sys

sys.path.insert(0, str(_HERE))
import verify  # noqa: E402

OUT = pathlib.Path(str(_REPO) + "/.workbuddy-ai/artifacts/page-overview.png")


def main() -> int:
    verify.build_site()
    httpd, port = verify.serve(verify.SITE)
    url = f"http://127.0.0.1:{port}/index.html?shot=overview"
    print(f"shot url: {url}")
    try:
        verify.run_chrome(url, [f"--screenshot={OUT}", "--virtual-time-budget=60000"])
    finally:
        httpd.shutdown()
        httpd.server_close()
    print(f"截图: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
