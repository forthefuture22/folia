// e2e38：**并发打开两份文档**会不会静默吃掉一个标签。
//
// 背景（两个事实叠在一起）：
//
//   1. `bridge.js` 的 `getPageSizes()` / `getOutline()` 读的是模块里**那一个**
//      全局 `pdfDoc`，**不认路径** —— 谁最后 `openDocumentPath` 谁就是"当前那份"。
//   2. `apply_document` 从 `openDocumentPath` 到提交标签之间要跨三次 await
//      （`page_sizes` / `outline` / 读批注），而 `tabs` 快照是在 await **之前**
//      取的。
//
// 于是两条打开流水线一重叠：
//   · 第二条的 `open_path` 会在第一条还在读的时候把 `pdfDoc` 换掉 ——
//     第一条拿到的是**别人的**页数（还会拿它去夹续读页码）；
//   · 两条各自拿一份"还没有对方"的 `tabs` 快照去 `set`，后提交的那条把
//     前一条刚 push 的标签整个覆盖掉 —— 标签栏里只剩一份。
//
// 触发方式照真实路径来：连着两次 `folia://open-path`（双击 .pdf 走的就是它，
// 冷启动那条不走这里）。真机上等价于"第一份还在转圈，用户又双击了第二份"。
//
// 桩只桩 Tauri IPC 和 `bridge.js` 里那几个"读全局那份"的接口；`onFileDrop`
// 照旧走真实现（包一层拿监听器，见下），应用与 wasm 全是真的。
//
// 判据：
//   · ★ 主判据：两个标签都在 / 每份的页数跟它自己对得上 / 两次打开确实没重叠。
//   · 配对判据：切标签能来回、阅读器正常、最近列表里两份都在。

const DOCS = {
  "/tmp/h38a.pdf": { name: "h38a.pdf", pages: 120 },
  "/tmp/h38b.pdf": { name: "h38b.pdf", pages: 37 },
};
const PATH_A = "/tmp/h38a.pdf";
const PATH_B = "/tmp/h38b.pdf";

const PAGE_W = 595;
const PAGE_H = 842;

window.__invokes = [];
window.__saved = [];
window.__listeners = {};

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        // 空书架：这一套只关心"打开"这条流水线。
        return Promise.resolve(
          JSON.stringify({ recentFiles: [], lastPath: null, lastPage: 1, scale: 1 })
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
  // 把监听器存下来，稍后手动触发 —— 这就是"用户双击了一份 PDF"的那一下。
  event: {
    listen: (name, cb) => {
      window.__listeners[name] = cb;
      return Promise.resolve(() => {});
    },
  },
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
const result = { stage: "module" };
if (!bridge) {
  result.error = "bridge.js 没有先执行：桩的插入位置不对";
}

// ⚠️ 不能整个替换 `onFileDrop`：bridge.js 里那个函数自己会 `listen` 两条事件，
// 覆盖掉它应用就再也收不到「直接打开」那条路了。包一层：拿到监听器，也让原实现照常注册。
const realOnFileDrop = bridge.onFileDrop;
bridge.onFileDrop = (cb) => {
  window.__dropCb = cb;
  return realOnFileDrop(cb);
};

// ---------------------------------------------------------------- 时间线
//
// 把"哪一步在什么时候发生"记下来 —— 这是唯一能直接证明**两次打开有没有重叠**
// 的东西：串行的话 A 的尺寸一定在 B 的 `openDocumentPath` 之前读完。
window.__events = [];
const ev = (tag, path) => window.__events.push(`${tag}:${path || "-"}`);

// ⚠️ **"当前那份"必须是个真的全局量。** 真实现里 `getPageSizes()` 读的就是
// `pdfDoc` 这个模块级变量，不认路径 —— 桩要是按路径返回，这个 bug 就永远测不出来。
let CURRENT = null;

if (bridge) {
  Object.assign(bridge, {
    openDocumentPath: (path) => {
      const p = String(path || "");
      ev("open", p);
      return new Promise((resolve) => {
        setTimeout(() => {
          CURRENT = p; // 换掉全局那一份
          const doc = DOCS[p] || { name: "unknown.pdf", pages: 1 };
          resolve(
            JSON.stringify({
              ok: true,
              cancelled: false,
              error: null,
              meta: {
                id: `doc-${doc.name}`,
                path: p,
                name: doc.name,
                size: 24000,
                numPages: doc.pages,
              },
            })
          );
        }, 40);
      });
    },
    // 读的是**全局那份**（跟真实现一致）。
    getPageSizes: () =>
      new Promise((resolve) => {
        setTimeout(() => {
          ev("sizes", CURRENT);
          const doc = DOCS[CURRENT] || { pages: 0 };
          resolve(
            JSON.stringify(
              Array.from({ length: doc.pages }, () => ({ width: PAGE_W, height: PAGE_H }))
            )
          );
        }, 40);
      }),
    getOutline: () =>
      new Promise((resolve) => {
        setTimeout(() => {
          ev("outline", CURRENT);
          resolve("[]");
        }, 20);
      }),
    search: () => Promise.resolve("[]"),
    renderThumbnail: (_path, _page, maxWidth) => Promise.resolve(makeThumb(maxWidth)),
    renderPage: (canvasId, _page, scale) => {
      const canvas = document.getElementById(canvasId);
      if (canvas) {
        canvas.width = Math.round(PAGE_W * scale);
        canvas.height = Math.round(PAGE_H * scale);
        const ctx = canvas.getContext("2d");
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
      }
      return Promise.resolve("");
    },
    renderTextLayer: () => Promise.resolve(""),
  });
}

// ---------------------------------------------------------------- 驱动

function stage(name) {
  result.stage = name;
  try {
    document.body.setAttribute("data-e2e38-stage", name);
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

function press(el) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 })
    );
  }
}

