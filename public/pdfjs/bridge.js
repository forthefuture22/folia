// Folia ⇄ pdf.js 桥接层
//
// 职责边界：
//   - 本文件只负责「与 pdf.js 和 Tauri IPC 打交道」的底层能力（打开、渲染、取文本、搜索、选区几何）。
//   - 所有 UI 状态、布局、批注模型都由 Rust/Yew 侧持有，这里不保存业务状态。
//
// 以 window.FoliaBridge 的形式暴露给 Rust（wasm-bindgen 通过 js_namespace 绑定），
// 因此本文件不依赖打包器，直接用 <script type="module"> 引入即可。
// pdf.js 采用动态 import，保证 window.FoliaBridge 在脚本执行时立刻可用（无加载竞态）。

const PDF_DIR = new URL("./", import.meta.url);

let libPromise = null;
function lib() {
  if (!libPromise) {
    libPromise = import("./pdf.mjs").then((mod) => {
      mod.GlobalWorkerOptions.workerSrc = new URL("./pdf.worker.mjs", PDF_DIR).href;
      return mod;
    });
  }
  return libPromise;
}

function invoke(cmd, args) {
  const tauri = window.__TAURI__;
  if (!tauri || !tauri.core) {
    return Promise.reject(new Error("Tauri API 不可用（请通过 Tauri 启动，而不是直接用浏览器打开）"));
  }
  return tauri.core.invoke(cmd, args);
}

// ---------------------------------------------------------------- 状态

let pdfDoc = null;
let currentMeta = null;
const textCache = new Map(); // page -> TextContent
const viewCache = new Map(); // page -> [x0,y0,x1,y1]
const renderTasks = new Map(); // canvasId -> RenderTask

/**
 * 一页文本内容的缓存条数上限。
 *
 * ⚠️ 这个上限**必须**有。`search()` 会把整本 PDF 逐页 `getTextContent()` 走一遍，
 * 一页的 `TextContent.items` 是「一个对象一个字符片段」的数组
 * （`str` / `transform` 六个数字 / `width` / `height` / `fontName` / `dir` / `hasEOL`），
 * 密排的文字页动辄上千条 → 单页几十到几百 KB。**没有上限就是"搜一次全文，
 * 整本的文本层就永久留在内存里"**（500 页 ≈ 几十到上百 MB，且再也回不去）。
 *
 * 16 是够用的：阅读区同时渲染的只有可视窗口那几页（`layout.rs` 的
 * `visible_range` 再外扩 600px），侧栏缩略图不读文本层。
 *
 * ⚠️ `viewCache` 必须**跟着一起淘汰**，它同样是"无界"的：每页只有 4 个数
 * （`p.view`），但 `search()` 每页都要读它拿页面高度去算命中的 y 坐标，
 * 所以不能单独删掉"还在 textCache 里的页"的几何。两个缓存的键集**始终一致**
 * —— 进 `textCache` 必进 `viewCache`，淘汰也一起淘汰，留着没有文本的几何没意义。
 */
const TEXT_CACHE_LIMIT = 16;

function resetCaches() {
  for (const task of renderTasks.values()) {
    try {
      task.cancel();
    } catch (_) {}
  }
  renderTasks.clear();
  textCache.clear();
  viewCache.clear();
  pageCanvases.clear();
  pageImageQuads.clear();
}

/**
 * 画到 canvas 上时的设备像素比上限。
 *
 * 位图字节数 = 宽 × 高 × dpr² × 4，所以 dpr 从 2 降到 1.5 就直接省掉 **44%**
 * （A4 页在缩放 1.25 下：12.5 MB → 6.7 MB）。2 倍只有在放大到 200% 以上、
 * 盯着字看时才看得出差别，日常阅读不值得为它多占一倍多的内存。
 */
const MAX_DPR = 1.5;

async function getPage(page) {
  return pdfDoc.getPage(page);
}

async function getTextContent(page) {
  const cached = textCache.get(page);
  if (cached) {
    // 命中也要挪到队尾 —— 否则淘汰的是"最早放进来的"，而不是"最久没用过的"。
    // 搜完全文之后窗口里那几页会被后 15 页顶掉，正是这条在兜住它。
    touch(page, cached);
    return cached;
  }
  const p = await getPage(page);
  const view = p.view.slice();
  const content = await p.getTextContent();
  viewCache.set(page, view);
  touch(page, content);
  return content;
}

/**
 * 把一页放进文本缓存并挪到队尾，超限就淘汰队头那一页（连它的几何一起）。
 *
 * ⚠️ 两个 `Map` 的键集必须**始终一致**，所以只有这一个入口能改它们 ——
 * 在任何别的地方 `set`/`delete` 都会让两边错位，`search()` 就会给某一页
 * 算错 y 坐标（退回默认视口 `[0,0,612,792]`）。
 */
function touch(page, content) {
  textCache.delete(page);
  textCache.set(page, content);
  const view = viewCache.get(page);
  if (view) {
    viewCache.delete(page);
    viewCache.set(page, view);
  }
  while (textCache.size > TEXT_CACHE_LIMIT) {
    const oldest = textCache.keys().next().value;
    textCache.delete(oldest);
    viewCache.delete(oldest);
  }
}

// ---------------------------------------------------------------- 打开文档

function toBytes(value) {
  if (value instanceof ArrayBuffer) return new Uint8Array(value);
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  if (Array.isArray(value)) return new Uint8Array(value);
  if (typeof value === "string") {
    const bin = atob(value);
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  }
  throw new Error("无法识别的字节数据格式");
}

async function loadDocument(meta) {
  const { getDocument } = await lib();
  if (pdfDoc) {
    try {
      await pdfDoc.destroy();
    } catch (_) {}
    pdfDoc = null;
  }
  resetCaches();

  const raw = await invoke("read_pdf_bytes", { path: meta.path });
  const data = toBytes(raw);

  const task = getDocument({
    data,
    cMapUrl: new URL("./cmaps/", PDF_DIR).href,
    cMapPacked: true,
    standardFontDataUrl: new URL("./standard_fonts/", PDF_DIR).href,
  });

  task.onPassword = (updatePassword, reason) => {
    const label = reason === 1 ? "该 PDF 已加密，请输入密码：" : "密码不正确，请重新输入：";
    const pwd = window.prompt(label, "");
    if (pwd === null) {
      task.destroy();
      return;
    }
    updatePassword(pwd);
  };

  pdfDoc = await task.promise;
  currentMeta = { ...meta, numPages: pdfDoc.numPages };
  return currentMeta;
}

export async function openDocumentDialog() {
  const meta = await invoke("open_pdf");
  if (!meta) return JSON.stringify({ ok: false, cancelled: true });
  try {
    const loaded = await loadDocument(meta);
    return JSON.stringify({ ok: true, meta: loaded });
  } catch (err) {
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

export async function openDocumentPath(path) {
  try {
    const meta = await invoke("read_pdf_path", { path });
    const loaded = await loadDocument(meta);
    return JSON.stringify({ ok: true, meta: loaded });
  } catch (err) {
    return JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) });
  }
}

export function closeDocument() {
  if (pdfDoc) {
    try {
      pdfDoc.destroy();
    } catch (_) {}
  }
  pdfDoc = null;
  currentMeta = null;
  resetCaches();
}

