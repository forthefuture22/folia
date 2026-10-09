// e2e41：图片层（深色模式下"保持原色"的那一层）的内存不变量 + 算子表解析的单测。
//
// 三件事：
//
// ① **`quadsFromOps` 的单测**（`?mode=units`）。它是纯函数，直接喂**合成算子表** ——
//    这一段正确性全靠算子表语义，靠真 PDF 凑样本很难凑全。特别是
//    `paintImageXObjectRepeat`(88) / `paintInlineImageXObjectGroup`(87)：
//    ⚠️ 它们**在本版本 pdf.js 下走不到**（`getOperatorList()` 硬编码带 `OPLIST`
//    标志 → worker 用 `NullOptimizer` → 不合并；`render()` 才用优化过的表），
//    所以只能这样测。别删了这两个分支，也别以为真 PDF 能覆盖到它们。
//
// ② **浅色下不该收四边形**。收一次 = worker 再解析一遍内容流 + 多留一份算子表
//    （`getOperatorList()` 的 intent 和 `render()` 不同，**不是**缓存命中）。
//    浅色是大多数人的默认状态，这份钱不该付。切到深色时由 `syncDarkImages` 补收。
//
// ③ **内存不变量**：图片层的账（`pageCanvases` / `pageImageQuads` / DOM 里的小
//    canvas）必须随页离开保留窗口一起还回去。三种压力各验一遍：滚一遍全书 /
//    反复缩放 / 反复切主题。任何一项**随次数增长**就是漏。
//
// ⚠️ 虚拟时钟的三个坑都在这里踩过，处理办法见 `tests/e2e/README.md`：
//    rAF 不触发要垫片；不能裸 `await`（要用持续 tick 的 `waitFor`）；
//    **条件本来就不成立时别用 `waitFor`**（要用固定空转的 `settle`）。

const MODE = new URLSearchParams(location.search).get("mode") || "units";
const PAGES = 24;
const META = {
  id: "e2e41",
  path: "/tmp/e2e41/sample.pdf",
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
            recentFiles: [{ ...META, pages: PAGES, last_page: 1, opened_at: 0, favorite: false }],
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

// ---- 虚拟时钟的补丁（同 e2e35/e2e36，见 tests/e2e/README.md）----
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
// 调用次数本身就是"释放发生了"的证据。
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
    document.body.setAttribute("data-e2e41-stage", `${MODE}/${name}`);
  } catch (_) {}
}

const qsa = (sel) => [...document.querySelectorAll(sel)];
const canvasOf = (page) => document.querySelector(`.page[data-page="${page}"] .page-canvas`);

/// 「这一页真画过」的判据：`renderPage` 是**唯一**会写 `canvas.style.width` 的地方。
/// 不能用 `c.width > 1` —— 刚 `createElement("canvas")` 的默认尺寸就是 300×150。
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

/// 固定空转：**不假设结果**的等待（"给它时间做完，做没做成都行"）。
/// 条件本来就不成立时用 `waitFor` 会一直等到预算耗尽、`result` 都写不出去。
async function settle(ticks = 10) {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 20));
}

const stats = () => {
  try {
    return JSON.parse(bridge.cacheStats());
  } catch (_) {
    return {};
  }
};

const statNum = (key) => {
  const v = stats()[key];
  return typeof v === "number" ? v : -1;
};

/// `renderPage` **真正收尾**的信号：`renderTasks` 归零。
///
/// ⚠️ 不能只用 `painted()` —— 它看的 `canvas.style.width` 是在 `page.render()` **之前**
/// 就设好的，所以它成立时渲染可能还在飞，`pageCanvases` / `pageImageQuads` 都还没写进去。
/// 实测这个差别会让 `canvases` 在两次运行里分别读到 0 和 1（**不稳定**）。
/// `renderTasks` 是在 `finally` 里删的，而 `finally` 排在"抠图片层"那一段**之后**。
const rendersDone = () => statNum("renderTasks") === 0;

/// 主题的唯一入口就是 `<html class>`（`index.html` 里那段同步脚本）。
function setTheme(dark) {
  document.documentElement.classList.toggle("theme-dark", !!dark);
}

/// 第 `page` 页图片层里每块小 canvas 的**CSS 位置与尺寸**。
function layerRects(page) {
  const host = document.querySelector(`.page[data-page="${page}"] .page-image-layer`);
  if (!host) return [];
  return [...host.children].map((c) => ({
    left: Math.round(parseFloat(c.style.left) || 0),
    top: Math.round(parseFloat(c.style.top) || 0),
    width: Math.round(parseFloat(c.style.width) || 0),
    height: Math.round(parseFloat(c.style.height) || 0),
  }));
}

