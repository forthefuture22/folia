// e2e42：缩略图那条路上**同一份文件的并发渲染** —— 先回来的那个不能把还在飞的
// 那份实例 `destroy()` 掉。
//
// 背景（bridge.js 的 `thumbDocs` / `thumbRefs`）：
//   `openThumbDoc(path)` 把 `path -> Promise<PDFDocumentProxy>` 缓存在 `thumbDocs`
//   里，于是**同一份文件**的两个 `renderThumbnail` 会拿到**同一个** `PDFDocumentProxy`。
//   而 `renderThumbnail` 的语义是"用完即放"（一张缩略图没理由让整份 PDF 常驻）。
//   两者一撞就是这个竞态：
//     ① 任务甲、任务乙同时要 C 的第 1 页 / 第 3 页 → 共享一份实例；
//     ② 甲先回来（页小、画得快），`finally` 里 `dropThumbDoc` → `doc.destroy()`；
//     ③ 乙还在 `await page.render()` —— `destroy()` 把 worker 一起收了，
//        乙那次渲染被 reject → `catch` 吞掉异常返回**空串**；
//     ④ 而乙（比如书架那条 effect 的旧任务）往往已经因为代次被作废，
//        不会有人再补一次 → **那张缩略图永久静默消失**（既不报错也不重试）。
//
// 修法是给每条 path 记一个"在飞计数"（`thumbRefs`）：最后一个使用者走了才真放。
//
// 观测量：`read_pdf_bytes` 的调用次数 + `renderThumbnail` 返回的 data URL 是不是
// 真 JPEG。两条都是**从外部唯一能看见**的信号 —— 实例的生死在 DOM 和 pdf.js 的
// 公开 API 上都看不见。
//
// ⚠️ 判据**与"谁先回来"无关**：无论甲先还是乙先，旧实现下**总有一个**拿到空串。
//    所以断言写成"两个都得是真 JPEG"，不依赖虚拟时钟下的具体调度顺序。
//
// ⚠️ 本套件**只桩 Tauri IPC**，pdf.js 与 bridge.js 全走真实实现；不驱动 Yew 应用
//    去渲染正文（那会把 `--virtual-time-budget` 烧光，见 e2e35 的 `keepAlive`）。

const C = "/tmp/e2e42-c.pdf";

const nameOf = (p) => String(p).split("/").pop();

window.__reads = []; // read_pdf_bytes 读过的 path（按调用顺序）

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
        // 空的最近列表：应用自己不会去要缩略图，`__reads` 里就只有我们的调用。
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
      window.__listeners = window.__listeners || {};
      window.__listeners[name] = cb;
      return Promise.resolve(() => {});
    },
  },
};

const bridge = window.FoliaBridge;
const result = { stage: "module" };

/**
 * ⚠️ **虚拟时钟下 `requestAnimationFrame` 不触发**，而 pdf.js 的 canvas 渲染器
 * 靠 rAF 调度 —— 不补这一句，`page.render()` 的 promise **永远不 resolve**。
 * 与 e2e18 / e2e35 是同一对补丁。
 */
window.requestAnimationFrame = (cb) => window.setTimeout(() => cb(performance.now()), 16);
window.cancelAnimationFrame = (id) => window.clearTimeout(id);

/**
 * ⚠️ 还要把虚拟时钟压慢：`--virtual-time-budget` 在**主线程空闲**时一路快进，
 * 而等 worker 回复时主线程恰好空闲 → 虚拟时间瞬间烧掉几万毫秒、worker 只拿到
 * 几毫秒真实 CPU。每拍做一小段**真实**忙等，把两者比例压到 ~16:1。
 */
const BURN = 60000;
(function paceVirtualClock() {
  const tick = () => {
    let x = 0;
    for (let i = 0; i < BURN; i++) x += i * 0.5;
    if (x === -1) window.__burn = x; // 防 JIT 把循环优化掉
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
    document.body.setAttribute("data-e2e42-stage", name);
  } catch (_) {}
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
 * ⚠️ **必须用"轮询保活"包一层，不能直接 `await`**：Chrome 的虚拟时钟在主线程
 * 空闲时会把预算一路快进完，页面被 dump 而我们还挂在 await 上。
 * 每 20ms 排一个 `setTimeout` 就能让虚拟时间一格一格走。见 e2e35 的同一段。
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
      throw new Error(`keepAlive 超时: ${label}（${ticks} 拍 / 虚拟 ${Date.now() - t0}ms）`);
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

async function run() {
  await settleApp();

  // ---- ① 同一份文件、同一时刻发起两次渲染（**不 await 第一次**）
  //
  // 页 1 用最小宽度（40px，画得飞快）、页 4 用很大宽度（1200px，画得慢）——
  // 两者面积差 ~600 倍，先回来的必然是页 1 那次。
  // `openThumbDoc` 在**第一个 await 之前**就同步把 promise 放进 Map，
  // 所以这两次调用（同一 tick 内）一定共享**同一份**实例。
  stage("concurrent");
  const base = reads();
  const fast = bridge.renderThumbnail(C, 1, 40);
  const slow = bridge.renderThumbnail(C, 4, 1200);
  const [fastUrl, slowUrl] = await keepAlive(Promise.all([fast, slow]), "并发这一对");

  result.concurrent = {
    fastIsImage: isJpeg(fastUrl),
    slowIsImage: isJpeg(slowUrl),
    fastLen: String(fastUrl || "").length,
    slowLen: String(slowUrl || "").length,
    delta: reads() - base, // 期望 1（共享同一份实例，只读一次文件）
  };

  // ---- ② 这一对收尾之后，实例**确实**被放掉了（"计数"不能变成"永不释放"）
  stage("after");
  const afterBase = reads();
  const again = await thumb(C, 1, 40, "C#again");
  result.afterRelease = {
    isImage: isJpeg(again),
    delta: reads() - afterBase, // 期望 1（上一轮那份已经放了 → 重新读）
  };

  result.readsOfC = readsOf(C);
  result.readOrder = window.__reads.map(nameOf);
  stage("done");
}

async function main() {
  try {
    await run();
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }
  finish();
}

if (!bridge) {
  result.error = "bridge.js 没有先执行：桩的插入位置不对";
  finish();
} else {
  main();
}
