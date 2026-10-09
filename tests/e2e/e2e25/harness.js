// 验证「拖入 / 直接打开」的 PDF 有没有进历史记录（/tmp/e2e25）。
//
// 用户回报：把 PDF 拖进窗口、或者双击 PDF 直接打开，这两条路打开的文档
// **没有出现在首页「最近」里**。这个套件专门钉这两条路。
//
// 场景用 `?case=` 切换（见 verify.py）：
//   drop      —— 停在首页时，拖一个 PDF 进窗口
//   drop2     —— 已经开着一份文档时，再拖一个 PDF 进来
//   openpath  —— 热启动：`folia://open-path` 事件（双击 / Dock 图标 / 打开方式）
//   pending   —— 冷启动：后端已经把路径存好了，前端启动时取
//   reopen    —— 把**已经开着**的那份再打开一次（命中 `apply_document` 的
//                「已经在这个标签里了」分支），顺带查切标签回不回得来阅读位置
//
// 只桩 Tauri IPC 和 `bridge.onFileDrop`，其余（含真实 wasm）全走真实现。

const PAGES = 12;
const PAGE_W = 595;
const PAGE_H = 842;

const CASE = new URLSearchParams(location.search).get("case") || "drop";

// 把 Rust 侧 `console.log` / `console.error` 收下来 —— `--dump-dom` 看不到控制台，
// 出问题时这些日志是唯一的线索。这个模块在 `<head>` 里，排在 `<body>` 末尾那个
// 启动 wasm 的模块之前，所以能赶上。结果挂在 `result.consoleLogs`。
window.__logs = [];
{
  const orig = console.log;
  console.log = (...args) => {
    try {
      window.__logs.push(args.map((a) => String(a)).join(" "));
    } catch (_) {}
    return orig.apply(console, args);
  };
}

// 首页上先摆两份历史（这两份是"以前打开过"的）。
const RECENTS = [
  { id: "doc-old1.pdf", path: "/tmp/old1.pdf", name: "old1.pdf" },
  { id: "doc-old2.pdf", path: "/tmp/old2.pdf", name: "old2.pdf" },
];

const DROPPED = "/tmp/dropped-in.pdf";
const PENDING_PATH = "/tmp/from-dock.pdf";

window.__invokes = [];
window.__saved = [];
window.__listeners = {};
window.__dropCb = null;

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: RECENTS.map((r) => ({
              ...r,
              size: 24000,
              pages: PAGES,
              last_page: 1,
              opened_at: 0,
              favorite: false,
            })),
            lastPath: RECENTS[0].path,
            lastPage: 1,
            scale: 1,
          })
        );
      }
      if (cmd === "save_app_state") {
        try {
          window.__saved.push(JSON.parse((args && args.data) || "{}"));
        } catch (_) {
          window.__saved.push({ parseError: true });
        }
        return Promise.resolve(null);
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "take_pending_open") {
        return Promise.resolve(CASE === "pending" ? PENDING_PATH : null);
      }
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      if (cmd === "set_default_pdf_handler") return Promise.resolve(null);
      return Promise.resolve(null);
    },
  },
  // 把监听器存下来，稍后手动触发 —— 这就是"用户拖文件进来"的那一下。
  event: {
    listen: (name, cb) => {
      window.__listeners[name] = cb;
      return Promise.resolve(() => {});
    },
  },
};

function makeThumb(maxWidth) {
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(maxWidth);
  canvas.height = Math.round(maxWidth / (PAGE_W / PAGE_H));
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg");
}

const bridge = window.FoliaBridge;
if (!bridge) {
  throw new Error("bridge.js 没有先执行：桩的插入位置不对");
}

// ⚠️ 不能整个替换 `onFileDrop`：bridge.js 里那个函数除了回调之外，
// 还会自己 `listen("tauri://drag-drop")` 和 `listen("folia://open-path")`。
// 覆盖掉它，应用就再也收不到「直接打开」那条路了（第一次跑就是这么栽的）。
// 包一层：既拿到回调，也让原实现照常注册监听。
const realOnFileDrop = bridge.onFileDrop;
bridge.onFileDrop = (cb) => {
  window.__dropCb = cb;
  realOnFileDrop(cb);
};

Object.assign(bridge, {
  openDocumentPath: (path) => {
    const p = String(path || "/tmp/unknown.pdf");
    const name = p.split("/").pop() || "unknown.pdf";
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
      JSON.stringify(Array.from({ length: PAGES }, () => ({ width: PAGE_W, height: PAGE_H })))
    ),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: (_path, _page, maxWidth) => Promise.resolve(makeThumb(maxWidth)),
  renderPage: (canvasId, _page, scale) => {
    const canvas = document.getElementById(canvasId);
    if (canvas) {
      canvas.width = Math.round(PAGE_W * scale);
      canvas.height = Math.round(PAGE_H * scale);
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
    }
    return Promise.resolve("");
  },
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

const settle = async (ticks = 12) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

function press(el) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 })
    );
  }
}