/// 「同一行上有几块」—— 第 1 页有 6 个只差平移的戳记（y 相同）。
function repeatRow(page, tol = 6) {
  const rects = layerRects(page);
  const rows = new Map();
  for (const r of rects) {
    let hit = null;
    for (const key of rows.keys()) if (Math.abs(key - r.top) <= tol) hit = key;
    const key = hit === null ? r.top : hit;
    rows.set(key, (rows.get(key) || 0) + 1);
  }
  let best = 0;
  for (const n of rows.values()) best = Math.max(best, n);
  return { rects: rects.length, rowMax: best };
}

/// 把应用打开到阅读器（走真实路径：点书架卡片）。
async function openReader() {
  stage("shelf");
  const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片");
  card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  stage("reader");
  await waitFor(() => document.querySelector("main.stage"), "阅读器");
  await waitFor(() => qsa(".page").length === PAGES, `${PAGES} 个页面占位盒`);
  stage("first-render");
  await waitFor(() => painted(canvasOf(1)), "第 1 页画出来");
  // 等**所有**渲染任务走完（含"抠图片层"那段尾巴）—— 见 `rendersDone` 的注释。
  await waitFor(() => rendersDone() && statNum("canvases") >= 1, "渲染任务全部收尾");
  // 图片层是渲染完之后**异步**补的（`collectImageQuads` 要 await 算子表），
  // 而且侧栏那批缩略图还在抢 worker —— 给它一段时间，但**不假设**它一定建起来。
  await settle(24);
}

/// 缩放：走真实的键盘路径（`Ctrl/Cmd` + `=` / `-`），和用户按的是同一条。
function pressZoom(key) {
  document.dispatchEvent(
    new KeyboardEvent("keydown", { key, ctrlKey: true, bubbles: true, cancelable: true })
  );
}

// ---------------------------------------------------------------- ① 算子表单测

async function runUnits() {
  stage("units");
  // `quadsFromOps` 是 bridge.js 的 **named export**，故意没挂到 `window.FoliaBridge`
  // （那是 Rust 侧的调用面，不该塞测试用的纯函数）。这里按**同一个 URL** 重新 import
  // —— 浏览器按 URL 缓存模块，拿到的是**同一个实例**，不会把 bridge.js 再执行一遍。
  const [pdfjs, bridgeMod] = await Promise.all([
    import("/public/pdfjs/pdf.mjs"),
    import("/public/pdfjs/bridge.js"),
  ]);
  const { OPS, Util } = pdfjs;
  const libRef = { OPS, Util };
  const base = [1, 0, 0, 1, 0, 0];
  const run = (fnArray, argsArray) => bridgeMod.quadsFromOps(fnArray, argsArray, base, libRef);

  const out = {};
  out.opsAvailable = {
    p85: OPS.paintImageXObject,
    p86: OPS.paintInlineImageXObject,
    p87: OPS.paintInlineImageXObjectGroup,
    p88: OPS.paintImageXObjectRepeat,
    p83: OPS.paintImageMaskXObject,
    p90: OPS.paintSolidColorImageMask,
  };

  // 单张位图 + 一层 transform：[2,0,0,3,10,20] 作用在单位正方形上
  out.single = run(
    [OPS.save, OPS.transform, OPS.paintImageXObject, OPS.restore],
    [null, [2, 0, 0, 3, 10, 20], ["img", 1, 1], null]
  );
  // inline image，没有 transform → 就是基矩阵下的单位正方形
  out.inline = run([OPS.paintInlineImageXObject], [{ width: 2, height: 2 }]);
  // repeat：5 个位置，只有平移不同
  out.repeat = run(
    [OPS.paintImageXObjectRepeat],
    [["img", 2, 3, new Float32Array([0, 0, 10, 0, 20, 0, 30, 0, 40, 0])]]
  );
  // inline group：3 个 entry
  out.group = run(
    [OPS.paintInlineImageXObjectGroup],
    [
      [
        { width: 1, height: 1 },
        [
          { transform: [1, 0, 0, 1, 5, 5] },
          { transform: [1, 0, 0, 1, 7, 7] },
          { transform: [1, 0, 0, 1, 9, 9] },
        ],
      ],
    ]
  );
  // 遮罩类：**故意不算**（单色遮罩被 PDF 用来画彩色文字和图标，属于正文）
  out.masks = run(
    [
      OPS.paintImageMaskXObject,
      OPS.paintSolidColorImageMask,
      OPS.paintImageMaskXObjectGroup,
      OPS.paintImageMaskXObjectRepeat,
    ],
    [["m", 1, 1], [], [{ width: 1 }], ["m", 1, 1, new Float32Array([0, 0])]]
  );
  // save/restore 嵌套：第二个 PIXO 必须回到外层矩阵，不能被内层污染
  out.nested = run(
    [
      OPS.save,
      OPS.transform,
      OPS.save,
      OPS.transform,
      OPS.paintImageXObject,
      OPS.restore,
      OPS.paintImageXObject,
      OPS.restore,
    ],
    [
      null,
      [2, 0, 0, 2, 0, 0],
      null,
      [1, 0, 0, 1, 5, 5],
      ["a", 1, 1],
      null,
      ["a", 1, 1],
      null,
    ]
  );
  // form XObject：`paintFormXObjectBegin` 自带一层 save + matrix
  out.form = run(
    [OPS.paintFormXObjectBegin, OPS.paintImageXObject, OPS.paintFormXObjectEnd],
    [[[3, 0, 0, 3, 1, 1]], ["a", 1, 1], null]
  );
  // 上限：200 张单图 → 收够 `IMAGE_LAYER_MAX * 4` 就停
  const manyFns = [];
  const manyArgs = [];
  for (let i = 0; i < 200; i++) {
    manyFns.push(OPS.paintImageXObject);
    manyArgs.push(["a", 1, 1]);
  }
  out.capped = run(manyFns, manyArgs).length;

  result.units = out;
  stage("done");
}

