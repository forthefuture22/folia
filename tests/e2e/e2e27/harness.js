// 探针：卡片上「hover 有时候看不到文件名」到底是怎么回事（/tmp/e2e27）。
//
// 思路：原生 `title` 提示框由**光标下那个元素**决定 —— 浏览器从该元素往上找
// 第一个有非空 `title` 的祖先。所以只要用 `document.elementFromPoint` 沿着
// 卡片各处采样一遍，看每一处"最终会显示哪个 title"，就知道哪块区域是坏的。
//
// `?case=` 切换：
//   probe  —— 采样并报告几何关系（默认）
//   menu   —— 右击菜单的行为（见下面的驱动）

const PAGES = 12;
const PAGE_W = 595;
const PAGE_H = 842;

// 探针自己出问题（语法错、顶层抛异常、未捕获的 rejection）时也要留下痕迹 ——
// 否则 `--dump-dom` 里只会看到"页面正常渲染了、但没有 harness-result"，
// 完全猜不到是驱动脚本挂了。
//
// ⚠️ 挂在 `documentElement` 上，**不能挂 `document.body`**：
// Yew 的 `AppHandle::mount_with_props` 一上来就 `clear_element(document.body)`，
// 把 body 的子节点**全删光**（`yew-0.21.0/src/app_handle.rs:85` 那个 `while let Some(child)
// = host.last_child() { remove_child }`）。所以在 wasm 挂载完成**之前**往 body 里塞任何东西，
// 都会被静默抹掉 —— 包括结果本身。`harness-result` 也必须等应用渲染出来之后再塞。
// 这个坑踩过一次：`menuCase` 忘了 `waitFor`，`finish()` 抢先 append，结果整个
// `<pre id="harness-result">` 人间蒸发，dump 出来只剩一片正常渲染的书架。
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

// 关掉所有 CSS 动画 / 过渡。
//
// ⚠️ headless 的 `--virtual-time-budget` **不驱动 CSS 动画时钟**：入场动画会永远卡在
// 第一帧。`.ctx-menu` 的 `folia-menu-in` 第一帧是 `transform: scale(0.97)`，
// 于是 `getBoundingClientRect()` 量到的是缩放中的中间态 —— 208px 宽量成 201.76、
// 32px 的项量成 31.04、位置偏 3.12px、底边偏 2.14px，全都"差一点点"，
// 看着特别像 Rust 侧常量写错了。
//
// 更坑的是：想"等动画跑完再量"也等不到 —— `animation.finished` 永远不 resolve，
// 直接 await 会把整个驱动挂死（踩过一次，dump 出来只有半截 DOM）。
//
// 判据要的是**最终布局**，不是动画过程，所以干脆把动画停掉。
// 真实浏览器里动画照常跑，这里只影响测量。
// 挂在 `head` 上而不是 `body` —— 见 `bail()` 上面那段 `clear_element` 的说明。
{
  const noAnim = document.createElement("style");
  noAnim.textContent =
    "*, *::before, *::after { animation: none !important; transition: none !important; }";
  document.head.appendChild(noAnim);
}

bail("boot", "harness 脚本开始执行");

const CASE = new URLSearchParams(location.search).get("case") || "probe";

const LONG_NAME =
  "Java 热门面试题 200 道速记通关版 _ 面试刷题 mianshiya.com_加水印_最终版_v3.pdf";
const SHORT_NAME = "短名字.pdf";

const RECENTS = [
  {
    id: "doc-long.pdf",
    path: "/tmp/long.pdf",
    name: LONG_NAME,
    opened_at: Date.now() - 3600_000,
    favorite: false,
  },
  // 故意让第二张**一开始就是收藏状态**：这样"右击哪张卡、菜单就显示哪张卡的文案"
  // 才有对照 —— 右击 long 该是「收藏」，右击 short 该是「取消收藏」。
  {
    id: "doc-short.pdf",
    path: "/tmp/short.pdf",
    name: SHORT_NAME,
    opened_at: 0,
    favorite: true,
  },
];

