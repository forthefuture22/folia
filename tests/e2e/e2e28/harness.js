// 端到端验证：**正文里右击一条批注**能不能删掉它（/tmp/e2e28）。
//
// 和 e2e17 的区别：那边是「先选字、再点浮层按钮把批注做出来」，
// 这边不关心怎么造出来的 —— 直接让 `load_annotations` 返回四条现成的
// （四种样式各一条），专心测右击菜单。
//
// 为什么必须在**真实应用**里测而不是单测：命中测试是纯几何
// （把鼠标的视口坐标换算成页面内归一化坐标，再和 `Highlight::rects` 比），
// 换算里用到的页面矩形只有真排版才有 —— 单测复述一遍等于什么都没验。

const PAGE_W = 595;
const PAGE_H = 842;
const RATIO = PAGE_W / PAGE_H;
const META = {
  id: "harness",
  path: "/tmp/harness.pdf",
  name: "harness.pdf",
  size: 24000,
  numPages: 1,
};

// 四条批注，四种样式，纵向错开 —— 这样"右击哪条就删哪条"才有对照。
// 矩形是**归一化**坐标（0..1），缩放无关，正是 `Highlight::rects` 的契约。
const KIND_LABELS = ["高亮", "下划线", "删划线", "波浪线"];
const KINDS = ["highlight", "underline", "strike", "wavy"];
const ANNOTS = KINDS.map((kind, i) => ({
  id: `ann-${kind}`,
  page: 1,
  color: ["yellow", "green", "blue", "pink"][i],
  text: `第 ${i + 1} 条被标注的原文（${kind}）`,
  note: `第 ${i + 1} 条批注内容`,
  kind,
  rects: [{ x: 0.1, y: 0.06 + i * 0.1, w: 0.35, h: 0.035 }],
  created_at: 0,
}));

const LABEL_TO_KIND = Object.fromEntries(KIND_LABELS.map((l, i) => [l, KINDS[i]]));

// ---------------------------------------------------------------- 桩

