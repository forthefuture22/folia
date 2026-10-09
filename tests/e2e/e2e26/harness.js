// 验证本轮四项改动（/tmp/e2e26）。
//
//   1. 首页卡片的文件名**只占一行**，hover 名字能看到完整文件名；
//   3. 「已经是默认 PDF 阅读器」时，那个「设为默认」按钮**不显示**；
//   4. 首页那个「筛选」（sliders）按钮**被删掉**。
//
// 场景用 `?case=` 切换（见 verify.py）：
//   home            —— 不是默认（`default_pdf_handler` 返回 false）
//   alreadydefault  —— 已经是默认（返回 true）
//   clickdefault    —— 先返回 false，点一下按钮之后应该消失
//
// 只桩 Tauri IPC，其余（含真实 wasm）全走真实现。

const PAGES = 12;
const PAGE_W = 595;
const PAGE_H = 842;

const CASE = new URLSearchParams(location.search).get("case") || "home";

// 特意放一个**超长文件名**：单行截断 / hover 提示这两件事只有它会露出来。
const LONG_NAME =
  "Java 热门面试题 200 道速记通关版 _ 面试刷题 mianshiya.com_加水印_最终版_v3.pdf";
const SHORT_NAME = "短名字.pdf";

// 第一份给个**真实时间戳**（提示里应该带「今天」），
// 第二份故意留 `opened_at: 0`（相对时间算不出来，提示里不该多出一条空行）。
const RECENTS = [
  { id: "doc-long.pdf", path: "/tmp/long.pdf", name: LONG_NAME, opened_at: Date.now() - 3600_000 },
  { id: "doc-short.pdf", path: "/tmp/short.pdf", name: SHORT_NAME, opened_at: 0 },
];

let defaultHandlerAnswer = CASE === "alreadydefault";

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
              favorite: false,
            })),
            lastPath: RECENTS[0].path,
            lastPage: 1,
            scale: 1,
          })
        );
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "default_pdf_handler") return Promise.resolve(defaultHandlerAnswer);
      if (cmd === "set_default_pdf_handler") {
        // 设置成功之后，系统里就是它了 —— 再查一次要返回 true。
        defaultHandlerAnswer = true;
        return Promise.resolve(null);
      }
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
  openDocumentPath: () => Promise.resolve(JSON.stringify({ ok: true, cancelled: false, error: null, meta: null })),
  getPageSizes: () =>
    Promise.resolve(
      JSON.stringify(Array.from({ length: PAGES }, () => ({ width: PAGE_W, height: PAGE_H })))
    ),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: (_path, _page, maxWidth) => Promise.resolve(makeThumb(maxWidth)),
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

function press(el) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 })
    );
  }
}

const cards = () => [...document.querySelectorAll(".file-card")];
const titles = () => cards().map((c) => c.querySelector(".file-title"));
const toolBtn = (title) =>
  [...document.querySelectorAll(".panel-tools .icon-btn")].find((b) => b.title === title);
const defaultBtn = () =>
  [...document.querySelectorAll(".lib-actions .btn")].find((b) =>
    b.textContent.includes("设为默认")
  );
const openBtn = () =>
  [...document.querySelectorAll(".lib-actions .btn-primary")].find((b) =>
    b.textContent.includes("打开文件")
  );

async function main() {
  try {
    await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    await settle(24);

    // 出截图用：只等页面稳定，不跑断言、也不往页面上塞 <pre>（会入镜）。
    if (new URLSearchParams(location.search).get("shot")) {
      window.__HARNESS_DONE__ = true;
      return;
    }

    // ================================================================ 1. 文件名一行 + hover
    const els = titles();
    result.titleCount = els.length;
    result.titleTexts = els.map((e) => e.textContent.trim());
    if (els[0]) {
      const style = getComputedStyle(els[0]);
      const rect = els[0].getBoundingClientRect();
      result.titleWhiteSpace = style.whiteSpace;
      result.titleOverflow = style.overflow;
      result.titleTextOverflow = style.textOverflow;
      result.titleFontSize = parseFloat(style.fontSize);
      result.titleLineHeight = parseFloat(style.lineHeight);
      result.titleHeight = Math.round(rect.height * 100) / 100;
      result.titleScrollWidth = els[0].scrollWidth;
      result.titleClientWidth = els[0].clientWidth;
      // 长名字确实被截了（否则这条用例是空验证）
      result.titleIsTruncated = els[0].scrollWidth > els[0].clientWidth;
      // hover 提示 = 完整文件名
      result.titleAttr = els[0].getAttribute("title");
    }
    if (els[1]) {
      result.shortTitleHeight = Math.round(els[1].getBoundingClientRect().height * 100) / 100;
    }
    // 卡片整体仍然挂着「名字 + 时间」的提示
    result.cardAttr = cards()[0]?.getAttribute("title") ?? null;
    // 时间戳缺失的那张：提示里**不该**多出一条空行
    result.cardAttrNoTime = cards()[1]?.getAttribute("title") ?? null;

    // ================================================================ 4. 筛选按钮没了
    result.hasFilterBtn = !!toolBtn("筛选");
    result.hasSlidersIcon = !!document.querySelector('.panel-tools [title="筛选"]');
    result.panelToolTitles = [...document.querySelectorAll(".panel-tools .icon-btn")].map(
      (b) => b.title
    );

    // ================================================================ 3. 设为默认按钮的显隐
    result.defaultBtnExists = !!defaultBtn();
    result.openBtnExists = !!openBtn();

    if (CASE === "clickdefault") {
      const btn = defaultBtn();
      if (!btn) throw new Error("一开始就没有「设为默认」按钮，点不了");
      press(btn);
      await settle(10);
      result.toastAfterClick = document.querySelector(".toast")?.textContent.trim() ?? null;
      result.setInvoked = window.__invokes.filter((i) => i.cmd === "set_default_pdf_handler").length;
      result.checkInvoked = window.__invokes.filter((i) => i.cmd === "default_pdf_handler").length;
      // 设完之后按钮应该消失
      await settle(16);
      result.defaultBtnExistsAfter = !!defaultBtn();
      result.openBtnExistsAfter = !!openBtn();
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