window.__invokes = [];
window.__saved = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: RECENTS.map((r) => ({
              ...r,
              size: 24000,
              pages: PAGES,
              last_page: 1,
            })),
            lastPath: RECENTS[0].path,
            lastPage: 1,
            scale: 1,
          })
        );
      }
      if (cmd === "save_app_state") {
        try {
          window.__saved.push(JSON.parse((args && args.data) || "{}"));
        } catch (_) {}
        return Promise.resolve(null);
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      if (cmd === "set_default_pdf_handler") return Promise.resolve(null);
      if (cmd === "reveal_in_finder") return Promise.resolve(null);
      if (cmd === "trash_file") return Promise.resolve(null);
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
if (!bridge) throw new Error("bridge.js 没有先执行：桩的插入位置不对");

Object.assign(bridge, {
  openDocumentPath: () =>
    Promise.resolve(JSON.stringify({ ok: true, cancelled: false, error: null, meta: null })),
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

const cards = () => [...document.querySelectorAll(".file-card")];
const round = (n) => Math.round(n * 100) / 100;

/// 浏览器决定 `title` 提示的方式：从光标下的元素往上找第一个非空 title。
function effectiveTitle(el) {
  let node = el;
  while (node && node !== document.documentElement) {
    const t = node.getAttribute && node.getAttribute("title");
    if (t) return { from: describe(node), title: t };
    node = node.parentElement;
  }
  return { from: describe(el), title: null };
}

function describe(el) {
  if (!el) return "null";
  const cls = (el.className && String(el.className)) || "";
  return cls ? `${el.tagName.toLowerCase()}.${cls.split(" ").join(".")}` : el.tagName.toLowerCase();
}

// ---------------------------------------------------------------- 右击菜单驱动

const LONG = "doc-long.pdf";
const SHORT = "doc-short.pdf";

// 和 `src/app.rs` 里的 `CARD_MENU_*` 常量一一对应。**故意在这里重抄一遍**：
// 抄错了/改了一边忘了另一边，判据就会红 —— 这才是"常量守卫"的意义。
// 高度 = 项高×条数 + 上下内边距 + 分隔线 + 上下边框（全局 `box-sizing: border-box`
// 管不到"没写 height、由内容撑开"的元素，所以 2px 边框必须算）。
const MENU_W = 208;
const MENU_ITEM_H = 32;
const MENU_PAD = 12;
const MENU_SEP_H = 1;
const MENU_BORDER = 2;
const MENU_ITEMS = 4;
const MENU_H = MENU_ITEM_H * MENU_ITEMS + MENU_PAD + MENU_SEP_H + MENU_BORDER;

const EXPECT_LABELS = ["收藏", "在访达中显示", "删除记录", "删除记录和文件"];

const invokesOf = (cmd) => window.__invokes.filter((i) => i.cmd === cmd);
const savedLast = () => (window.__saved.length ? window.__saved[window.__saved.length - 1] : null);

const cardById = (id) => document.querySelector(`.file-card[data-card-id="${id}"]`);
const menuEl = () => document.querySelector('[data-ctx-menu="card"]');
const menuItems = () => [...document.querySelectorAll('[data-ctx-menu="card"] .ctx-menu-item')];
const menuLabels = () => menuItems().map((b) => b.textContent.trim());
const itemByText = (text) => menuItems().find((b) => b.textContent.trim() === text);
const confirmEl = () => document.querySelector('[data-confirm="1"]');
const confirmOk = () => document.querySelector('[data-confirm-ok="1"]');

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

/// 右击。真实浏览器发的是 `mousedown(button=2)` → `contextmenu`，两个都补上 ——
/// 顺带验证"在 A 上右击、再在 B 上右击"能一次换过去
/// （document 上那个"点哪儿都关菜单"的 mousedown 会把 A 的菜单关掉，
/// 紧接着 B 的 contextmenu 又开一个，最终只剩 B 的）。
function rightClick(el, x, y) {
  fire(el, "mousedown", { button: 2, buttons: 2, clientX: x, clientY: y });
  return fire(el, "contextmenu", { button: 2, buttons: 2, clientX: x, clientY: y });
}

/// 点菜单项。**必须是 `mousedown`** —— 菜单项挂的就是 `onmousedown`，
/// 而 document 上"点哪儿都关"的监听也是 mousedown；发 `click` 的话
/// 等它轮到，菜单已经被重渲染摘掉了，动作根本不会触发。
function pressItem(text) {
  const btn = itemByText(text);
  if (!btn) throw new Error(`菜单里没有「${text}」，当前是 ${JSON.stringify(menuLabels())}`);
  fire(btn, "mousedown");
}

function rectOf(el) {
  const r = el.getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height, right: r.right, bottom: r.bottom };
}

/// 等元素上的 CSS 动画跑完再量尺寸。
///
/// 正常情况下上面那段 `<style>` 已经把动画关掉了，这里是第二道保险 ——
/// 万一以后有人加了 `!important` 的动画或者用 WAAPI 起了一个。
///
/// ⚠️ 必须有超时兜底：headless 的虚拟时间不驱动动画时钟，
/// `animation.finished` 可能永远不 resolve，直接 await 会把整个驱动挂死。
async function animSettled(el, ms = 400) {
  const animations = (el && el.getAnimations && el.getAnimations()) || [];
  if (animations.length) {
    await Promise.race([
      Promise.all(animations.map((a) => a.finished.catch(() => {}))),
      new Promise((r) => setTimeout(r, ms)),
    ]);
  }
  await settle(3);
}

/// 卡片上还有没有悬浮的收藏 / 删除按钮。
///
/// 这两个按钮**已经删掉了** —— 收藏 / 从列表移除 / 删除记录和文件现在都在右键菜单里。
/// 留这个函数是为了把"删掉了"变成一个**被判据钉住的事实**，
/// 而不是"我记得删了"。
const hoverButtons = () =>
  document.querySelectorAll(".file-card .file-actions, .file-card .file-action").length;

const favoriteOf = (id) => {
  const s = savedLast();
  const item = s && (s.recentFiles || []).find((f) => f.id === id);
  return item ? item.favorite : null;
};

async function menuCase() {
  const T = [];
  const check = (name, pass, detail) =>
    T.push({ name, pass: !!pass, detail: detail === undefined ? null : detail });

  // 先把书架等出来 —— 不等的话 `cardById()` 全是 null，右击无处可发。
  await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
  await settle(20);
  bail("menu", "书架就绪");

  // ---- 1. 初始状态
  check("初始没有菜单", menuEl() === null, menuEl() && menuEl().outerHTML.slice(0, 80));
  check("书架上有两张卡", cards().length === 2, cards().length);
  // 收藏状态不再有 DOM 按钮可读 —— 卡片右下角那两颗浮起的小按钮已经删了。
  // 所以"收藏了没有"一律从右键菜单的文案 + 落盘 payload 看（见下面第 8 步）。
  check(
    "卡片上不再有悬浮的收藏 / 删除按钮",
    hoverButtons() === 0,
    hoverButtons()
  );
  check("卡片正面只剩「打开」一个动作（整张卡都是点击区）",
        cards().every((c) => c.getAttribute("onclick") === null && c.querySelectorAll("button").length === 0),
        cards().map((c) => c.querySelectorAll("button").length));
  bail("menu", "第 1 步（初始状态）完成");

  // ---- 2. 右击打开：位置 / 文案 / 拦掉系统菜单
  const evOpen = rightClick(cardById(LONG), 300, 200);
  await settle(4);
  check("右击后菜单出现", menuEl() !== null);
  check("contextmenu 被 preventDefault（不然 macOS 会叠一层系统菜单）", evOpen.defaultPrevented);
  check("菜单文案与顺序完全一致", JSON.stringify(menuLabels()) === JSON.stringify(EXPECT_LABELS), menuLabels());

  // ⚠️ 量几何之前必须等入场动画结束，否则量到的是 scale(0.97) 的中间态。
  await animSettled(menuEl());
  const r1 = rectOf(menuEl());
  check(
    "菜单落在光标处",
    Math.abs(r1.x - 300) < 0.6 && Math.abs(r1.y - 200) < 0.6,
    { x: round(r1.x), y: round(r1.y) }
  );

  // ---- 3. 常量守卫：渲染出来的真实尺寸必须和 Rust 侧常量对上
  check(`宽度 == CARD_MENU_W(${MENU_W})`, Math.abs(r1.w - MENU_W) < 0.6, round(r1.w));
  check(
    `高度 == 项高×${MENU_ITEMS} + 内边距 + 分隔线 + 边框(${MENU_H})`,
    Math.abs(r1.h - MENU_H) < 0.6,
    round(r1.h)
  );
  // 顺带把每一项的高度也量一遍 —— 项高算错的话总高也会错，
  // 但那样报出来的是"总高不对"，指不到具体哪一项。
  const itemHeights = menuItems().map((b) => round(b.getBoundingClientRect().height));
  check(
    `每项高度都 == CARD_MENU_ITEM_H(${MENU_ITEM_H})`,
    itemHeights.length === MENU_ITEMS && itemHeights.every((h) => Math.abs(h - MENU_ITEM_H) < 0.6),
    itemHeights
  );

  // ---- 4. 右击另一张卡：菜单只有一个，且内容换成新那张的
  rightClick(cardById(SHORT), 500, 300);
  await settle(4);
  check("同时只存在一个菜单", document.querySelectorAll('[data-ctx-menu="card"]').length === 1,
        document.querySelectorAll('[data-ctx-menu="card"]').length);
  await animSettled(menuEl());
  const r2 = rectOf(menuEl());
  check("菜单跟着光标移到新位置", Math.abs(r2.x - 500) < 0.6 && Math.abs(r2.y - 300) < 0.6,
        { x: round(r2.x), y: round(r2.y) });
  check("右击 short 后文案是「取消收藏」（菜单内容跟着换主人）",
        menuLabels()[0] === "取消收藏", menuLabels()[0]);
  rightClick(cardById(LONG), 300, 200);
  await settle(4);
  check("再右击 long 又变回「收藏」", menuLabels()[0] === "收藏", menuLabels()[0]);

  // ---- 5. Esc 关闭
  document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }));
  await settle(4);
  check("Esc 关闭菜单", menuEl() === null, menuEl() && menuEl().outerHTML.slice(0, 80));

  // ---- 6. 在别处按下鼠标关闭
  rightClick(cardById(LONG), 300, 200);
  await settle(4);
  check("重新打开成功", menuEl() !== null);
  fire(document.body, "mousedown");
  await settle(4);
  check("在别处按下鼠标后菜单关闭", menuEl() === null);

  // ---- 7. 夹取：贴右下角右击不能溢出屏幕
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  result.viewport = { vw, vh };
  rightClick(cardById(LONG), vw - 5, vh - 5);
  await settle(4);
  await animSettled(menuEl());
  const r3 = rectOf(menuEl());
  check(
    "右下角右击时菜单被完整夹进视口",
    r3.x >= 0 && r3.y >= 0 && r3.right <= vw + 0.5 && r3.bottom <= vh + 0.5,
    { menu: { x: round(r3.x), y: round(r3.y), right: round(r3.right), bottom: round(r3.bottom) }, viewport: { vw, vh } }
  );
  check("确实夹了（没原样放在光标处）", r3.x < vw - 6 && r3.y < vh - 6, { x: round(r3.x), y: round(r3.y) });
  check(
    "夹取后仍留了 8px 边距（右下角正好是 viewport - 8）",
    Math.abs(r3.right - (vw - 8)) < 0.6 && Math.abs(r3.bottom - (vh - 8)) < 0.6,
    { right: round(r3.right), bottom: round(r3.bottom), want: { right: vw - 8, bottom: vh - 8 } }
  );
  fire(document.body, "mousedown");
  await settle(4);

  // ---- 8. 收藏 → 取消收藏（含落盘）
  rightClick(cardById(LONG), 300, 200);
  await settle(4);
  check("未收藏时文案是「收藏」", menuLabels()[0] === "收藏", menuLabels()[0]);
  pressItem("收藏");
  await settle(6);
  check("点「收藏」后菜单关闭", menuEl() === null);
  check("点「收藏」后状态真的翻成已收藏（看落盘）", favoriteOf(LONG) === true, favoriteOf(LONG));
  // 再右击一次，文案必须跟着变 —— 这是"收藏生效了"的**用户可见**证据
  // （卡片上那颗星标已经删掉了，文案是现在唯一看得见的反馈）。
  rightClick(cardById(LONG), 300, 200);
  await settle(4);
  check("已收藏时文案变成「取消收藏」", menuLabels()[0] === "取消收藏", menuLabels()[0]);
  pressItem("取消收藏");
  await settle(6);
  check("点「取消收藏」后菜单关闭", menuEl() === null);
  check("取消收藏也落盘了", favoriteOf(LONG) === false, favoriteOf(LONG));
  check("取消收藏没有误伤另一张卡", favoriteOf(SHORT) === true, favoriteOf(SHORT));
  // 回到「收藏」页签看一眼：两张卡里只有 short 是收藏的
  {
    const segs = () => [...document.querySelectorAll(".seg")];
    const segByText = (t) => segs().find((s) => s.textContent.trim() === t);
    check("书架上有「最近 / 收藏」两个页签", segs().length === 2, segs().map((s) => s.textContent.trim()));
    fire(segByText("收藏"), "click");
    await settle(10);
    const favCards = cards().map((c) => c.getAttribute("data-card-id"));
    check("「收藏」页签里只剩 short 那张（long 刚被取消收藏）",
          favCards.length === 1 && favCards[0] === SHORT, favCards);
    fire(segByText("最近"), "click");
    await settle(10);
    check("切回「最近」还是两张卡", cards().length === 2, cards().length);
  }

  // ---- 9. 在访达中显示
  const revealBefore = invokesOf("reveal_in_finder").length;
  rightClick(cardById(SHORT), 400, 250);
  await settle(4);
  pressItem("在访达中显示");
  await settle(6);
  check("点「在访达中显示」后菜单关闭", menuEl() === null);
  const reveals = invokesOf("reveal_in_finder");
  check("调了 reveal_in_finder 且只多一次", reveals.length === revealBefore + 1, reveals.length);
  check(
    "reveal_in_finder 拿到的是被右击那张卡的路径",
    reveals.length > 0 && reveals[reveals.length - 1].args.path === "/tmp/short.pdf",
    reveals.length ? reveals[reveals.length - 1].args : null
  );

  // ---- 10. 删除记录：只摘记录，不碰文件
  const trashBefore = invokesOf("trash_file").length;
  rightClick(cardById(LONG), 300, 200);
  await settle(4);
  pressItem("删除记录");
  await settle(6);
  check("点「删除记录」后菜单关闭", menuEl() === null);
  check("「删除记录」后少一张卡", cards().length === 1, cards().length);
  check("删掉的正是被右击那张", cardById(LONG) === null && cardById(SHORT) !== null);
  check("「删除记录」**没有**动文件（没调 trash_file）",
        invokesOf("trash_file").length === trashBefore, invokesOf("trash_file").length);
  check("「删除记录」落盘了", (() => {
    const s = savedLast();
    const ids = s ? (s.recentFiles || []).map((f) => f.id) : null;
    return !!ids && ids.length === 1 && ids[0] === SHORT;
  })(), savedLast() && (savedLast().recentFiles || []).map((f) => f.id));

  // ---- 11. 删除记录和文件：先弹确认框，**点一下不能就走**
  rightClick(cardById(SHORT), 400, 250);
  await settle(4);
  pressItem("删除记录和文件");
  await settle(6);
  check("点「删除记录和文件」后菜单关闭", menuEl() === null);
  check("弹出了确认框", confirmEl() !== null);
  const nameEl = document.querySelector(".confirm-name");
  // 注意比的是 `RecentFile.name`（给人看的"短名字.pdf"），**不是** `id`
  // （"doc-short.pdf"，内部标识）。确认框要让用户认得出是哪份文件，所以用 name。
  check("确认框里原样写出文件名", !!nameEl && nameEl.textContent.trim() === SHORT_NAME,
        nameEl && nameEl.textContent.trim());
  check("确认前**没有**动文件", invokesOf("trash_file").length === trashBefore,
        invokesOf("trash_file").length);
  check("确认前记录还在", cards().length === 1, cards().length);
  check("确认框里有「取消」和「移到废纸篓」两个按钮",
        document.querySelectorAll(".confirm-actions .btn").length === 2,
        document.querySelectorAll(".confirm-actions .btn").length);

  // ---- 12. 取消：什么都不该发生
  const cancelBtn = [...document.querySelectorAll(".confirm-actions .btn")].find(
    (b) => !b.hasAttribute("data-confirm-ok")
  );
  check("找得到「取消」按钮", !!cancelBtn);
  fire(cancelBtn, "click");
  await settle(6);
  check("点「取消」后确认框关闭", confirmEl() === null);
  check("点「取消」后记录还在", cards().length === 1, cards().length);
  check("点「取消」后没动文件", invokesOf("trash_file").length === trashBefore,
        invokesOf("trash_file").length);

  // ---- 13. 确认：移进废纸篓 + 摘掉记录
  rightClick(cardById(SHORT), 400, 250);
  await settle(4);
  pressItem("删除记录和文件");
  await settle(6);
  check("再次弹出确认框", confirmEl() !== null);
  check("找得到「移到废纸篓」按钮", !!confirmOk());
  fire(confirmOk(), "click");
  await settle(8);
  const trashes = invokesOf("trash_file");
  check("确认后调了 trash_file 且只多一次", trashes.length === trashBefore + 1, trashes.length);
  check(
    "trash_file 拿到的是被右击那张卡的路径",
    trashes.length > 0 && trashes[trashes.length - 1].args.path === "/tmp/short.pdf",
    trashes.length ? trashes[trashes.length - 1].args : null
  );
  check("确认后确认框关闭", confirmEl() === null);
  check("确认后卡片没了", cards().length === 0, cards().length);
  check("确认后落盘为空列表", (() => {
    const s = savedLast();
    return !!s && (s.recentFiles || []).length === 0;
  })(), savedLast() && (savedLast().recentFiles || []).length);

  result.tests = T;
  result.failed = T.filter((t) => !t.pass).length;
  result.total = T.length;
}

