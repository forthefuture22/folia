#!/usr/bin/env python3
"""给深色模式出几张截图，肉眼复核（/tmp/e2e23/shot.py）。

复用 verify.py 的站点构建（含 matchMedia 桩），跑 4 张：
浅色/深色 × 书架/正文。图落在 /tmp/e2e23/*.png。
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

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import verify  # noqa: E402

WORK = pathlib.Path(str(_HERE))

SHOTS = [
    ("light", "shelf", "shelf-light.png"),
    ("dark", "shelf", "shelf-dark.png"),
    ("light", "reader", "reader-light.png"),
    ("dark", "reader", "reader-dark.png"),
    ("dark", "overview", "overview-dark.png"),
]


def main() -> int:
    verify.build_site()
    httpd, port = verify.serve(verify.SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        for case, shot, out in SHOTS:
            url = f"{base}?case={case}&shot={shot}"
            path = WORK / out
            print(f"[{case}/{shot}] -> {path}")
            verify.run_chrome(
                url,
                [
                    "--screenshot=" + str(path),
                    "--virtual-time-budget=40000",
                ],
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
