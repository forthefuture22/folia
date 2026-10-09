// 端到端验证：选中浮层工具栏（高亮 / 下划线 / 删划线 / 波浪线 / 复制）、
// 右上角的撤回与保存、以及缩略图与页面比例是否对齐。
//
// 这个模块插在**真实 bridge.js 之后、应用模块之前**（模块脚本按文档顺序执行，
// 所以没有竞态）。它只替换掉「需要真 pdf.js / 真 Tauri」的那几个方法 ——
// `getSelectionInfo` / `clearSelection` 保留**真实实现**：它们是纯 DOM 计算，
// 也正是被测代码（`selection_box`）要去反解的那份契约。

const PAGE_W = 595;
const PAGE_H = 842;
const RATIO = PAGE_W / PAGE_H;
const META = {
  id: "harness",
  path: "/tmp/harness.pdf",
  name: "harness.pdf",
  size: 24000,
  numPages: 1,
};

const LINES = [
  "The quick brown fox jumps over the lazy dog.",
  "Select this second line to raise the toolbar.",
  "Third line of the harness text layer here.",
  "Fourth line, used for the copy check.",
];

// 页面里的文字全部来自文本层（canvas 只画白底），
// 这样截图里看到的就是「被选中的字 + 浮层」，不会和 canvas 上的字重影。
function drawPage(canvasId, scale) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const w = Math.round(PAGE_W * scale);
  const h = Math.round(PAGE_H * scale);
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, w, h);
}

function fillTextLayer(containerId, scale) {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = LINES.map(
    (text, i) =>
      `<span style="left:${(64 * scale).toFixed(2)}px;top:${((92 + i * 30) * scale).toFixed(2)}px;` +
      `font-size:${(15 * scale).toFixed(2)}px;line-height:1;">${text}</span>`
  ).join("");
}

// 缩略图按**页面真实比例**生成 —— 这样 `img.naturalWidth/naturalHeight` 才有意义，
// 才能验证「框的比例 == 图的比例 == 页面的比例」这条链子。
function makeThumb(maxWidth) {
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(maxWidth);
  canvas.height = Math.round(maxWidth / RATIO);
  const ctx = canvas.getContext("2d");
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#c9c9d2";
  ctx.fillRect(0, 0, canvas.width, 6);
  return canvas.toDataURL("image/jpeg");
}

// 记录所有 Tauri 命令调用，用来验证「保存」真的落盘了。
window.__invokes = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      window.__invokes.push({ cmd, args });
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: [
              {
                id: META.id,
                path: META.path,
                name: META.name,
                size: META.size,
                pages: 1,
                last_page: 1,
                opened_at: 0,
                favorite: false,
              },
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

const bridge = window.FoliaBridge;
if (!bridge) {
  throw new Error("bridge.js 没有先执行：桩的插入位置不对");
}
Object.assign(bridge, {
  openDocumentPath: () =>
    Promise.resolve(JSON.stringify({ ok: true, cancelled: false, error: null, meta: META })),
  getPageSizes: () => Promise.resolve(JSON.stringify([{ width: PAGE_W, height: PAGE_H }])),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: (_path, _page, maxWidth) => Promise.resolve(makeThumb(maxWidth)),
  renderPage: (canvasId, _page, scale) => {
    drawPage(canvasId, scale);
    return Promise.resolve("");
  },
  renderTextLayer: (containerId, _page, scale) => {
    fillTextLayer(containerId, scale);
    return Promise.resolve("");
  },
});

// ---------------------------------------------------------------- 驱动

const result = {};

const params = new URLSearchParams(location.search);
// 截图专用。`shot=1`：选完字、浮层刚出来就收工，画面上除了浮层和选区什么都没有；
// `shot=marks`：四种样式都画完之后收工，用来看四条批注长什么样。
const SHOT = params.get("shot");

function waitFor(predicate, label, maxTicks = 600) {
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

const settle = async (ticks = 8) => {
  for (let i = 0; i < ticks; i++) {
    await new Promise((r) => setTimeout(r, 16));
  }
};

const round = (n) => Math.round(n * 100) / 100;

function rectOf(el) {
  const r = el.getBoundingClientRect();
  return {
    left: round(r.left),
    top: round(r.top),
    right: round(r.right),
    bottom: round(r.bottom),
    width: round(r.width),
    height: round(r.height),
  };
}

// 在某个元素上完整走一遍「按下 → 松开 → 点击」。
function press(el) {
  for (const type of ["mousedown", "mouseup", "click"]) {
    el.dispatchEvent(
      new MouseEvent(type, { bubbles: true, cancelable: true, view: window, detail: 1 })
    );
  }
}

let spans = null;
let stage = null;
let page = null;

/// 选中第 `line` 行里从 `from` 开始的 `length` 个字符，然后松开鼠标。
async function selectLine(line, from, length) {
  const node = spans[line].firstChild;
  const range = document.createRange();
  range.setStart(node, from);
  range.setEnd(node, from + length);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  const text = range.toString();
  stage.dispatchEvent(
    new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window, detail: 1 })
  );
  await settle();
  return { text, rect: rectOf(range) };
}

