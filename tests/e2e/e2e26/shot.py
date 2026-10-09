#!/usr/bin/env python3
"""给首页出截图，肉眼复核"文件名只占一行"（/tmp/e2e26/shot.py）。

复用 verify.py 的站点构建，跑两张：不是默认 / 已经是默认（后者按钮该消失）。
图落在 /tmp/e2e26/*.png。
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
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import verify  # noqa: E402

WORK = pathlib.Path(str(_HERE))

SHOTS = [
    ("home", "shelf.png"),
    ("alreadydefault", "shelf-already-default.png"),
]


def main() -> int:
    verify.build_site()
    httpd, port = verify.serve(verify.SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        for case, out in SHOTS:
            url = f"{base}?case={case}&shot=shelf"
            path = WORK / out
            print(f"[{case}] -> {path}")
            verify.run_chrome(
                url,
                ["--screenshot=" + str(path), "--virtual-time-budget=40000"],
            )
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
