// 验证顶栏改动（新设计）：
//   1. 「工具」pill 里的 sparkles（星星）去掉了；
//   2. 它右边那个按钮是「展开全部缩略图」—— 点击后盖出**页面总览**浮层，
//      把所有页排成网格，点哪页跳哪页；
//   3. 「打开其它 PDF」搬到了标签栏右端的 + 号；
//   4. 批注面板的空态那句「选中正文后会浮出一排工具…」删掉了。
//
// 只桩 Tauri IPC，bridge.js 全走真实实现。
// 点击一律走 `elementFromPoint()` 命中测试（真实鼠标的路径）——
// 直接 dispatchEvent 会绕过命中测试，浮层盖住了也测不出来。

const PAGES = 30;
const PAGE_W = 595;
const PAGE_H = 842;

const META = {
  id: "h19",
  path: "/tmp/h19.pdf",
  name: "h19.pdf",
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
  // 故意加一点延迟：这样"一张一张冒出来"是可观察的，
  // 也能证明「点一下就排 30 页」不是本来就已经排好了。
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

function press(el) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 })
    );
  }
}

// 真实鼠标路径：先命中测试，谁在最上面就点谁。
function realClick(el) {
  const r = el.getBoundingClientRect();
  return clickAt(r.left + r.width / 2, r.top + r.height / 2);
}

function clickAt(x, y) {
  const top = document.elementFromPoint(x, y);
  if (top) {
    for (const type of ["mousedown", "mouseup", "click"]) {
      top.dispatchEvent(
        new MouseEvent(type, {
          bubbles: true,
          cancelable: true,
          view: window,
          detail: 1,
          clientX: x,
          clientY: y,
        })
      );
    }
  }
  return top;
}

const thumbImgs = () => document.querySelectorAll(".thumb-frame img").length;
const poItems = () => document.querySelectorAll(".po-item").length;
const poImgs = () => document.querySelectorAll(".po-frame img").length;
const overviewEl = () => document.querySelector(".page-overview");

async function main() {
  try {
    const shot = new URLSearchParams(location.search).get("shot");

    // ---- 0. 从书架打开文档
    const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await waitFor(() => document.querySelector(".thumb-item"), "缩略图面板出现");
    await settle(40);

    // 截图专用：只把总览掀开，停在那儿等截图。
    if (shot === "overview") {
      const btn = document.querySelector(".reader-top-left .icon-btn");
      realClick(btn);
      await waitFor(() => (poImgs() >= PAGES ? poImgs() : null), "全部缩略图", 8000).catch(
        () => null
      );
      await settle(10);
      result.shotMode = "overview";
      result.poImgs = poImgs();
      const pre0 = document.createElement("pre");
      pre0.id = "harness-result";
      pre0.textContent = JSON.stringify(result, null, 2);
      document.body.appendChild(pre0);
      window.__HARNESS_DONE__ = true;
      return;
    }

    // ---- 1. 顶栏左侧：工具 pill 里不该再有星星，右边那个按钮改成缩略图
    const left = document.querySelector(".reader-top-left");
    const pill = left.querySelector(".pill-tool");
    result.pillText = pill.textContent.trim();
    result.pillSvgCount = pill.querySelectorAll("svg").length;

    const thumbBtn = left.querySelector(".icon-btn");
    result.thumbBtnTitle = thumbBtn.title;
    result.thumbBtnHasSvg = !!thumbBtn.querySelector("svg");
    // `pages` 图标是 4 个矩形（2×2），`cards` 是 2 个 —— 用来区分换没换图标
    result.thumbBtnRects = thumbBtn.querySelectorAll("svg rect").length;
    result.leftStillHasOpenOther = !!left.querySelector('button[title="打开其它 PDF"]');

    // ---- 2. 标签栏右端的 +
    const add = document.querySelector(".tabbar-add");
    result.addBtnExists = !!add;
    result.addBtnTitle = add ? add.title : null;
    result.addBtnInTabbar = !!add && add.parentElement?.classList.contains("tabbar");
    result.addBtnIsLast = !!add && add.parentElement?.lastElementChild === add;

    // ---- 3. 点之前：侧栏默认就是开着的、就停在缩略图页（这就是"点下去看不出来"的根源）
    result.sidebarOpenAtStart = !!document.querySelector(".sidebar");
    result.activeTabAtStart = document.querySelector(".rail .rail-btn.active")?.title ?? null;
    result.renderedBefore = thumbImgs();
    result.overviewBeforeClick = !!overviewEl();

    // ---- 4. 批注面板空态：那句提示应该没了
    const railComment = [...document.querySelectorAll(".rail .rail-btn")].find(
      (b) => b.title === "批注"
    );
    result.railCommentExists = !!railComment;
    press(railComment);
    await settle(24);
    const body = document.querySelector(".side-body");
    result.annBodyText = body ? body.textContent.trim() : null;
    result.annHasHint = (body ? body.textContent : "").includes("浮出一排工具");
    result.annEmptyEls = document.querySelectorAll(".side-body .side-empty").length;
    result.tabAfterRail = document.querySelector(".rail .rail-btn.active")?.title ?? null;

    // ---- 5. 点「展开全部缩略图」→ 应该盖出总览浮层
    const hit = realClick(thumbBtn);
    result.thumbBtnTopIsSelf = hit === thumbBtn || (hit && thumbBtn.contains(hit));
    await settle(20);

    const overview = overviewEl();
    result.overviewAfterClick = !!overview;
    result.poItemsAfterClick = poItems();
    result.poTitle = document.querySelector(".po-title")?.textContent.trim() ?? null;
    result.poProgress = document.querySelector(".po-progress")?.textContent.trim() ?? null;
    result.poCurrentCount = document.querySelectorAll(".po-item.is-current").length;
    result.poCurrentNum = document.querySelector(".po-item.is-current .po-num")?.textContent ?? null;
    result.poImgsRightAfterClick = poImgs();

    // 30 页全都填进去
    await waitFor(() => (poImgs() >= PAGES ? poImgs() : null), "总览里 30 张缩略图", 8000).catch(
      () => null
    );
    result.poImgsAfterWait = poImgs();
    result.poProgressAfter = document.querySelector(".po-progress")?.textContent.trim() ?? null;

    // ---- 6. 点网格里第 5 页 → 跳过去 + 浮层收起
    const items = [...document.querySelectorAll(".po-item")];
    realClick(items[4]);
    await settle(24);
    result.overviewAfterPick = !!overviewEl();
    result.pageAfterPick = document.querySelector(".page-input")?.value ?? null;

    // ---- 7. 点遮罩空白处 → 关闭
    realClick(thumbBtn);
    await settle(20);
    result.overviewReopened = !!overviewEl();
    // 面板是居中的，所以遮罩只在四个角露出来 —— 点左上角那圈。
    clickAt(6, 6);
    await settle(16);
    result.overviewAfterBackdrop = !!overviewEl();

    // ---- 8. Esc → 关闭，但**不要**退回书架
    realClick(thumbBtn);
    await settle(20);
    result.overviewBeforeEsc = !!overviewEl();
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
    );
    await settle(20);
    result.overviewAfterEsc = !!overviewEl();
    result.stillInReaderAfterEsc = !!document.querySelector(".stage");
    result.backAtLibrary = !!document.querySelector(".library");
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
