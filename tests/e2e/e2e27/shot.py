#!/usr/bin/env python3
"""给书架卡片的右键菜单拍两张图（/tmp/e2e27）。

和 `verify.py` 用同一套桩，只是把界面摆到某个状态就停，交给 `--screenshot` 拍。
产出：

- `shelf-menu.png`    —— 右击卡片弹出的菜单（四项：收藏 / 在访达中显示 / 删除记录 / 删除记录和文件）
- `shelf-confirm.png` —— 「删除记录和文件」的二次确认框

产物同时复制到项目的 `.workbuddy-ai/artifacts/`。
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
import shutil
import sys

sys.path.insert(0, str(_E2E))
sys.path.insert(0, str(_HERE))
from verify import SITE, WORK, build_site, run_chrome, serve  # noqa: E402

ROOT = pathlib.Path(str(_REPO))
ARTIFACTS = ROOT / ".workbuddy-ai" / "artifacts"

SHOTS = [
    ("shot-menu", "shelf-menu.png"),
    ("shot-confirm", "shelf-confirm.png"),
]


def main() -> int:
    build_site()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        for case, name in SHOTS:
            out = WORK / name
            if out.exists():
                out.unlink()
            run_chrome(
                f"{base}?case={case}",
                ["--screenshot=" + str(out), "--virtual-time-budget=90000"],
            )
            if not out.exists():
                print(f"!! {case} 没截出图")
                return 1
            shutil.copy(out, ARTIFACTS / name)
            print(f"{name}: {out.stat().st_size} bytes -> {ARTIFACTS / name}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
