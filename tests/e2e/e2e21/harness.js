// 验证全局键盘快捷键（/tmp/e2e21）。
//
// 背景：快捷键那个 effect 原来是 `use_effect_with((), …)` —— 只注册一次，
// 闭包捕获的 `UseStateHandle` / `UseMemo` 是**注册那一帧的快照**。
// 而第一帧是"文档还没载入"：`meta` 是 `None`、`layout` 是空的（`len() == 0`）。
// 于是 `is_typing || meta.is_none()` 直接 return，`total` 又恒等于 1 ——
// **方向键 / PageUp / PageDown / 空格 / Home / End 全都按不动**。
//
// 修法是把这几项放进依赖里（状态一变就重新注册），这个套件就是钉住它。
//
// 只桩 Tauri IPC，bridge.js 全走真实实现。

const PAGES = 30;
const PAGE_W = 595;
const PAGE_H = 842;

const META = {
  id: "h21",
  path: "/tmp/h21.pdf",
  name: "h21.pdf",
  size: 24000,
  numPages: PAGES,
};

window.__invokes = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd) => {
      window.__invokes.push({ cmd });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              {
                id: META.id,
                path: META.path,
                name: META.name,
                size: META.size,
                pages: PAGES,
                last_page: 1,
                opened_at: 0,
                favorite: false,
              },
            ],
            lastPath: META.path,
            lastPage: 1,
            scale: 1,
          })
        );
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      return Promise.resolve(null);
    },
  },
  event: { listen: () => Promise.resolve(() => {}) },
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

Object.assign(bridge, {
  openDocumentPath: () =>
    Promise.resolve(JSON.stringify({ ok: true, cancelled: false, error: null, meta: META })),
  getPageSizes: () =>
    Promise.resolve(
      JSON.stringify(Array.from({ length: PAGES }, () => ({ width: PAGE_W, height: PAGE_H })))
    ),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: (_path, _page, maxWidth) =>
    Promise.resolve(makeThumb(maxWidth)),
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

const settle = async (ticks = 10) => {
  for (let i = 0; i < ticks; i++) {
    await new Promise((r) => setTimeout(r, 16));
  }
};

const pageNow = () => document.querySelector(".page-input")?.value ?? null;

async function key(name, settleTicks = 14) {
  document.dispatchEvent(
    new KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true })
  );
  await settle(settleTicks);
  return pageNow();
}

async function main() {
  try {
    const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await waitFor(() => document.querySelector(".thumb-item"), "缩略图面板出现");
    await settle(30);

    result.startPage = pageNow();

    // 方向键：必须能一路走上去，而不是卡在第 1 页
    result.rightSeq = [];
    for (let i = 0; i < 3; i++) result.rightSeq.push(await key("ArrowRight"));

    // PageDown / 空格 也应该前进
    result.afterPageDown = await key("PageDown");
    result.afterSpace = await key(" ");

    // End / Home 跳到首尾
    result.afterEnd = await key("End");
    result.afterHome = await key("Home");

    // 往回走，以及第 1 页时不该再退
    result.afterLeft = await key("ArrowLeft");
    result.afterLeftAgain = await key("ArrowLeft");

    // 真跳过去了吗？光看页码框不够 —— 量一下正文的滚动位置。
    result.scrollTopAtPage1 = document.querySelector(".stage")?.scrollTop ?? null;
    result.afterEndAgain = await key("End");
    result.scrollTopAtEnd = document.querySelector(".stage")?.scrollTop ?? null;
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