// 探针自己出问题也要留下痕迹。挂在 `documentElement` 上 —— 不能挂 `body`：
// Yew 的 `mount_with_props` 一上来就 `clear_element(document.body)`，
// 在 wasm 挂载完成**之前**往 body 里塞的任何东西都会被静默抹掉。
function bail(label, detail) {
  let pre = document.getElementById("harness-error");
  if (!pre) {
    pre = document.createElement("pre");
    pre.id = "harness-error";
    document.documentElement.appendChild(pre);
  }
  pre.textContent += `[${label}] ${detail}\n`;
}
window.addEventListener("error", (e) => bail("error", `${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener("unhandledrejection", (e) =>
  bail("unhandledrejection", String((e.reason && e.reason.stack) || e.reason))
);

// 关掉 CSS 动画：headless 的 `--virtual-time-budget` 不驱动动画时钟，
// `.ctx-menu` 的入场动画会永远卡在 `scale(0.97)`，量出来的尺寸全部偏小 3%
// （208 量成 201.76），看着像常量写错了。详见 /tmp/e2e27/harness.js 里那段长注释。
{
  const noAnim = document.createElement("style");
  noAnim.textContent =
    "*, *::before, *::after { animation: none !important; transition: none !important; }";
  document.head.appendChild(noAnim);
}

window.__invokes = [];
window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              {
                id: META.id,
                path: META.path,
                name: META.name,
                size: META.size,
                pages: 1,
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
      if (cmd === "load_annotations") return Promise.resolve(JSON.stringify(ANNOTS));
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      return Promise.resolve(null);
    },
  },
  event: { listen: () => Promise.resolve(() => {}) },
};

const bridge = window.FoliaBridge;
if (!bridge) throw new Error("bridge.js 没有先执行：桩的插入位置不对");

function drawPage(canvasId, scale) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  canvas.width = Math.round(PAGE_W * scale);
  canvas.height = Math.round(PAGE_H * scale);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
}

function makeThumb(maxWidth) {
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(maxWidth);
  canvas.height = Math.round(maxWidth / RATIO);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg");
}

Object.assign(bridge, {
  openDocumentPath: () =>
    Promise.resolve(JSON.stringify({ ok: true, cancelled: false, error: null, meta: META })),
  getPageSizes: () => Promise.resolve(JSON.stringify([{ width: PAGE_W, height: PAGE_H }])),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: (_path, _page, maxWidth) => Promise.resolve(makeThumb(maxWidth)),
  renderPage: (canvasId, _page, scale) => {
    drawPage(canvasId, scale);
    return Promise.resolve("");
  },
  // 文本层留空：这一套只关心标注层，不需要真的文字。
  renderTextLayer: () => Promise.resolve(""),
});

// ---------------------------------------------------------------- 驱动

const CASE = new URLSearchParams(location.search).get("case") || "verify";

const result = {};
const T = [];
const check = (name, pass, detail) =>
  T.push({ name, pass: !!pass, detail: detail === undefined ? null : detail });

const round = (n) => Math.round(n * 100) / 100;
const settle = async (ticks = 8) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};
const waitFor = (predicate, label, maxTicks = 4000) =>
  new Promise((resolve, reject) => {
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

const menuEl = () => document.querySelector('[data-ctx-menu="mark"]');
const menuItems = () => [...document.querySelectorAll('[data-ctx-menu="mark"] .ctx-menu-item')];
const menuLabels = () => menuItems().map((b) => b.textContent.trim());
const itemByText = (t) => menuItems().find((b) => b.textContent.trim() === t);
const markEls = () => [...document.querySelectorAll(".page .hl-layer > .folia-mark")];
const savedAnnots = () => {
  const saves = window.__invokes.filter((c) => c.cmd === "save_annotations");
  if (!saves.length) return null;
  try {
    return JSON.parse(saves[saves.length - 1].args.data);
  } catch (_) {
    return null;
  }
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

/// 在 `page` 上右击某个点（真实浏览器发 `mousedown(button=2)` → `contextmenu`）。
///
/// ⚠️ 坐标要**先取整**：`MouseEvent.clientX/clientY` 在 DOM 里是 `long`，
/// 传 603.13 进去、应用读出来是 603。拿没取整的值去比对，
/// 会得到"菜单偏了 0.13px"这种假失败（判据本身错了，不是代码错了）。
function rightClick(page, x, y) {
  const cx = Math.round(x);
  const cy = Math.round(y);
  fire(page, "mousedown", { button: 2, buttons: 2, clientX: cx, clientY: cy });
  return fire(page, "contextmenu", { button: 2, buttons: 2, clientX: cx, clientY: cy });
}

function pressItem(text) {
  const btn = itemByText(text);
  if (!btn) throw new Error(`菜单里没有「${text}」，当前是 ${JSON.stringify(menuLabels())}`);
  fire(btn, "mousedown");
}

function rectOf(el) {
  const r = el.getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height, right: r.right, bottom: r.bottom };
}

/// 某条标注（按 kind）在视口里的中心点。
function centerOf(kind) {
  const el = document.querySelector(`.page .hl-layer > .folia-mark-${kind}`);
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: (r.left + r.right) / 2, y: (r.top + r.bottom) / 2, rect: rectOf(el) };
}

// 和 `src/app.rs` 的常量一一对应（故意重抄一遍，改一边忘另一边就红）。
const MENU_W = 208;
const MENU_ITEM_H = 32;
const MENU_PAD = 12;
const MENU_SEP_H = 1;
const MENU_BORDER = 2;
const MARK_MENU_ITEMS = 2;
const MARK_MENU_H = MENU_ITEM_H * MARK_MENU_ITEMS + MENU_PAD + MENU_SEP_H + MENU_BORDER;

/// 截图专用：把界面摆到「右击了一条标注、菜单开着」的状态就停下，交给 `--screenshot` 拍。
/// `?case=shot&kind=underline` 选右击哪一条（默认下划线）。
///
/// 会把 `.page-canvas` **藏起来**：无头 `--screenshot` 下 canvas 会合成成一片黑
/// （见 README「别用 headless --screenshot 去证伪计算样式」一节）。
/// 用 `visibility: hidden` 而不是 `display: none` —— 布局不能动，
/// 否则标注矩形的位置会跟着变，拍出来的就不是真实位置了。
async function shotCase() {
  const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
  card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  await waitFor(() => document.querySelector(".stage"), "阅读器出现");
  const page = await waitFor(() => document.querySelector('.page[data-page="1"]'), "页面出现");
  await waitFor(() => (markEls().length === 4 ? markEls() : null), "四条批注出现");
  await settle(20);

  const hide = document.createElement("style");
  hide.textContent = ".page-canvas { visibility: hidden !important; }";
  document.head.appendChild(hide);

  const kind = new URLSearchParams(location.search).get("kind") || "underline";
  const at = centerOf(kind);
  if (!at) throw new Error(`找不到标注 ${kind}`);
  rightClick(page, at.x, at.y);
  await settle(12);
  window.__SHOT_READY__ = true;
}

async function main() {
  try {
    // ---- 1. 进阅读器
    const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    const page = await waitFor(() => document.querySelector('.page[data-page="1"]'), "页面出现");
    await settle(20);

    const marks = await waitFor(() => (markEls().length === 4 ? markEls() : null), "四条批注出现");
    result.markClasses = marks.map((el) => el.className);
    check("四条现成批注都画出来了", marks.length === 4, marks.length);
    check(
      "四种样式的类名各自正确",
      KINDS.every((k) => document.querySelector(`.page .hl-layer > .folia-mark-${k}`)),
      result.markClasses
    );

    // ---- 2. 右击每一条 → 菜单文案必须跟着这条的**样式**走
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    result.viewport = { vw, vh };

    for (const kind of KINDS) {
      const at = centerOf(kind);
      const ev = rightClick(page, at.x, at.y);
      await settle(8);
      const label = KIND_LABELS[KINDS.indexOf(kind)];
      check(`右击「${label}」→ 菜单打开`, menuEl() !== null, menuEl() && menuEl().outerHTML.slice(0, 60));
      check(`右击「${label}」→ contextmenu 被 preventDefault`, ev.defaultPrevented);
      check(
        `右击「${label}」→ 文案是「编辑批注 / 删除${label}」`,
        JSON.stringify(menuLabels()) === JSON.stringify(["编辑批注", `删除${label}`]),
        menuLabels()
      );
      check(
        `右击「${label}」→ 菜单落在光标处`,
        Math.abs(rectOf(menuEl()).x - Math.round(at.x)) < 0.6 &&
          Math.abs(rectOf(menuEl()).y - Math.round(at.y)) < 0.6,
        {
          x: round(rectOf(menuEl()).x),
          y: round(rectOf(menuEl()).y),
          want: { x: Math.round(at.x), y: Math.round(at.y) },
        }
      );
      // 关掉再测下一条
      fire(document.body, "mousedown");
      await settle(6);
    }

    // ---- 3. 常量守卫（只在最后一条上量一次就够，尺寸和内容无关）
    {
      const at = centerOf("highlight");
      rightClick(page, at.x, at.y);
      await settle(8);
      const r = rectOf(menuEl());
      check(`宽度 == MENU_W(${MENU_W})`, Math.abs(r.w - MENU_W) < 0.6, round(r.w));
      check(
        `高度 == 项高×${MARK_MENU_ITEMS} + 内边距 + 分隔线 + 边框(${MARK_MENU_H})`,
        Math.abs(r.h - MARK_MENU_H) < 0.6,
        round(r.h)
      );
      const hs = menuItems().map((b) => round(b.getBoundingClientRect().height));
      check(
        `每项高度都 == MENU_ITEM_H(${MENU_ITEM_H})`,
        hs.length === MARK_MENU_ITEMS && hs.every((h) => Math.abs(h - MENU_ITEM_H) < 0.6),
        hs
      );
      check("同时只存在一个菜单", document.querySelectorAll('[data-ctx-menu="mark"]').length === 1,
            document.querySelectorAll('[data-ctx-menu="mark"]').length);

      // ---- 4. 右击正文空白处：不该弹菜单，但系统默认菜单要被**全局兜底**关掉
      fire(document.body, "mousedown");
      await settle(6);
      const blankEv = rightClick(page, page.getBoundingClientRect().left + 8,
                                 page.getBoundingClientRect().bottom - 8);
      await settle(8);
      check("右击正文空白处不弹菜单", menuEl() === null, menuEl() && menuEl().outerHTML.slice(0, 60));
      check("右击正文空白处：系统默认菜单被关掉", blankEv.defaultPrevented);

      // ---- 4a. 兜底是 document 级的 —— 右击"没人管右键"的地方也一样
      //      （`.stage` 是阅读区滚动容器，它自己没有任何 contextmenu 处理器）
      const stage = document.querySelector(".stage");
      check("找到 .stage（下面那条断言才有意义）", !!stage);
      const stageEv = fire(stage, "contextmenu", { button: 2, buttons: 2, clientX: 400, clientY: 400 });
      await settle(4);
      check("右击阅读区空白容器：系统默认菜单同样被关掉", stageEv.defaultPrevented);

      // ---- 4b. **例外**：可编辑区域要留住系统菜单里的「粘贴」
      const pageInput = document.querySelector(".page-input");
      check("找到页码输入框（下面那条断言才有意义）", !!pageInput);
      const inputEv = fire(pageInput, "contextmenu", { button: 2, buttons: 2, clientX: 0, clientY: 0 });
      await settle(4);
      check("右击输入框：**不**拦系统菜单（要留住「粘贴」）", !inputEv.defaultPrevented);

      // ---- 5. Esc 关闭
      const at2 = centerOf("wavy");
      rightClick(page, at2.x, at2.y);
      await settle(8);
      check("重新打开成功", menuEl() !== null);
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
      );
      await settle(8);
      check("Esc 关闭批注菜单", menuEl() === null, menuEl() && menuEl().outerHTML.slice(0, 60));

      // ---- 6. 在 A 上右击、再在 B 上右击：一次就换过去（不用点两下）
      const a = centerOf("highlight");
      const b = centerOf("strike");
      rightClick(page, a.x, a.y);
      await settle(6);
      check("先在「高亮」上右击", menuLabels()[1] === "删除高亮", menuLabels());
      rightClick(page, b.x, b.y);
      await settle(8);
      check("再在「删划线」上右击 → 菜单只剩一个", document.querySelectorAll('[data-ctx-menu="mark"]').length === 1);
      check("且内容已换成「删划线」", menuLabels()[1] === "删除删划线", menuLabels());
    }

    // ---- 7. 真的删掉一条
    const before = savedAnnots();
    result.savedBefore = before ? before.map((a) => a.kind) : null;

    const target = centerOf("strike");
    rightClick(page, target.x, target.y);
    await settle(8);
    pressItem("删除删划线");
    await settle(12);

    check("点「删除删划线」后菜单关闭", menuEl() === null);
    check("DOM 里只剩三条标记", markEls().length === 3, markEls().length);
    check(
      "删掉的正是「删划线」那条",
      document.querySelector(".page .hl-layer > .folia-mark-strike") === null,
      markEls().map((el) => el.className)
    );
    check(
      "另外三条都还在",
      ["highlight", "underline", "wavy"].every((k) =>
        document.querySelector(`.page .hl-layer > .folia-mark-${k}`)
      ),
      markEls().map((el) => el.className)
    );
    const after = savedAnnots();
    result.savedAfter = after ? after.map((a) => a.kind) : null;
    check("改动被落盘（save_annotations 收到了新列表）", !!after, !!after);
    check(
      "落盘的列表里没有删划线了，另外三条在",
      !!after && after.length === 3 && !after.some((a) => a.kind === "strike"),
      result.savedAfter
    );
    check(
      "落盘的 id 就是被删那条之外的三个",
      !!after && JSON.stringify(after.map((a) => a.id).sort()) ===
        JSON.stringify(["ann-highlight", "ann-underline", "ann-wavy"]),
      after && after.map((a) => a.id)
    );

    // ---- 8. 「编辑批注」→ 侧栏打开并选中这一条
    const note = centerOf("underline");
    rightClick(page, note.x, note.y);
    await settle(8);
    pressItem("编辑批注");
    await settle(14);
    check("点「编辑批注」后菜单关闭", menuEl() === null);
    const active = document.querySelector(".ann-item.active");
    result.activeAnn = active ? active.textContent.replace(/\s+/g, " ").trim() : null;
    check("侧栏里选中了「下划线」那一条", !!active && result.activeAnn.includes("下划线"), result.activeAnn);
    check(
      "选中项显示的是这条批注的内容",
      !!active && result.activeAnn.includes("第 2 条批注内容"),
      result.activeAnn
    );
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  } finally {
    result.tests = T;
    result.failed = T.filter((t) => !t.pass).length;
    result.total = T.length;
    const pre = document.createElement("pre");
    pre.id = "harness-result";
    pre.textContent = JSON.stringify(result, null, 2);
    document.body.appendChild(pre);
    window.__HARNESS_DONE__ = true;
  }
}

if (CASE === "shot") {
  shotCase().catch((err) => bail("shot", String((err && err.stack) || err)));
} else {
  main();
}