// ---------------------------------------------------------------- 缩略图
//
// 书架卡片和侧栏都要「某一页长什么样」。
//
// ⚠️ 这里的独立实例**用完就放**，不要长期缓存。理由：
//   · 一个 `PDFDocumentProxy` 扣着**整份 PDF 的字节** + 自己的解析结构 +
//     **自己的 pdf.js worker**（`getDocument` 不传 `worker` 时，pdf.js 每次都
//     `new PDFWorker`）。书架一次要 10 份（最近列表上限），长期缓存就是
//     10 份 PDF 全量常驻。
//   · 结果本身（JPEG data URL）由 Rust 侧缓存着，这里没有"留着实例省一次渲染"
//     的收益 —— 每个 path 在一次书架渲染里只会被问一次。
//
// ⚠️ **当前正在阅读的那份直接复用主 `pdfDoc`**，不要另开一份。否则同一份 PDF
// 会常驻两遍（两份字节 + 两个 worker），而 `closeDocument` 只 destroy 主那份
// —— 用户看到的就是"关掉 PDF 之后内存降不下来"。侧栏（含"渲染全部页"那条路）
// 要的都是当前文档的页，复用之后这批请求一份新实例都不开。

/** 缩略图用的独立实例。**只**给「不是当前正在阅读的」文档用，且用完即放。 */
const thumbDocs = new Map(); // path -> Promise<PDFDocumentProxy>

function openThumbDoc(path) {
  if (thumbDocs.has(path)) return thumbDocs.get(path);
  const promise = (async () => {
    const { getDocument } = await lib();
    const raw = await invoke("read_pdf_bytes", { path });
    const task = getDocument({
      data: toBytes(raw),
      cMapUrl: new URL("./cmaps/", PDF_DIR).href,
      cMapPacked: true,
      standardFontDataUrl: new URL("./standard_fonts/", PDF_DIR).href,
    });
    // 加密文档直接放弃，否则会一直挂在密码提示上。
    task.onPassword = () => task.destroy();
    return task.promise;
  })();
  thumbDocs.set(path, promise);
  return promise;
}

/** 放掉某一份缩略图实例。 */
function dropThumbDoc(path) {
  const promise = thumbDocs.get(path);
  if (!promise) return;
  thumbDocs.delete(path);
  promise.then((doc) => doc.destroy()).catch(() => {});
}

/**
 * 每条路径**正在渲染中**的缩略图调用数。
 *
 * ⚠️ 存在的理由是一个真实的竞态：书架那条 effect 会在 `recent` 变化时**重跑**，
 * 而旧任务不会立刻退出 —— 它要等手上那次 `await thumbnail(...)` 回来才检查代次。
 * 于是同一个 path 会有**两个** `renderThumbnail` 同时在飞，它们从 `openThumbDoc`
 * 拿到的是**同一个** `PDFDocumentProxy`。先回来的那个在 `finally` 里
 * `dropThumbDoc` → `doc.destroy()`，把还在渲染的那个的 `page.render()` 一起打掉；
 * 它的 `catch` 吞掉异常返回空串 → **那张缩略图静默消失**（旧任务又已经因为代次
 * 被作废，不会再补上）。修法：最后一个使用者走了才真放。
 */
const thumbRefs = new Map(); // path -> 在飞计数

function retainThumbDoc(path) {
  thumbRefs.set(path, (thumbRefs.get(path) || 0) + 1);
}

function releaseThumbDoc(path) {
  const left = (thumbRefs.get(path) || 1) - 1;
  if (left > 0) {
    thumbRefs.set(path, left);
    return;
  }
  thumbRefs.delete(path);
  dropThumbDoc(path);
}

/**
 * 释放某一份文档的缩略图实例（从最近列表移除该文件时调用）。
 *
 * ⚠️ 有人在飞就**推迟**到它自己收尾（那次 `releaseThumbDoc` 会补上释放）——
 * 否则 `doc.destroy()` 会打断正在跑的那次渲染（见 `thumbRefs`）。
 */
export function releaseThumbnail(path) {
  const key = String(path);
  if (thumbRefs.has(key)) return;
  dropThumbDoc(key);
}

/** 这份文档是不是当前正在阅读的那份（→ 该复用主实例）。 */
function isCurrentDoc(path) {
  return Boolean(pdfDoc && currentMeta && String(currentMeta.path) === String(path));
}

/** 缩略图路径上"顺手放掉算子表"的次数（**诊断用**，见 `renderThumbnail`）。 */
let thumbCleanups = 0;

/**
 * 缩略图里"按深色合成过"的张数（**诊断用**）。
 *
 * 深色下每张缩略图都要额外走一次 `getOperatorList()` + 一趟逐像素反相，这是
 * **看不见的开销**；而"深色下缩略图到底是深是浅"只能靠取像素来判断（见 `e2e23`）。
 * 这个计数让"这条路径到底跑没跑"变成一个可断言的数。
 */
let thumbDark = 0;

/**
 * 渲染某一页的缩略图。
 * @returns {Promise<string>} JPEG data URL；失败时返回空串。
 */
export async function renderThumbnail(path, pageNumber, maxWidth) {
  const key = String(path);
  // 当前文档：借主实例用。借来的**不能**在 finally 里放掉 —— 它归 `closeDocument` 管。
  const borrowed = isCurrentDoc(key);
  // 独立实例要登记"有人在用"：同路径并发时，先回来的那个不能把它 destroy 掉。
  if (!borrowed) retainThumbDoc(key);
  try {
    const doc = borrowed ? pdfDoc : await openThumbDoc(key);
    const page = await doc.getPage(pageNumber || 1);

    const base = page.getViewport({ scale: 1 });
    const width = Math.max(40, Number(maxWidth) || 200);
    const scale = width / base.width;
    const viewport = page.getViewport({ scale });

    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(viewport.width));
    canvas.height = Math.max(1, Math.round(viewport.height));

    const ctx = canvas.getContext("2d", { alpha: false });
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    await page.render({ canvasContext: ctx, viewport }).promise;

    // ---- 深色主题：缩略图要"深色页面 + 原色图片"
    //
    // ⚠️ 必须在下面那次 `page.cleanup()` **之前**做完：`cleanup()` 会把整份
    // `_intentStates` 清掉，之后再来要算子表就是从内容流重新解析一遍。
    //
    // ⚠️ 只在深色下做。`collectImageQuads` 走的是 `getOperatorList()`，它和
    // `render()` 的 intent 不同（多一个 `OPLIST` 位）→ worker 会**再解析一遍内容流**。
    // 浅色下这张合成图根本用不到（CSS 那边也不给缩略图加滤镜），白付这份钱 ——
    // 而浅色正是大多数人的默认状态。这条判断和阅读区 `pageImageQuads` 同一条纪律。
    let url = "";
    if (isDarkTheme()) {
      try {
        const quads = await collectImageQuads(page, viewport, 1);
        url = composeDarkThumbnail(canvas, quads).toDataURL("image/jpeg", 0.75);
        thumbDark += 1;
      } catch (_) {
        // 合成失败就退回"真实页面"那张：宁可浅色，也不要没有缩略图。
        url = "";
      }
    }
    if (!url) url = canvas.toDataURL("image/jpeg", 0.75);

    // ⚠️ 借用主实例时，这一页的算子表是"渲染缩略图"的**副产品**，得自己收拾。
    //
    // 阅读区的释放靠 `page_view.rs`：页**离开保留窗口**时调 `releasePage`。但缩略图
    // 渲染过的页（侧栏按需那十几页、总览那**一整本**）**从来没进过保留窗口** ——
    // 那条释放分支永远不会为它们触发，于是整本的算子表一直留到 `closeDocument`。
    //
    // ⚠️ 但**阅读区正在看的那几页要跳过**：缩略图和阅读区共用**同一个
    // `PDFPageProxy`**，而 `cleanup()` 清的是它的**整份 `_intentStates`**
    // （含阅读区那条 DISPLAY 记录）→ 往回滚一页就要重解析一次内容流。
    // 而侧栏会自动滚到当前页，也就是缩略图和阅读区**看的是同一批页** ——
    // 这条"顺手清"会反复打掉阅读区刚建好的算子表。那几页归 `releasePage` 管
    // （离开保留窗口时），不该在这里被清。
    // 非借用那条路不用管 —— 那份独立实例马上会在 `finally` 里被 `destroy()`。
    if (borrowed) {
      if (!isInReader(pageNumber || 1)) {
        thumbCleanups += 1;
        // 返回 `false` = 这一页还有渲染任务在飞，不是错误（pdf.js 自己会补一次）。
        try { page.cleanup(); } catch (_) {}
      }
    }
    return url;
  } catch (_) {
    // 让下一次调用重新尝试打开（可能是文件被移走/损坏）。
    if (!borrowed) thumbDocs.delete(key);
    return "";
  } finally {
    // 用完就放：一张缩略图没理由让整份 PDF 继续常驻。
    // ⚠️ 走计数版 `releaseThumbDoc` 而不是直接 `dropThumbDoc` —— 同路径可能还有
    // 别的渲染在飞（见 `thumbRefs`）。
    if (!borrowed) releaseThumbDoc(key);
  }
}

