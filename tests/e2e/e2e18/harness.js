// 真实管线验证：**只**桩掉 Tauri IPC（文件字节、状态持久化），
// pdf.js 与 bridge.js 全走真实实现。
//
// 为什么值得单独做一套：上一套 harness 里 `renderThumbnail` 是我自己写的，
// 返回的图**天然就是页面比例** —— 于是"图的比例 == 页面比例"这条断言
// 验的是我的桩，不是应用。缩略图到底有没有被裁，只有让真 pdf.js 画出来才知道。
//
// 判据也不靠比例，直接**读像素**：测试 PDF 四角各有一个 40×40 的实心黑方块，
// 只要缩略图裁掉一点，角上就不是黑的。这是"裁没裁"的直接证据。
//
// ⚠️ 书架和阅读器**分两次跑**（`?mode=shelf` / `?mode=reader`），不要合在一起：
// 虚拟时钟推进得比真实时间快得多，一次运行里先等书架缩略图，
// 会把 `--virtual-time-budget` 提前烧完，后面阅读器的渲染就永远完不成
// （表现为正文 canvas 一片白）。分开跑各自有一份预算，互不干扰。

const PDF_URL = "/sample.pdf";
const META = {
  id: "sample",
  path: "/tmp/e2e18/sample.pdf",
  name: "sample.pdf",
  size: 1298,
  numPages: 5,
};

let bytesPromise = null;
function pdfBytes() {
  if (!bytesPromise) {
    bytesPromise = fetch(PDF_URL)
      .then((r) => r.arrayBuffer())
      .then((buf) => Array.from(new Uint8Array(buf)));
  }
  return bytesPromise;
}

window.__invokes = [];
window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "read_pdf_bytes") return pdfBytes();
      if (cmd === "read_pdf_path" || cmd === "open_pdf") return Promise.resolve(META);
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              { ...META, pages: 1, last_page: 1, opened_at: 0, favorite: false },
            ],
            lastPath: META.path,
            lastPage: 1,
            scale: 1,
          })
        );
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      return Promise.resolve(null);
    },
  },
  event: { listen: () => Promise.resolve(() => {}) },
};

// 虚拟时钟下 `requestAnimationFrame` 不触发，而 pdf.js 的 canvas 渲染器
// 靠 rAF 调度 —— 不补这一句，`page.render()` 的 promise 永远不 resolve。
window.requestAnimationFrame = (cb) => window.setTimeout(() => cb(performance.now()), 16);
window.cancelAnimationFrame = (id) => window.clearTimeout(id);

const result = {};
const params = new URLSearchParams(location.search);
const SHOT = params.get("shot");
const MODE = params.get("mode") || (SHOT === "shelf" ? "shelf" : "reader");

