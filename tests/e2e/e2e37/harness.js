// e2e37：总览（全部页面）的缩略图**按需补齐**在超过缓存上限之后还成不成立。
//
// 背景：缩略图缓存有条数上限（`state/handles.rs` 的 `PAGE_THUMB_LIMIT = 60`），
// 淘汰规则是「挤掉**离焦点页最远**的那张」。而总览是一张**所有页**的网格 ——
// 用户会在网格里滚到远离阅读位置的地方。焦点要是取**阅读位置**（`current_page`），
// 那么"网格里刚渲染完的那页"自己就是离它最远的那个：
//
//    插进去 → 立刻被当成最远的删掉 → 推版本号 → 补齐 effect 重跑 → 再请求 → 转不停
//
// 这一套专门用 **150 页**（> 60）的文档，把那个组合摆出来：
//   ① 打开总览；
//   ② **分档**把网格滚到底（一步跳到底的话请求量到不了 60，淘汰不参与，
//      主判据会假绿 —— 见下面 `scroll-grid` 那段）；
//   ③ 看三件事：网格**看得见**的格子有没有图 / 静止时渲染次数还涨不涨 /
//      等一会儿会不会自己好。
//
// ⚠️ 为什么现有套件抓不到：`e2e19` 用的是 **30 页**文档（< 60），上限根本
// 不触发，它的判据甚至是「总览里 30 张缩略图全部填好」—— 那在小文档上是对的。
//
// 只桩 Tauri IPC；`renderThumbnail` 桩成一个固定延迟的假图（本 bug 在 Rust/wasm
// 侧的淘汰策略里，跟 pdf.js 真渲染无关，桩掉更快更稳）。包一层计数是为了把
// 「还在不在循环里」变成可观测的**副作用计数**（真实现保留）。

const PAGES = 150;
const PAGE_W = 595;
const PAGE_H = 842;

const META = {
  id: "h37",
  path: "/tmp/h37.pdf",
  name: "h37.pdf",
  size: 24000,
  numPages: PAGES,
};

window.__TAURI__ = {
  core: {
    invoke: (cmd) => {
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
const result = { pages: PAGES, stage: "module" };
if (!bridge) {
  result.error = "bridge.js 没有先执行：桩的插入位置不对";
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
    }
    return Promise.resolve("");
  },
  renderTextLayer: () => Promise.resolve(""),
});

// ---- 副作用计数：`renderThumbnail` 真被调了几次 ----------------------------
//
// ⚠️ 保留真实现（上面那个桩），只包一层计数 —— 整个换掉就等于在测自己的桩。
window.__thumbs = [];
if (bridge) {
  const real = bridge.renderThumbnail.bind(bridge);
  bridge.renderThumbnail = (path, page, maxWidth) => {
    window.__thumbs.push(page);
    return real(path, page, maxWidth);
  };
}

// ---------------------------------------------------------------- 驱动

function stage(name) {
  result.stage = name;
  try {
    document.body.setAttribute("data-e2e37-stage", name);
  } catch (_) {}
}

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

const grid = () => document.querySelector(".po-grid");
const poItems = () => document.querySelectorAll(".po-item").length;
const poImgs = () => document.querySelectorAll(".po-frame img").length;
const overviewEl = () => document.querySelector(".page-overview");

/// 应用自己的「哪几项在可视区」规则：`dom::visible_pages(grid, ".po-item", 240)`。
/// 这里照抄一份 —— 探针要量的是**应用认为该看见的那些格子**有没有图。
const BAND = 240;
function visiblePages() {
  const g = grid();
  if (!g) return [];
  const rect = g.getBoundingClientRect();
  const top = rect.top - BAND;
  const bottom = rect.bottom + BAND;
  const out = [];
  for (const el of g.querySelectorAll(".po-item")) {
    const r = el.getBoundingClientRect();
    if (r.bottom < top || r.top > bottom) continue;
    const page = Number(el.getAttribute("data-page"));
    if (page) out.push(page);
  }
  return out;
}

function visibleMissing() {
  const g = grid();
  if (!g) return -1;
  const pages = visiblePages();
  const missing = pages.filter((page) => {
    const el = g.querySelector(`.po-item[data-page="${page}"]`);
    return !(el && el.querySelector(".po-frame img"));
  });
  result.visibleSample = pages.slice(0, 6);
  result.visibleMissingSample = missing.slice(0, 6);
  return missing.length;
}

