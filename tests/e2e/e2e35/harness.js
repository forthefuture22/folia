// e2e35：缩略图实例的**生命周期** —— 用完就该放掉，当前文档那份该复用主实例。
//
// 背景：bridge.js 的 `thumbDocs` 是 `path -> PDFDocumentProxy`，而那个代理扣着
// **整份 PDF 的字节** + 自己的解析结构 + **自己的 pdf.js worker**
// （`getDocument` 不传 `worker` 时，pdf.js 每次都 `new PDFWorker` —— 见
//  `pdf.mjs` 里 `worker = workerParams.port ? PDFWorker.fromPort(...) : new PDFWorker(...)`）。
//
// 原来的实现有两个问题：
//   ① 书架渲染完缩略图后**永不释放**：只有"被挤出最近列表"才放。于是书架上
//      挂着 10 份最近文件 ≈ 10 份 PDF 全量常驻 + 10 个 worker。
//   ② 侧栏给**当前正在看的**那份要缩略图时，走的是同一个 `thumbDocs` ——
//      等于又开了**第二份**完整实例（第二份 worker + 第二份字节）。
//      关文档时只 `destroy()` 了主那份，这第二份还在
//      → 用户看到的"关闭 PDF 后内存不降"。
//
// 观测量：`read_pdf_bytes` 的**调用次数**。实例一旦被放掉，再要缩略图就必然
// 要重新读一次文件 —— 这是从外部唯一能看见"有没有释放"的信号。
// 所以下面全部用**增量**断言（先取基准再比较），不依赖绝对计数。
//
// ⚠️ 判据是成对的：既断言"放掉了"（增量 1），也断言"图还是画得出来的"
//    （data URL 非空）—— 否则一个"永远返回空串"的实现也能让前半段全绿。
//
// ⚠️ 本套件**只桩 Tauri IPC**，pdf.js 与 bridge.js 全走真实实现；但"打开文档"
//    是直接调 `bridge.openDocumentPath`，**不驱动 Yew 应用**。原因见 `keepAlive`
//    的注释：让应用真去渲染正文会把 `--virtual-time-budget` 烧光。
//    "应用驱动下缩略图仍然正常"由 e2e18 覆盖。
//
// ⚠️ **必须分两次运行**（`?mode=shelf` / `?mode=main`），不能合成一次：
//    虚拟时钟推进得比真实时间快得多，一次运行里跑完三个阶段的渲染会把
//    `--virtual-time-budget` 提前烧完，后面的阶段就永远完不成
//    （表现为"卡在 current-1"）。e2e18 出于同样的原因把书架/阅读器拆开。

const A = "/tmp/e2e35-a.pdf";
const B = "/tmp/e2e35-b.pdf";
const C = "/tmp/e2e35-c.pdf";

const nameOf = (p) => String(p).split("/").pop();
const MODE = new URLSearchParams(location.search).get("mode") || "shelf";

window.__reads = []; // read_pdf_bytes 读过的 path（按调用顺序）
window.__listeners = {};

/** base64 → 普通数字数组（bridge.js 的 `toBytes` 认这个形态）。 */
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
      if (cmd === "read_pdf_path") {
        const p = String((args && args.path) || "");
        return Promise.resolve({
          id: `doc-${nameOf(p)}`,
          path: p,
          name: nameOf(p),
          size: 4096,
        });
      }
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({ recentFiles: [], lastPath: null, lastPage: null, scale: null })
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
  event: {
    listen: (name, cb) => {
      window.__listeners[name] = cb;
      return Promise.resolve(() => {});
    },
  },
};

const bridge = window.FoliaBridge;
const result = { mode: MODE, stage: "module" };

/**
 * ⚠️ **虚拟时钟下 `requestAnimationFrame` 不触发**，而 pdf.js 的 canvas 渲染器
 * 靠 rAF 调度 —— 不补这一句，`page.render()` 的 promise **永远不 resolve**
 * （表现为卡在 `rt:page` 之后，看着像 worker 死了）。e2e18 也有同样的一对补丁。
 */