export function documentInfo() {
  return JSON.stringify(currentMeta);
}

/**
 * 桥接层几个"看不见的不变量"（**诊断用**，只读）。
 *
 * `textCache` 的条数上限、以及"缩略图渲染完有没有顺手放掉算子表"，从外面完全
 * 看不见 —— 而它们正是「搜一次全文会不会把整本 PDF 的文本永久留在内存里」和
 * 「翻一遍总览会不会把整本的算子表留下来」的分水岭。
 * 给测试和排查留一个口子，比"靠断言 DOM 去猜"可靠。
 *
 * 后加的六个（`renderTasks` / `imageQuads` / `quads` / `canvases` / `layers` / `thumbDocs`）
 * 是同一套路子：**内存问题只能靠"数有几个"来量**，数不到就没法判断
 * "翻完一遍之后该还的还了没有"。见 `tests/e2e/e2e41`。
 */
export function cacheStats() {
  // `layers` 是**当前 DOM 里**的图片层小 canvas 总数 —— 只数容器不够：
  // 容器是 `page_view.rs` 静态渲染的（每页一个），里面的 canvas 才是
  // `paintImageLayer` 按需开的。这两个数一个是"有几个页面挂了层"，
  // 一个是"一共开了几块位图"，用来判断"离开保留窗口有没有真的清掉"。
  let layers = 0;
  for (const host of document.querySelectorAll(".page-image-layer")) {
    layers += host.childElementCount;
  }
  // `quads` 是**所有页加起来**记了多少个图片四边形。它跟着算子表里图片算子的
  // 条数走 —— `collectImageQuads` 是**全收**的，而 `paintImageLayer` 只用前
  // `IMAGE_LAYER_MAX` 个。这个数是"收的比用的多多少"的量化证据。
  let quads = 0;
  for (const rec of pageImageQuads.values()) quads += rec.quads.length;
  return JSON.stringify({
    text: textCache.size,
    view: viewCache.size,
    thumbCleanups,
    thumbDark,
    renderTasks: renderTasks.size,
    imageQuads: pageImageQuads.size,
    quads,
    canvases: pageCanvases.size,
    thumbDocs: thumbDocs.size,
    layers,
  });
}

// ---------------------------------------------------------------- 页面尺寸与渲染

export async function getPageSizes() {
  if (!pdfDoc) return "[]";
  const total = pdfDoc.numPages;
  const sizes = new Array(total);
  const CHUNK = 24;
  for (let start = 1; start <= total; start += CHUNK) {
    const end = Math.min(start + CHUNK - 1, total);
    const jobs = [];
    for (let i = start; i <= end; i++) {
      jobs.push(
        getPage(i).then((p) => {
          const vp = p.getViewport({ scale: 1 });
          sizes[i - 1] = { width: vp.width, height: vp.height };
        }),
      );
    }
    await Promise.all(jobs);
  }
  return JSON.stringify(sizes);
}

// ---------------------------------------------------------------- 深色模式下的图片保护
//
// 深色模式靠 `html.theme-dark .page-canvas { filter: invert(1) hue-rotate(180deg) }`
// 把「白纸黑字」翻成「黑纸白字」。副作用是页面里的**位图图片一起被反相** ——
// 照片和插图会变成负片（明暗颠倒、色相旋转）。
//
// ⚠️ 先说一个**试过、行不通**的解法，免得下一个人再走一遍：渲染完之后，对图片那
// 一小块用 `ctx.filter` 再反相一次，指望「反两次 = 原样」。**对合只在没有截断时
// 成立** —— `invert(1) hue-rotate(180deg)` 作用在纯红上，R 分量会被顶到 1.574，
// 落到 [0,1] 之外**被截断**，信息就丢了：第二次反相只能得到 (172,63,63)，
// 永远回不到 (255,0,0)。而饱和色在 PDF 插图和数据图表里太常见了。这条路直接否掉。
//
// 现在走的是「让图片**脱离**那个滤镜的作用范围」：`.page` 里挂了一层
// `.page-image-layer`（见 `page_view.rs`），把图片所在的那几块**原色像素**从主
// canvas 抠进独立的小 canvas。CSS 反相只作用在主 canvas 上，图片层不受影响
// → 图片保持原色，周围的文字照常变暗。
//
// 内存：只按**图片的包围盒**开 canvas，不是整页。主 canvas 是页面里最大的一块
// （A4@1.25 / dpr1.5 ≈ 6.7 MB），整页再开一层等于翻倍 —— 宁可多写几行做裁剪。

/** 太小的块（项目符号、小图标）不值得单开一张 canvas。 */
const IMAGE_LAYER_MIN_SIDE = 24;
/** 一页最多抠几块，防止「整页由几百张小图拼成」时 DOM 爆炸。 */
const IMAGE_LAYER_MAX = 48;
/**
 * 抠出来的**总面积**上限（相对主 canvas 的倍数）。
 *
 * 条数有上限、面积没有的话，最坏情况是 48 张整页大小的图**互相重叠** ——
 * 一页就是 48 × 7 MB ≈ 336 MB。正常文档里图片是**铺开**的（平铺底纹、扫描图分块），
 * 总面积不超过一页，这条上限碰都碰不到；它只挡那种病态的重叠。
 */
const IMAGE_LAYER_MAX_AREA = 2;

/**
 * 缩略图位图里的"太小，不值得保护"阈值。
 *
 * ⚠️ **不能直接复用 `IMAGE_LAYER_MIN_SIDE`**：那个是**绝对**像素值，而缩略图比
 * 阅读区的 canvas 小一个数量级（宽 170–320 px，阅读区 A4@1.25/dpr1.5 ≈ 1115 px）。
 * 拿 24 px 去量 170 px 宽的图，等于要求"这块图至少占页面宽度的 14%" —— 页面里
 * 几乎所有插图都会被判成"太小、不值得保护"，于是**同一个图标在阅读区是原色、
 * 在缩略图里却是负片**。按比例折过来：24 × 170/1115 ≈ 3.7 → 4。
 *
 * ⚠️ 另外两条上限（`IMAGE_LAYER_MAX` 条数、`IMAGE_LAYER_MAX_AREA` 面积倍数）
 * 是**尺度无关**的，缩略图原样沿用 —— 这样两边的"哪几张图受保护"才一致。
 */
