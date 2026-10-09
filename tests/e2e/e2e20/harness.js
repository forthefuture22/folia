// 探针：查「点不动」到底卡在哪。
// 关键差异 —— e2e19 是直接往按钮上 dispatchEvent（绕过了命中测试），
// 这里改成先问 `document.elementFromPoint()` 谁在最上面，再把事件交给它，
// 这正是真实鼠标点击走的路径。

const PAGES = 30;
const PAGE_W = 595;
const PAGE_H = 842;

const META = {
  id: "h20",
  path: "/tmp/h20.pdf",
  name: "h20.pdf",
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
  ctx.fillStyle = "#c9c9d2";
  ctx.fillRect(0, 0, canvas.width, 6);
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
    new Promise((resolve) => setTimeout(() => resolve(makeThumb(maxWidth)), 25)),
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

const thumbImgs = () => document.querySelectorAll(".thumb-frame img").length;

// 像真实鼠标那样点：先命中测试，谁在最上面就点谁。
function realClick(el) {
  const r = el.getBoundingClientRect();
  const cx = r.left + r.width / 2;
  const cy = r.top + r.height / 2;
  const top = document.elementFromPoint(cx, cy);
  const info = {
    rect: {
      x: Math.round(r.left),
      y: Math.round(r.top),
      w: Math.round(r.width),
      h: Math.round(r.height),
    },
    cx: Math.round(cx),
    cy: Math.round(cy),
    topTag: top ? top.tagName : null,
    topClass: top ? String(top.className) : null,
    topIsTarget: top === el,
    topInsideTarget: !!(top && el.contains(top)),
  };
  if (top) {
    for (const type of ["mousedown", "mouseup", "click"]) {
      top.dispatchEvent(
        new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          detail: 1,
          clientX: cx,
          clientY: cy,
        })
      );
    }
  }
  return info;
}

function describeSidebar() {
  const sidebar = document.querySelector(".sidebar");
  const active = document.querySelector(".rail .rail-btn.active");
  return {
    sidebarExists: !!sidebar,
    sidebarClass: sidebar ? String(sidebar.className) : null,
    activeTab: active ? active.title : null,
  };
}

async function main() {
  try {
    // ---- 0. 打开文档
    const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await waitFor(() => document.querySelector(".thumb-item"), "缩略图面板出现");
    await settle(40);

    // ---- 1. 打开文档后，侧栏本来是什么状态？（用户还没点任何东西）
    result.sidebarAtStart = describeSidebar();
    result.renderedAtStart = thumbImgs();
    result.thumbItemsAtStart = document.querySelectorAll(".thumb-item").length;

    // ---- 2. 缩略图按钮的命中测试
    const left = document.querySelector(".reader-top-left");
    const thumbBtn = left.querySelector(".icon-btn");
    result.thumbBtn = realClick(thumbBtn);
    await settle(24);

    result.sidebarAfterThumbClick = describeSidebar();
    result.renderedAfterThumbClick = thumbImgs();

    // 等到全渲染（如果回调真的跑起来了）
    await waitFor(
      () => {
        const n = thumbImgs();
        return n >= PAGES ? n : null;
      },
      "全部缩略图",
      8000
    ).catch(() => null);
    result.renderedAfterWait = thumbImgs();

    // ---- 3. 标签栏 + 号的命中测试（它在 data-tauri-drag-region="deep" 里面）
    const add = document.querySelector(".tabbar-add");
    result.addBtn = realClick(add);
    await settle(20);
    result.invokesAfterAdd = window.__invokes.map((i) => i.cmd);

    // ---- 4. 全局 keydown 监听器是不是"读到旧值"了？
    // 它是 `use_effect_with((), …)` 只注册一次，闭包捕获的是第一帧的 `UseStateHandle`。
    // 如果方向键只能从第 1 页走到第 2 页，就说明读的是陈旧快照。
    const pageInput = () => document.querySelector(".page-input")?.value;
    result.pageBeforeKeys = pageInput();
    const keySeq = [];
    for (let i = 0; i < 3; i++) {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true, cancelable: true })
      );
      await settle(12);
      keySeq.push(pageInput());
    }
    result.pageKeySeq = keySeq;
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