window.requestAnimationFrame = (cb) => window.setTimeout(() => cb(performance.now()), 16);
window.cancelAnimationFrame = (id) => window.clearTimeout(id);

/**
 * ⚠️ **还要把虚拟时钟压慢**，光补 rAF 不够。
 *
 * `--virtual-time-budget` 在**主线程空闲**时一路快进。我们等 pdf.js worker 回复时
 * 主线程恰好完全空闲 → 虚拟时间瞬间烧掉几万毫秒，而 worker 只拿到几毫秒真实 CPU
 * → `page.render()` 迟迟不 resolve。**它是概率性的**（同一份代码时过时不过），
 * 所以"多给点预算"治不了根：真实时间与虚拟时间根本不成比例（实测约 1000:1）。
 *
 * 办法：挂一个自续期的定时器，每拍做一小段**真实**忙等。这样虚拟时间每前进
 * 16ms，真实时间也前进约 1ms，比例压到 ~16:1，worker 拿到的真实时间就与虚拟
 * 预算成正比了。
 *
 * ⚠️ 忙等的量只能写死成循环次数：`Date.now()` 与 `performance.now()` 在虚拟时钟下
 * **也是虚拟的**，页面内没有可信的真实时钟可用。
 */
const BURN = 60000;
(function paceVirtualClock() {
  const tick = () => {
    let x = 0;
    for (let i = 0; i < BURN; i++) x += i * 0.5;
    // 防 JIT 把整个循环优化掉：条件恒假但编译器证不出来。
    if (x === -1) window.__burn = x;
    setTimeout(tick, 16);
  };
  setTimeout(tick, 16);
})();

function finish() {
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
  window.__HARNESS_DONE__ = true;
}

/** 分阶段打点：没产出结果时，靠它看卡在哪一步。 */
function stage(name) {
  result.stage = name;
  try {
    document.body.setAttribute("data-e2e35-stage", `${MODE}/${name}`);
  } catch (_) {}
}

function traceNow() {
  try {
    return document.body.getAttribute("data-trace") || "（无）";
  } catch (_) {
    return "（读不到）";
  }
}

const reads = () => window.__reads.length;
const readsOf = (p) => window.__reads.filter((x) => x === p).length;
const isJpeg = (s) => typeof s === "string" && s.startsWith("data:image/jpeg") && s.length > 100;

