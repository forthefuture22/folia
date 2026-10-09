// 验证「缩略图缓存会不会随最近列表淘汰而释放」（e2e29）。
//
// 背景：书架会给每个「最近」条目渲染首页缩略图，bridge.js 把每份 PDF 的
// `PDFDocumentProxy` 按 path 缓存进 `thumbDocs` —— 那个代理扣着整份 PDF 的
// 字节与解析结果。只有 `releaseThumbnail(path)` 会把它放掉。
//
// 原先只有「删除记录」和「清空最近打开」两处记得放，而 `record_open` 里那个
// `list.truncate(10)` 把第 11 条挤出去时**没人放** —— 那份代理就永久常驻了。
// 更糟的是 Rust 侧那份 `thumbs` 快照还留着它的 id，于是以后也不会再请求它，
// 等于再也回收不掉。缓存按"本次会话里出现过的所有文件"增长，书架却只显示 10 个。
//
// 修法：把「前后差集」放进 `write_recent`（最近列表的唯一读写入口），
// 消失的 path 一律 `release_thumbnail`。这个套件钉的就是它：
//
//   连着打开 12 份不同的 PDF（上限是 10），然后查
//     · 被挤出列表的那两份，**有没有**调 releaseThumbnail
//     · 还在列表里的那十份，**有没有被误伤**
//
// 只桩 Tauri IPC 和 bridge 的几个方法，其余（含真实 wasm）全走真实现。

const PAGES = 4;
const PAGE_W = 595;
const PAGE_H = 842;
const N = 12; // 打开多少份
const CAP = 10; // 最近列表上限（见 handles.rs 里那句 truncate(10)）

const paths = Array.from({ length: N }, (_, i) => `/tmp/evict-${i + 1}.pdf`);
const nameOf = (p) => String(p).split("/").pop();

window.__logs = [];
{
  const orig = console.log;
  console.log = (...args) => {
    try {
      window.__logs.push(args.map((a) => String(a)).join(" "));
    } catch (_) {}
    return orig.apply(console, args);
  };
}

window.__invokes = [];
window.__saved = [];
window.__listeners = {};
window.__rendered = []; // renderThumbnail 请求过的 path（按调用顺序，含重复）
window.__released = []; // releaseThumbnail 被调用过的 path

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        // 从**空**历史开始：这样"被挤出去的是哪几份"完全由本套件决定，
        // 不用去猜预置数据会不会掺进来。
        return Promise.resolve(
          JSON.stringify({ recentFiles: [], lastPath: null, lastPage: null, scale: null })
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
  event: {
    listen: (name, cb) => {
      window.__listeners[name] = cb;
      return Promise.resolve(() => {});
    },
  },
};

const bridge = window.FoliaBridge;
if (!bridge) {
  throw new Error("bridge.js 没有先执行：桩的插入位置不对");
}

// ⚠️ 不能整个替换 `onFileDrop`：bridge.js 里那个函数除了回调之外，还会自己
// `listen("tauri://drag-drop")` 和 `listen("folia://open-path")`。覆盖掉它，
// 应用就再也收不到「直接打开」那条路了。包一层，让原实现照常注册监听。
const realOnFileDrop = bridge.onFileDrop;
bridge.onFileDrop = (cb) => {
  window.__dropCb = cb;
  realOnFileDrop(cb);
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

Object.assign(bridge, {
  openDocumentPath: (path) => {
    const p = String(path || "/tmp/unknown.pdf");
    const name = nameOf(p);
    return Promise.resolve(
      JSON.stringify({
        ok: true,
        cancelled: false,
        error: null,
        meta: { id: `doc-${name}`, path: p, name, size: 24000, numPages: PAGES },
      })
    );
  },
  getPageSizes: () =>
    Promise.resolve(
      JSON.stringify(Array.from({ length: PAGES }, () => ({ width: PAGE_W, height: PAGE_H })))
    ),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  // 这两处是本套件的**观测点**：只记录，不实现真实缓存。
  // 真实现住在 bridge.js 里，这里要验的是「Rust 侧有没有在正确的时机调它们」。
  renderThumbnail: (path, _page, maxWidth) => {
    window.__rendered.push(String(path));
    return Promise.resolve(makeThumb(maxWidth));
  },
  releaseThumbnail: (path) => {
    window.__released.push(String(path));
    return undefined; // bridge.js 里这个是同步的，Rust 侧按 `-> ()` 绑
  },
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

// ---------------------------------------------------------------- 驱动

const result = { n: N, cap: CAP };

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
const cards = () => [...document.querySelectorAll(".file-card")];
const cardNames = () =>
  cards().map((c) => c.querySelector(".file-title")?.textContent.trim());

const lastSavedNames = () => {
  const s = window.__saved[window.__saved.length - 1];
  return s && s.recentFiles ? s.recentFiles.map((f) => f.name) : null;
};

async function goHome() {
  const home = document.querySelector(".tabbar-home");
  if (home) {
    press(home);
    await waitFor(() => document.querySelector(".library"), "回到书架");
  }
  await settle(20);
}

async function main() {
  try {
    // 应用挂上（书架是空历史，所以不能等 `.file-card`）。
    await waitFor(() => document.querySelector(".app"), "应用挂载");
    await settle(20);

    const cb = window.__listeners["folia://open-path"];
    if (!cb) throw new Error("folia://open-path 监听器没注册上");

    // 一份一份**顺序**打开 —— 并发发出去的话，谁先被 truncate 掉就不确定了，
    // 而下面要断言"被挤掉的正是最前面那两份"。
    for (let i = 0; i < N; i++) {
      const name = nameOf(paths[i]);
      cb({ payload: paths[i] });
      await waitFor(() => tabNames().includes(name), `第 ${i + 1} 份（${name}）打开`);
      await settle(16);
    }
    result.tabCount = tabNames().length;
    result.tabNames = tabNames();

    result.rendered = [...new Set(window.__rendered)];
    result.released = [...window.__released];

    await goHome();
    result.cardNames = cardNames();
    result.savedNames = lastSavedNames();
    result.consoleLogs = window.__logs;
  } catch (err) {
    result.error = String((err && err.message) || err);
    result.stack = String((err && err.stack) || "");
  }
  finish();
}

function finish() {
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
  window.__HARNESS_DONE__ = true;
}

main();