const THUMB_IMAGE_MIN_SIDE = 4;

/**
 * canvasId -> `{ page, transform }`。**不管什么主题都记。**
 *
 * 浅色下不收四边形（见下），所以切到深色时得知道"屏幕上这几页分别是第几页、
 * 当时的坐标变换是什么" —— 就靠这张表把图片层**补**出来。
 */
const pageCanvases = new Map();

/**
 * canvasId -> `{ page, quads }`；`quads` 是设备像素坐标下的四边形，每个 8 个数。
 *
 * ⚠️ **只在深色主题下收集。** 收集的代价不是零 —— `page.getOperatorList()`
 * 走的是**另一个 intent**（比 `render()` 多一个 `OPLIST` 位，见 `collectImageQuads`），
 * worker 要**再解析一遍内容流**、并多留一份算子表。浅色下这个结果根本用不到，
 * 白付这份钱 —— 而浅色正是大多数人的默认状态。
 */
const pageImageQuads = new Map();

/** 主题的唯一入口就是 `<html class>`（index.html 里那段同步脚本）。 */
function isDarkTheme() {
  return document.documentElement.classList.contains("theme-dark");
}

function imageLayerHost(canvas) {
  const page = canvas.parentElement;
  return page ? page.querySelector(".page-image-layer") : null;
}

function clearImageLayer(canvas) {
  const host = imageLayerHost(canvas);
  if (!host) return;
  while (host.firstChild) host.removeChild(host.firstChild);
}

/** 把变换矩阵 `m` 作用在单位正方形上 → 图片的四角（设备像素）。 */
function unitSquareQuad(m) {
  const [a, b, c, d, e, f] = m;
  const X = (x, y) => a * x + c * y + e;
  const Y = (x, y) => b * x + d * y + f;
  return [X(0, 0), Y(0, 0), X(1, 0), Y(1, 0), X(1, 1), Y(1, 1), X(0, 1), Y(0, 1)];
}

/**
 * 从算子表里捞出所有位图图片的四边形（**纯函数**，不碰 DOM / pdf.js 实例）。
 *
 * `base` 是「PDF 用户空间 → 设备像素」的基矩阵（`viewport.transform` 再乘 dpr）。
 * 抽成纯函数是为了**能单独喂合成算子表做测试** —— 见 `tests/e2e/e2e41` 的
 * `?mode=units`。这一段的正确性全靠算子表语义，靠真 PDF 去凑样本很难凑全。
 *
 * ⚠️ 只认「真正的位图」：`paintImageXObject` / `paintInlineImageXObject`。
 * `paintImageMaskXObject` / `paintSolidColorImageMask` 是**单色遮罩**，PDF 常拿它
 * 画彩色文字和图标（遮罩 + 填充色），那些属于正文，该跟着一起反相，所以**故意不列**。
 *
 * ⚠️⚠️ **`paintImageXObjectRepeat`(88) / `paintInlineImageXObjectGroup`(87) 这两个
 * 分支在本版本 pdf.js 下是「安全网」，走不到。** 别以为它们没用就删掉 ——
 * 但更别以为**靠它们**在兜底。事实是：
 *
 *   · worker 里确实有个优化器（`QueueOptimizer`），会把「连续 ≥3 组
 *     `q / 只差平移的 cm / /ImN Do / Q`」合并成一条 `paintImageXObjectRepeat`
 *     （`MAX_IMAGES_IN_BLOCK = 1000`），inline image 版本合成 group，并把那三组
 *     `q`/`cm`/`Q` 全部 splice 掉；
 *   · 但它在 `OperatorList` 构造时被这个条件挡住：
 *     `streamSink && !(intent & RenderingIntentFlag.OPLIST)`
 *     （`pdf.worker.mjs` 的 `OperatorList` 构造函数）；
 *   · 而 **`PDFPageProxy.getOperatorList()` 硬编码传 `isOpList = true`**
 *     （`pdf.mjs`：`getRenderingIntent(intent, …, isEditing, true)`），
 *     `render()` 那条路传的是 `false`。
 *
 *   → **`render()` 用的算子表是合并过的，`getOperatorList()` 拿到的是没合并的。**
 *     两边的 `cacheKey` 不同（`renderingIntent` 是 key 的一部分），是**两份独立的
 *     缓存**。所以本函数永远只会看到 85/86；88/87 只是防着 pdf.js 哪天把
 *     `getOperatorList()` 改成复用 display 的那份（那正是我们**希望**的优化，
 *     见下一条）时，图片保护不会静默失效。
 *
 * ⚠️⚠️ **`page.getOperatorList()` 不是免费的缓存命中**（曾经的注释写错了，订正）。
 * 它和 `render()` 的 intent 不同 → `_intentStates` 里是**另一条**记录 → worker 会
 * **再解析一遍内容流**，并且**多留一份算子表**，直到 `page.cleanup()`。
 * 代价不算小，所以调用方必须**只在真正需要时**调它（浅色下不收，见 `pageImageQuads`）。
 *
 * ⚠️ pdf.js 画图片的坐标约定（`CanvasGraphics.paintInlineImageXObject`）：
 * `ctx.scale(1/w, -1/h)` 之后再 `drawImage(img, 0, -h, w, h)` —— 净效果正好把图片
 * 铺在**变换后的单位正方形** `[0,0]–[1,1]` 上（y 轴翻过来）。所以只要跟踪变换矩阵、
 * 记下单位正方形就够了，不必知道图片的像素尺寸。
 * group 算子同理：`ctx.transform(...entry.transform)` 之后 `ctx.scale(1, -1)` +
 * `drawImage(..., 0, -1, 1, 1)` —— 还是同一个单位正方形。
 */
export function quadsFromOps(fnArray, argsArray, base, { OPS, Util }) {
  let m = base.slice();
  const stack = [];
  const quads = [];
  // 够了就停：一条 repeat 算子最多带 `MAX_IMAGES_IN_BLOCK = 1000` 个位置，
  // 全收下来纯属白占内存（`paintImageLayer` 只用前 `IMAGE_LAYER_MAX` 个）。
  // 留 4 倍余量，是因为被 `IMAGE_LAYER_MIN_SIDE` 挡掉的退化块不算数。
  const limit = IMAGE_LAYER_MAX * 4;

  for (let i = 0; i < fnArray.length; i++) {
    const fn = fnArray[i];
    if (fn === OPS.save) {
      stack.push(m.slice());
    } else if (fn === OPS.restore) {
      if (stack.length) m = stack.pop();
    } else if (fn === OPS.transform) {
      m = Util.transform(m, argsArray[i]);
    } else if (fn === OPS.paintFormXObjectBegin) {
      // form XObject 自带一层 `q` + 可选 matrix（pdf.js 里就是 save + transform）。
      stack.push(m.slice());
      const matrix = argsArray[i] && argsArray[i][0];
      if (matrix) m = Util.transform(m, matrix);
    } else if (fn === OPS.paintFormXObjectEnd) {
      if (stack.length) m = stack.pop();
    } else if (fn === OPS.paintImageXObject || fn === OPS.paintInlineImageXObject) {
      quads.push(unitSquareQuad(m));
    } else if (fn === OPS.paintImageXObjectRepeat) {
      // 安全网（本版本走不到，理由见函数头）。args = [objId, sx, sy, positions]
      const args = argsArray[i] || [];
      const sx = args[1];
      const sy = args[2];
      const positions = args[3];
      if (positions) {
        for (let k = 0; k + 1 < positions.length; k += 2) {
          quads.push(
            unitSquareQuad(Util.transform(m, [sx, 0, 0, sy, positions[k], positions[k + 1]])),
          );
        }
      }
    } else if (fn === OPS.paintInlineImageXObjectGroup) {
      // 安全网。args = [img, map]，`map[q].transform` 就是上面那条 [a,0,0,d,e,f]
      const map = (argsArray[i] && argsArray[i][1]) || [];
      for (const entry of map) {
        if (entry && entry.transform) quads.push(unitSquareQuad(Util.transform(m, entry.transform)));
      }
    }
    if (quads.length >= limit) break;
  }
  return quads;
}