/// 点浮层上第 `index` 个动作按钮（0-3 是四种样式，4 是复制）。
async function clickBarButton(index) {
  const bar = document.querySelector(".sel-bar");
  if (!bar) throw new Error("浮层不在，点不到第 " + index + " 个按钮");
  const buttons = bar.querySelectorAll(".sel-bar-btn");
  if (buttons.length <= index) {
    throw new Error(`浮层只有 ${buttons.length} 个按钮，取不到第 ${index} 个`);
  }
  press(buttons[index]);
  await settle();
}

// 注意选择器要挑 `.folia-mark` 而不是 `.hl-layer > *` ——
// 每条批注在层里是**两个**兄弟节点（标记 + 侧栏色点），
// 用 `> *` 数出来的会是批注数的两倍。
const marks = () =>
  [...document.querySelectorAll(".page .hl-layer > .folia-mark")].map((el) => el.className);

const markCount = () => document.querySelectorAll(".page .hl-layer > .folia-mark").length;

const toast = () => document.querySelector(".toast")?.textContent?.trim() ?? null;

async function main() {
  try {
    // ---- 1. 从书架打开文档（应用不会自动打开，得点一下卡片）
    const card = await waitFor(() => document.querySelector(".file-card"), "书架卡片出现");
    card.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
    await waitFor(() => document.querySelector(".stage"), "阅读器出现");

    spans = await waitFor(() => {
      const list = document.querySelectorAll('.page[data-page="1"] .textLayer span');
      return list.length ? list : null;
    }, "文本层就绪");
    result.spanCount = spans.length;

    stage = document.querySelector(".stage");
    page = document.querySelector('.page[data-page="1"]');

    // 鼠标在页面上移动 / 右击 —— 两者都派发在 `.page` 上（应用的监听器挂在那儿）。
    // 坐标要**先取整**：`MouseEvent.clientX/clientY` 在 DOM 里是 `long`，
    // 传 603.13 进去、应用读出来是 603，拿没取整的值去比对会得到假失败。
    const moveTo = (x, y) =>
      page.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: Math.round(x),
          clientY: Math.round(y),
        })
      );
    // 真实浏览器右击是 `mousedown(button=2)` → `contextmenu`，两个都要发：
    // 应用靠前者关掉已经打开的菜单，靠后者开新菜单。
    //
    // ⚠️ 返回的是**事件对象**，不是 `dispatchEvent` 的返回值 ——
    // 后者是个布尔（"有没有被取消"），`bool.defaultPrevented` 是 `undefined`，
    // 断言会静默变成"永远不通过"。
    const rightClick = (x, y) => {
      const cx = Math.round(x);
      const cy = Math.round(y);
      page.dispatchEvent(
        new MouseEvent("mousedown", {
          bubbles: true,
          cancelable: true,
          view: window,
          button: 2,
          buttons: 2,
          clientX: cx,
          clientY: cy,
        })
      );
      const ev = new MouseEvent("contextmenu", {
        bubbles: true,
        cancelable: true,
        view: window,
        button: 2,
        buttons: 2,
        clientX: cx,
        clientY: cy,
      });
      page.dispatchEvent(ev);
      return ev;
    };
    // 点别处 → 关菜单（应用是在 document 上听 mousedown 关的）。
    const closeMenu = () =>
      document.body.dispatchEvent(
        new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window })
      );
    const ctxMenu = () => document.querySelector('[data-ctx-menu="mark"]');

    // ---- 2. 缩略图与页面比例是否对齐
    const frame = await waitFor(() => document.querySelector(".thumb-frame"), "缩略图出现");
    await settle(10);
    const frameRect = frame.getBoundingClientRect();
    result.thumb = {
      inlineAspectRatio: frame.style.aspectRatio,
      frameRatio: round(frameRect.width / frameRect.height),
      pageRatio: round(RATIO),
      img: (() => {
        const img = frame.querySelector("img");
        if (!img) return null;
        return {
          naturalRatio: img.naturalHeight ? round(img.naturalWidth / img.naturalHeight) : 0,
          objectFit: getComputedStyle(img).objectFit,
        };
      })(),
    };

    // ---- 2b. 截图模式：选中底色 vs 高亮底色
    //
    // 选中底色（`::selection`）没法用 `getComputedStyle` 读，只能在**截图里读像素**。
    // 所以这一趟先标一条高亮（第 0 行），再选**第 3 行**（离得远，浮层不会盖到那条高亮），
    // 把两个取样点报给 verify.py，由它去截图里取色比对。
    if (SHOT === "selcmp") {
      // 这一趟只比底色，把字形藏掉 —— 和真实应用一致（字其实画在 canvas 上），
      // 否则取样点可能正好落在一个字上，读到的就是墨色而不是底色。
      const hide = document.createElement("style");
      hide.textContent = ".textLayer span { color: transparent !important; }";
      document.head.appendChild(hide);

      await selectLine(0, 0, 10);
      await clickBarButton(0);
      const sel = await selectLine(3, 0, 12);

      // 浮层就挂在选区正上方，它的投影会落到选区那一条上，把底色压暗几个色阶
      // （实测 245,229,144 vs 255,238,148）—— 那样比色就比不准了。
      // 这一趟只比底色，把浮层藏掉。
      const hideBar = document.createElement("style");
      hideBar.textContent = ".sel-bar { display: none !important; }";
      document.head.appendChild(hideBar);
      await settle(6);
      const markEl = document.querySelector(".page .hl-layer > .folia-mark");
      const mr = markEl.getBoundingClientRect();
      result.selcmp = {
        sel: {
          x: Math.round((sel.rect.left + sel.rect.right) / 2),
          y: Math.round((sel.rect.top + sel.rect.bottom) / 2),
        },
        mark: {
          x: Math.round((mr.left + mr.right) / 2),
          y: Math.round((mr.top + mr.bottom) / 2),
        },
        textLayerOpacity: getComputedStyle(page.querySelector(".textLayer")).opacity,
      };
      result.stoppedAt = "selcmp";
      throw { shot: true };
    }

    // ---- 2c. 截图模式：悬停提示框长什么样
    if (SHOT === "tip") {
      await selectLine(1, 0, "Select this second line".length);
      await clickBarButton(0);
      const editBtn = [...document.querySelectorAll(".ann-item .ann-actions .btn")].find(
        (el) => el.textContent.trim() === "批注"
      );
      press(editBtn);
      await settle(10);
      const area = document.querySelector(".ann-input");
      area.value = "这一段是重点，回头核对一下出处。";
      area.dispatchEvent(new Event("input", { bubbles: true }));
      await settle(4);
      area.blur();
      await settle(12);
      window.getSelection().removeAllRanges();
      await settle(6);
      const markEl = document.querySelector(".page .hl-layer > .folia-mark");
      const mr = markEl.getBoundingClientRect();
      page.dispatchEvent(
        new MouseEvent("mousemove", {
          bubbles: true,
          cancelable: true,
          view: window,
          clientX: Math.round((mr.left + mr.right) / 2),
          clientY: Math.round((mr.top + mr.bottom) / 2),
        })
      );
      await settle(10);
      result.tipText = document.querySelector(".mark-tip")?.textContent ?? null;
      result.stoppedAt = "tip";
      throw { shot: true };
    }

    // ---- 3. 选中一行 → 浮层出现
    const first = await selectLine(1, 0, "Select this second line".length);
    result.selection = first.rect;
    result.selectedText = first.text;

    const bar = document.querySelector(".sel-bar");
    result.barPresent = !!bar;
    if (!bar) throw new Error("松开鼠标后浮层工具栏没有出现");

    result.barRect = rectOf(bar);
    result.barOffset = { width: bar.offsetWidth, height: bar.offsetHeight };
    result.barStyle = {
      position: getComputedStyle(bar).position,
      zIndex: getComputedStyle(bar).zIndex,
      transform: getComputedStyle(bar).transform,
    };
    result.barButtons = [...bar.querySelectorAll(".sel-bar-btn")].map((el) => el.title);
    result.barSwatchCount = bar.querySelectorAll(".swatch").length;
    result.barParentIsStage = bar.parentElement === stage;
    // `offsetParent` 是定位祖先：浮层的 left/top 是相对它算的，必须是 .stage。
    result.barOffsetParentIsStage = bar.offsetParent === stage;
    // 浮层与文本层的层叠关系：浮层必须在上面，否则会被正文压住点不到。
    const zOf = (el) => Number(getComputedStyle(el).zIndex) || 0;
    result.barAboveTextLayer =
      bar.parentElement === stage &&
      zOf(bar) >= zOf(page.querySelector(".textLayer") || page) &&
      bar.contains(bar.querySelector(".sel-bar-btn"));

    const stageBox = stage.getBoundingClientRect();
    result.stageRect = {
      left: round(stageBox.left),
      top: round(stageBox.top),
      right: round(stageBox.right),
      bottom: round(stageBox.bottom),
    };
    result.barInsideStage =
      result.barRect.left >= stageBox.left - 0.5 &&
      result.barRect.right <= stageBox.right + 0.5 &&
      result.barRect.top >= stageBox.top - 0.5 &&
      result.barRect.bottom <= stageBox.bottom + 0.5;

    const barCentre = (result.barRect.left + result.barRect.right) / 2;
    const selCentre = (first.rect.left + first.rect.right) / 2;
    result.barCentreDelta = round(barCentre - selCentre);
    result.barGap = round(first.rect.top - result.barRect.bottom);

    // ---- 3b. 顶部工具栏：色板搬到这里（浮层里没有了），「注释」改名「高亮」
    const top = document.querySelector(".reader-top-center");
    result.topSwatchCount = top.querySelectorAll(".swatches .swatch").length;
    const pill = [...top.querySelectorAll(".pill-btn")].find((el) =>
      el.title?.includes("高亮")
    );
    result.topMarkButtonText = pill ? pill.textContent.trim() : null;
    result.topMarkButtonTitle = pill ? pill.title : null;
    result.topUndo = !!document.querySelector('button[title="撤回上一步批注改动"]');
    result.topSave = !!document.querySelector('button[title="立即把批注保存到本地"]');
    // 「撤回 / 保存」必须在顶栏**右侧**那一格里，不能混进中间的胶囊组。
    const rightBox = document.querySelector(".reader-top-right");
    result.undoInTopRight = !!rightBox?.querySelector('button[title="撤回上一步批注改动"]');
    result.saveInTopRight = !!rightBox?.querySelector('button[title="立即把批注保存到本地"]');
    result.undoDisabledAtStart = rightBox?.querySelector('button[title="撤回上一步批注改动"]')?.disabled;

    if (SHOT === "1") {
      result.stoppedAt = "first-bar";
      throw { shot: true };
    }

    // ---- 4. 四种样式各来一条
    await clickBarButton(0);
    result.afterHighlight = marks();
    result.barAfterHighlight = !!document.querySelector(".sel-bar");

    await selectLine(2, 0, 5);
    await clickBarButton(1);
    result.afterUnderline = marks();

    await selectLine(2, 6, 5);
    await clickBarButton(2);
    result.afterStrike = marks();

    await selectLine(2, 12, 5);
    await clickBarButton(3);
    result.afterWavy = marks();

    result.annotationCount = markCount();
    result.badgeCount = document.querySelectorAll(".page .hl-layer .folia-hl-badge").length;
    result.annKinds = [...document.querySelectorAll(".ann-kind")].map((el) => el.textContent);

    // 四种样式不能只是「类名不同」——CSS 得真的画出四样东西来。
    // 逐条读计算样式：高亮是底色，下划线是下边框，波浪线是遮罩，删划线在伪元素上。
    result.markStyles = [...document.querySelectorAll(".page .hl-layer > .folia-mark")].map((el) => {
      const cs = getComputedStyle(el);
      const after = getComputedStyle(el, "::after");
      return {
        cls: el.className,
        bg: cs.backgroundColor,
        borderBottom: cs.borderBottomWidth,
        mask: cs.maskImage || cs.webkitMaskImage || "none",
        afterHeight: after.height,
        afterBg: after.backgroundColor,
        blend: cs.mixBlendMode,
      };
    });

    // 四种样式都画完了，截图模式在这里收工。
    if (SHOT === "marks") {
      result.stoppedAt = "four-marks";
      throw { shot: true };
    }

    // ---- 5. 复制
    //
    // 分两趟走。第一趟真调 `document.execCommand`：无头 Chrome 里页面拿不到焦点
    // （`document.hasFocus()` 是 false），它**一律返回 false** —— 这是环境限制，
    // 跟应用写得对不对无关，所以那一趟只核对「应用按规矩调了、参数对、结果如实汇报」。
    // 第二趟把 `execCommand` 换成一定成功，用来验证成功分支的文案与字数。
    // 两趟都要确认临时 textarea 里装的是**当前选中的那段文字**，并且用完就摘掉。
    const spy = [];
    const realExec = document.execCommand.bind(document);
    const installSpy = (force) => {
      document.execCommand = function (cmd) {
        // 侧栏的批注备注也是 textarea（`.ann-input`），得把它排除掉，
        // 否则读到的可能是备注框的内容而不是应用刚插进来的那个临时框。
        // 另外 `select()` 不保证聚焦，所以不能靠 `document.activeElement` 认。
        const area = [...document.querySelectorAll("textarea")].find(
          (el) => !el.classList.contains("ann-input")
        );
        let returned;
        if (force) {
          returned = true;
        } else {
          try {
            returned = realExec(cmd);
          } catch (_) {
            returned = false;
          }
        }
        spy.push({ cmd, value: area ? area.value : null, forced: force, returned });
        return returned;
      };
    };

    const textareasBefore = document.querySelectorAll("textarea").length;

    // 5a. 真调
    installSpy(false);
    const copyTarget = await selectLine(3, 0, "Fourth line".length);
    await clickBarButton(4);
    result.copySpy = spy.slice();
    result.copyExpected = copyTarget.text;
    result.copyToastReal = toast();
    result.barAfterCopy = !!document.querySelector(".sel-bar");

    // 5b. 强制成功
    spy.length = 0;
    installSpy(true);
    await selectLine(3, 0, "Fourth line".length);
    await clickBarButton(4);
    result.copySpyForced = spy.slice();
    result.copyToastForced = toast();
    document.execCommand = realExec;

    // 临时 textarea 必须被摘掉，否则页面里会攒下一堆幽灵节点。
    result.textareasBefore = textareasBefore;
    result.textareasAfter = document.querySelectorAll("textarea").length;

    // ---- 6. 撤回
    const undoBtn = document.querySelector('button[title="撤回上一步批注改动"]');
    result.undoButtonExists = !!undoBtn;
    result.undoDisabledBefore = undoBtn?.disabled ?? null;
    const before = markCount();
    press(undoBtn);
    await settle(10);
    result.undoRemoved = before - markCount();
    result.undoToast = toast();
    result.undoDisabledAfter = undoBtn.disabled;

    // 一路撤到底：栈空后按钮必须自己变灰
    for (let i = 0; i < 10; i++) {
      if (undoBtn.disabled) break;
      press(undoBtn);
      await settle(4);
    }
    result.afterUndoAll = markCount();
    result.undoDisabledEmpty = undoBtn.disabled;

    // ---- 7. 保存
    window.__invokes.length = 0;
    await selectLine(1, 0, 4);
    await clickBarButton(0);
    await settle(4);
    window.__invokes.length = 0;
    const saveBtn = document.querySelector('button[title="立即把批注保存到本地"]');
    result.saveButtonExists = !!saveBtn;
    press(saveBtn);
    await settle(14);
    const saves = window.__invokes.filter((call) => call.cmd === "save_annotations");
    result.saveCalls = saves.length;
    result.savePayload = saves.length ? JSON.parse(saves[0].args.data).length : null;
    result.saveToast = toast();

    // ---- 7a-bis. **没批注**的标注：悬停什么都不显示，但右击照样能弹菜单
    //
    // 刻意放在 7b **之前** —— 此刻页面上只有 §7 刚标的那一条，而且它**还没有批注**，
    // 正好命中「整页一条批注都没有」这个分支（应用里对应 `has_any_note == false`，
    // 悬停路径整个被短路掉）。
    //
    // 两件事必须**同时**成立，缺一不可：
    //   ① 划过它**不冒提示框** —— 用户明确要求的「没批注就别显示」；
    //   ② 右击它**仍然弹菜单** —— 悬停的"有没有话说"和右击的"点没点到"是两套判据。
    //      为了 ① 把 ② 一起改坏，等于悄悄删掉了「右击删除样式」这个功能。
    window.getSelection().removeAllRanges();
    await settle(8);
    const bareEl = document.querySelector(".page .hl-layer > .folia-mark");
    result.bareMarkFound = !!bareEl;
    result.bareMarkCount = markCount();
    if (bareEl) {
      // 没批注时那个小圆点上的**原生**提示也不该说「查看批注」——
      // 点下去打开的是空输入框，说"查看"会让人以为里面有东西。
      const badges = document.querySelectorAll(".page .hl-layer .folia-hl-badge");
      result.noteLessBadgeTitle = badges.length ? badges[badges.length - 1].title : null;

      const br = bareEl.getBoundingClientRect();
      const bx = (br.left + br.right) / 2;
      const by = (br.top + br.bottom) / 2;

      moveTo(bx, by);
      await settle(6);
      result.tipOnNoteLessMark = !!document.querySelector(".mark-tip");

      const ev = rightClick(bx, by);
      await settle(8);
      result.menuOnNoteLessMark = !!ctxMenu();
      result.menuEvPrevented = ev.defaultPrevented;
      result.menuOnNoteLessLabels = [...document.querySelectorAll(".ctx-menu-item")].map((el) =>
        el.textContent.trim()
      );

      closeMenu();
      await settle(6);
      result.menuClosedAfterNoteLess = !!ctxMenu();
    }

    // ---- 7b. 批注编辑：失焦即保存
    //
    // 编辑态现在**没有**保存/取消按钮，全靠 textarea 的 blur。
    // 所以这里要验三件事：点「批注」后输入框出现并**自动获得焦点**；
    // 输入后一 blur 就落盘、编辑态退出；保存的内容出现在列表里。
    const editBtn = [...document.querySelectorAll(".ann-item .ann-actions .btn")].find(
      (el) => el.textContent.trim() === "批注"
    );
    result.noteEditButtonExists = !!editBtn;
    if (editBtn) {
      press(editBtn);
      await settle(8);
      const area = document.querySelector(".ann-input");
      result.noteEditorOpened = !!area;
      result.noteEditorFocused = !!area && document.activeElement === area;
      // 编辑态里不该再有按钮行（保存/取消都没了）
      result.noteEditorHasButtons = !!document.querySelector(".ann-edit .ann-actions");
      if (area) {
        area.value = "这是一条测试批注";
        area.dispatchEvent(new Event("input", { bubbles: true }));
        await settle(4);
        // 真 blur（输入框已经被 effect 聚焦过）。无头环境偶尔不给真焦点，
        // 那就补一次合成 blur —— 只有还开着才补，避免重复提交。
        area.blur();
        await settle(8);
        if (document.querySelector(".ann-input")) {
          area.dispatchEvent(new FocusEvent("blur", { bubbles: false }));
          await settle(8);
        }
      }
      result.noteEditorClosed = !document.querySelector(".ann-input");
      result.noteText = document.querySelector(".ann-note")?.textContent ?? null;
      const saves = window.__invokes.filter((call) => call.cmd === "save_annotations");
      result.noteSavedPayload = saves.length
        ? (JSON.parse(saves[saves.length - 1].args.data)[0]?.note ?? null)
        : null;
    }

    // ---- 7c. 悬停在标注上 → 黑底白字的批注框
    //
    // 标注本身收不到鼠标事件（`.hl-layer` 是 `pointer-events: none`，文本层又压在它上面），
    // 所以应用是在 `.page` 上听 mousemove 做几何命中。这里就照它的方式派发。
    window.getSelection().removeAllRanges();
    await settle(6);
    const markEl = document.querySelector(".page .hl-layer > .folia-mark");

    result.tipBefore = !!document.querySelector(".mark-tip");
    if (markEl) {
      const mr = markEl.getBoundingClientRect();
      result.tipMarkRect = rectOf(markEl);
      moveTo((mr.left + mr.right) / 2, (mr.top + mr.bottom) / 2);
      await settle(6);
      const tip = document.querySelector(".mark-tip");
      result.tipShown = !!tip;
      result.tipText = tip?.textContent?.trim() ?? null;
      if (tip) {
        const cs = getComputedStyle(tip);
        result.tipStyle = {
          background: cs.backgroundColor,
          color: cs.color,
          position: cs.position,
          pointerEvents: cs.pointerEvents,
        };
        result.tipRect = rectOf(tip);
      }
      // 移到页面下方空白处 → 提示要收掉
      const pr = page.getBoundingClientRect();
      moveTo(pr.left + pr.width / 2, pr.top + 400);
      await settle(6);
      result.tipAfterMoveAway = !!document.querySelector(".mark-tip");
    }

    // ---- 7d. 页面上**有**带批注的标注时，另一条没批注的照样不显示
    //
    // 7a-bis 覆盖的是「整页一条批注都没有」（悬停路径被整个短路）；
    // 这里覆盖另一半：页面**有**批注，所以悬停逻辑是**跑着**的 ——
    // 此时划过一条没批注的标注，依然不能冒框。两个分支都得钉住，
    // 否则以后只改一边也不会有人发现。
    window.getSelection().removeAllRanges();
    await settle(6);
    await selectLine(2, 20, 6);
    await clickBarButton(0); // 只标高亮，不写批注
    window.getSelection().removeAllRanges();
    await settle(8);
    const allMarks = [...document.querySelectorAll(".page .hl-layer > .folia-mark")];
    result.marksBeforeBareProbe = allMarks.length;
    // 新标的这条是 DOM 里的最后一条（批注按加入顺序渲染）。
    const secondBare = allMarks[allMarks.length - 1];
    if (secondBare) {
      const sr = secondBare.getBoundingClientRect();
      moveTo((sr.left + sr.right) / 2, (sr.top + sr.bottom) / 2);
      await settle(6);
      result.tipOnSecondBareMark = !!document.querySelector(".mark-tip");
      // 反向对照：同一次运行里，带批注的那条**必须**还能弹框 ——
      // 不然上面这条「没冒框」可能只是因为提示框整个坏了。
      const notedEl = allMarks[0];
      const nr = notedEl.getBoundingClientRect();
      moveTo((nr.left + nr.right) / 2, (nr.top + nr.bottom) / 2);
      await settle(6);
      result.tipOnNotedMarkAgain = document.querySelector(".mark-tip")?.textContent?.trim() ?? null;
      // 收尾：移开鼠标，别把提示框留到第 8 节。
      const pr2 = page.getBoundingClientRect();
      moveTo(pr2.left + pr2.width / 2, pr2.top + 400);
      await settle(6);
    }

    // ---- 8. 点正文空白处：选区没了，浮层要收掉
    // 先真的把浮层叫出来，否则这一步是「浮层本来就不在」的空验证。
    await selectLine(0, 0, 5);
    result.barBeforeClickAway = !!document.querySelector(".sel-bar");
    const blank = page.querySelector(".page-canvas");
    blank.dispatchEvent(
      new MouseEvent("mousedown", { bubbles: true, cancelable: true, view: window })
    );
    window.getSelection().removeAllRanges();
    blank.dispatchEvent(
      new MouseEvent("mouseup", { bubbles: true, cancelable: true, view: window })
    );
    await settle();
    result.barAfterClickAway = !!document.querySelector(".sel-bar");
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
  window.__HARNESS_DONE__ = true;
}

main();
