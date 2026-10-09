// 验证本轮六项改动（/tmp/e2e22）。
//
//   1. 中键点标签栏 → 关掉那个标签（左键点标签不该关）；
//   2. 「收藏」分页空着时**什么都不显示**（原来那句提示 + 云朵图标都删了）；
//   3. 拖到 Dock 图标上 / Finder「打开方式」：冷启动靠 `take_pending_open`；
//   4. 「设为默认 PDF 阅读器」按钮 + 两种结果各自的话术；
//   5. 页面总览的浮层变小了，并且带弹出动效；
//   6. 吐司 2 秒后自动收起。
//
// 三个场景用 `?case=` 切换（见 verify.py）：
//   main            —— 上面 1/2/4/5/6
//   pending         —— 冷启动时 `take_pending_open` 直接返回一个路径
//   alreadydefault  —— `default_pdf_handler` 返回 true（已经是默认了）
//
// 只桩 Tauri IPC，bridge.js 全走真实实现。

const PAGES = 12;
const PAGE_W = 595;
const PAGE_H = 842;

const CASE = new URLSearchParams(location.search).get("case") || "main";

// 书架上摆两份，点两张卡片就能开出两个标签。
//
// ⚠️ `id` 必须和 `openDocumentPath` 回显出来的那个一致：
// 应用是按 `meta.id` 去重的（同一份文档已经在标签里就只激活、不再开新标签），
// id 对不上会凭空多出一张卡片，第二次点卡片也就开不出第二个标签。
const RECENTS = [
  { id: "doc-h22-1.pdf", path: "/tmp/h22-1.pdf", name: "h22-1.pdf" },
  { id: "doc-h22-2.pdf", path: "/tmp/h22-2.pdf", name: "h22-2.pdf" },
];

// `pending` 场景：后端在冷启动时就已经把路径存好了（用户拖到 Dock 图标上的）。
const PENDING_PATH = "/tmp/from-dock.pdf";

window.__invokes = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd) => {
      window.__invokes.push({ cmd });
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
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "take_pending_open") {
        return Promise.resolve(CASE === "pending" ? PENDING_PATH : null);
      }
      if (cmd === "default_pdf_handler") {
        return Promise.resolve(CASE === "alreadydefault" ? true : false);
      }
      if (cmd === "set_default_pdf_handler") return Promise.resolve(null);
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
  // 回显请求的路径：这样"打开的是哪一份"一眼可查
  //（`pending` 场景就是靠这个断言打开的是 from-dock.pdf 而不是书架那份）。
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

const settle = async (ticks = 10) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

function press(el, opts = {}) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1, ...opts })
    );
  }
}

// 中键：真实浏览器只发 mousedown/mouseup(button=1) + auxclick，**不发 click**。
function middlePress(el) {
  for (const type of ["mousedown", "mouseup", "auxclick"]) {
    el.dispatchEvent(
      new MouseEvent(type, {
        bubbles: true,
        cancelable: true,
        view: window,
        detail: 1,
        button: 1,
        buttons: 4,
      })
    );
  }
}

const tabs = () => [...document.querySelectorAll(".tab")];
const tabNames = () => tabs().map((t) => t.querySelector(".tab-label").textContent.trim());
const invokesOf = (cmd) => window.__invokes.filter((i) => i.cmd === cmd).length;
const cards = () => [...document.querySelectorAll(".file-card")];

