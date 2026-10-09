// 复现：**从书架打开一份「不是最后关掉的那份」文档，它的阅读进度会不会被重置？**
//
// `apply_document` 决定从第几页打开时，只看**一个格子** —— `handles.restore`：
//
//     let (page, scale, resuming) = match handles.restore.borrow().clone() {
//         Some(target) if target.path == meta.path => (target.page.clamp(..), .., true),
//         _ => (1, *handles.scale, false),        // ← 对不上就回第 1 页
//     };
//     ...
//     entry.last_page = page;                      // ← 再把这个 1 写回最近列表
//
// 而 `restore` 只在两处写：**启动时**（来自 state.json 的 lastPath/lastPage）
// 和 **close_tab 关掉正在看的那份时**。也就是说它**只有一个槽位**。
// 于是「上次关掉的不是这份」时，这份文档就被当成"从没读过"：
// 既从第 1 页打开，又把它记录在案的进度**覆盖成 1**。
//
// 本套件钉的就是这件事。只桩 Tauri IPC 与 bridge，其余（含真实 wasm）全走真实现。

const PAGES = 100;
// ⚠️ `id` 要和下面 `openDocumentPath` 桩返回的**完全一致**。
// 真实后端里 id 是 `doc_id_for(path)`（绝对路径哈希），同一条路径永远是同一个 id；
// 桩里如果写别的（比如 "doc-a.pdf"），`write_recent` 那句 `retain(|i| i.id != meta.id)`
// 就删不掉旧条目 → 落盘里出现**两条同路径记录**，那是桩的问题，不是应用的。
const A = { id: "doc-prog-a.pdf", path: "/tmp/prog-a.pdf", name: "prog-a.pdf", last_page: 30 };
const B = { id: "doc-prog-b.pdf", path: "/tmp/prog-b.pdf", name: "prog-b.pdf", last_page: 5 };

window.__saved = [];
window.__listeners = {};

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      if (cmd === "load_app_state") {
        // 两份历史都**带着进度**。`lastPath` 指向 B —— 于是启动后
        // `restore` 槽位里装的是 B，不是 A。本套件要打开的是 A。
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              { ...A, size: 24000, pages: PAGES, opened_at: 0, favorite: false },
              { ...B, size: 24000, pages: PAGES, opened_at: 0, favorite: false },
            ],
            lastPath: B.path,
            lastPage: B.last_page,
            scale: 1.25,
          })
        );
      }
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "save_app_state") {
        window.__saved.push(args && args.data);
        return Promise.resolve(null);
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

// ⚠️ 不要整个替换 onFileDrop —— 原实现里还带着两个 listen(...)，换掉就把
// `folia://open-path` 那条路也拆了（e2e25 栽过）。包一层。
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

const result = {};

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

const settle = async (ticks = 30) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const cardMetas = () =>
  [...document.querySelectorAll(".file-card")].map((c) => ({
    name: c.querySelector(".file-title")?.textContent.trim(),
    meta: c.querySelector(".file-meta")?.textContent.trim(),
  }));

/** 最后一次落盘里，prog-a.pdf 那条的 last_page。 */
function savedLastPageOfA() {
  const last = window.__saved[window.__saved.length - 1];
  if (!last) return undefined;
  try {
    const hit = (JSON.parse(last).recentFiles || []).find((f) => f.path === A.path);
    return hit ? hit.last_page : null;
  } catch (_) {
    return undefined;
  }
}

async function main() {
  try {
    await waitFor(() => document.querySelector(".app"), "应用挂载");
    await settle(40);

    result.cardMetas = cardMetas();
    result.pageInputBefore = document.querySelector(".page-input")?.value ?? null;

    const cb = window.__listeners["folia://open-path"];
    if (!cb) throw new Error("folia://open-path 监听器没注册上");

    cb({ payload: A.path });
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await settle(50);

    result.opened = true;
    result.pageInputAfter = document.querySelector(".page-input")?.value ?? null;
    result.savedLastPageOfA = savedLastPageOfA();
    result.savedAll = (() => {
      const last = window.__saved[window.__saved.length - 1];
      try {
        return (JSON.parse(last).recentFiles || []).map(
          (f) => `${f.name}:${f.last_page}/${f.pages}`
        );
      } catch (_) {
        return null;
      }
    })();
    result.saveCount = window.__saved.length;
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
