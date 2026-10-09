// e2e33 —— 「打开两份规格相同的 PDF，再按 Esc 回书架，第二个标签会不会消失？」
//
// 背景（`src/app.rs` 的 keydown effect）：
// 那个 document 级监听器是**按依赖重注册**的，依赖里有
// `(meta.is_some(), scale, current_page, layout, overview_open, menu)`。
// 而 `layout` 是 `use_memo((page_sizes, scale))` 出来的值 ——
// **两份页面尺寸、页数都相同的 PDF，layout 一模一样**，于是打开第二份时
// 依赖元组不变，effect **不会重注册**，闭包里那个 `handles` 就停在
// 「只开着第一份」那一帧（`UseStateHandle` 的 deref 是快照，见 Yew 文档
// 的 Caution 段 / MEMORY 约定一）。
//
// 而它按 Esc 时调的是 `go_library(&handles)` —— 里面第一步就是
// `sync_tab_from_live(handles, *handles.active)`，会拿**旧帧的 `handles.tabs`**
// 当基准、改完再 `set` 回去 → 第二个标签被静默吃掉。
//
// `?case=`：
//   esc  —— 打开两份 → 按 Esc 回书架 → 断言两个标签都还在（★ 本套件的主角）
//   home —— 打开两份 → **点标签栏的 home 按钮**回书架 → 断言两个标签都还在
//           （对照：home 走的是每帧新建的回调，`handles` 是新的。它绿而 esc 红，
//            才说明问题出在「闭包读到旧帧」而不是「回书架本身会掉标签」）
//
// 只桩 Tauri IPC 与 bridge 的渲染函数，其余（含真实 wasm）全走真实现。

const PAGES = 12;
const PAGE_W = 595;
const PAGE_H = 842;

const CASE = new URLSearchParams(location.search).get("case") || "esc";

const PATH_A = "/tmp/dup-a.pdf";
const PATH_B = "/tmp/dup-b.pdf";

// 首页上先摆一份历史，保证书架渲染得出来（我们只看标签栏）。
const RECENTS = [{ id: "doc-old1.pdf", path: "/tmp/old1.pdf", name: "old1.pdf" }];

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
      if (cmd === "take_pending_open") return Promise.resolve(null);
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

// ⚠️ 不能整个替换 `onFileDrop` —— 它自己还负责注册
// `tauri://drag-drop` 与 `folia://open-path` 两个监听（见 MEMORY 约定一）。
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
  // 两份文档的页面尺寸**故意完全一样** —— 这正是让 keydown effect 不重注册的条件。
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

function waitFor(predicate, label, maxTicks = 6000) {
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

function press(el) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 })
    );
  }
}

const tabs = () => [...document.querySelectorAll(".tab")];
const tabNames = () =>
  tabs().map((t) => {
    const label = t.querySelector(".tab-label");
    return label ? label.textContent.trim() : "";
  });

const openPath = (p) => {
  const listener = window.__listeners["folia://open-path"];
  if (typeof listener !== "function") throw new Error("folia://open-path 监听器没注册");
  listener({ payload: p });
};

async function main() {
  try {
    await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    await settle();

    // ---- 打开第一份
    openPath(PATH_A);
    await waitFor(() => tabs().length === 1, "第一个标签出现");
    await settle();

    // ---- 打开第二份（规格与第一份完全相同 → keydown effect 不重注册）
    openPath(PATH_B);
    await waitFor(() => tabs().length === 2, "第二个标签出现");
    await settle();

    result.tabsBefore = tabNames();
    result.scaleBefore = document.querySelector(".page-input") ? true : false;

    if (CASE === "esc") {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
      );
    } else {
      const home = document.querySelector(".tabbar-home");
      if (!home) throw new Error("找不到 .tabbar-home");
      press(home);
    }
    await settle(30);

    result.backInLibrary = !!document.querySelector(".library") && !document.querySelector(".stage");
    result.tabsAfter = tabNames();
    result.savedNames = (() => {
      const s = window.__saved[window.__saved.length - 1];
      return s && s.recentFiles ? s.recentFiles.map((f) => f.name) : null;
    })();
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }

  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
  window.__HARNESS_DONE__ = true;
}

main();