// ---------------------------------------------------------------- ②③ 真实应用

async function runScroll() {
  setTheme(true);
  await openReader();
  result.before = { cache: stats(), row: repeatRow(1) };

  const stageEl = document.querySelector("main.stage");
  result.scroll = { clientHeight: stageEl.clientHeight, scrollHeight: stageEl.scrollHeight };

  // ⚠️ **必须分步滚，不能一步跳到底。** 一步跳只会渲染「起点那几页 + 终点那几页」，
  // 中间十几页**根本没渲染过**，那"翻过去的页有没有还账"就验不到 —— 实测一步跳
  // 主跑 `after=2`、对照组 `after=5`，都挤在阈值下面，**判据形同虚设**。
  // 分步之后主跑 ≈ 保留窗口量级、对照组 ≈ 全书页数，才分得开。
  stage("scroll");
  const max = Math.max(0, stageEl.scrollHeight - stageEl.clientHeight);
  const STEPS = 12;
  for (let i = 1; i <= STEPS; i++) {
    stageEl.scrollTop = Math.round((max * i) / STEPS);
    stageEl.dispatchEvent(new Event("scroll"));
    await settle(10);
    await waitFor(() => rendersDone(), `第 ${i} 段滚动后渲染收尾`);
  }
  stage("scroll-bottom");
  await waitFor(() => painted(canvasOf(PAGES)) && rendersDone(), "最后一页画完并收尾");
  await settle(30);

  stage("measure");
  result.after = { cache: stats(), lastRow: repeatRow(PAGES) };
  result.released = window.__released.length;
  result.releasedSample = window.__released.slice(0, 10);
  stage("done");
}

async function runZoom() {
  setTheme(true);
  await openReader();
  result.before = { cache: stats(), row: repeatRow(1) };

  stage("zoom");
  result.samples = [];
  for (let i = 0; i < 6; i++) {
    pressZoom(i % 2 === 0 ? "=" : "-");
    await settle(16);
    result.samples.push(stats());
  }
  pressZoom("0");
  await settle(16);
  await waitFor(() => rendersDone(), "缩放后的渲染全部收尾");
  await settle(16);

  stage("measure");
  result.after = { cache: stats(), row: repeatRow(1) };
  stage("done");
}

async function runTheme() {
  // 从**浅色**开局：这一拍就是"浅色下不收四边形"的判据来源。
  setTheme(false);
  await openReader();
  result.before = { cache: stats(), row: repeatRow(1) };

  stage("theme");
  result.samples = [];
  for (let i = 0; i < 6; i++) {
    const dark = i % 2 === 0;
    setTheme(dark);
    // 浅色→深色那次要补收算子表（worker 往返），给足时间但不假设结果。
    await settle(24);
    result.samples.push({ dark, cache: stats(), row: repeatRow(1) });
  }
  setTheme(true);
  await settle(24);

  stage("measure");
  result.after = { cache: stats(), row: repeatRow(1) };
  stage("done");
}

async function main() {
  try {
    if (!bridge) {
      result.error = "bridge.js 没有先执行：桩的插入位置不对";
    } else if (MODE === "scroll") {
      await runScroll();
    } else if (MODE === "zoom") {
      await runZoom();
    } else if (MODE === "theme") {
      await runTheme();
    } else {
      await runUnits();
    }
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }
  finish();
}

main();
