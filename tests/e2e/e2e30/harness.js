// 复现：**重新打开一份已收藏的文档，会不会把「收藏」标记清掉？**
//
// 背景：`apply_document` 在登记「最近打开」时是这样写的 ——
//
//     let mut entry = RecentFile::from(&meta);   // ← 这里 favorite 恒为 false
//     entry.pages = page_count;
//     entry.last_page = page;
//     entry.opened_at = Date::now();
//     write_recent(&handles, move |list| {
//         list.retain(|item| item.id != meta.id); // ← 把带 favorite:true 的旧条目删掉
//         list.insert(0, entry);                  // ← 换成 favorite:false 的新条目
//         list.truncate(10);
//     });
//
// `RecentFile::from(&DocMeta)` 里写死了 `favorite: false`（types.rs），
// 而 `DocMeta` 根本不带收藏信息 —— 于是**每打开一次，收藏就被抹一次**。
//
// 这条路只在「这份文档**不在标签里**」时才走到：`apply_document` 开头有个
// 早退分支（已经开着就直接激活、`return`），那条路不碰最近列表。
// 所以「收藏 → 退出应用 → 从书架再打开」正是命中的那一步。
//
// 本套件就钉这一件事：把一份 `favorite: true` 的历史打开一遍，看落盘里还剩不剩 true。
// 只桩 Tauri IPC 与 bridge，其余（含真实 wasm）全走真实现。

const PAGES = 4;
const FAV = { id: "doc-fav.pdf", path: "/tmp/fav.pdf", name: "fav.pdf" };

window.__saved = [];
window.__listeners = {};

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      if (cmd === "load_app_state") {
        // 只有这一条历史，而且是**已收藏**的。lastPath 给 null，
        // 这样启动时不会自动打开任何东西，打开动作完全由本套件触发。
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              { ...FAV, size: 24000, pages: PAGES, last_page: 1, opened_at: 0, favorite: true },
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

const settle = async (ticks = 20) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const press = (el) => {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 }));
  }
};

/** 最后一次落盘里，fav.pdf 的 favorite 值。找不到条目返回 null。 */
function savedFavoriteOfFav() {
  const last = window.__saved[window.__saved.length - 1];
  if (!last) return undefined;
  let parsed;
  try {
    parsed = JSON.parse(last);
  } catch (_) {
    return undefined;
  }
  const hit = (parsed.recentFiles || []).find((f) => f.path === FAV.path);
  return hit ? hit.favorite : null;
}

async function main() {
  try {
    await waitFor(() => document.querySelector(".app"), "应用挂载");
    await settle(30);

    // 前置：书架上确实有这张卡片，而且它一开始是「已收藏」。
    result.shelfNames = [...document.querySelectorAll(".file-card .file-title")].map((e) =>
      e.textContent.trim()
    );
    result.initialSavedFavorite = savedFavoriteOfFav();

    // 前置：收藏分页里能看到它。
    const favTab = [...document.querySelectorAll(".segmented button")].find(
      (b) => b.textContent.trim() === "收藏"
    );
    if (favTab) {
      press(favTab);
      await settle(20);
      result.favTabNames = [...document.querySelectorAll(".file-card .file-title")].map((e) =>
        e.textContent.trim()
      );
    }
    // 回到「最近」，再从书架把它打开。
    const recentTab = [...document.querySelectorAll(".segmented button")].find(
      (b) => b.textContent.trim() === "最近"
    );
    if (recentTab) {
      press(recentTab);
      await settle(10);
    }

    const cb = window.__listeners["folia://open-path"];
    if (!cb) throw new Error("folia://open-path 监听器没注册上");

    cb({ payload: FAV.path });
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    result.opened = true;
    await settle(40);

    result.finalSavedFavorite = savedFavoriteOfFav();
    result.finalSavedNames = (() => {
      const last = window.__saved[window.__saved.length - 1];
      try {
        return (JSON.parse(last).recentFiles || []).map((f) => `${f.name}:${f.favorite}`);
      } catch (_) {
        return null;
      }
    })();

    // 回书架看看「收藏」分页里还在不在。
    document.querySelector(".tabbar-home")?.dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true })
    );
    await settle(30);
    const favTab2 = [...document.querySelectorAll(".segmented button")].find(
      (b) => b.textContent.trim() === "收藏"
    );
    if (favTab2) {
      press(favTab2);
      await settle(20);
      result.favTabNamesAfter = [...document.querySelectorAll(".file-card .file-title")].map((e) =>
        e.textContent.trim()
      );
    }
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
