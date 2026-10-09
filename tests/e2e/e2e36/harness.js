// e2e36：三处"内存上限"的回归 —— 页面位图、pdf.js 算子表、检索文本缓存。
//
// 背景（都是"没有上限"的东西）：
//   ① 原来**每一页都挂**一个 `<canvas>` 和一个 `.textLayer`，页面滚出可视区后
//      没有任何人释放位图。位图字节 = 宽 × 高 × dpr² × 4，A4 页在缩放 1.25、
//      dpr 2 下是 12.5 MB —— 翻 30 页就是几百 MB 挂着不还。
//      现在只有「保留窗口」（可视窗口每侧外扩 `KEEP_BAND = 2` 页）内的页才挂，
//      出了窗口就摘掉元素（位图 + DOM 一起还给浏览器）并调 `releasePage`
//      放掉 pdf.js 那一页的算子表。
//   ② `textCache` 原来没有条数上限，而 `search()` 会逐页 `getTextContent()`
//      走完整本 —— 搜一次全文，整本的文本就永久留在内存里。同一处写入的
//      `viewCache`（每页的 `p.view`，`search()` 拿它算命中的 y 坐标）同样是
//      无界的，两个缓存键集必须一起淘汰。
//   ③ `renderPage` 的 dpr 上限从 2 降到 1.5，位图直接少 44%。
//   ④ 缩略图那条路（**借用主实例**）渲染完不 `page.cleanup()`：阅读区靠
//      「离开保留窗口」触发释放，而缩略图渲染过的页（侧栏那 13 页、总览那**一整本**）
//      **从来没进过保留窗口**，那条分支永远不会为它们触发 —— 整本的算子表
//      一直留到 `closeDocument`。现在借用路径渲染完就自己收拾，计数见
//      `cacheStats().thumbCleanups`（page 模式断言 > 0）。
//
// ⚠️ 分三次运行（`?mode=page` / `?mode=scroll` / `?mode=search`）：虚拟时钟的
//    预算烧完后面的阶段就永远完不成，见 `tests/e2e/README.md`。
//
// ⚠️ ① 没有 `REGRESS` 对照组：它的判断在 **Rust/wasm 里**，而站点里的是**编译好的
//    wasm** —— 文本替换改不动它。所以 ① 用的是绝对判据（`canvas 数 < 页数`）
//    加一条配对判据（当前页照样画得出来）。②③ 都在 `bridge.js` 里，有对照组。

const MODE = new URLSearchParams(location.search).get("mode") || "page";
const PAGES = 30;
const META = {
  id: "e2e36",
  path: "/tmp/e2e36/sample.pdf",
  name: "sample.pdf",
  size: 8192,
  numPages: PAGES,
};

window.__reads = [];
window.__released = [];

let bytesArr = null;
function pdfBytes() {
  if (!bytesArr) {
    const bin = atob(window.__PDF_B64);
    bytesArr = Array.from({ length: bin.length }, (_, i) => bin.charCodeAt(i));
  }
  return Promise.resolve(bytesArr);
}

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      if (cmd === "read_pdf_bytes") {
        window.__reads.push(String(args && args.path));
        return pdfBytes();
      }
      if (cmd === "read_pdf_path" || cmd === "open_pdf") return Promise.resolve(META);
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [{ ...META, pages: 1, last_page: 1, opened_at: 0, favorite: false }],
            lastPath: null,
            lastPage: null,
            scale: null,
          })
        );
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      if (cmd === "save_app_state") return Promise.resolve(null);
      if (cmd === "quarantine_app_state") return Promise.resolve(null);
      return Promise.resolve(null);
    },
  },
  event: { listen: () => Promise.resolve(() => {}) },
};

const bridge = window.FoliaBridge;
const result = { mode: MODE, stage: "module" };

// ---- 虚拟时钟的两个补丁（同 e2e35，见 tests/e2e/README.md）----
window.requestAnimationFrame = (cb) => window.setTimeout(() => cb(performance.now()), 16);
window.cancelAnimationFrame = (id) => window.clearTimeout(id);