async function main() {
  try {
    if (result.error) throw new Error(result.error);

    // ---- 从书架打开文档
    stage("shelf");
    const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    stage("reader");
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await waitFor(() => document.querySelector(".thumb-item"), "缩略图面板出现");
    await settle(20);

    // ---- 打开总览（逐页渲染整本）
    stage("open-overview");
    const btn = document.querySelector(".reader-top-left .icon-btn");
    realClick(btn);
    await waitFor(() => overviewEl(), "总览浮层");
    await waitFor(() => (poItems() === PAGES ? true : null), `${PAGES} 个格子`);
    result.poItems = poItems();

    // 让「生成全部缩略图」那条串行循环跑一阵
    await settle(300);
    result.rendersAfterOpen = window.__thumbs.length;
    result.poImgsAfterOpen = poImgs();
    result.progressAfterOpen = document.querySelector(".po-progress")?.textContent.trim() ?? null;
    result.uniquePagesRendered = new Set(window.__thumbs).size;

    // ---- 把网格滚到底：可视区落在第 ~127–150 页，阅读位置仍在第 1 页
    //
    // ⚠️ **要分档滚，不能一步跳到底。** `open_all_thumbs` 不再逐页渲染整本之后，
    // 打开时只有可视区那一片（实测 24 张）会被请求；一步跳到底再加 24 张 = 48，
    // **没到 `PAGE_THUMB_LIMIT = 60`** —— 淘汰根本不会触发，这套就测不到东西了
    // （主判据会假绿）。分档滚动才把 store 推过上限，让"淘汰焦点"真正参与进来。
    stage("scroll-grid");
    const g = grid();
    for (const frac of [0.25, 0.5, 0.75, 1]) {
      g.scrollTop = g.scrollHeight * frac;
      g.dispatchEvent(new Event("scroll"));
      await settle(60);
    }
    result.scrollTop = Math.round(g.scrollTop);
    result.scrollHeight = Math.round(g.scrollHeight);
    result.pageInput = document.querySelector(".page-input")?.value ?? null;
    result.poImgsAtBottom = poImgs();

    // a) 给足时间让**看得见**的格子拿到图
    result.visibleAtScroll = visiblePages().length;
    let settled = false;
    try {
      await waitFor(() => (visibleMissing() === 0 ? true : null), "网格可视区的缩略图", 1200);
      settled = true;
    } catch (_) {}
    result.visibleSettled = settled;
    result.visibleMissing = visibleMissing();
    result.poImgsAfterScroll = poImgs();

    // b) 静止不动（不再滚、不再点）时，渲染次数还会不会涨
    stage("idle");
    const a = window.__thumbs.length;
    await settle(250);
    const b = window.__thumbs.length;
    result.rendersIdle = b - a;
    result.visibleMissingAfter = visibleMissing();
    result.totalRenders = b;

    // ---- 配对：总览还能用（点一个看得见的格子应该跳过去）
    //
    // ⚠️ 目标要挑**真的能点**的那个：`visiblePages()` 带 ±240 的余量，最上面那一
    // 行可能有一半被网格裁掉了，`elementFromPoint` 根本命不中它（用户也点不到）。
    // 取"离网格中心最近、且中心点确实落在网格可视区内"的有图格子。
    stage("jump");
    const gr = grid().getBoundingClientRect();
    const cx = gr.left + gr.width / 2;
    const cy = gr.top + gr.height / 2;
    let best = null;
    let bestDist = Infinity;
    for (const el of grid().querySelectorAll(".po-item")) {
      if (!el.querySelector(".po-frame img")) continue;
      const r = el.getBoundingClientRect();
      const x = r.left + r.width / 2;
      const y = r.top + r.height / 2;
      if (y < gr.top || y > gr.bottom || x < gr.left || x > gr.right) continue;
      const dist = Math.abs(y - cy) + Math.abs(x - cx);
      if (dist < bestDist) {
        bestDist = dist;
        best = el;
      }
    }
    const target = best ? Number(best.getAttribute("data-page")) : null;
    result.jumpTarget = target;
    if (best) {
      const hit = realClick(best);
      result.jumpHitIsSelf = hit === best || (hit && best.contains(hit));
      await settle(24);
      result.pageAfterJump = document.querySelector(".page-input")?.value ?? null;
      result.overviewAfterJump = !!overviewEl();
    }

    // ---- 收尾：**无条件**把总览关掉，让页面能闲下来。
    //
    // ⚠️ 这一步不是为了断言，是为了让 Chrome 能退出。补齐那条 effect 挂在
    // 版本号上，总览开着的时候它每来一张图就重跑一次；如果主判据红了（可视区
    // 一直没有图），上面的跳页就不会发生、浮层一直盖着 → 循环一直转 →
    // Chrome 会把整个虚拟时间预算烧光（实测 600s 预算跑满 300s 真实超时）。
    // 关掉浮层 → `use_effect_with` 里那句 `if *open` 直接短路 → 循环停。
    stage("drain");
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
    );
    await settle(40);
    result.overviewClosedForDrain = !overviewEl();
    stage("done");
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
