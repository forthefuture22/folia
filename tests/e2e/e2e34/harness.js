// 复现：**读不出 state.json 的时候，磁盘上那份会不会被一个空列表盖掉？**
//
// 两条链，同一个根因（"读失败"被当成"没有数据"）：
//
// ① 后端 `load_app_state`（src-tauri/src/commands.rs）：
//
//        match fs::read_to_string(&file) {
//            Ok(content) => Ok(content),
//            Err(_) => Ok("{}".into()),      // ← 所有错误都当成"没有状态"
//        }
//
//    只有 `NotFound` 才该是"这份还没存过"；权限 / I/O 错误一律得是 `Err`。
//    （`load_annotations` 早就改成这样了，注释里还写着"早期版本是 `Err(_) => Ok(\"[]\")`
//      —— 把'读不出来'和'没有批注'混成同一个结果"。这条没跟上。）
//
// ② 前端（src/app.rs 启动 effect）：
//
//        if let Ok(raw) = pdf::load_app_state().await {
//            if let Ok(state) = serde_json::from_str::<PersistedState>(&raw) { …灌进 recent_store… }
//        }
//
//    `PersistedState` 每个字段都是 `#[serde(default)]`，所以**解析失败也无声无息**：
//    `recent_store` 保持空。而它没有 `annotations_ok` 那样的"这份数据可不可信"标志 ——
//    于是紧接着的 `persist_state` / 存盘 effect 会拿**空列表**去覆盖磁盘。
//
// 触发 ② 的现实路径：`save_app_state` 用的是 `fs::write`（O_TRUNC 后写），
// **不是原子写** —— 写一半断电 / 磁盘写满，磁盘上就留一个截断的 JSON。
// 下次启动解析失败 → 内存里是空的 → 用户随便打开一份文档 → **最近列表、
// 所有收藏、阅读进度一起静默消失**。
//
// 本套件用 `?case=` 跑三趟：
//   ok       对照：state 正常读出来 → 打开文档后落盘**必须**保住原来那几条
//   corrupt  磁盘上是个截断的 JSON（解析失败）
//   readerr  读盘直接报错（权限 / I/O）
//
// 只桩 Tauri IPC 与 bridge，其余（含真实 wasm）全走真实现。

const CASE = new URLSearchParams(location.search).get("case") || "corrupt";

const PAGES = 10;

/** 磁盘上"本来有"的内容：3 条，其中 1 条被用户收藏过。 */
const SEED = [
  { id: "doc-a.pdf", path: "/tmp/a.pdf", name: "a.pdf", size: 1000, pages: 5, last_page: 2, opened_at: 0, favorite: true },
  { id: "doc-b.pdf", path: "/tmp/b.pdf", name: "b.pdf", size: 2000, pages: 7, last_page: 3, opened_at: 0, favorite: false },
  { id: "doc-c.pdf", path: "/tmp/c.pdf", name: "c.pdf", size: 3000, pages: 9, last_page: 4, opened_at: 0, favorite: false },
];

const GOOD_STATE = JSON.stringify({
  recentFiles: SEED,
  lastPath: null,
  lastPage: null,
  scale: null,
});

/** 截断的 JSON —— `fs::write` 写一半就是这个样子。 */
const TRUNCATED = '{"recentFiles":[{"id":"doc-a.pdf","path":"/tmp/a.pdf","name":"a.pd';

window.__saved = [];        // 每次 save_app_state 的载荷
window.__calls = [];        // 所有 IPC 命令名（按序）
window.__listeners = {};

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__calls.push(cmd);
      if (cmd === "load_app_state") {
        if (CASE === "ok") return Promise.resolve(GOOD_STATE);
        if (CASE === "corrupt") return Promise.resolve(TRUNCATED);
        // readerr：模拟权限 / I/O 错误（invoke 走 reject）
        return Promise.reject(new Error("读取状态失败：Permission denied (os error 13)"));
      }
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "save_app_state") {
        window.__saved.push(args && args.data);
        return Promise.resolve(null);
      }
      if (cmd === "quarantine_app_state") {
        window.__calls.push("__quarantine_done__");
        return Promise.resolve("state.json.corrupt-20261006");
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      return Promise.resolve(null);
    },
  },
  event: {
    listen: (name, cb) => {
      window.__listeners[name] = cb;
      return Promise.resolve(() => {});
    },
  },
};