function main() {
  const run = CASE === "menu" ? menuCase : CASE.startsWith("shot") ? shotCase : probeCase;
  run()
    .catch((err) => {
      result.error = String((err && err.message) || err);
      result.stack = String((err && err.stack) || "");
    })
    .finally(finish);
}

/// 截图专用：把界面摆到某个状态就停下，交给 `--screenshot` 拍。
///
/// 不产出判据，所以把结果 `<pre>` 也关掉 —— 不然它会挡在画面里。
/// `?case=shot-menu` / `?case=shot-confirm`。
async function shotCase() {
  result.suppressPre = true;
  await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
  await settle(24);

  if (CASE === "shot-menu") {
    // 右击第一张卡。位置挑在卡片右下角外侧一点，让菜单不压住卡片本身。
    const card = cards()[0];
    const r = card.getBoundingClientRect();
    rightClick(card, r.right - 24, r.bottom - 12);
    await settle(8);
    result.menuOpen = menuEl() !== null;
    result.labels = menuLabels();
    return;
  }

  if (CASE === "shot-confirm") {
    rightClick(cards()[0], 300, 200);
    await settle(6);
    pressItem("删除记录和文件");
    await settle(8);
    result.confirmOpen = confirmEl() !== null;
    result.confirmName = document.querySelector(".confirm-name")?.textContent ?? null;
    return;
  }
}

