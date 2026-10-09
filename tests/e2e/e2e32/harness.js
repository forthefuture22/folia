// 复现：**收藏会不会被「最近 10 条」的上限挤掉？**
//
// `apply_document` 登记最近列表时：
//
//     write_recent(&handles, move |list| {
//         list.retain(|item| item.id != meta.id);
//         list.insert(0, entry);
//         list.truncate(10);        // ← 无条件砍尾巴
//     });
//
// `truncate(10)` 不看 `favorite` —— 被挤到第 11 位的条目（哪怕用户亲手标了星）
// 直接消失，而且**下一句 `persist_state` 就把它从磁盘上抹掉**。
// 结果：「收藏」实际上只是「最近 10 条之内的标记」，用户标了星的东西
// 只要再打开 10 份别的文档就会**静默消失**。
//
// 本套件钉这一件事：先收藏一份，再打开 10 份别的，看那份收藏还在不在。
// 只桩 Tauri IPC 与 bridge，其余（含真实 wasm）全走真实现。

const PAGES = 10;
const CAP = 10; // 对齐 handles.rs 里那句 truncate(10)
const FAV = { id: "doc-keep-fav.pdf", path: "/tmp/keep-fav.pdf", name: "keep-fav.pdf" };
const EXTRAS = Array.from({ length: CAP }, (_, i) => ({
  id: `doc-extra-${i + 1}.pdf`,
  path: `/tmp/extra-${i + 1}.pdf`,
  name: `extra-${i + 1}.pdf`,
}));

window.__saved = [];
window.__listeners = {};

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              { ...FAV, size: 24000, pages: PAGES, last_page: 3, opened_at: 0, favorite: true },
            ],
            lastPath: null,
            lastPage: null,
            scale: null,
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

const settle = async (ticks = 20) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const press = (el) => {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 }));
  }
};

const tabNames = () => [...document.querySelectorAll(".tab-label")].map((t) => t.textContent.trim());

function lastSaved() {
  const last = window.__saved[window.__saved.length - 1];
  if (!last) return null;
  try {
    return JSON.parse(last);
  } catch (_) {
    return null;
  }
}

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
    await waitFor(() => document.querySelector(".app"), "应用挂载");
    await settle(30);

    result.initialShelf = [...document.querySelectorAll(".file-card .file-title")].map((e) =>
      e.textContent.trim()
    );

    const cb = window.__listeners["folia://open-path"];
    if (!cb) throw new Error("folia://open-path 监听器没注册上");

    // 顺序打开 10 份**别的**文档 —— 刚好把 keep-fav.pdf 挤出 10 条上限。
    for (const extra of EXTRAS) {
      cb({ payload: extra.path });
      await waitFor(() => tabNames().includes(extra.name), `打开 ${extra.name}`);
      await settle(12);
    }
    result.tabCount = tabNames().length;

    await goHome();
    result.shelfAfter = [...document.querySelectorAll(".file-card .file-title")].map((e) =>
      e.textContent.trim()
    );

    // 收藏分页里还看得到吗？
    const favTab = [...document.querySelectorAll(".segmented button")].find(
      (b) => b.textContent.trim() === "收藏"
    );
    if (favTab) {
      press(favTab);
      await settle(20);
      result.favTabAfter = [...document.querySelectorAll(".file-card .file-title")].map((e) =>
        e.textContent.trim()
      );
    }

    const saved = lastSaved();
    result.savedNames = saved ? (saved.recentFiles || []).map((f) => `${f.name}:${f.favorite}`) : null;
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
