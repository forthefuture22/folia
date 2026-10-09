#!/usr/bin/env python3
"""给正文标注的右键菜单拍两张图（/tmp/e2e28）。

和 `verify.py` 用同一套桩，只是把界面摆到「右击了一条标注、菜单开着」的状态就停，
交给 `--screenshot` 拍。**不跑判据** —— 判据走 `verify.py`。

产出（同时复制到项目的 `.workbuddy-ai/artifacts/`）：

- `mark-menu-underline.png` —— 右击「下划线」那条 → 「编辑批注 / 删除下划线」
- `mark-menu-strike.png`    —— 右击「删划线」那条 → 「编辑批注 / 删除删划线」

两张故意选**不同样式**，好看出文案确实跟着样式走，不是写死的。

⚠️ 桩会把 `.page-canvas` 藏起来：无头 `--screenshot` 下 canvas 会合成成一片黑
（见 README「别用 headless --screenshot 去证伪计算样式」）。
用 `visibility: hidden`，布局不动，所以标注矩形还在真实位置上。
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
    ("underline", "mark-menu-underline.png"),
    ("strike", "mark-menu-strike.png"),
]


def main() -> int:
    build_site()
    ARTIFACTS.mkdir(parents=True, exist_ok=True)

    httpd, port = serve(SITE)
    base = f"http://127.0.0.1:{port}/index.html"
    try:
        for kind, name in SHOTS:
            out = WORK / name
            if out.exists():
                out.unlink()
            run_chrome(
                f"{base}?case=shot&kind={kind}",
                ["--screenshot=" + str(out), "--virtual-time-budget=90000"],
            )
            if not out.exists():
                print(f"!! {kind} 没截出图")
                return 1
            shutil.copy(out, ARTIFACTS / name)
            print(f"{name}: {out.stat().st_size} bytes -> {ARTIFACTS / name}")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