function waitFor(predicate, label, maxTicks = 1500) {
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

/**
 * ⚠️ **必须用"轮询保活"包一层，不能直接 `await`。**
 *
 * Chrome 的 `--virtual-time-budget` 在**主线程空闲**时会一路快进到预算上限。
 * `await` 一个要等 pdf.js worker 回复的 promise 时，主线程恰好完全空闲 →
 * 虚拟时钟瞬间跑完预算 → 页面被 dump，而我们还在等（表现为"卡在某个阶段"）。
 * e2e18 没踩到是因为它**驱动的是应用本身**：Yew 的 effect 与 `spawn_local`
 * 循环一直在排定时器，虚拟时间只能一格一格走，worker 有真实时间回复。
 *
 * 每 20ms 排一个 `setTimeout` 就能复现同样的效果。
 * 期限按**虚拟**毫秒算 —— `Date.now()` 在虚拟时钟下也是虚拟的（实测 4001 拍
 * × 20ms 正好报 80000ms），所以它就是现成的虚拟时钟，不用另找口径。
 */
const KEEP_ALIVE_BUDGET_MS = 60000;

async function keepAlive(promise, label) {
  let settled = false;
  let value;
  let failure;
  promise.then(
    (v) => {
      settled = true;
      value = v;
    },
    (e) => {
      settled = true;
      failure = e;
    }
  );
  const t0 = Date.now();
  let ticks = 0;
  while (!settled) {
    if (Date.now() - t0 > KEEP_ALIVE_BUDGET_MS) {
      throw new Error(
        `keepAlive 超时: ${label}（${ticks} 拍 / 虚拟 ${Date.now() - t0}ms；` +
          `bridge 打点=${traceNow()}）`
      );
    }
    await new Promise((r) => setTimeout(r, 20));
    ticks++;
  }
  if (failure) throw failure;
  return value;
}

const thumb = (path, page, maxWidth, label) =>
  keepAlive(bridge.renderThumbnail(path, page, maxWidth), label);

/** 等应用挂载完（顺带让它可能发的 invoke 先落地，别混进我们的增量里）。 */
async function settleApp() {
  stage("mount");
  await waitFor(() => document.querySelector(".app"), "应用挂载");
  await new Promise((r) => setTimeout(r, 200));
}

// ---------------------------------------------------------------- ① 书架那份
// 同一份连要两次缩略图。实例用完就该放掉 → 第二次必须**重新读文件**。
async function runShelf() {
  await settleApp();

  stage("shelf-1");
  const a1 = await thumb(A, 1, 200, "A#1");
  const c1 = reads();

  stage("shelf-2");
  const a2 = await thumb(A, 1, 200, "A#2");
  const c2 = reads();

  result.shelfRepeat = {
    firstIsImage: isJpeg(a1),
    secondIsImage: isJpeg(a2),
    delta: c2 - c1, // 期望 1（放掉了 → 重新读）；旧实现是 0（一直留着）
    readsOfA: readsOf(A),
  };
  result.readOrder = window.__reads.map(nameOf);
  stage("done");
}

// ------------------------------------------------------- ② 当前文档 / ③ 别的文档
// 直接走 bridge.js 自己的 `openDocumentPath`（应用那条路调的就是它），
// 这样 `pdfDoc` / `currentMeta` 会被设上，但 Yew 侧不参与渲染 ——
// 否则正文渲染会把虚拟时间预算烧光。
async function runMain() {
  await settleApp();

  stage("open-b");
  const opened = JSON.parse(await keepAlive(bridge.openDocumentPath(B), "open B"));
  result.openOk = Boolean(opened && opened.ok);

  // ---- ② 当前文档：缩略图应当复用主实例，一次都不该再读文件
  const base = reads();
  stage("current-1");
  const b1 = await thumb(B, 1, 170, "B#1");
  const d1 = reads() - base;

  stage("current-2");
  const b2 = await thumb(B, 1, 170, "B#2");
  const d2 = reads() - base;

  result.currentDoc = {
    firstIsImage: isJpeg(b1),
    secondIsImage: isJpeg(b2),
    delta: d2, // 期望 0（复用主实例）；旧实现是 1（又开第二份）
    firstDelta: d1, // 期望 0
    base,
  };

  // 防连坐：复用主实例不能把主文档弄坏。
  let info = null;
  try {
    info = JSON.parse(bridge.documentInfo());
  } catch (_) {
    info = null;
  }
  result.mainDocAlive = Boolean(info && info.path === B);

  // ---- ③ 别的文档：临时开一份，用完放掉
  const cBase = reads();
  stage("other-1");
  const c1s = await thumb(C, 1, 200, "C#1");
  const e1 = reads() - cBase;

  stage("other-2");
  const c2s = await thumb(C, 1, 200, "C#2");
  const e2 = reads() - cBase;

  result.otherDoc = {
    firstIsImage: isJpeg(c1s),
    secondIsImage: isJpeg(c2s),
    firstDelta: e1, // 期望 1（临时开一份）
    delta: e2, // 期望 2（第一次那份已放掉）
  };

  // ---- ④ 对照：整个过程里主文档 B 只该被读一次（打开时那一次）
  result.readsOfB = readsOf(B);
  result.totalReads = reads();
  result.readOrder = window.__reads.map(nameOf);
  stage("done");
}

async function main() {
  try {
    if (MODE === "shelf") {
      await runShelf();
    } else {
      await runMain();
    }
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }
  finish();
}

if (!bridge) {
  result.error = "bridge.js 没有先执行：桩的插入位置不对";
  result.hasBridge = false;
  finish();
} else {
  main();
}