const tabs = () => [...document.querySelectorAll(".tab")];
const tabNames = () => tabs().map((t) => t.querySelector(".tab-label").textContent.trim());
const cards = () => [...document.querySelectorAll(".file-card")];
const cardNames = () =>
  cards().map((c) => c.querySelector(".file-title")?.textContent.trim());

const lastSavedNames = () => {
  const s = window.__saved[window.__saved.length - 1];
  return s && s.recentFiles ? s.recentFiles.map((f) => f.name) : null;
};
const anySavedNames = () =>
  window.__saved
    .filter((s) => s && s.recentFiles)
    .map((s) => s.recentFiles.map((f) => f.name));

async function goHome() {
  const home = document.querySelector(".tabbar-home");
  if (home) {
    press(home);
    await waitFor(() => document.querySelector(".library"), "回到书架");
  }
  await settle(20);
}

async function main() {
  try {
    // ================================================================ pending（冷启动）
    if (CASE === "pending") {
      await waitFor(() => document.querySelector(".stage"), "阅读器出现", 6000);
      await settle(30);
      result.tabNames = tabNames();
      result.openedPending = (tabNames()[0] || "").includes("from-dock");
      result.savedNames = lastSavedNames();
      result.allSaved = anySavedNames();
      result.cardNamesAfterHome = null;
      await goHome();
      result.cardNamesAfterHome = cardNames();
      finish();
      return;
    }

    // ================================================================ 等首页出来
    await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    await settle(20);
    result.cardNamesBefore = cardNames();
    result.dropCbRegistered = typeof window.__dropCb === "function";
    result.listeners = Object.keys(window.__listeners);

    if (CASE === "drop2") {
      // 先开一份（点卡片），再拖一份进来
      press(cards()[0]);
      await waitFor(() => document.querySelector(".stage"), "阅读器出现");
      await settle(24);
      result.tabNamesBeforeDrop = tabNames();
    }

    // ================================================================ reopen
    //
    // 把一份**已经开着**的 PDF 再打开一次。`open_path_flow` 没有去重，会直接命中
    // `apply_document` 里「已经在这个标签里了」那条分支。
    //
    // 那条分支以前**不**先把当前标签的实时状态写回 `tabs`（实时句柄才是唯一真相，
    // `tabs[idx]` 只是"上次切走时"的快照）。后果有两个：
    //   · 阅读位置当场退回切走之前（页码从 7 变回 1）—— 本套件量这个；
    //   · 更糟的是批注：旧快照的 `annotations_ok` 还是真的，存盘 effect 随即把
    //     旧快照写进那份文档的文件 → **用户刚加的批注从磁盘上静默消失**。
    //     本套件的 `renderTextLayer` 是空实现，造不出批注，所以用页码钉同一个
    //     「少了一次 sync」——两处走的是同一行代码。
    //
    // ⚠️ 修的时候踩过一次：光加 `sync_tab_from_live` 不够。`set()` 是**排队**，
    // 同一帧里句柄还是 `set` 之前那份，所以"sync 完再 `(*handles.tabs).clone()`
    // 取一份来改"会拿到**没有这次写回的**列表，把刚存进去的页码又冲掉。
    // 必须用 `sync_tab_from_live` 的**返回值**。下面那两条判据就是钉这个的
    // （对照那条之所以会跟着红，是因为被冲掉的正是它后面要用的那份快照）。
    if (CASE === "reopen") {
      const c0 = cards()[0];
      const name0 = c0.querySelector(".file-title").textContent.trim();
      press(c0);
      await waitFor(() => document.querySelector(".stage"), "阅读器出现");
      await settle(24);
      result.tabNamesAfterOpen = tabNames();
      result.openedName = name0;

      // ---- 读「当前页码」必须读**状态派生**的东西，不能读 `.page-input`。
      //
      // `.page-input` 上挂的是 `value={current_page}`，但脚本往里打过字之后这个
      // input 就"脏"了；Yew 的 diff **只在虚拟 DOM 里的字符串变了**时才写回 DOM。
      // 于是"状态 1 → 1"这一帧压根不会碰它，输入框会一直留着脚本敲进去的 "7"
      // —— 判据变成**永远通过**的假绿。
      //
      // 侧栏缩略图上的 `.thumb-item.active` 是 `page == current_page` 直接算出来的，
      // 每帧重渲染，不会脏。用它当判据；`.page-input` 只作为对照记录下来。
      const statePage = () => {
        const el = document.querySelector(".thumb-item.active .thumb-num");
        return el ? el.textContent.trim() : null;
      };
      const inputPage = () => document.querySelector(".page-input")?.value ?? null;

      // 滚动容器的几何 —— 页码一旦被重置，scrollTop 会跟着掉到 0，是个好旁证。
      const stageInfo = () => {
        const el = document.querySelector(".stage");
        if (!el) return null;
        return {
          scrollTop: Math.round(el.scrollTop),
          clientHeight: el.clientHeight,
          scrollHeight: el.scrollHeight,
        };
      };

      // 跳页也走状态：点侧栏第 n 页的缩略图 → `on_jump(n)` → `current_page.set(n)`。
      // 不走"往输入框打字"那条路 —— 理由同上。
      const setPageViaThumb = async (n) => {
        const btn = [...document.querySelectorAll(".thumb-item")].find(
          (b) => (b.querySelector(".thumb-num")?.textContent || "").trim() === String(n)
        );
        if (!btn) throw new Error(`侧栏里找不到第 ${n} 页的缩略图按钮`);
        press(btn);
        await settle(24);
      };

      // 判据能不能成立，先看这一层：侧栏得真的画出了 12 张缩略图。
      result.thumbCount = document.querySelectorAll(".thumb-item").length;
      await setPageViaThumb(7);
      result.pageBeforeReopen = statePage();
      result.pageBeforeReopenInput = inputPage();
      result.stageAfterJump = stageInfo();

      // 把**同一份**再拖进来 → 命中「已经开着」那条分支
      const cb = window.__listeners["tauri://drag-drop"];
      if (!cb) throw new Error("tauri://drag-drop 监听器没注册上");
      cb({ payload: { paths: [`/tmp/${name0}`] } });
      await settle(40);
      result.tabNamesAfterReopen = tabNames();
      result.pageAfterReopen = statePage();
      result.pageAfterReopenInput = inputPage();
      result.stageAfterReopen = stageInfo();

      // 再开**另一份**，然后点回第一份的标签 —— 走切标签那条 sync。
      // 这条路和上面那条命中的是**同一份 `tabs` 快照**，所以它同样要保住位置：
      // 上面那次如果被冲掉，这里也会跟着红（当年就是这么连带发现的）。
      const cb2 = window.__listeners["folia://open-path"];
      if (!cb2) throw new Error("folia://open-path 监听器没注册上");
      cb2({ payload: "/tmp/old2.pdf" });
      await waitFor(() => tabNames().length === 2, "第二个标签出现");
      await settle(24);
      result.tabNamesTwoDocs = tabNames();
      result.pageAfterOpenSecond = statePage();
      result.stageAfterOpenSecond = stageInfo();
      result.activeTabAfterOpenSecond =
        document.querySelector(".tab.active .tab-label")?.textContent.trim() ?? null;
      const first = tabs().find((t) =>
        t.querySelector(".tab-label").textContent.includes(name0)
      );
      if (!first) throw new Error(`标签栏里找不到 ${name0}`);
      press(first.querySelector(".tab-label"));
      await settle(32);
      result.activeTabAfterRoundTrip =
        document.querySelector(".tab.active .tab-label")?.textContent.trim() ?? null;
      result.pageAfterTabRoundTrip = statePage();
      result.pageAfterTabRoundTripInput = inputPage();
      result.stageAfterRoundTrip = stageInfo();
      result.consoleLogs = window.__logs;

      finish();
      return;
    }

    // ================================================================ 触发「拖入」
    if (CASE === "drop" || CASE === "drop2") {
      // 走 bridge.js 真正注册的那个 `tauri://drag-drop` 监听器，
      // 而不是直接调回调 —— 顺带把它的「只挑 .pdf」那条判断也覆盖上。
      const cb = window.__listeners["tauri://drag-drop"];
      if (!cb) throw new Error("tauri://drag-drop 监听器没注册上");
      cb({ payload: { paths: ["/tmp/notes.txt", DROPPED] } });
    } else if (CASE === "openpath") {
      const cb = window.__listeners["folia://open-path"];
      if (!cb) throw new Error("folia://open-path 监听器没注册上");
      cb({ payload: PENDING_PATH });
    }

    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await settle(30);
    result.tabNamesAfter = tabNames();

    await goHome();
    result.cardNamesAfter = cardNames();
    result.savedNames = lastSavedNames();
    result.allSaved = anySavedNames();
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }
  finish();
}

function finish() {
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
  window.__HARNESS_DONE__ = true;
}

main();