/** 「PDF 用户空间 → 设备像素」的基矩阵：`viewport.transform` 再乘 dpr。 */
function baseMatrix(viewport, dpr, { Util }) {
  return dpr === 1
    ? viewport.transform.slice()
    : Util.transform([dpr, 0, 0, dpr, 0, 0], viewport.transform);
}

/**
 * 取这一页的算子表 → 算出图片四边形。
 *
 * ⚠️ 必须在**渲染完之后、释放之前**调：`page.cleanup()` 会把 `_intentStates`
 * 整个清掉，之后再来取就是从头解析。
 */
async function collectImageQuads(page, viewport, dpr) {
  const libRef = await lib();
  const { fnArray, argsArray } = await page.getOperatorList();
  return quadsFromOps(fnArray, argsArray, baseMatrix(viewport, dpr, libRef), libRef);
}

/**
 * 按四边形把主 canvas 的**原色像素**抠进图片层。
 *
 * 用 `clip()` 按四边形裁，而不是直接贴包围盒 —— 包围盒会把旋转图片四周的背景
 * 也一起抠过来（深色页面上就是一块白角）。图片层是透明底（默认 `getContext("2d")`），
 * 裁掉的部分自然透出下面的主 canvas。
 */
/**
 * 按四边形遍历「值得保护的图片块」，把裁剪矩形交给回调。
 *
 * 抽出来是因为有**两个**使用者，而它们必须用**同一套**上限：
 *   · `paintImageLayer`（阅读区）—— 每块开一张独立的小 canvas；
 *   · `composeDarkThumbnail`（缩略图）—— 在同一张合成画布上把这几块贴回原色。
 * 上限写两遍迟早会漂。`minSide` 是**唯一**允许不同的那个（缩略图位图小得多，
 * 见 `THUMB_IMAGE_MIN_SIDE`）；条数与面积这两条尺度无关，两边共用。
 *
 * ⚠️ 按四边形 `clip()` 而不是直接贴包围盒 —— 包围盒会把旋转图片四周的背景
 * 也一起抠过来（深色页面上就是一块白角）。
 */
function eachImageRect(canvas, quads, minSide, fn) {
  const areaBudget = canvas.width * canvas.height * IMAGE_LAYER_MAX_AREA;
  let painted = 0;
  let paintedArea = 0;
  for (const q of quads) {
    if (painted >= IMAGE_LAYER_MAX) break;
    if (paintedArea >= areaBudget) break;
    const x = Math.max(0, Math.floor(Math.min(q[0], q[2], q[4], q[6])));
    const y = Math.max(0, Math.floor(Math.min(q[1], q[3], q[5], q[7])));
    const w = Math.min(canvas.width, Math.ceil(Math.max(q[0], q[2], q[4], q[6]))) - x;
    const h = Math.min(canvas.height, Math.ceil(Math.max(q[1], q[3], q[5], q[7]))) - y;
    if (w < minSide || h < minSide) continue;
    fn(q, x, y, w, h);
    painted++;
    paintedArea += w * h;
  }
}

/** 把四边形裁成路径（`dx`/`dy` 是目标画布相对四边形包围盒原点的偏移）。 */
function clipQuad(ctx, q, dx, dy) {
  ctx.beginPath();
  ctx.moveTo(q[0] - dx, q[1] - dy);
  ctx.lineTo(q[2] - dx, q[3] - dy);
  ctx.lineTo(q[4] - dx, q[5] - dy);
  ctx.lineTo(q[6] - dx, q[7] - dy);
  ctx.closePath();
  ctx.clip();
}

function paintImageLayer(canvas, quads) {
  const host = imageLayerHost(canvas);
  if (!host) return;
  clearImageLayer(canvas);
  if (!quads.length) return;

  const cssW = parseFloat(canvas.style.width) || canvas.width;
  const dpr = cssW > 0 ? canvas.width / cssW : 1;

  eachImageRect(canvas, quads, IMAGE_LAYER_MIN_SIDE, (q, x, y, w, h) => {
    const layer = document.createElement("canvas");
    layer.width = w;
    layer.height = h;
    layer.style.left = `${x / dpr}px`;
    layer.style.top = `${y / dpr}px`;
    layer.style.width = `${w / dpr}px`;
    layer.style.height = `${h / dpr}px`;
    const lctx = layer.getContext("2d");
    clipQuad(lctx, q, x, y);
    lctx.drawImage(canvas, x, y, w, h, 0, 0, w, h);
    host.appendChild(layer);
  });
}

/**
 * `invert(1) hue-rotate(180deg)` 的**逐像素等价实现**（入参 0–1 归一化，出参 0–255）。
 *
 * ⚠️ **为什么不用 `ctx.filter`**：`CanvasRenderingContext2D.filter` 在 WebKit 上要
 * **Safari 17（macOS 14）** 才有，而 Tauri 2 的默认最低系统是 10.15 —— 老系统上
 * `ctx.filter` 会被**静默忽略**：缩略图还是白纸，而且**不报任何错**。自己算就没有
 * 这个不确定性，代价是一趟 `getImageData`（缩略图宽 170–320 px，十几万像素，几毫秒）。
 *
 * 推导（`cos180° = -1`、`sin180° = 0`）：
 *   · `invert(1)` → `v = 1 - c`；
 *   · `hue-rotate(180deg)` 的矩阵（Filter Effects 规范）**每行之和恰为 1**，
 *     于是 `out = H·(1 - c) = 1 - H·c`，代入即得下面三个系数。
 * 交叉验证：纯红 (255,0,0) 过一次得 (255,146,146)，**再过一次**得 (171,62,62) ——
 * 正是 `invert+hue-rotate` **不是对合**的那个已知数值（见 `MEMORY.md` §五）。
 *
 * ⚠️ `out` 是**复用**的小数组：热循环里每像素 new 一个数组会拖慢一个数量级。
 */
export function darkPixel(r, g, b, out) {
  const R = 1 + 0.574 * r - 1.43 * g - 0.144 * b;
  const G = 1 - 0.426 * r - 0.43 * g - 0.144 * b;
  const B = 1 - 0.426 * r - 1.43 * g + 0.856 * b;
  out[0] = R <= 0 ? 0 : R >= 1 ? 255 : (R * 255 + 0.5) | 0;
  out[1] = G <= 0 ? 0 : G >= 1 ? 255 : (G * 255 + 0.5) | 0;
  out[2] = B <= 0 ? 0 : B >= 1 ? 255 : (B * 255 + 0.5) | 0;
  return out;
}

