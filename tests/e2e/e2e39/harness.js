// e2e39：书架缩略图那条 effect 的**陈旧任务**会不会继续跑。
//
// 背景：那条 effect 的依赖是 `(*recent).clone()`，每变一次就起一条新任务
// （串行渲染每份的首页预览）。**旧任务不会自己停**，而它手里那份 `known` 是它
// 启动那一刻的快照 —— 里面可能有已经被删掉/被上限挤掉的 id。它接着
// `thumbs.set(known.clone())` 就把那些条目的 data URL（约 20–30 KB 一张）又塞回
// map 里，而这时"丢掉不在列表里的条目"那句 `retain` 早就跑完了：要等**下一次**
// `recent` 变化才会再清一遍（自愈，但中间一直在漏），而且旧任务还在白渲染
// 已经删掉的那份文件的首页。
//
// 怎么把"旧任务还在跑"变成可观测的：数 `renderThumbnail` 的调用**按路径**分。
// 删掉最后一份（它排在旧任务渲染顺序的末尾，此刻一定还没轮到）之后，
// 只要还出现**它**的渲染调用，就说明旧任务没退场。
//
// 只桩 Tauri IPC 与 `renderThumbnail`；右击菜单、应用、wasm 全走真实现。

const SHELF_W = 320; // 书架卡片缩略图的宽度（跟 `app.rs` 里那处调用一致）
const COUNT = 8;
const DOCS = Array.from({ length: COUNT }, (_, i) => {
  const n = i + 1;
  return {
    id: `doc-f${n}.pdf`,
    path: `/tmp/f${n}.pdf`,
    name: `f${n}.pdf`,
    // 越大越靠前（列表按 opened_at 倒序渲染）
    opened_at: (COUNT - i) * 1000,
  };
});
const LAST = DOCS[DOCS.length - 1];

window.__invokes = [];
window.__saved = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: DOCS.map((d) => ({
              ...d,
              size: 24000,
              pages: 12,
              last_page: 1,
              favorite: false,
            })),
            lastPath: null,
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
  event: { listen: () => Promise.resolve(() => {}) },
};

const PAGE_W = 595;
const PAGE_H = 842;

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
const result = { stage: "module", count: COUNT };
if (!bridge) {
  result.error = "bridge.js 没有先执行：桩的插入位置不对";
}

// 副作用计数：按**路径**记下每一次书架缩略图渲染。
//
// ⚠️ **先装桩、再包计数** —— 反过来的话 `Object.assign` 会把计数那层覆盖掉
// （第一次写就是这么栽的：`__shelf` 一直是空的）。
// ⚠️ 而且只包一层、保留真实现（上面那个桩），整个换掉就等于在测自己的桩。
window.__shelf = [];
if (bridge) {
  Object.assign(bridge, {
    openDocumentPath: () =>
      Promise.resolve(JSON.stringify({ ok: true, cancelled: true, error: null, meta: null })),
    getPageSizes: () => Promise.resolve("[]"),
    getOutline: () => Promise.resolve("[]"),
    search: () => Promise.resolve("[]"),
    // 故意慢一点：给"删掉末尾那一份"留出足够宽的窗口，让判据不靠运气。
    renderThumbnail: (_path, _page, maxWidth) =>
      new Promise((resolve) => setTimeout(() => resolve(makeThumb(maxWidth)), 60)),
    renderPage: () => Promise.resolve(""),
    renderTextLayer: () => Promise.resolve(""),
  });
  const real = bridge.renderThumbnail.bind(bridge);
  bridge.renderThumbnail = (path, page, maxWidth) => {
    if (maxWidth === SHELF_W) window.__shelf.push(String(path));
    return real(path, page, maxWidth);
  };
}

// ---------------------------------------------------------------- 驱动

function stage(name) {
  result.stage = name;
  try {
    document.body.setAttribute("data-e2e39-stage", name);
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

const settle = async (ticks = 12) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

function fire(el, type, opts = {}) {
  if (!el) throw new Error(`fire(${type}): 元素不存在`);
  const ev = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    view: window,
    detail: 1,
    ...opts,
  });
  el.dispatchEvent(ev);
  return ev;
}

const cards = () => [...document.querySelectorAll(".file-card")];
const cardById = (id) => document.querySelector(`.file-card[data-card-id="${id}"]`);
const menuEl = () => document.querySelector('[data-ctx-menu="card"]');
const menuItems = () => [...document.querySelectorAll('[data-ctx-menu="card"] .ctx-menu-item')];
const itemByText = (text) => menuItems().find((b) => b.textContent.trim() === text);
const rendersOf = (path) => window.__shelf.filter((p) => p === path).length;
const savedNames = () => {
  const s = window.__saved[window.__saved.length - 1];
  return s && s.recentFiles ? s.recentFiles.map((f) => f.name) : null;
};

/// 点菜单项必须是 `mousedown`（菜单项挂的是 `onmousedown`，document 上"点哪儿都关"
/// 也是 mousedown）—— 发 `click` 等它轮到，菜单已经被摘掉了。
function pressItem(text) {
  const btn = itemByText(text);
  if (!btn) throw new Error(`菜单里没有「${text}」`);
  fire(btn, "mousedown");
}

async function main() {
  try {
    if (result.error) throw new Error(result.error);

    stage("shelf");
    await waitFor(() => cards().length === COUNT, `${COUNT} 张卡片`);
    result.cardsBefore = cards().length;

    // ---- 等旧任务渲染出 2 张（它此刻在按顺序渲染第 3 份）
    stage("wait-renders");
    await waitFor(() => (window.__shelf.length >= 2 ? true : null), "书架缩略图渲染", 1200);
    result.rendersBeforeRemove = window.__shelf.length;
    result.lastRendersBefore = rendersOf(LAST.path);

    // ---- 删掉**最后一份**（旧任务还没轮到它）
    stage("remove-last");
    const card = cardById(LAST.id);
    if (!card) throw new Error(`找不到 ${LAST.id} 的卡片`);
    const r = card.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    fire(card, "mousedown", { button: 2, buttons: 2, clientX: x, clientY: y });
    fire(card, "contextmenu", { button: 2, buttons: 2, clientX: x, clientY: y });
    await waitFor(() => menuEl(), "右键菜单");
    result.menuLabels = menuItems().map((b) => b.textContent.trim());
    pressItem("删除记录");
    // 删完立刻记一次 —— 下面只比"这一瞬间之后"的**增量**。
    await settle(10);
    result.cardsAfter = cards().length;
    result.lastRendersAtRemove = rendersOf(LAST.path);

    // ---- 给旧任务足够时间跑完它自己那份列表（8 × (60+16) ≈ 610ms）
    stage("drain");
    await settle(90);
    result.lastRendersAfter = rendersOf(LAST.path);
    result.shelfRendersAfter = window.__shelf.length;

    // ---- 配对：剩下那 7 份照样渲染出来了（不是把整条 effect 干掉了）
    result.keptRendered = DOCS.slice(0, COUNT - 1).map((d) => rendersOf(d.path));
    result.savedNames = savedNames();

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
