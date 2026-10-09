#!/usr/bin/env python3
"""读不出 state.json 时，磁盘上那份会不会被一个空列表盖掉？（e2e34）

两条链，同一个根因 ——「读失败」被当成「没有数据」：

① 后端 `load_app_state` 是 `Err(_) => Ok("{}")`，把**所有**读盘错误都变成
   「这份还没存过」。`load_annotations` 早就改成「只有 NotFound 才算空」了，
   这条没跟上。
② 前端没有 `annotations_ok` 那样的可信标志：`PersistedState` 每个字段都是
   `#[serde(default)]`，所以**解析失败也无声无息**，`recent_store` 保持空；
   紧接着的存盘就把空列表写回磁盘。

现实触发路径：`save_app_state` 用 `fs::write`（O_TRUNC 后写）而不是原子写，
写一半断电 / 磁盘写满就留一个截断的 JSON → 下次启动解析失败 → 用户随手打开
一份文档 → **最近列表、所有收藏、阅读进度一起静默消失**。

跑三趟：
  ok       对照 —— state 正常读出来，落盘**必须**保住原来那几条
  corrupt  磁盘上是个截断的 JSON
  readerr  读盘直接报错

只桩 Tauri IPC 与 bridge，其余（含真实 wasm）全走真实现。
"""

# --- 路径推导 ---------------------------------------------------------
import pathlib as _pl
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                      # 仓库根
# ---------------------------------------------------------------------
import html
import http.server
import json
import pathlib
import re
import shutil
import socketserver
import subprocess
import sys
import threading

sys.path.insert(0, str(_E2E))
from theme_pin import pin_light  # noqa: E402

ROOT = pathlib.Path(str(_REPO))
DIST = ROOT / "dist"
WORK = pathlib.Path(str(_HERE))
SITE = WORK / "site"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

SEED_NAMES = ["a.pdf", "b.pdf", "c.pdf"]
NEW_NAME = "zzz-new.pdf"


def build_site() -> None:
    if SITE.exists():
        shutil.rmtree(SITE, ignore_errors=True)
    SITE.mkdir(parents=True)
    subprocess.run(["cp", "-R", str(DIST) + "/.", str(SITE)], check=True)

    index = (SITE / "index.html").read_text()
    index, n = re.subn(r'<script>"use strict";.*?</script>', "", index, flags=re.S)
    assert n <= 1, f"Trunk 热重载脚本出现了 {n} 次"
    index = pin_light(index)

    anchor = '<script type="module" src="public/pdfjs/bridge.js"></script>'
    assert anchor in index, "没找到 bridge.js 的 script 标签"
    index = index.replace(
        anchor, anchor + '\n    <script type="module" src="/harness-stub.js"></script>'
    )
    (SITE / "index.html").write_text(index)
    subprocess.run(["cp", str(WORK / "harness.js"), str(SITE / "harness-stub.js")], check=True)


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory: pathlib.Path):
    handler = lambda *a, **kw: QuietHandler(*a, directory=str(directory), **kw)  # noqa: E731
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def run_chrome(url: str, extra: list[str], timeout: int = 180) -> subprocess.CompletedProcess:
    cmd = [
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        "--window-size=1200,900", "--force-device-scale-factor=1", *extra, url,
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def extract_result(dom: str) -> dict:
    match = re.search(r'<pre id="harness-result">(.*?)</pre>', dom, flags=re.S)
    if not match:
        raise SystemExit("页面里没有 harness-result\n" + dom[-3000:])
    return json.loads(html.unescape(match.group(1)))


def main() -> int:
    build_site()
    httpd, port = serve(SITE)
    try:
        base = f"http://127.0.0.1:{port}/index.html"
        runs = {}
        for case in ("ok", "corrupt", "readerr"):
            dom = run_chrome(
                f"{base}?case={case}", ["--dump-dom", "--virtual-time-budget=90000"]
            ).stdout
            runs[case] = extract_result(dom)
    finally:
        httpd.shutdown()
        httpd.server_close()

    for case, res in runs.items():
        print(f"\n========== case = {case} ==========")
        print(json.dumps(res, indent=2, ensure_ascii=False))

    checks: list[tuple[str, bool, str]] = []

    def check(name, ok, detail=""):
        checks.append((name, bool(ok), detail))

    for case, res in runs.items():
        if "error" in res:
            print(f"\n!! [{case}] 驱动脚本报错:", res["error"])
            print(res.get("stack", ""))
            return 1

    # ---------------- 对照：ok ----------------
    ok = runs["ok"]
    check(
        "[ok·对照] 前置：state 真的读进去了（书架上 3 条）",
        ok.get("shelfAtStart") == SEED_NAMES,
        f"书架={ok.get('shelfAtStart')}",
    )
    check(
        "[ok·对照] 前置：文档真的打开了",
        NEW_NAME in (ok.get("openedTabs") or []),
        f"标签={ok.get('openedTabs')}",
    )
    check(
        "[ok·对照] 前置：打开之后确实落过盘",
        (ok.get("savedAfterOpenCount") or 0) >= 1,
        f"落盘次数={ok.get('savedAfterOpenCount')}",
    )
    check(
        "[ok·对照] ★ 落盘**保住了**原来那 3 条（含被收藏那条）",
        (ok.get("firstSavedAfterOpen") or {}).get("count") == 4
        and "a.pdf:true" in ((ok.get("firstSavedAfterOpen") or {}).get("names") or []),
        f"落盘={ok.get('firstSavedAfterOpen')}",
    )

    # ---------------- 出问题的两趟 ----------------
    for case in ("corrupt", "readerr"):
        res = runs[case]
        label = {"corrupt": "截断的 JSON", "readerr": "读盘报错"}[case]
        check(
            f"[{case}] 前置：state 确实**没**读进来（书架是空的）",
            res.get("shelfAtStart") == [],
            f"书架={res.get('shelfAtStart')}",
        )
        check(
            f"[{case}] 前置：动作真的发生了（文档打开了）",
            NEW_NAME in (res.get("openedTabs") or []),
            f"标签={res.get('openedTabs')}",
        )
        # ★ 机制无关的判据：要么根本不写，要么写之前先把旧文件留档。
        #   两种都算"没有静默丢弃"；只要出现"直接写了个空的"就是 bug。
        wrote = (res.get("savedAfterOpenCount") or 0) >= 1
        quarantined = (res.get("quarantineCalls") or 0) >= 1
        discarded = wrote and not quarantined
        check(
            f"[{case}] ★ 读不出来的那份 state 没有被**静默丢弃**（{label}）",
            not discarded,
            f"落盘次数={res.get('savedAfterOpenCount')} 留档次数={res.get('quarantineCalls')} "
            f"落盘内容={res.get('firstSavedAfterOpen')}",
        )
        # 反向：光"不写"还不够 —— 留档之后必须**恢复**正常落盘，
        # 否则用户这次的打开记录 / 收藏全存不下去（那是看得见的坏）。
        check(
            f"[{case}] ★ 留档之后恢复正常落盘（不会把应用卡成永不保存）",
            quarantined and wrote,
            f"落盘次数={res.get('savedAfterOpenCount')} 留档次数={res.get('quarantineCalls')}",
        )

    print("\n================ checks ================")
    failed = 0
    for name, ok_, detail in checks:
        if not ok_:
            failed += 1
        print(f"[{'PASS' if ok_ else 'FAIL'}] {name}" + (f"   ({detail})" if detail else ""))
    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