async function main() {
  try {
    // ================================================================ 场景 pending
    if (CASE === "pending") {
      // 什么都不点：文档是后端"从 Dock 拖进来"的，前端启动时取到就该直接开。
      await waitFor(() => document.querySelector(".stage"), "阅读器出现", 6000);
      result.pendingOpenedDirectly = true;
      result.pendingTabName = document.querySelector(".tab-label")?.textContent.trim() ?? null;
      result.pendingInvokeCount = invokesOf("take_pending_open");
      result.pendingIsDockFile = (result.pendingTabName || "").includes("from-dock");
      finish();
      return;
    }

    // ================================================================ 开两个标签
    await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    await settle(20);
    result.cardCount = cards().length;
    result.cardNames = cards().map((c) => c.querySelector(".file-title")?.textContent.trim());

    result.trace = [];
    const snap = (label) =>
      result.trace.push({
        label,
        view: document.querySelector(".stage")
          ? "reader"
          : document.querySelector(".library")
          ? "shelf"
          : "?",
        cards: cards().map((c) => c.querySelector(".file-title")?.textContent.trim()),
        tabs: tabNames(),
      });

    snap("初始");
    press(cards()[0]);
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await settle(24);
    snap("点了卡片1之后");

    press(document.querySelector(".tabbar-home"));
    await waitFor(() => document.querySelector(".library"), "回到书架");
    await settle(20);
    snap("回到书架之后");

    press(cards()[1]);
    await waitFor(() => document.querySelector(".stage"), "第二份文档出现");
    await settle(24);
    snap("点了卡片2之后");

    result.tabNamesBefore = tabNames();
    result.tabCountBefore = tabs().length;

    // ================================================================ 1. 中键关标签
    // 左键点标签本身：只切换，不该关
    press(tabs()[0].querySelector(".tab-label"));
    await settle(16);
    result.tabCountAfterLeftClick = tabs().length;
    result.activeTabAfterLeftClick =
      document.querySelector(".tab.active .tab-label")?.textContent.trim() ?? null;

    // 中键点第一个标签 → 关掉它
    const closing = tabs()[0].querySelector(".tab-label").textContent.trim();
    result.middleTarget = closing;
    middlePress(tabs()[0]);
    await settle(24);
    result.tabNamesAfterMiddle = tabNames();
    result.tabCountAfterMiddle = tabs().length;

    // ================================================================ 5. 总览浮层大小 + 动效
    press(document.querySelector(".reader-top-left .icon-btn"));
    await settle(20);
    const panel = document.querySelector(".po-panel");
    result.panelExists = !!panel;
    if (panel) {
      const rect = panel.getBoundingClientRect();
      const style = getComputedStyle(panel);
      result.panelWidth = Math.round(rect.width);
      result.panelHeight = Math.round(rect.height);
      result.panelAnimation = style.animationName;
      result.panelAnimationMs = style.animationDuration;
      result.overlayAnimation = getComputedStyle(
        document.querySelector(".page-overview")
      ).animationName;
      // 面板四周确实留出了遮罩（"点空白关闭"才有地方可点）
      result.backdropTopLeftHitsOverlay =
        document.elementFromPoint(6, 6)?.classList.contains("page-overview") ?? false;
      result.poItems = document.querySelectorAll(".po-item").length;
    }
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
    );
    await settle(16);
    result.overviewClosedByEsc = !document.querySelector(".page-overview");

    // ================================================================ 回书架看「收藏」分页
    press(document.querySelector(".tabbar-home"));
    await waitFor(() => document.querySelector(".library"), "回到书架");
    await settle(20);

    const segs = () => [...document.querySelectorAll(".segmented .seg")];
    const favSeg = segs().find((b) => b.textContent.trim() === "收藏");
    result.favSegExists = !!favSeg;
    press(favSeg);
    await settle(24);
    result.favEmptyEls = document.querySelectorAll(".lib-empty").length;
    result.favEmptyIconEls = document.querySelectorAll(".lib-empty .empty-icon").length;
    result.favGridEls = document.querySelectorAll(".file-grid").length;
    const panelEl = document.querySelector(".panel");
    result.favPanelText = panelEl ? panelEl.textContent.trim() : null;
    result.favStillMentionsHint = (panelEl ? panelEl.textContent : "").includes("还没有收藏");

    // 切回「最近」，卡片应该照旧（证明删的只是收藏那一支）
    const recentSeg = segs().find((b) => b.textContent.trim() === "最近");
    press(recentSeg);
    await settle(20);
    result.recentCardCount = cards().length;

    // ================================================================ 4. 设为默认 + 6. 吐司
    const defBtn = [...document.querySelectorAll(".lib-actions .btn")].find((b) =>
      b.textContent.includes("设为默认")
    );
    result.defaultBtnExists = !!defBtn;
    result.defaultBtnTitle = defBtn ? defBtn.title : null;

    if (CASE === "alreadydefault") {
      // 已经是默认了 → 按钮**不该出现**（点了什么都不会变，摆着只会被当成坏了）。
      // 所以这里没有可点的东西，只断言"不存在"+"没调过设置命令"。
      result.defaultCheckedInvoked = invokesOf("default_pdf_handler");
      result.defaultSetInvoked = invokesOf("set_default_pdf_handler");
      result.toastAtStart = document.querySelector(".toast")?.textContent.trim() ?? null;
    } else {
      press(defBtn);
      await settle(6);
      result.toastAtStart = document.querySelector(".toast")?.textContent.trim() ?? null;
      result.defaultCheckedInvoked = invokesOf("default_pdf_handler");
      result.defaultSetInvoked = invokesOf("set_default_pdf_handler");

      // 1.2 秒时还在（不能一出来就消失）
      await wait(1200);
      result.toastAt1200ms = document.querySelector(".toast")?.textContent.trim() ?? null;

      // 再等 1.4 秒（累计 2.6s）应该已经收起来了
      await wait(1400);
      result.toastAt2600ms = document.querySelector(".toast")?.textContent.trim() ?? null;
    }
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