function waitFor(predicate, label, maxTicks = 6000) {
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

const settle = async (ticks = 10) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

const round = (n) => Math.round(n * 100) / 100;

/// 把一张 <img> 画到 canvas 上读像素。
function sampleImage(img) {
  const w = img.naturalWidth;
  const h = img.naturalHeight;
  if (!w || !h) return null;
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const data = ctx.getImageData(0, 0, w, h).data;

  const at = (x, y) => {
    const i = (y * w + x) * 4;
    return [data[i], data[i + 1], data[i + 2]];
  };
  const isDark = ([r, g, b]) => r < 90 && g < 90 && b < 90;

  // 四个角往内缩 4px 采样：那里应当是 40×40 黑方块的正中心附近。
  const corners = {
    tl: at(4, 4),
    tr: at(w - 5, 4),
    bl: at(4, h - 5),
    br: at(w - 5, h - 5),
  };
  const centre = at(Math.floor(w / 2), Math.floor(h / 2));

  // 左上角那个 40×40 的黑方块还剩多少：往右 / 往下扫到不黑为止。
  // 页面 595pt 宽画成 w px，40pt 应当 ≈ 40 * w / 595 px。
  let right = 0;
  while (right < w - 2 && isDark(at(right + 1, 4))) right++;
  let down = 0;
  while (down < h - 2 && isDark(at(4, down + 1))) down++;
  let left = 0;
  while (left < w - 2 && isDark(at(w - 2 - left, h - 5))) left++;
  let up = 0;
  while (up < h - 2 && isDark(at(w - 5, h - 2 - up))) up++;

  return {
    w,
    h,
    ratio: round(w / h),
    cornerDark: Object.fromEntries(
      Object.entries(corners).map(([k, v]) => [k, isDark(v)])
    ),
    expectedCornerPx: round((40 * w) / 595),
    cornerExtent: { tlRight: right, tlDown: down, brLeft: left, brUp: up },
    centreIsBlack: isDark(centre),
    centreRgb: centre,
  };
}

/// 把 `object-position` 的两种写法都解析成 0~1 的比例。
function parseObjectPosition(value) {
  const named = { left: 0, center: 0.5, top: 0, right: 1, bottom: 1 };
  const parts = String(value || "50% 50%").trim().split(/\s+/);
  const one = (token, fallback) => {
    if (token === undefined) return fallback;
    if (token in named) return named[token];
    const num = parseFloat(token);
    return Number.isFinite(num) ? num / 100 : fallback;
  };
  let [a, b] = parts;
  // `top center` / `center top` 这类关键字顺序是可以反的，统一成 (x, y)
  const verticalFirst = a === "top" || a === "bottom";
  if (verticalFirst) [a, b] = [b, a];
  return [one(a, 0.5), one(b, 0.5)];
}

/// 算出「图片的哪一块真的被显示出来了」—— 这才是判断有没有裁的依据。
///
/// 直接读源图的像素是**验不出** `cover` 裁剪的：被裁掉的那部分像素
/// 仍然在源图里，只是没画出来。必须按 `object-fit` 的规则反推出可视源矩形，
/// 再看页面的四角标记有没有落在这个矩形里。
function visibleSourceRect(nw, nh, dw, dh, fit, position) {
  const cover = fit === "cover";
  const scale = cover ? Math.max(dw / nw, dh / nh) : Math.min(dw / nw, dh / nh);
  const vw = dw / scale;
  const vh = dh / scale;
  const [fx, fy] = parseObjectPosition(position);
  const x0 = (nw - vw) * fx;
  const y0 = (nh - vh) * fy;
  return { x0, y0, x1: x0 + vw, y1: y0 + vh, vw, vh };
}

/// 量一个缩略图（框 + 图 + 像素 + 可视源矩形）。
function measureThumb(frame) {
  const img = frame.querySelector("img");
  const fr = frame.getBoundingClientRect();
  const ir = img.getBoundingClientRect();
  const cs = getComputedStyle(img);
  const nw = img.naturalWidth;
  const nh = img.naturalHeight;

  const vis = visibleSourceRect(nw, nh, ir.width, ir.height, cs.objectFit, cs.objectPosition);

  // 源图里 40pt 的角方块有多大；四个方块都必须**完整**落在可视矩形里。
  const sc = nw / 595;
  const sq = 40 * sc;
  const squares = {
    tl: [0, 0, sq, sq],
    tr: [nw - sq, 0, nw, sq],
    bl: [0, nh - sq, sq, nh],
    br: [nw - sq, nh - sq, nw, nh],
  };
  const eps = 0.6;
  const inside = (r) =>
    r[0] >= vis.x0 - eps && r[2] <= vis.x1 + eps && r[1] >= vis.y0 - eps && r[3] <= vis.y1 + eps;

  return {
    inlineAspectRatio: frame.style.aspectRatio,
    frameRatio: round(fr.width / fr.height),
    frameBox: { w: round(fr.width), h: round(fr.height) },
    natural: { w: nw, h: nh, ratio: round(nw / nh) },
    shownRatio: round(ir.width / ir.height),
    objectFit: cs.objectFit,
    objectPosition: cs.objectPosition,
    srcPrefix: img.src.slice(0, 30),
    srcLength: img.src.length,
    // 可视源矩形（源图像素坐标）
    visible: {
      x0: round(vis.x0),
      y0: round(vis.y0),
      x1: round(vis.x1),
      y1: round(vis.y1),
    },
    // 各边被裁掉多少源像素（>0 就是裁了）
    cropPx: {
      left: round(Math.max(0, vis.x0)),
      top: round(Math.max(0, vis.y0)),
      right: round(Math.max(0, nw - vis.x1)),
      bottom: round(Math.max(0, nh - vis.y1)),
    },
    cropFraction: round(1 - (vis.vw * vis.vh) / (nw * nh)),
    // ★ 决定性判据：四个角方块是不是都完整可见
    cornersVisible: Object.fromEntries(
      Object.entries(squares).map(([k, r]) => [k, inside(r)])
    ),
    pixels: sampleImage(img),
  };
}

// ---------------------------------------------------------------- 书架
async function runShelf() {
  await waitFor(() => document.querySelector(".file-card"), "书架卡片");
  const frame = await waitFor(() => {
    const f = document.querySelector(".file-thumb");
    const im = f && f.querySelector("img");
    return im && im.naturalWidth > 0 ? f : null;
  }, "书架缩略图（真 pdf.js）");
  await settle(10);
  result.shelf = measureThumb(frame);
  result.thumbCount = document.querySelectorAll(".file-thumb").length;
  result.imgCount = document.querySelectorAll(".file-thumb img").length;
}

// ---------------------------------------------------------------- 阅读器
async function runReader() {
  const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片");
  card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
  await waitFor(() => document.querySelector(".stage"), "阅读器出现");

  const frame = await waitFor(() => {
    const f = document.querySelector(".thumb-frame");
    const im = f && f.querySelector("img");
    return im && im.naturalWidth > 0 ? f : null;
  }, "侧栏缩略图（真 pdf.js）");
  await settle(20);
  result.sidebar = measureThumb(frame);

  // 正文页面：作为"没有被裁"的对照组
  const page = await waitFor(() => {
    const p = document.querySelector('.page[data-page="1"]');
    const c = p && p.querySelector(".page-canvas");
    return c && c.width > 0 ? p : null;
  }, "正文页 canvas");
  const canvas = page.querySelector(".page-canvas");
  result.page = {
    canvasW: canvas.width,
    canvasH: canvas.height,
    canvasRatio: round(canvas.width / canvas.height),
    boxW: round(page.getBoundingClientRect().width),
    boxH: round(page.getBoundingClientRect().height),
  };

  // 正文 canvas 也读一下四角：它**没有**被裁，是缩略图的对照组
  try {
    const c2 = document.createElement("canvas");
    c2.width = canvas.width;
    c2.height = canvas.height;
    const ctx2 = c2.getContext("2d", { willReadFrequently: true });
    ctx2.drawImage(canvas, 0, 0);
    const d2 = ctx2.getImageData(0, 0, canvas.width, canvas.height).data;
    const at2 = (x, y) => {
      const i = (y * canvas.width + x) * 4;
      return [d2[i], d2[i + 1], d2[i + 2]];
    };
    const dark2 = ([r, g, b]) => r < 90 && g < 90 && b < 90;
    result.page.cornerDark = {
      tl: dark2(at2(4, 4)),
      tr: dark2(at2(canvas.width - 5, 4)),
      bl: dark2(at2(4, canvas.height - 5)),
      br: dark2(at2(canvas.width - 5, canvas.height - 5)),
    };
  } catch (e) {
    result.page.cornerError = String(e);
  }

  // ---- 顶边对齐：侧栏面板 / 正文滚动区 / 页面 三者的上边缘应当在同一条水平线上
  //
  // 侧栏和 `.stage` 是同一行的兄弟，本来就齐平；真正的坑是 `.pages` 的顶部内边距
  // —— 它会只把页面往下推，看起来就是"页面和缩略图没对齐"。
  const stage = document.querySelector(".stage");
  const side = document.querySelector(".sidebar");
  const pages = document.querySelector(".pages");
  const cs = getComputedStyle(pages);
  result.align = {
    sidebarTop: round(side.getBoundingClientRect().top),
    stageTop: round(stage.getBoundingClientRect().top),
    pageTop: round(page.getBoundingClientRect().top),
    pagesPaddingTop: cs.paddingTop,
    pagesPaddingBottom: cs.paddingBottom,
    // 页面比侧栏低多少（0 = 对齐）
    pageMinusSidebar: round(
      page.getBoundingClientRect().top - side.getBoundingClientRect().top
    ),
    // 页面在正文区里水平居中吗
    pageLeftGap: round(page.getBoundingClientRect().left - stage.getBoundingClientRect().left),
    pageRightGap: round(stage.getBoundingClientRect().right - page.getBoundingClientRect().right),
  };
  // ---- 跳页：验证 `layout.rs` 里算出来的偏移量和真实 DOM 一致
  //
  // `.pages` 的顶部内边距是 layout 不知道的，所以它一旦不为 0，
  // `scroll_to_page` 和「当前是第几页」都会偏掉同样的距离。这里跳三次，
  // 每次都要求目标页的上边缘落在正文区顶部附近（源码里是 offset - 12）。
  const items = [...document.querySelectorAll(".thumb-item")];
  result.thumbCount = items.length;
  result.jumps = [];
  for (const target of [2, 3, 4]) {
    if (items.length < target) break;
    items[target - 1].dispatchEvent(
      new MouseEvent("click", { bubbles: true, cancelable: true, view: window })
    );
    await settle(24);
    const el = document.querySelector(`.page[data-page="${target}"]`);
    result.jumps.push({
      target,
      rendered: !!el,
      topInStage: el
        ? round(el.getBoundingClientRect().top - stage.getBoundingClientRect().top)
        : null,
      activeThumb: document.querySelector(".thumb-item.active .thumb-num")?.textContent ?? null,
      pageInput: document.querySelector(".page-input")?.value ?? null,
    });
  }
}

async function main() {
  try {
    if (MODE === "shelf") {
      await runShelf();
    } else {
      await runReader();
    }
    if (SHOT) {
      result.stoppedAt = MODE;
      throw { shot: true };
    }
  } catch (err) {
    if (!(err && err.shot)) {
      result.error = String((err && err.message) || err);
      result.stack = String((err && err.stack) || "");
    }
  }

  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
}

main();