/** 把整张画布按 [`darkPixel`] 反成深色（**原地**改）。 */
function invertToDark(ctx, w, h) {
  const img = ctx.getImageData(0, 0, w, h);
  const d = img.data;
  const out = [0, 0, 0];
  for (let i = 0; i < d.length; i += 4) {
    darkPixel(d[i] / 255, d[i + 1] / 255, d[i + 2] / 255, out);
    d[i] = out[0];
    d[i + 1] = out[1];
    d[i + 2] = out[2];
  }
  ctx.putImageData(img, 0, 0);
}

/**
 * 缩略图专用的深色合成：**整页反相 → 把图片那几块按原色贴回去**。
 *
 * 阅读区靠"两层 DOM"做到这件事（`.page-canvas` 反相 + `.page-image-layer` 保原色）；
 * 缩略图只有**一张 `<img>`**（data URL），所以等价做法是**在烘焙时就合成好**：
 * 先把整页反成深色，再把图片块从**未反相的那一份**像素上裁下来贴回去。
 *
 * ⚠️ 正因为"CSS 滤镜作用在整张位图上、做不到只反一部分区域"，缩略图**不能**改用
 * `html.theme-dark .thumb-frame img { filter: … }` 那条路 —— 那正是照片变负片的来路。
 *
 * ⚠️ 贴回去的源必须是 `src`（原色）而**不是** `out`：已经反过的像素没法还原，
 * 再反一次也不是原样（对合不成立，见 [`darkPixel`]）。
 *
 * `quads` 是**位图像素**坐标（缩略图 dpr 恒为 1，所以 `viewport.transform` 就是它）。
 *
 * @returns {HTMLCanvasElement} 可直接 `toDataURL` 的合成结果。
 */
export function composeDarkThumbnail(src, quads) {
  const out = document.createElement("canvas");
  out.width = src.width;
  out.height = src.height;
  const ctx = out.getContext("2d", { alpha: false });
  ctx.drawImage(src, 0, 0);
  invertToDark(ctx, out.width, out.height);
  eachImageRect(out, quads, THUMB_IMAGE_MIN_SIDE, (q, x, y, w, h) => {
    ctx.save();
    clipQuad(ctx, q, 0, 0);
    ctx.drawImage(src, x, y, w, h, x, y, w, h);
    ctx.restore();
  });
  return out;
}

/**
 * 主题来回切时，把所有已经画好的页在「有图片层 / 没有图片层」之间翻一遍。
 *
 * ⚠️ 观察者盯的是 `<html class>` 的**任何**变动，不是"主题真的变了"。
 * 少了下面那道去重，任何一次无关的 `classList` 改动（比如 `main.rs` 那句
 * `tag_platform()` 以后再被调一次）都会让整屏的页**全部重抠一遍图** ——
 * 那是几十次 `drawImage`，白干。所以记住上一次的状态，没变就直接返回。
 *
 * ⚠️ **浅色期间渲染的页没有四边形记录**（浅色下不收集，见 `pageImageQuads`）。
 * 所以切到深色时要用 `pageCanvases` 里那份 transform **补收一次**。
 * 这决定了本函数是 `async`；`token` 用来作废"补到一半又切回去了"的那一轮。
 */
let darkImagesOn = null;
let darkSyncToken = 0;

async function syncDarkImages() {
  const dark = isDarkTheme();
  if (darkImagesOn === dark) return;
  darkImagesOn = dark;
  const token = ++darkSyncToken;

  for (const [canvasId, rec] of pageImageQuads) {
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
      pageImageQuads.delete(canvasId);
      continue;
    }
    if (dark) paintImageLayer(canvas, rec.quads);
    else clearImageLayer(canvas);
  }
  if (!dark) return;

  const libRef = await lib();
  // 复制一份再遍历：循环里会 await，期间这张表可能被 renderPage / 释放路径改动。
  for (const [canvasId, rec] of [...pageCanvases]) {
    if (token !== darkSyncToken || !isDarkTheme()) return; // 又切走了 → 这一轮作废
    if (pageImageQuads.has(canvasId)) continue;
    const canvas = document.getElementById(canvasId);
    if (!canvas) {
      pageCanvases.delete(canvasId);
      continue;
    }
    const cssW = parseFloat(canvas.style.width);
    // 还没画过（`style.width` 是空的）→ 这一页的 `renderPage` 还在飞，
    // 它渲染完会自己收（那时已经是深色了）。跳过。
    if (!(cssW > 0)) continue;
    const dpr = canvas.width / cssW;
    try {
      const page = await getPage(rec.page);
      const { fnArray, argsArray } = await page.getOperatorList();
      if (token !== darkSyncToken || !isDarkTheme()) return;
      const base = baseMatrix({ transform: rec.transform }, dpr, libRef);
      const quads = quadsFromOps(fnArray, argsArray, base, libRef);
      pageImageQuads.set(canvasId, { page: rec.page, quads });
      paintImageLayer(canvas, quads);
    } catch (_) {}
  }
}

/**
 * 这一页此刻是不是挂在**阅读区**里（有一块 canvas 正在显示它）。
 *
 * `pageCanvases` 的键集就是「保留窗口内那几页」—— `renderPage` 写、`releasePage`
 * （页离开保留窗口时）删。缩略图渲染完要不要顺手 `page.cleanup()`，判据就是它：
 * 阅读区那几页的算子表**正被用着**，清了等于让下一次滚动重解析。
 */
function isInReader(pageNumber) {
  for (const rec of pageCanvases.values()) {
    if (rec.page === pageNumber) return true;
  }
  return false;
}

/** 页滚出保留窗口（canvas 连同图片层一起被摘掉）时，把记录也删了。 */
function forgetPageImages(pageNumber) {
  for (const [canvasId, rec] of pageCanvases) {
    if (rec.page === pageNumber) pageCanvases.delete(canvasId);
  }
  for (const [canvasId, rec] of pageImageQuads) {
    if (rec.page !== pageNumber) continue;
    const canvas = document.getElementById(canvasId);
    if (canvas) clearImageLayer(canvas);
    pageImageQuads.delete(canvasId);
  }
}

/** 盯着 `<html class>` —— 主题的唯一入口。第一次渲染时挂上。 */
let darkWatcher = null;
function watchTheme() {
  if (darkWatcher || typeof MutationObserver !== "function") return;
  darkWatcher = new MutationObserver(syncDarkImages);
  darkWatcher.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
}

