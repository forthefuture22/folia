// 验证深色模式（/tmp/e2e23）。
//
// 这一套**不点任何按钮**，只干一件事：把两套主题下"关键元素算出来的样式"抄下来，
// 交给 verify.py 比对。理由 —— 深色模式是个纯 CSS 特性，没有"行为"可测，
// 能测的是「同一批选择器在两个主题下算出的值确实不同，且各自等于预期」。
//
// 主题开关是 `html.theme-dark` 这一个类。无头 Chrome 切不了系统偏好，
// 所以 verify.py 在**主题脚本之前**塞了一段 matchMedia 桩，把偏好钉成
// 当前用例要的那个值（light 钉 light、dark 钉 dark）—— 不钉的话，
// 宿主机入夜自动转深色，`case=light` 那趟就会跟着变深，用例凭空红。
// 让 index.html 里那段真实的脚本自己去读、自己去挂类，测的是真实链路。
//
// 只桩 Tauri IPC，bridge.js 全走真实实现。

const PAGES = 12;
const PAGE_W = 595;
const PAGE_H = 842;

const CASE = new URLSearchParams(location.search).get("case") || "light";
const SHOT = new URLSearchParams(location.search).get("shot") || null;

const RECENTS = [
  { id: "doc-h23-1.pdf", path: "/tmp/h23-1.pdf", name: "h23-1.pdf" },
];

window.__invokes = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd) => {
      window.__invokes.push({ cmd });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: RECENTS.map((r) => ({
              ...r,
              size: 24000,
              pages: PAGES,
              last_page: 1,
              opened_at: 0,
              favorite: false,
            })),
            lastPath: RECENTS[0].path,
            lastPage: 1,
            scale: 1,
          })
        );
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "take_pending_open") return Promise.resolve(null);
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      if (cmd === "set_default_pdf_handler") return Promise.resolve(null);
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
if (!bridge) {
  throw new Error("bridge.js 没有先执行：桩的插入位置不对");
}

Object.assign(bridge, {
  openDocumentPath: (path) => {
    const p = String(path || "/tmp/unknown.pdf");
    const name = p.split("/").pop() || "unknown.pdf";
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

const settle = async (ticks = 10) => {
  for (let i = 0; i < ticks; i++) await new Promise((r) => setTimeout(r, 16));
};

function press(el, opts = {}) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1, ...opts })
    );
  }
}

/** 根元素上的某个 CSS 自定义属性。 */
function token(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/** 现成元素算出来的某个属性。 */
function styleOf(sel, prop, pseudo = null) {
  const el = document.querySelector(sel);
  if (!el) return null;
  return getComputedStyle(el, pseudo).getPropertyValue(prop).trim();
}

/** 造一段临时 DOM 量样式，量完立刻摘掉 —— 用来量那些"当前页面里碰巧没有"的元素
 *  （搜索命中片段、兜底封面、正文检索框…）。 */
function probe(markup, sel, prop) {
  const host = document.createElement("div");
  host.style.position = "fixed";
  host.style.left = "-9999px";
  host.style.top = "0";
  host.innerHTML = markup;
  document.body.appendChild(host);
  const el = host.querySelector(sel);
  const value = el ? getComputedStyle(el).getPropertyValue(prop).trim() : null;
  host.remove();
  return value;
}

async function main() {
  try {
    // ---- 截图模式：只把界面摆到该有的样子，不做任何断言（verify.py 不跑这一支）
    if (SHOT) {
      await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
      await settle(20);
      if (SHOT === "shelf") {
        finish();
        return;
      }
      press(document.querySelector(".file-card"));
      await waitFor(() => document.querySelector(".page-canvas"), "正文出现");
      await settle(80);
      if (SHOT === "overview") {
        press(document.querySelector(".reader-top-left .icon-btn"));
        await settle(40);
      }
      finish();
      return;
    }

    // ---- 1. 主题类本身（由 index.html 那段真实脚本挂上的）
    result.themeClass = document.documentElement.classList.contains("theme-dark");
    // 宿主机的真实偏好：桩只钉应用看到的那个值，这个字段留作排查用。
    result.hostPrefersDark = window.__HOST_MQ_DARK__ === true;

    // ---- 2. 两套 tokens
    result.tokens = {};
    for (const name of [
      "--bg",
      "--surface",
      "--panel",
      "--text",
      "--text-2",
      "--border",
      "--accent",
      "--hl-yellow",
      "--mk-yellow",
      "--danger",
    ]) {
      result.tokens[name] = token(name);
    }

    // ---- 3. 外壳（书架）算出来的底色 / 字色
    await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    await settle(20);
    result.bodyBg = styleOf("body", "background-color");
    result.bodyColor = styleOf("body", "color");
    result.thumbImgFilter = styleOf(".file-thumb img", "filter");

    // ---- 4. 打开一份文档，量正文区
    press(document.querySelector(".file-card"));
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");
    await settle(30);
    result.canvasExists = !!document.querySelector(".page-canvas");
    result.pageCanvasFilter = styleOf(".page-canvas", "filter");
    result.pageBg = styleOf(".page", "background-color");
    result.thumbFrameImgFilter = styleOf(".thumb-frame img", "filter");

    // ---- 5. 批注层 / 检索层：靠临时 DOM 量
    result.foliaMarkBlend = probe(
      '<div class="hl-layer"><div class="folia-mark folia-mark-highlight"></div></div>',
      ".folia-mark",
      "mix-blend-mode"
    );
    result.markShBg = probe(
      '<div class="textLayer"><mark class="sh">x</mark></div>',
      "mark.sh",
      "background-color"
    );

    // ---- 6. 侧栏 / 书架里那几个写死颜色的散件
    result.hitMarkBg = probe('<span class="hit-mark">x</span>', ".hit-mark", "background-color");
    result.hitMarkColor = probe('<span class="hit-mark">x</span>', ".hit-mark", "color");
    result.fallbackColor = probe(
      '<div class="file-thumb-fallback"><span class="fallback-name">x</span>' +
        '<span class="fallback-badge">PDF</span></div>',
      ".file-thumb-fallback",
      "color"
    );
    result.fallbackBadgeColor = probe(
      '<div class="file-thumb-fallback"><span class="fallback-name">x</span>' +
        '<span class="fallback-badge">PDF</span></div>',
      ".fallback-badge",
      "color"
    );
    // 右键菜单：面板底色跟 token 走，危险项在深色下换成亮红。
    // （原来是探卡片上那颗 `.file-action` 悬浮按钮的"白药丸 → 深药丸"，
    //  但那两颗按钮已经删掉了 —— 收藏 / 删除都进了右键菜单，改探菜单。）
    result.ctxMenuBg = probe(
      '<div class="ctx-menu"><button class="ctx-menu-item danger">x</button></div>',
      ".ctx-menu",
      "background-color"
    );
    result.ctxMenuDangerColor = probe(
      '<div class="ctx-menu"><button class="ctx-menu-item danger">x</button></div>',
      ".ctx-menu-item.danger",
      "color"
    );

    // ---- 7. 总览浮层的底色（跟 tokens 走，顺带确认没写死白）
    press(document.querySelector(".reader-top-left .icon-btn"));
    await settle(20);
    result.poPanelBg = styleOf(".po-panel", "background-color");
    result.poOverlayBg = styleOf(".page-overview", "background-color");
    document.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
    );
    await settle(10);
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