const BURN = 60000;
(function paceVirtualClock() {
  const tick = () => {
    let x = 0;
    for (let i = 0; i < BURN; i++) x += i * 0.5;
    if (x === -1) window.__burn = x;
    setTimeout(tick, 16);
  };
  setTimeout(tick, 16);
})();

// `releasePage` 包一层计数（**保留真实现**）：只有"滚出保留窗口"才会被调，
// 所以调用次数本身就是"释放发生了"的证据。别整个换掉 —— 那就测的是桩了。
if (bridge) {
  const realRelease = bridge.releasePage.bind(bridge);
  bridge.releasePage = (page) => {
    window.__released.push(page);
    return realRelease(page);
  };
}

function finish() {
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
}

function stage(name) {
  result.stage = name;
  try {
    document.body.setAttribute("data-e2e36-stage", `${MODE}/${name}`);
  } catch (_) {}
}

const qsa = (sel) => [...document.querySelectorAll(sel)];
const canvasOf = (page) => document.querySelector(`.page[data-page="${page}"] .page-canvas`);

/// 「这一页真画过」的判据：`renderPage` 是**唯一**会写 `canvas.style.width` 的地方。
///
/// ⚠️ 不能用 `c.width > 1` 当判据 —— 浏览器给一个刚 `createElement("canvas")`
/// 的默认尺寸就是 300×150，`width` 早就是 300 了，"等它画出来"会**立刻通过**，
/// 等于没等（滚到底那一步就是这么假绿的，样本里那个 `{w:300,h:150,cssW:''}`
/// 就是一块没画过的默认画布）。
const painted = (c) => !!c && !!c.style.width && c.width > 1;