export async function renderPage(canvasId, pageNumber, scale) {
  const canvas = document.getElementById(canvasId);
  if (!canvas || !pdfDoc) return JSON.stringify({ ok: false });

  const existing = renderTasks.get(canvasId);
  if (existing) {
    renderTasks.delete(canvasId);
    try {
      existing.cancel();
    } catch (_) {}
    // 等上一次渲染彻底结束，避免同一 canvas 上的并发绘制。
    try {
      await existing.promise;
    } catch (_) {}
  }

  const page = await getPage(pageNumber);
  const viewport = page.getViewport({ scale });
  const dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);

  canvas.width = Math.max(1, Math.floor(viewport.width * dpr));
  canvas.height = Math.max(1, Math.floor(viewport.height * dpr));
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;

  const ctx = canvas.getContext("2d", { alpha: false });
  const task = page.render({
    canvasContext: ctx,
    viewport,
    transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null,
  });
  renderTasks.set(canvasId, task);

  try {
    await task.promise;
    // 渲染成功：顺手把「深色模式下要保持原色」的图片抠到图片层。
    // 这一段是锦上添花 —— 任何一步失败都不该影响渲染结果本身，所以自带 try。
    if (renderTasks.get(canvasId) === task) {
      // 观察者**不管什么主题都要挂上**：浅色下渲染的页，得靠它把图片层补出来。
      watchTheme();
      pageCanvases.set(canvasId, { page: pageNumber, transform: viewport.transform });
      darkImagesOn = isDarkTheme();
      if (darkImagesOn) {
        // ⚠️ 只在深色下收。`page.getOperatorList()` 不是免费缓存命中 ——
        // 它带的 intent 比 `render()` 多一个 `OPLIST` 位，worker 会**再解析一遍
        // 内容流**并多留一份算子表。浅色下这个结果用不到，不该付这份钱。
        try {
          const quads = await collectImageQuads(page, viewport, dpr);
          // 抠图期间可能又来了新的一轮渲染（缩放变了），那就别往旧画面上贴。
          if (renderTasks.get(canvasId) === task) {
            pageImageQuads.set(canvasId, { page: pageNumber, quads });
            paintImageLayer(canvas, quads);
          }
        } catch (_) {}
      } else {
        // 浅色：这份记录留着会变成**过期坐标**（canvas 被缩放重画了它没跟着变）
        // → 删掉。切到深色时 `syncDarkImages` 会拿 `pageCanvases` 里那份新 transform 重算。
        pageImageQuads.delete(canvasId);
      }
    }
  } catch (err) {
    if (err && err.name === "RenderingCancelledException") {
      return JSON.stringify({ ok: false, cancelled: true });
    }
    return JSON.stringify({ ok: false, error: String(err) });
  } finally {
    // ⚠️ 清理必须放 `finally`：取消和失败两条路都是 `return`，
    // 把 `renderTasks.delete` 写在 `try` 后面**永远跑不到**，
    // 那次 RenderTask 就留在 map 里（扣着 canvas context 和 page）。
    // 只删「还挂着的正是**这一次**任务」的情况 —— 取消路径上很可能已经有
    // 新的一轮把同一个 canvasId 换成了新任务，别把新的误删掉。
    if (renderTasks.get(canvasId) === task) {
      renderTasks.delete(canvasId);
    }
  }
  return JSON.stringify({ ok: true, width: viewport.width, height: viewport.height });
}

/**
 * 放掉某一页在 pdf.js 侧的解析产物（**算子表**是大头）。
 *
 * 页面滚出保留窗口时由 `page_view.rs` 调用。不调用的话，每渲染过一页
 * 就有一份算子表常驻在 worker 里 —— `PDFPageProxy` 的 `_intentStates`
 * 会把 `operatorList` 缓存住以便重复渲染，而它**只有 `destroy()` 才会清**。
 *
 * ⚠️ 只对**已经离开保留窗口**的页调用。清理会让下次渲染重新跑一遍
 * `getOperatorList`（worker 往返 + 重新解析内容流），留在窗口里就等于白清。
 *
 * `page.cleanup()` 在"这一页还有渲染任务在飞"时返回 `false` —— 那不是错误，
 * pdf.js 自己会置上 `#pendingCleanup`，等渲染结束再补一次（`#tryCleanup(true)`）。
 */
export async function releasePage(pageNumber) {
  if (!pdfDoc) return JSON.stringify({ ok: false });
  // 这一页的 canvas（连同图片层）马上要被 `page_view.rs` 摘掉了，记录也一起清。
  forgetPageImages(pageNumber);
  try {
    const page = await getPage(pageNumber);
    return JSON.stringify({ ok: page.cleanup() });
  } catch (_) {
    return JSON.stringify({ ok: false });
  }
}

// ---------------------------------------------------------------- 文本层（选择 / 复制 / 检索高亮）

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

/** 在单个文本片段内部把命中的关键字包成 <mark>，key 与搜索结果一一对应。 */
function markupMatches(text, query, page, itemIndex, activeKey) {
  const lower = text.toLowerCase();
  let out = "";
  let cursor = 0;
  let occurrence = 0;
  let found = lower.indexOf(query, cursor);
  while (found !== -1) {
    out += escapeHtml(text.slice(cursor, found));
    const key = `${page}-${itemIndex}-${occurrence}`;
    const cls = activeKey === key ? "sh active" : "sh";
    out += `<mark class="${cls}">${escapeHtml(text.slice(found, found + query.length))}</mark>`;
    cursor = found + query.length;
    occurrence += 1;
    found = lower.indexOf(query, cursor);
  }
  out += escapeHtml(text.slice(cursor));
  return out;
}

export async function renderTextLayer(containerId, pageNumber, scale, query, activeKey) {
  const container = document.getElementById(containerId);
  if (!container || !pdfDoc) return;

  container.textContent = "";
  const page = await getPage(pageNumber);
  const viewport = page.getViewport({ scale });
  const content = await getTextContent(pageNumber);
  const { Util } = await lib();

  container.style.width = `${viewport.width}px`;
  container.style.height = `${viewport.height}px`;

  const normalizedQuery = (query || "").trim().toLowerCase();
  const fragment = document.createDocumentFragment();
  const targets = [];

  for (let i = 0; i < content.items.length; i++) {
    const item = content.items[i];
    if (typeof item.str !== "string" || item.str.length === 0) continue;
    if (!Array.isArray(item.transform)) continue;

    const tx = Util.transform(viewport.transform, item.transform);
    const fontHeight = Math.hypot(tx[2], tx[3]);
    if (!Number.isFinite(fontHeight) || fontHeight <= 0) continue;

    const span = document.createElement("span");
    span.style.left = `${tx[4]}px`;
    span.style.top = `${tx[5] - fontHeight}px`;
    span.style.fontSize = `${fontHeight}px`;
    span.style.fontFamily = "sans-serif";

    if (normalizedQuery) {
      span.innerHTML = markupMatches(item.str, normalizedQuery, pageNumber, i, activeKey || null);
    } else {
      span.textContent = item.str;
    }

    fragment.appendChild(span);
    targets.push(item.width * viewport.scale);
  }

  container.appendChild(fragment);

  // 把每个片段水平缩放到与 canvas 一致的宽度，保证选区与画面对齐。
  const spans = container.children;
  for (let i = 0; i < spans.length; i++) {
    const span = spans[i];
    const natural = span.getBoundingClientRect().width;
    const target = targets[i];
    if (!natural || !target || !Number.isFinite(target) || target <= 0) continue;
    const ratio = target / natural;
    if (!Number.isFinite(ratio) || ratio <= 0 || Math.abs(ratio - 1) < 0.01) continue;
    span.style.transform = `scaleX(${ratio})`;
  }
}

// ---------------------------------------------------------------- 目录大纲

async function resolveDestPage(dest) {
  try {
    let explicit = dest;
    if (typeof dest === "string") explicit = await pdfDoc.getDestination(dest);
    if (!Array.isArray(explicit) || explicit.length === 0) return null;
    const index = await pdfDoc.getPageIndex(explicit[0]);
    return index + 1;
  } catch (_) {
    return null;
  }
}