const bridge = window.FoliaBridge;
if (!bridge) {
  throw new Error("bridge.js 没有先执行：桩的插入位置不对");
}

function makeThumb(maxWidth) {
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(maxWidth);
  canvas.height = Math.round(maxWidth / (595 / 842));
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg");
}

const realOnFileDrop = bridge.onFileDrop;
bridge.onFileDrop = (cb) => {
  window.__dropCb = cb;
  realOnFileDrop(cb);
};

Object.assign(bridge, {
  openDocumentPath: (path) => {
    const p = String(path || "");
    const name = p.split("/").pop();
    return Promise.resolve(
      JSON.stringify({
        ok: true,
        cancelled: false,
        error: null,
        meta: { id: `doc-${name}`, path: p, name, size: 24000, numPages: PAGES },
      })
    );
  },
  getPageSizes: () =>
    Promise.resolve(
      JSON.stringify(Array.from({ length: PAGES }, () => ({ width: 595, height: 842 })))
    ),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: (_p, _page, maxWidth) => Promise.resolve(makeThumb(maxWidth)),
  releaseThumbnail: () => undefined,
  renderPage: () => Promise.resolve(""),
  renderTextLayer: () => Promise.resolve(""),
});

// ---------------------------------------------------------------- 驱动

const result = { case: CASE };

function waitFor(predicate, label, maxTicks = 4000) {
  return new Promise((resolve, reject) => {
    let ticks = 0;
    const tick = () => {
      let value = null;
      try {
        value = predicate();
      } catch (_) {
        value = null;
      }
      if (value) return resolve(value);
      if (++ticks > maxTicks) return reject(new Error("等待超时: " + label));
      setTimeout(tick, 10);
    };
    tick();
  });
}

const settle = async (ticks = 20) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const tabNames = () => [...document.querySelectorAll(".tab-label")].map((t) => t.textContent.trim());
const shelfNames = () =>
  [...document.querySelectorAll(".file-card .file-title")].map((e) => e.textContent.trim());

const parse = (raw) => {
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
};

/** 打开文档**之前**落盘的次数 —— 后面只看这之后的那些。 */
let savedBeforeOpen = 0;

async function main() {
  try {
    await waitFor(() => document.querySelector(".app"), "应用挂载");
    await settle(30);

    result.shelfAtStart = shelfNames();
    result.savedBeforeOpen = window.__saved.length;

    const cb = window.__listeners["folia://open-path"];
    if (!cb) throw new Error("folia://open-path 监听器没注册上");

    savedBeforeOpen = window.__saved.length;
    cb({ payload: "/tmp/zzz-new.pdf" });
    await waitFor(() => tabNames().includes("zzz-new.pdf"), "打开 zzz-new.pdf");
    await settle(30);

    result.openedTabs = tabNames();

    // 打开文档**之后**的第一次落盘 —— 就是"拿空列表去覆盖"的那一次。
    const after = window.__saved.slice(savedBeforeOpen);
    result.savedAfterOpenCount = after.length;
    const first = parse(after[0]);
    result.firstSavedAfterOpen = first
      ? {
          count: (first.recentFiles || []).length,
          names: (first.recentFiles || []).map((f) => `${f.name}:${f.favorite}`),
        }
      : null;

    // 有没有把读不出来的那份"留档"（而不是直接盖掉）？
    result.quarantineCalls = window.__calls.filter((c) => c === "__quarantine_done__").length;
    result.stateReadAttempts = window.__calls.filter((c) => c === "load_app_state").length;
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }

  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
}

main();