/// 卡片 hover 提示的采样。
///
/// 这里曾经是在查一个真 bug：卡片右上角那两颗悬浮按钮（收藏 / 从列表移除）
/// 和**文件名行重叠 44×18px**，导致 hover 在文件名右边那一段时命中的是按钮、
/// 提示框弹的是「收藏」/「从列表移除」，看不到完整文件名。
/// 当时的结论是"把按钮挪到缩略图右下角"。
///
/// **现在那两个按钮已经整个删掉了**（收藏 / 删除都进了右键菜单），
/// 所以这个用例的断言从"重叠面积是 0"升级成"那颗按钮压根不存在"，
/// 顺带把标题行 12 个采样点再扫一遍 —— 只要全部命中 `.file-title`，
/// 就说明整行都归文件名自己。
async function probeCase() {
  await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
  await settle(24);
  const card = cards()[0];
  const title = card.querySelector(".file-title");
  const thumb = card.querySelector(".file-thumb");
  const meta = card.querySelector(".file-meta");
  const actions = card.querySelector(".file-actions");

  const tr = title.getBoundingClientRect();
  const cr = card.getBoundingClientRect();
  const mr = meta.getBoundingClientRect();
  const thr = thumb.getBoundingClientRect();

  result.card = { x: round(cr.left), y: round(cr.top), w: round(cr.width), h: round(cr.height) };
  result.titleRect = { x: round(tr.left), y: round(tr.top), w: round(tr.width), h: round(tr.height) };
  result.metaRect = { x: round(mr.left), y: round(mr.top), w: round(mr.width), h: round(mr.height) };
  result.thumbRect = { x: round(thr.left), y: round(thr.top), w: round(thr.width), h: round(thr.height) };

  // 曾经的肇事者：现在必须**不存在**。
  result.actionsExists = !!actions;
  result.cardButtons = card.querySelectorAll("button").length;

  // 沿标题行横扫：每一处"实际会显示哪个 title"
  const samples = [];
  const ys = [tr.top + tr.height / 2, tr.top + 3];
  for (const y of ys) {
    for (const frac of [0.05, 0.25, 0.5, 0.7, 0.85, 0.97]) {
      const x = tr.left + tr.width * frac;
      const el = document.elementFromPoint(x, y);
      const eff = effectiveTitle(el);
      samples.push({
        at: `x=${Math.round(frac * 100)}% y=${Math.round(y - tr.top)}`,
        hit: eff.from,
        title: eff.title === LONG_NAME ? "<完整文件名>" : eff.title,
      });
    }
  }
  result.titleSamples = samples;
  result.titleSamplesBad = samples.filter((s) => s.title !== "<完整文件名>").length;
  result.titleSampleCount = samples.length;

  // 缩略图 / 元信息行也采一下（这两块本来应该显示卡片整体提示）
  const extra = [];
  for (const [label, rect] of [
    ["thumb", thr],
    ["meta", mr],
  ]) {
    const el = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
    const eff = effectiveTitle(el);
    extra.push({ at: label, hit: eff.from, title: eff.title });
  }
  result.otherSamples = extra;

  result.cardTitleAttr = card.getAttribute("title");
  result.titleAttr = title.getAttribute("title");
}

function finish() {
  if (result.suppressPre) {
    window.__HARNESS_DONE__ = true;
    return;
  }
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  // 只在应用挂载完之后塞才有用 —— 见 `bail()` 上面那段关于 `clear_element` 的注释。
  document.body.appendChild(pre);
  window.__HARNESS_DONE__ = true;
}

main();
