// 冷启动 + 待打开文件：书架上的「最近」列表会不会被冲掉？
//
// 背景：用户报告「拖到 Dock 图标打开 PDF」不生效。查下来根因是应用从没打过包，
// 但排查过程中发现 state.json 的 recentFiles 从 6 条掉到 1 条。
// 这个套件专门钉这件事：冷启动时若后端已存了一个待打开文件，
// 前端「先 load 再 open」的顺序会不会把刚读回来的最近列表覆盖掉。
//
// 桩只做两件事：load_app_state 给 3 条历史，take_pending_open 给 1 个新路径。
// save_app_state 不落盘，而是把每次写入的 payload 记下来供断言。

const HISTORY = [
  { id: "doc-a.pdf", path: "/tmp/a.pdf", name: "a.pdf" },
  { id: "doc-b.pdf", path: "/tmp/b.pdf", name: "b.pdf" },
  { id: "doc-c.pdf", path: "/tmp/c.pdf", name: "c.pdf" },
];
const PENDING = "/tmp/from-dock.pdf";
const PAGES = 5;

window.__saved = [];

window.__TAURI__ = {
  core: {
    invoke: (cmd, args) => {
      if (cmd === "load_app_state") {
        return Promise.resolve(
          JSON.stringify({
            recentFiles: HISTORY.map((h, i) => ({
              ...h, size: 1000 * (i + 1), pages: PAGES, last_page: 1, opened_at: 0, favorite: i === 1,
            })),
            lastPath: HISTORY[0].path, lastPage: 1, scale: 1,
          })
        );
      }
      if (cmd === "take_pending_open") return Promise.resolve(PENDING);
      if (cmd === "save_app_state") {
        window.__saved.push(args && args.data);
        return Promise.resolve(null);
      }
      if (cmd === "load_annotations") return Promise.resolve("[]");
      if (cmd === "default_pdf_handler") return Promise.resolve(false);
      return Promise.resolve(null);
    },
  },
  event: { listen: () => Promise.resolve(() => {}) },
};

const bridge = window.FoliaBridge;
Object.assign(bridge, {
  openDocumentPath: (path) => {
    const p = String(path || "");
    const name = p.split("/").pop();
    return Promise.resolve(JSON.stringify({
      ok: true, cancelled: false, error: null,
      meta: { id: `doc-${name}`, path: p, name, size: 24000, numPages: PAGES },
    }));
  },
  getPageSizes: () => Promise.resolve(JSON.stringify(Array.from({ length: PAGES }, () => ({ width: 595, height: 842 })))),
  getOutline: () => Promise.resolve("[]"),
  search: () => Promise.resolve("[]"),
  renderThumbnail: () => Promise.resolve(""),
  renderPage: () => Promise.resolve(""),
  renderTextLayer: () => Promise.resolve(""),
});

const result = {};
const waitFor = (p, label, n = 6000) => new Promise((res, rej) => {
  let t = 0;
  const tick = () => { let v = null; try { v = p(); } catch (_) {} if (v) return res(v); if (++t > n) return rej(new Error("超时: " + label)); setTimeout(tick, 10); };
  tick();
});
const settle = async (n) => { for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 16)); };

(async () => {
  try {
    await waitFor(() => document.querySelector(".stage"), "阅读器出现（待打开文件被自动打开）");
    result.pendingOpened = true;
    result.tabName = document.querySelector(".tab-label")?.textContent.trim() ?? null;
    await settle(60);

    result.saveCount = window.__saved.length;
    const parsed = window.__saved.map((s) => { try { return JSON.parse(s); } catch (_) { return null; } }).filter(Boolean);
    result.savedRecentCounts = parsed.map((p) => (p.recentFiles || []).length);
    result.lastSavedPaths = parsed.length ? (parsed[parsed.length - 1].recentFiles || []).map((r) => r.path) : [];
    result.lastSavedFavorite = parsed.length
      ? (parsed[parsed.length - 1].recentFiles || []).map((r) => `${r.name}:${r.favorite}`) : [];
    // 书架 DOM 上也看一眼
    document.querySelector(".tabbar-home")?.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    await settle(30);
    result.shelfCardNames = [...document.querySelectorAll(".file-card .file-title")].map((e) => e.textContent.trim());
  } catch (e) {
    result.error = String(e && e.message || e);
  }
  const pre = document.createElement("pre");
  pre.id = "harness-result";
  pre.textContent = JSON.stringify(result, null, 2);
  document.body.appendChild(pre);
})();
