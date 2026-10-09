// Folia e2e40 —— 深色模式下 PDF 里的**位图图片不该被反相**。
//
// 深色模式靠 `.page-canvas { filter: invert(1) hue-rotate(180deg) }` 把「白纸黑字」
// 翻成「黑纸白字」，副作用是页面里的照片/插图一起变负片。修法是让图片**脱离**
// 那个滤镜：渲染完之后，把图片那几块的原色像素从主 canvas 抠进 `.page-image-layer`
// 里的小 canvas（详见 bridge.js 的「深色模式下的图片保护」）。
//
// 这个套件**不桩 bridge**：直接 import 真的 `public/pdfjs/bridge.js`，用真的 pdf.js
// 渲染一份手写 PDF（白底 + 黑块 + 一张纯红位图），再让 verify.py 截图取色。
//
// ⚠️ 两个**必须**的垫片（e2e18 踩过，这里又踩了一遍）：
//   1. 虚拟时钟下 `requestAnimationFrame` 不触发，而 pdf.js 的 canvas 渲染器靠它
//      调度 —— 不补这一句，`page.render()` 的 promise 永远不 resolve。
//   2. 不能直接 `await`：主线程一空闲，虚拟时钟就一口气快进到预算耗尽、pdf.js 的
//      worker 根本来不及回。要用**持续 tick** 的轮询等它。
//      症状很迷惑：页面 DOM 是好的、`title` 停在 `C:imported`、截图却已经拍完了。

window.requestAnimationFrame = (cb) => window.setTimeout(() => cb(performance.now()), 16);
window.cancelAnimationFrame = (id) => window.clearTimeout(id);

const params = new URLSearchParams(location.search);
const THEME = params.get("theme") === "light" ? "light" : "dark";
document.documentElement.classList.toggle("theme-dark", THEME === "dark");

const PDF_PATH = "/dark.pdf";

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

/** 空转若干个 tick，让微任务 / MutationObserver 回调跑完（不假设结果是什么）。 */
const settle = async (ticks = 24) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const result = { stage: "boot", theme: THEME, layersDark: -1, layersLight: -1, layersBack: -1 };

(async () => {
  try {
    const bytes = new Uint8Array(await (await fetch(PDF_PATH)).arrayBuffer());
    window.__TAURI__ = {
      core: {
        invoke: async (cmd) => {
          if (cmd === "read_pdf_path") return { path: PDF_PATH };
          if (cmd === "read_pdf_bytes") return Array.from(bytes);
          throw new Error("unexpected invoke: " + cmd);
        },
      },
    };

    const bridge = await import("./public/pdfjs/bridge.js");
    const host = document.querySelector(".page-image-layer");
    const canvas = document.getElementById("folia-canvas-1");
    const layerCount = () => host.querySelectorAll("canvas").length;

    let opened = null;
    let rendered = null;
    let err = null;
    bridge.openDocumentPath(PDF_PATH).then((r) => { opened = r; }).catch((e) => { err = e; });
    await waitFor(() => opened !== null || err !== null, "打开文档");
    if (err) throw err;

    bridge.renderPage("folia-canvas-1", 1, 1).then((r) => { rendered = r; }).catch((e) => { err = e; });
    await waitFor(() => rendered !== null || err !== null, "渲染页面");
    if (err) throw err;

    result.stage = "rendered";
    result.render = rendered;
    result.canvasSize = canvas.width + "x" + canvas.height;
    result.layersDark = layerCount();
    result.layerRects = [...host.querySelectorAll("canvas")].map((c) => ({
      left: c.style.left,
      top: c.style.top,
      width: c.style.width,
      height: c.style.height,
    }));

    // 主题来回切：图片层要跟着出现 / 消失 / 再出现（MutationObserver 那条路）。
    // ⚠️ 只在深色那趟做，而且最后一定要切回**深色** —— 截图是在脚本跑完之后拍的，
    // 最终态决定取色结果。浅色那趟必须**留在浅色**，否则配对判据量到的是深色画面。
    // ⚠️ 这里用 `settle`（固定空转）而不是 `waitFor`（等条件成立）：对照实验里
    // 图片层本来就建不起来，等条件会一直等到虚拟时钟耗尽，`result` 反而写不出去。
    if (THEME === "dark") {
      document.documentElement.classList.remove("theme-dark");
      await settle();
      result.layersLight = layerCount();

      document.documentElement.classList.add("theme-dark");
      await settle();
      result.layersBack = layerCount();
    }

    result.stage = "done";
  } catch (e) {
    result.stage = "error";
    result.error = e && e.stack ? e.stack : String(e);
  }
  window.__RESULT__ = result;
  document.title = JSON.stringify(result);
})();