const tabs = () => [...document.querySelectorAll(".tab")];
const tabNames = () => tabs().map((t) => t.querySelector(".tab-label").textContent.trim());
const activeTabName = () =>
  document.querySelector(".tab.active .tab-label")?.textContent.trim() ?? null;
const pageTotal = () => document.querySelector(".page-total")?.textContent.trim() ?? null;
const savedNames = () => {
  const s = window.__saved[window.__saved.length - 1];
  return s && s.recentFiles ? s.recentFiles.map((f) => f.name) : null;
};

async function switchTo(name) {
  const tab = tabs().find((t) => t.querySelector(".tab-label").textContent.trim() === name);
  if (!tab) return false;
  press(tab.querySelector(".tab-label"));
  await settle(20);
  return true;
}

async function main() {
  try {
    if (result.error) throw new Error(result.error);

    stage("shelf");
    await waitFor(() => document.querySelector(".library"), "书架出现");
    await settle(20);
    result.listenerNames = Object.keys(window.__listeners);
    result.dropCbRegistered = typeof window.__dropCb === "function";

    // ---- 连着双击两份 PDF（同一拍里发出两个事件）
    stage("open-two");
    const openPath = window.__listeners["folia://open-path"];
    if (!openPath) throw new Error("folia://open-path 监听器没注册上");
    openPath({ payload: PATH_A });
    openPath({ payload: PATH_B });

    // ---- 等两个标签都出现（串行的话第二份要等第一份跑完才轮到）
    stage("wait-tabs");
    try {
      await waitFor(() => (tabNames().length >= 2 ? true : null), "两个标签", 1500);
    } catch (_) {}
    result.tabNamesAfter = tabNames();
    result.eventsAfterOpen = window.__events.slice();
    result.activeTabAfter = activeTabName();

    // ---- 前提：两份都真的发起了打开
    result.openedA = window.__events.includes(`open:${PATH_A}`);
    result.openedB = window.__events.includes(`open:${PATH_B}`);

    // ---- ★ 每份的页数跟它自己对得上
    //
    // 切到 A 看 `/ 120`、切到 B 看 `/ 37`。重叠的话第一条流水线会读到全局那份
    // （= B）的尺寸，A 的标签就变成 37 页。
    if (await switchTo(DOCS[PATH_A].name)) {
      result.totalA = pageTotal();
      result.stageVisibleA = !!document.querySelector(".stage");
    }
    if (await switchTo(DOCS[PATH_B].name)) {
      result.totalB = pageTotal();
      result.stageVisibleB = !!document.querySelector(".stage");
    }

    // ---- 配对：切回第一份还能切回来
    if (await switchTo(DOCS[PATH_A].name)) {
      result.totalA2 = pageTotal();
      result.activeTabRoundTrip = activeTabName();
    }
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