async function buildOutline(items) {
  const result = [];
  for (const item of items) {
    result.push({
      title: (item.title || "").trim() || "(无标题)",
      pageNumber: await resolveDestPage(item.dest),
      bold: Boolean(item.bold),
      italic: Boolean(item.italic),
      children: item.items && item.items.length ? await buildOutline(item.items) : [],
    });
  }
  return result;
}

export async function getOutline() {
  if (!pdfDoc) return "[]";
  try {
    const raw = await pdfDoc.getOutline();
    if (!raw || raw.length === 0) return "[]";
    return JSON.stringify(await buildOutline(raw));
  } catch (_) {
    return "[]";
  }
}

// ---------------------------------------------------------------- 全文检索

function snippetOf(text, index, length) {
  const from = Math.max(0, index - 24);
  const to = Math.min(text.length, index + length + 24);
  return `${from > 0 ? "…" : ""}${text.slice(from, to).trim()}${to < text.length ? "…" : ""}`;
}

/**
 * 最近一次 `search()` 的令牌。
 *
 * ⚠️ 每搜一次都要**走完整本 PDF**（逐页 `getTextContent`），而用户是逐字打的 ——
 * 「文」「文档」「文档管」就是三遍全书扫描，前两遍的结果 Rust 侧（`search_gen`）
 * 根本不会用。所以新一次搜索一进来就把旧的作废：旧的在下一次 `await` 回来时
 * 自己退出，省掉剩下的几百次 worker 往返与内容流解析。
 */
let searchToken = 0;

export async function search(query) {
  const needle = (query || "").trim().toLowerCase();
  if (!needle || !pdfDoc) return "[]";

  const myToken = ++searchToken;
  const MAX_HITS = 3000;
  const hits = [];
  for (let pageNumber = 1; pageNumber <= pdfDoc.numPages; pageNumber++) {
    // 有更新的搜索了 → 这一遍的结果没人要，别再往下扫（Rust 侧也会按代次丢弃）。
    if (myToken !== searchToken) return "[]";
    const content = await getTextContent(pageNumber);
    const view = viewCache.get(pageNumber) || [0, 0, 612, 792];
    const pageHeight = view[3] - view[1] || 1;

    for (let i = 0; i < content.items.length; i++) {
      const item = content.items[i];
      if (typeof item.str !== "string" || !item.str) continue;
      if (!Array.isArray(item.transform)) continue;
      const lower = item.str.toLowerCase();
      let cursor = 0;
      let occurrence = 0;
      let found = lower.indexOf(needle, cursor);
      while (found !== -1) {
        hits.push({
          page: pageNumber,
          itemIndex: i,
          charIndex: found,
          length: needle.length,
          key: `${pageNumber}-${i}-${occurrence}`,
          y: Math.min(1, Math.max(0, (view[3] - item.transform[5]) / pageHeight)),
          snippet: snippetOf(item.str, found, needle.length),
        });
        cursor = found + needle.length;
        occurrence += 1;
        if (hits.length >= MAX_HITS) return JSON.stringify(hits);
        found = lower.indexOf(needle, cursor);
      }
    }
  }
  return JSON.stringify(hits);
}

// ---------------------------------------------------------------- 选区几何（用于生成高亮）

function closestPage(node) {
  let el = node instanceof Element ? node : node && node.parentElement;
  while (el) {
    if (el.classList && el.classList.contains("page")) return el;
    el = el.parentElement;
  }
  return null;
}

/** 合并同一行上的碎片矩形，减少高亮块数量。 */
function mergeRects(rects) {
  const sorted = rects.slice().sort((a, b) => a.y - b.y || a.x - b.x);
  const merged = [];
  for (const rect of sorted) {
    const last = merged[merged.length - 1];
    const sameLine = last && Math.abs(last.y - rect.y) < Math.max(last.h, rect.h) * 0.5;
    const adjacent = last && rect.x <= last.x + last.w + 0.02;
    if (sameLine && adjacent) {
      const right = Math.max(last.x + last.w, rect.x + rect.w);
      last.x = Math.min(last.x, rect.x);
      last.w = right - last.x;
      last.h = Math.max(last.h, rect.h);
    } else {
      merged.push({ ...rect });
    }
  }
  return merged;
}

export function getSelectionInfo() {
  const selection = window.getSelection();
  if (!selection || selection.isCollapsed || selection.rangeCount === 0) return "null";

  const range = selection.getRangeAt(0);
  const pageEl = closestPage(range.startContainer);
  if (!pageEl) return "null";

  const pageNumber = Number.parseInt(pageEl.dataset.page || "", 10);
  if (!Number.isFinite(pageNumber)) return "null";

  const base = pageEl.getBoundingClientRect();
  if (!base.width || !base.height) return "null";

  const rects = [];
  for (const rect of range.getClientRects()) {
    if (rect.width < 1 || rect.height < 1) continue;
    rects.push({
      x: (rect.left - base.left) / base.width,
      y: (rect.top - base.top) / base.height,
      w: rect.width / base.width,
      h: rect.height / base.height,
    });
  }

  const text = selection.toString().trim();
  if (!rects.length || !text) return "null";

  return JSON.stringify({ page: pageNumber, text, rects: mergeRects(rects) });
}

export function clearSelection() {
  const selection = window.getSelection();
  if (selection) selection.removeAllRanges();
}

// ---------------------------------------------------------------- 拖拽打开

/**
 * `onFileDrop` 只允许接线一次。
 *
 * ⚠️ 那两个 `listen` 是**永久**监听（不保存 `unlisten`），重复调就是重复投递。
 * Rust 侧现在是 `use_effect_with((), …)`、只调一次，但**调用方一旦换了依赖**
 * （或组件重新挂载）就会再调一次 —— 那时拖一次文件会触发两次 `open_path`
 * （结果一样，但白开一遍文档、白读一次字节）。加一道守卫，成本为零。
 */
let fileDropWired = false;

export function onFileDrop(callback) {
  const listen = window.__TAURI__ && window.__TAURI__.event && window.__TAURI__.event.listen;
  if (!listen || fileDropWired) return;
  fileDropWired = true;
  listen("tauri://drag-drop", (event) => {
    const paths = (event && event.payload && event.payload.paths) || [];
    const pdf = paths.find((p) => /\.pdf$/i.test(p));
    if (pdf) callback(pdf);
  });
  // 双击 .pdf / 资源管理器「打开方式 → Folia」/ 把 .pdf 拖到 exe 上 ——
  // Windows 上这三件事都会**再启动一个进程**，由 `tauri-plugin-single-instance`
  // 把 argv 里的路径转成这个事件推过来（payload 直接就是路径字符串）。
  // 冷启动的那一路不走这里：那时前端还没挂监听，改由 `take_pending_open` 命令取。
  listen("folia://open-path", (event) => {
    const path = event && event.payload;
    if (typeof path === "string" && path) callback(path);
  });
}

// ---------------------------------------------------------------- 暴露给 Rust

// Rust 侧通过 js_namespace = ["window", "FoliaBridge"] 绑定这些方法，
// 因此必须挂到全局对象上（本模块不依赖打包器，用 <script type="module"> 引入）。
if (typeof window !== "undefined") {
  window.FoliaBridge = {
    openDocumentDialog,
    openDocumentPath,
    closeDocument,
    releaseThumbnail,
    renderThumbnail,
    documentInfo,
    cacheStats,
    getPageSizes,
    renderPage,
    releasePage,
    renderTextLayer,
    getOutline,
    search,
    getSelectionInfo,
    clearSelection,
    onFileDrop,
  };
}