function waitFor(predicate, label, maxTicks = 3000) {
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

/** 把应用打开到阅读器（走真实路径：点书架卡片）。 */
async function openReader() {
  stage("shelf");
  const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片");
  card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  stage("reader");
  await waitFor(() => document.querySelector("main.stage"), "阅读器");
  // 等页面尺寸解析完（`.page` 是跟着 page_sizes 一起出现的）。
  await waitFor(() => qsa(".page").length === PAGES, `${PAGES} 个页面占位盒`);
  stage("first-render");
  await waitFor(() => painted(canvasOf(1)), "第 1 页画出来");
}

/// 等侧栏把附近那几页缩略图渲染完。
///
/// 侧栏默认是开着的、默认标签就是「缩略图」，打开文档时会请求当前页前后共 13 页。
/// 缩略图走的是**借用主实例**那条路（`isCurrentDoc` 为真），所以它涨的就是
/// `thumbCleanups` —— 那条判据的**唯一**来源。
///
/// ⚠️ 超时**不算错误**：这里只是把等待收敛掉，红了交给 verify 那条判据去红。
/// 把整段驱动脚本搞崩的话，后面所有判据都看不到，反而看不清是哪一条坏了。
async function waitThumbs(maxTicks = 600) {
  try {
    await waitFor(
      () => JSON.parse(bridge.cacheStats()).thumbCleanups > 0,
      "侧栏缩略图渲染",
      maxTicks
    );
  } catch (_) {}
}

/// 统计一页当前挂着的 canvas / 文本层状态。
///
/// ⚠️ dpr 那条判据必须取**已渲染**的 canvas（`painted`）：保留窗口里那些
/// "挂了但还没画"的 canvas 是浏览器给的默认尺寸（300×150，`style.width`
/// 还是空串），拿它算 `width / style.width` 会得到 NaN，看着像判据写错了。
function snapshot() {
  const canvases = qsa(".page-canvas");
  const rendered = canvases.filter(painted);
  const sample = rendered[0] || null;
  const c1 = canvasOf(1);
  const last = canvasOf(PAGES);
  const layer1 = document.querySelector('.page[data-page="1"] .textLayer');
  const box = (c) => (c ? { w: c.width, h: c.height, cssW: c.style.width } : null);
  return {
    pageCount: qsa(".page").length,
    canvasCount: canvases.length,
    renderedCount: rendered.length,
    textLayerCount: qsa(".page .textLayer").length,
    page1HasCanvas: !!c1,
    page1Rendered: painted(c1),
    page1HasTextLayer: !!layer1,
    page1Spans: layer1 ? layer1.querySelectorAll("span").length : 0,
    lastPageHasCanvas: !!last,
    lastPageRendered: painted(last),
    // 位图相对 CSS 尺寸的倍率 = dpr 上限（renderPage 里算的）
    dprRatio: sample && sample.style.width ? sample.width / parseFloat(sample.style.width) : null,
    renderedSample: box(sample),
    lastSample: box(last),
  };
}

async function runPage() {
  await openReader();
  // 侧栏缩略图跟正文渲染是两条并行的路，这里要等它把附近那几页画完，
  // 否则 `thumbCleanups` 是 0 —— 那不是"没释放"，是"还没渲染"。
  await waitThumbs();
  stage("measure");
  await new Promise((r) => setTimeout(r, 400));
  result.page = snapshot();
  result.released = window.__released.length;
  result.cache = JSON.parse(bridge.cacheStats());
  stage("done");
}

async function runScroll() {
  await openReader();
  result.before = snapshot();

  const stageEl = document.querySelector("main.stage");
  result.scroll = { clientHeight: stageEl.clientHeight, scrollHeight: stageEl.scrollHeight };

  // 一路滚到底：中间那些页会依次进入又离开可视窗口。
  stage("scroll-bottom");
  stageEl.scrollTop = stageEl.scrollHeight;
  stageEl.dispatchEvent(new Event("scroll"));
  await waitFor(() => painted(canvasOf(PAGES)), "最后一页画出来");

  stage("measure");
  await new Promise((r) => setTimeout(r, 600));
  result.after = snapshot();
  result.released = window.__released.length;
  result.releasedSample = window.__released.slice(0, 8);
  stage("done");
}

async function runSearch() {
  await openReader();

  // ⚠️ 必须走**真实 UI**：点图标轨的放大镜 → 面板切到「搜索结果」→ 往搜索框里打字。
  //
  // 直接调 `bridge.search()` 只能证明"pdf.js 侧搜了一遍"，应用的 `query` 状态
  // 还是空串 —— 而 `<mark>` 是 `renderTextLayer(containerId, page, scale, query, …)`
  // 拿 `query` 生成的。query 为空就一个 mark 都没有（`page1Marks` 永远是 0，
  // 看着像"为了省内存把高亮搞坏了"）。所以高亮这条判据只能在 UI 路径上量。
  stage("open-search");
  const rail = await waitFor(
    () => document.querySelector('button[title="搜索结果"]'),
    "图标轨上的搜索按钮"
  );
  rail.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));

  const input = await waitFor(() => document.querySelector(".search-input"), "搜索输入框");
  input.value = "FOLIA";
  input.dispatchEvent(new Event("input", { bubbles: true }));

  // 应用侧有 200ms 防抖，之后才真去逐页搜（30 页）。
  stage("wait-hits");
  await waitFor(() => document.querySelectorAll(".hit-item").length > 0, "搜索结果列表");

  stage("measure");
  result.hits = document.querySelectorAll(".hit-item").length;
  const first = document.querySelector(".hit-item");
  result.firstHit = first
    ? {
        page: first.querySelector(".hit-page").textContent,
        snippet: first.querySelector(".hit-text").textContent,
      }
    : null;

  const stats = JSON.parse(bridge.cacheStats());
  result.cache = stats;
  result.numPages = PAGES;

  // 高亮是文本层**重建**之后才有的（query 变了 → `page_view.rs` 的重渲染
  // → `renderTextLayer`），所以这里要等，不能定一个 sleep 去赌。
  stage("wait-marks");
  result.page1Marks = await waitFor(() => {
    const n = document.querySelectorAll('.page[data-page="1"] .textLayer mark.sh').length;
    return n > 0 ? n : null;
  }, "第 1 页的检索高亮");

  result.after = snapshot();
  stage("done");
}

async function main() {
  try {
    if (!bridge) {
      result.error = "bridge.js 没有先执行：桩的插入位置不对";
    } else if (MODE === "scroll") {
      await runScroll();
    } else if (MODE === "search") {
      await runSearch();
    } else {
      await runPage();
    }
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }
  finish();
}

main();
