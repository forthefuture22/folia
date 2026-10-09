//! 应用状态容器与状态机。
//!
//! `Handles` 把 App 的所有 `UseStateHandle` 打成一包，好让异步任务和回调
//! 能带着"一整套状态"到处走；下面那些 `fn` 是**所有会改状态的动作** ——
//! 打开 / 切换 / 关闭标签、回书架、续读、最近列表的读写与落盘。
//!
//! 把它们从渲染代码里分出来，是为了让"状态怎么变"这件事只有一个入口，
//! 而不是散落在几十个回调闭包里各改各的。

use std::cell::RefCell;
use std::collections::{HashMap, HashSet, VecDeque};
use std::rc::{Rc, Weak};

use js_sys::Date;
use wasm_bindgen_futures::spawn_local;
use yew::prelude::*;

use crate::ctx_menu::CtxMenu;
use crate::pdf;
use crate::pdf::types::{
    DocMeta, Highlight, OpenResult, OutlineNode, PageSize, PersistedState, RecentFile, SearchHit,
};
use crate::selbar::{SelBar, SelBox};
use crate::state::document::{DocTab, RestoreTarget};
use crate::state::history::History;

/// 「最近打开」的上限，**只约束非收藏条目**。
///
/// 收藏条目永不淘汰：`favorite` 是用户显式说的"留着"，被上限挤掉之后
/// `persist_state` 会连 `favorite: true` 一起写没 —— 收藏静默消失（见 `apply_document`）。
const RECENT_LIMIT: usize = 10;

/// 侧栏 / 总览的缩略图缓存上限（按页）。
///
/// 每张是 170px 宽的 JPEG data URL，实测（A4 文本页、q0.75）**约 12.5 KB**。
/// 所以这个上限同时是内存上限：60 张 ≈ 750 KB。
///
/// ⚠️ **必须有上限。** "生成全部缩略图"会把整本塞进来 —— 500 页就是 6 MB 的
/// base64 字符串，而且它存在 **WASM 线性内存**里（`memory.grow` 单向不可回收），
/// 外加切标签时进 `DocTab` 的第二份、以及每个已打开标签各一份。
///
/// 60 是够用的：侧栏一次只要当前页附近 19 页；总览网格对缺图的页**按需补**
/// （见 `app.rs` 的 `overview_top_up`），滚动时再生成。
pub(crate) const PAGE_THUMB_LIMIT: usize = 60;

/// 把一张缩略图放进缓存，超上限就淘汰**离焦点页最远**的那张。
///
/// ⚠️ 淘汰策略是"离焦点页最远"，不是"最早放进来的"：总览是**从第 1 页开始**
/// 顺序生成的，用 FIFO 的话跑完整本留下的是第 1..60 页，而用户正在看第 400 页
/// —— 留下的全是没用的。
///
/// ⚠️ **`focus` 必须是「用户正在看的地方」，不是「阅读位置」。** 总览是一张
/// **所有页**的网格，用户会滚到远离阅读位置的地方。焦点取 `current_page` 时，
/// 网格里刚渲染完的那页自己就是"离焦点最远"的那个 → 插进去立刻被删 →
/// 推版本号 → 补齐 effect 重跑 → 再请求 → **转不停**
/// （`tests/e2e/e2e37` 钉的就是它，实测 80 秒渲染 15,823 次）。
/// 焦点怎么取见 [`thumb_focus`]。
///
/// # 返回值
///
/// **这一张有没有留下来。** 调用方据此决定要不要推版本号 ——
/// 没留下来就说明 `store` 跟插入前**一模一样**（插进去又删掉同一个 key），
/// 推版本号只会让依赖它的补齐 effect 白跑一轮、然后再请求一次、再被淘汰：
/// 那就是上面那个死循环。这是防死循环的**兜底**，跟焦点取对是两件事。
pub(crate) fn put_thumb(
    store: &mut HashMap<u32, String>,
    page: u32,
    url: String,
    focus: u32,
) -> bool {
    store.insert(page, url);
    while store.len() > PAGE_THUMB_LIMIT {
        let Some(victim) = store
            .keys()
            .max_by_key(|&&p| (p as i64 - focus as i64).unsigned_abs())
            .copied()
        else {
            break;
        };
        store.remove(&victim);
    }
    store.contains_key(&page)
}

/// 缩略图淘汰的**焦点页**：用户此刻正在看的地方。
///
/// - 总览（全部页面）开着 → [`Handles::overview_focus`]：网格可视区的中心页。
///   由 `overview_scroll` 和「按需补齐」那条 effect 在算完可视区之后写进去。
/// - 总览关着 → 阅读位置 [`Handles::current_page`]。
///
/// ⚠️ 两个来源**不能合成一个"总是取 current_page"** —— 那正是 `e2e37` 那个死循环。
pub(crate) fn thumb_focus(handles: &Handles) -> u32 {
    if *handles.overview_open {
        let focus = *handles.overview_focus.borrow();
        if focus > 0 {
            return focus;
        }
    }
    (*handles.current_page).max(1)
}

/// 页码缩略图缓存的句柄（`Rc<RefCell<HashMap>>` 的一层薄包装）。
///
/// ⚠️ **存在的唯一理由是 `PartialEq`。** 这个 store 会作为 props 传进 `Sidebar`，
/// 而 Yew 每次渲染都要比一次新旧 props —— `Rc<RefCell<HashMap<u32, String>>>`
/// 的 `PartialEq` 会**逐条比对几百 KB 的 data URL 字符串**（`Rc` 没有指针捷径）。
/// 这里改成只比"是不是同一个 store"，那才是我们真正关心的。
#[derive(Clone)]
pub(crate) struct ThumbStore(pub(crate) Rc<RefCell<HashMap<u32, String>>>);

impl Default for ThumbStore {
    fn default() -> Self {
        Self(Rc::new(RefCell::new(HashMap::new())))
    }
}

impl PartialEq for ThumbStore {
    fn eq(&self, other: &Self) -> bool {
        Rc::ptr_eq(&self.0, &other.0)
    }
}

/// 推一格缩略图版本号，触发一次重渲染。
///
/// ⚠️ 版本号取自**活的** `Rc<RefCell<u64>>`，不是 `(*thumbs_gen)` ——
/// `UseStateHandle::set()` 是排队，同一帧里连着 `set` 两次同一个值，Yew 那边
/// 只认最后一次；而并发回来的缩略图任务都持有**自己那一帧**的句柄，
/// 直接 `set(*thumbs_gen + 1)` 会连着算出同一个数字，中间那些就白渲染了。
///
/// 形状跟 `search_gen`/`activation_gen`/`doc_gen` 一致（`use_mut_ref` 一定包一层
/// `RefCell`，`Rc<Cell<_>>` 那种写法在 `app.rs` 侧造不出来）。
pub(crate) fn bump_thumbs(handles: &Handles) {
    let mut counter = handles.thumbs_counter.borrow_mut();
    *counter += 1;
    let next = *counter;
    drop(counter);
    handles.thumbs_gen.set(next);
}

/// 推一格标签版本号，触发一次重渲染。
///
/// 形状跟 `bump_thumbs` 一致，理由也一样：版本号必须取自**活的** `Rc<RefCell<u64>>`，
/// 不能 `set(*tabs_gen + 1)`（`set` 是排队，并发回来的任务会连着算出同一个数）。
pub(crate) fn bump_tabs(handles: &Handles) {
    let mut counter = handles.tabs_counter.borrow_mut();
    *counter += 1;
    let next = *counter;
    drop(counter);
    handles.tabs_gen.set(next);
}

/// 改写标签页列表 —— **所有**写 `tabs` 的地方都走这里。
///
/// 写完顺手推一格版本号（渲染读的是 store，不推就画不出来）。
/// 形状跟 `write_recent` 一致：store 是唯一真相，state 只是"变了"的信号。
pub(crate) fn set_tabs(handles: &Handles, list: Vec<DocTab>) {
    *handles.tabs_store.borrow_mut() = Rc::new(list);
    bump_tabs(handles);
}

/// 读当下的标签页列表。**别用 `(*handles.tabs)`** —— 那是上一帧的快照。
///
/// 返回 `Rc<Vec<DocTab>>` 而不是 `Vec<DocTab>`：每个 `DocTab` 里带着自己的
/// 缩略图 data URL（最多 `PAGE_THUMB_LIMIT` 张），深拷贝一次就是几百 KB。
/// 调用方要改就先 `(*tabs_now(handles)).clone()` 再改。
pub(crate) fn tabs_now(handles: &Handles) -> Rc<Vec<DocTab>> {
    handles.tabs_store.borrow().clone()
}

/// 汇总需要跨异步任务传递的状态句柄。
#[derive(Clone)]
pub(crate) struct Handles {
    /// 标签页列表的**唯一真相**（`tabs_gen` 只是"变了"的信号）。
    ///
    /// ⚠️ **为什么不是 `UseStateHandle<Vec<DocTab>>`：`UseStateHandle` 不是活的。**
    /// 读它拿到的永远是"造出这个句柄的那一帧"的值（`set` 是排队，而且内部
    /// `Rc::make_mut` 会把旧句柄留在旧值上）。而 `tabs` 是典型的**读-改-写**：
    /// `apply_document` 从 `openDocumentPath` 到提交要跨三次 await。两条打开流水线
    /// （或者"打开途中用户点了别的标签"）就会各自拿一份过期列表去写，后写的把
    /// 先写的整个覆盖掉 —— 拖两份 PDF 进来只开出一份（`tests/e2e/e2e38`）。
    ///
    /// 外面套一层 `Rc` 是为了**读的时候不深拷贝**（每个 `DocTab` 里还带着几十张
    /// 缩略图 data URL）。形状跟 `page_thumbs_store` 一致。
    pub(crate) tabs_store: Rc<RefCell<Rc<Vec<DocTab>>>>,
    /// `tabs_store` 的版本号。`set_tabs` 每次推一格，触发重渲染。
    pub(crate) tabs_gen: UseStateHandle<u64>,
    /// `tabs_gen` 的**活**来源（`UseStateHandle` 不是活的，见 [`Handles::live`]）。
    pub(crate) tabs_counter: Rc<RefCell<u64>>,
    pub(crate) active: UseStateHandle<Option<usize>>,
    pub(crate) meta: UseStateHandle<Option<DocMeta>>,
    pub(crate) page_sizes: UseStateHandle<Vec<PageSize>>,
    pub(crate) outline: UseStateHandle<Vec<OutlineNode>>,
    pub(crate) annotations: UseStateHandle<Vec<Highlight>>,
    /// 当前的 `annotations` 是不是**可信**的，也就是"可以往磁盘写"。
    ///
    /// 为什么需要它：`load_annotations` 可能失败（IPC 报错、文件权限、
    /// JSON 损坏、出现了一个我们不认识的 `kind`）。失败时我们只能拿一个空列表
    /// 去渲染，而"持久化批注"那个 effect 会在 `annotations` 变化的**同一帧**
    /// 把它存回去 —— 于是**一次读取失败就变成了静默删光用户的批注**。
    ///
    /// 所以：读成功 → `true`；用户自己动过批注（新增 / 编辑 / 删除 / 撤回）
    /// → `true`；读失败 / 还没读过 / 没有打开的文档 → `false`。
    /// 存盘前必须检查这个标志。
    ///
    /// 用 `RefCell` 而不是 state：它不参与渲染，只被存盘 effect 读一次。
    pub(crate) annotations_ok: Rc<RefCell<bool>>,
    pub(crate) recent: UseStateHandle<Vec<RecentFile>>,
    /// 「最近打开」的真实数据。见 `recent_store` 声明处的长注释 ——
    /// `recent` 是快照，这个才是能跨帧安全读写的那个。
    pub(crate) recent_store: Rc<RefCell<Vec<RecentFile>>>,
    /// 磁盘上那份 `state.json` 现在是不是**可信**的（也就是"可以往磁盘写"）。
    ///
    /// 和 [`Handles::annotations_ok`] 是同一个道理，只是管的是另一份文件。
    /// 启动时 `load_app_state` 可能失败（权限 / I/O），也可能读得出来但**解析不了**
    /// （`save_app_state` 原来是 `fs::write`，写一半断电就留一个截断的 JSON）。
    /// 这两种情况下内存里的最近列表都是**空的**，而存盘 effect 会拿这个空列表
    /// 去覆盖磁盘 —— 用户的整个最近列表、所有收藏、阅读进度一起没
    /// （`tests/e2e/e2e34` 钉的就是它）。
    ///
    /// 所以默认是 `false`（**没读到之前一律不许写**），只有两种情况才转 `true`：
    /// - 读回来且解析成功；
    /// - 读不出来时**先把旧文件留档**（`quarantine_app_state`）—— 旧数据已经保住，
    ///   之后从干净状态开始写就没什么可丢的了。
    ///
    /// 留档也失败才一直是 `false`：那时宁可这次会话不落盘，也不能把没能保住的那份盖掉。
    pub(crate) state_ok: Rc<RefCell<bool>>,
    /// Folia 是不是系统默认的 PDF 阅读器（`None` = 未知 / 系统没登记）。
    pub(crate) is_default_pdf: UseStateHandle<Option<bool>>,
    pub(crate) scale: UseStateHandle<f64>,
    pub(crate) current_page: UseStateHandle<u32>,
    pub(crate) visible: UseStateHandle<(usize, usize)>,
    pub(crate) pending_scroll: UseStateHandle<Option<u32>>,
    /// 刚打开的新文档要不要自动适宽。
    ///
    /// 用 `RefCell` 而不是 state：它只被「文档就绪」那个 effect 读一次就清掉，
    /// 不需要触发渲染，也就不该占一个渲染依赖。切回已有标签、或续读上次关掉的那份
    /// 都要置回 `false` —— 它们各有自己存下来的缩放，重算会把用户的缩放冲掉。
    pub(crate) pending_fit: Rc<RefCell<bool>>,
    pub(crate) status: UseStateHandle<Option<String>>,
    pub(crate) loading: UseStateHandle<bool>,
    pub(crate) query: UseStateHandle<String>,
    pub(crate) hits: UseStateHandle<Vec<SearchHit>>,
    pub(crate) active_hit: UseStateHandle<Option<usize>>,
    pub(crate) active_highlight: UseStateHandle<Option<String>>,
    pub(crate) has_selection: UseStateHandle<bool>,
    /// 选区几何与浮层工具栏落点（见 `SelBox` / `SelBar`）。
    ///
    /// 换文档、切标签、回书架都要一起清掉，否则工具栏会留在原地指向一段
    /// 已经不属于当前文档的选中文字。
    pub(crate) sel_box: Rc<RefCell<Option<SelBox>>>,
    pub(crate) sel_bar: UseStateHandle<Option<SelBar>>,
    /// 批注撤销栈（见 `History`）。换文档 / 切标签要一起清掉。
    pub(crate) history: History,
    pub(crate) restore: Rc<RefCell<Option<RestoreTarget>>>,
    /// 上一次**落盘**时写进去的 `last_path` / `last_page` / `scale`。
    ///
    /// 为什么要单独存一份：书架上的动作（收藏 / 取消收藏 / 删除记录 / 删除记录和文件）
    /// 也会改最近列表，改完必须立刻落盘 —— 不然在首页收藏一下、直接退出应用，
    /// 收藏就没了。可这些动作发生在**首页**，那时一份文档都没打开，
    /// `last_path` 该填什么？
    ///
    /// 不能填 `None`：`last_path` 是"下次启动续读到哪一页"的依据
    /// （启动时读回来塞进 `restore`，`apply_document` 拿它比对 `meta.path`）。
    /// 清成 `None` 等于把用户上次读到第几页悄悄弄丢了。
    ///
    /// 所以留一份"上次存的原样"，落盘时原封不动写回去。只在两处写：
    /// 启动读回 `state` 时、阅读进度 effect 落盘时。
    ///
    /// 不直接复用 `restore`：`restore` 的语义是"**关掉**当前标签时记下的续读点"，
    /// 只有关标签才写；而且它记的是**已经关掉**那份的页码，
    /// 而这里要的是"当前正在看的"。两者碰巧常常相等，但不是一回事。
    pub(crate) session: Rc<RefCell<Option<RestoreTarget>>>,
    /// 「**最新那一帧**的 handles」，自引用。
    ///
    /// 专给**只在第 0 帧注册一次**的回调（拖拽打开 / `folia://open-path` /
    /// 全局 keydown 这类）用。那些闭包捕获的 `handles` 永远停在注册那一帧 ——
    /// `UseStateHandle::set()` 换的是它内部那个 `Rc`，不是原地改值，
    /// 所以**第 0 帧克隆出去的 handle 永远指向第 0 帧那份数据**
    /// （Yew 源码 `use_state` 的文档注释原话就是这么写的；详见 `recent_store` 声明处）。
    ///
    /// 不加这一层会踩的坑：拖入第二份 PDF 时，`apply_document` 里
    /// `tabs_now(&handles)` 拿到的是第 0 帧那个**空列表**，于是 `list.push(tab)`
    /// 之后标签栏里只剩新开的那一个 —— 已经打开的另一份文档被静默吃掉。
    /// （`tabs` 现在已经是活的 store（见 `tabs_store`），这一层对它是冗余的；
    /// 但 `meta` / `current_page` / `annotations` 这些**实时句柄**仍然是逐帧快照，
    /// 所以这层还是得要。）
    ///
    /// `RefCell` 跨渲染是同一个对象，所以每次渲染把当前这份写进去，
    /// 回调调 `now()` 就能拿到"当下"的。取值一律走 [`Handles::now`]。
    ///
    /// ⚠️ **必须是 `Weak`，不能是 `Rc`。** 这个格子里装的**就是一份 `Handles`**，
    /// 而那份 `Handles` 自己也有 `live` 字段 —— 用 `Rc` 就是一个自引用环：
    /// 格子 → Handles → 同一个 `Rc` → 格子。强引用计数永远回不到 0，
    /// 整个状态图（tabs / 批注 / 缩略图 data URL）再也释放不掉。
    /// 平时看不出来（App 只挂载一次，而且每次渲染都会用新的一份替换旧值），
    /// 但只要根组件重挂载一次，就整整漏一份。
    /// 强的那个 `Rc` 由 `app.rs` 自己拿着（`use_mut_ref`），这里只留弱引用。
    pub(crate) live: Weak<RefCell<Option<Handles>>>,
    /// 缩略图缓存的**版本号**，只用来触发重渲染。
    ///
    /// ⚠️ 这里**不再**放 `HashMap<u32, String>`。原来 store 和 state 各存一份
    /// data URL，而"每渲染完一页就把整张表克隆给 state"在整本生成时是
    /// **O(N²) 的字符串拷贝**（500 页累计约 1.5 GB 的瞬时分配）。
    /// 现在 data URL **只**活在 `page_thumbs_store` 里，这里只是一个信号。
    pub(crate) thumbs_gen: UseStateHandle<u64>,
    /// `thumbs_gen` 的**活**来源（`UseStateHandle` 不是活的，见 `Handles::live`）。
    pub(crate) thumbs_counter: Rc<RefCell<u64>>,
    /// 缩略图缓存的**唯一真相**（按页码 → JPEG data URL）。上限见 [`PAGE_THUMB_LIMIT`]。
    pub(crate) page_thumbs_store: Rc<RefCell<HashMap<u32, String>>>,
    /// 总览开着时，缩略图淘汰的**焦点页** = 网格可视区的中心页。
    ///
    /// 只在总览开着时有意义（[`thumb_focus`] 里判 `overview_open`），
    /// 由 `overview_scroll` 和「按需补齐」那条 effect 写。
    /// 关掉总览不用清它 —— [`thumb_focus`] 会直接回落到阅读位置。
    ///
    /// ⚠️ 少了它，淘汰就只剩"离阅读位置最远"这一条规则：总览滚到第 138 页时，
    /// 第 138 页自己就是"最远"的 → 插进去立刻被删 → 死循环（`e2e37`）。
    pub(crate) overview_focus: Rc<RefCell<u32>>,
    pub(crate) pages_busy: Rc<RefCell<HashSet<u32>>>,
    /// 标签激活的代次，见 [`begin_activation`]。
    ///
    /// `begin_activation` 里那次 `open_path` 是异步的：用户在它返回之前又点了别的
    /// 标签，就会有两个任务同时在飞。没有这个代次，"先发的后回"会在最后
    /// `apply_tab_to_live`，把**已经切好的标签又冲回前一个**（`active` 和屏幕上
    /// 显示的内容对不上）；失败路径更糟 —— 晚到的失败会 `clear_live` 并报错，
    /// 把**已经成功打开**的那份文档整个抹掉。
    pub(crate) activation_gen: Rc<RefCell<u32>>,
    /// 「当前实时状态属于哪一份文档」的代次。**只要实时状态被整体换掉就 +1**
    /// （切标签 / 打开新文档 / 回书架），见 `apply_tab_to_live` / `clear_live`。
    ///
    /// 给异步任务用：侧栏缩略图是按**页码**存进一份共享 store 的，而这份 store
    /// 在切标签时会被整体换成另一个标签的。没有这个代次，一份在切换**之前**
    /// 发出的缩略图任务回来后会把**上一份文档**的第 N 页塞进新标签的侧栏里
    /// —— 页码键相同，看起来就像"这一页渲染错了"。
    pub(crate) doc_gen: Rc<RefCell<u32>>,
    /// 「打开文档」流水线的队列（见 [`OpenQueue`]）。
    ///
    /// ⚠️ 所有打开入口都**必须**走 [`request_open`]，别再自己 `spawn_local`
    /// 一条流水线 —— 那正是"拖两份 PDF 进来只开出一份"的成因。
    pub(crate) open_queue: Rc<RefCell<OpenQueue>>,
    /// 页面总览浮层是否打开（见 `overview_open`）。
    pub(crate) overview_open: UseStateHandle<bool>,
    /// 右键菜单（`None` = 没打开）。书架卡片和正文批注共用，见 `CtxMenu`。
    pub(crate) menu: UseStateHandle<Option<CtxMenu>>,
    pub(crate) scroll_ref: NodeRef,
}

impl Handles {
    /// 取「当下这一帧」的那份 handles。
    ///
    /// **凡是"第 N 帧创建、第 N+k 帧才被调用"的闭包都要用它。**
    ///
    /// 最典型的是**只注册一次**的回调（拖拽打开 / `folia://open-path` / 全局监听器）。
    /// ⚠️ 但**别以为"按依赖重注册"就绕过去了** —— 依赖里可能有
    /// **两个不同状态算出来相等**的值：键盘监听那个 effect 的依赖里有
    /// `layout = use_memo((page_sizes, scale))`，两份页面尺寸与页数都相同的 PDF
    /// 算出来完全相等 → 打开第二份时依赖元组一字不变 → effect **不重注册** →
    /// 闭包里的 `handles` 仍然停在只开着第一份那一帧（`tests/e2e/e2e33` 钉的就是它）。
    /// 判据不是"我加了依赖"，而是"**这个闭包会不会跨帧被调用**"。
    ///
    /// 当前帧自己创建、当帧就用的闭包直接用手上那份就行，没必要绕这一圈。
    /// 原理与不这么写会踩的坑见字段 `live` 的注释。
    ///
    /// 兜底 `self.clone()`：`live` 只在渲染函数里写，理论上回调一定晚于第一次渲染，
    /// 但真要是空着（或者强引用已经没了），退回自己这份也比 panic 好。
    pub(crate) fn now(&self) -> Handles {
        self.live
            .upgrade()
            .and_then(|cell| cell.borrow().clone())
            .unwrap_or_else(|| self.clone())
    }
}

/// 读某份文档的批注，并且**如实报告这次读到底成没成**。
///
/// 返回 `(批注, 是否可信)`。第二个值必须一路传到存盘那一环 ——
/// 读失败时我们只能给一个空列表去渲染（界面总得画点什么），
/// 但**绝不能**把这个空列表写回去，那等于把用户攒了半天的批注一次抹掉。
///
/// 见 [`Handles::annotations_ok`] 的注释，以及 `src-tauri` 里
/// `load_annotations` 为什么只在 `NotFound` 时返回 `"[]"`。
pub(crate) async fn load_annotations_guarded(id: &str) -> (Vec<Highlight>, bool) {
    match pdf::load_annotations(id).await {
        Ok(raw) => match serde_json::from_str::<Vec<Highlight>>(&raw) {
            Ok(items) => (items, true),
            Err(err) => {
                // 解析不了：文件可能被外部改坏了，也可能含一个我们不认识的 `kind`。
                // 无论哪种都不能拿空列表去覆盖它 —— 但**要留下痕迹**，
                // 否则用户只会看到"批注不见了"而没有任何线索。
                web_sys::console::error_1(
                    &format!("批注文件解析失败（已跳过回写，磁盘上的原文件没动）：{err}").into(),
                );
                (Vec::new(), false)
            }
        },
        Err(err) => {
            web_sys::console::error_1(
                &format!("读取批注失败（已跳过回写，磁盘上的原文件没动）：{err}").into(),
            );
            (Vec::new(), false)
        }
    }
}

/// 清空「当前文档」的实时状态，但**不动标签列表**。
///
/// 切标签、回书架、关掉最后一个标签都要用它；三者对 `tabs` 的处理各不相同，
/// 所以列表的增删留给调用方。
pub(crate) fn clear_live(handles: &Handles) {
    handles.meta.set(None);
    handles.page_sizes.set(Vec::new());
    handles.outline.set(Vec::new());
    handles.annotations.set(Vec::new());
    handles.query.set(String::new());
    handles.hits.set(Vec::new());
    handles.active_hit.set(None);
    handles.active_highlight.set(None);
    handles.has_selection.set(false);
    *handles.sel_box.borrow_mut() = None;
    handles.sel_bar.set(None);
    handles.history.clear();
    handles.current_page.set(1);
    handles.visible.set((0, 0));
    handles.pending_scroll.set(None);
    *handles.pending_fit.borrow_mut() = false;
    handles.status.set(None);
    handles.loading.set(false);
    handles.page_thumbs_store.borrow_mut().clear();
    bump_thumbs(handles);
    handles.pages_busy.borrow_mut().clear();
    handles.overview_open.set(false);
    // 没有打开的文档，就没有"可信的批注"可以写回磁盘。
    *handles.annotations_ok.borrow_mut() = false;
    // 实时状态所属的文档已经没了 —— 推一格代次，作废所有还在飞的缩略图任务。
    *handles.doc_gen.borrow_mut() += 1;
}

/// 把实时句柄里属于「当前标签」的阅读状态写回 `tabs[idx]`，**并返回写回后的整份列表**。
///
/// 只在切标签 / 关标签 / 回书架 / 打开文档这几个时刻调用 —— 其余时间实时句柄
/// 就是唯一真相，每动一下就写回反而容易和渲染帧错位。
///
/// ⚠️ **要接着改 `tabs` 的调用方建议用这个返回值**（不用也行 —— `tabs` 现在是活的
/// store，回头 `tabs_now()` 读得到刚写进去的那份；但用返回值少一次深拷贝）。
///
/// 以前这里必须用返回值：那会儿 `tabs` 是 `UseStateHandle`，`set()` 是**排队**、
/// 同一帧里句柄里还是 `set` 之前那份，于是"先 `sync_tab_from_live` 写回、再重新
/// clone 一份、在它上面改"会拿到**没有这次写回的旧列表**，把刚存进去的页码/缩放
/// 又冲掉（`tests/e2e/e2e25` 的 reopen 用例钉的就是这个：重开同一份 PDF 时页码从
/// 7 掉回 1）。`tabs_store` 换成活格子之后这条陷阱消失了，判据留着防回归。
pub(crate) fn sync_tab_from_live(handles: &Handles, idx: usize) -> Vec<DocTab> {
    let mut list = (*tabs_now(handles)).clone();
    let Some(tab) = list.get_mut(idx) else {
        return list;
    };
    tab.page = *handles.current_page;
    tab.scale = *handles.scale;
    tab.query = (*handles.query).clone();
    tab.hits = (*handles.hits).clone();
    tab.active_hit = *handles.active_hit;
    tab.annotations = (*handles.annotations).clone();
    tab.annotations_ok = *handles.annotations_ok.borrow();
    tab.thumbs = handles.page_thumbs_store.borrow().clone();
    set_tabs(handles, list.clone());
    list
}

/// 把某个标签的状态灌回实时句柄（不碰 pdf.js）。
pub(crate) fn apply_tab_to_live(handles: &Handles, tab: &DocTab) {
    handles.meta.set(Some(tab.meta.clone()));
    handles.page_sizes.set(tab.sizes.clone());
    handles.outline.set(tab.outline.clone());
    handles.annotations.set(tab.annotations.clone());
    handles.scale.set(tab.scale);
    handles.current_page.set(tab.page);
    handles.query.set(tab.query.clone());
    handles.hits.set(tab.hits.clone());
    handles.active_hit.set(tab.active_hit);
    handles.visible.set((0, 2));
    handles.pending_scroll.set(Some(tab.page));
    // 切回已有标签：这个标签有自己的缩放，别拿适宽把它冲掉。
    *handles.pending_fit.borrow_mut() = false;
    handles.active_highlight.set(None);
    handles.has_selection.set(false);
    *handles.sel_box.borrow_mut() = None;
    handles.sel_bar.set(None);
    handles.history.clear();
    handles.status.set(None);
    *handles.page_thumbs_store.borrow_mut() = tab.thumbs.clone();
    bump_thumbs(handles);
    handles.pages_busy.borrow_mut().clear();
    // 这个标签的批注可不可信，跟着一起搬过来 —— 切回来之后如果不可信，
    // 存盘 effect 依然不会写（否则切一下标签就把那份文档的批注清空了）。
    *handles.annotations_ok.borrow_mut() = tab.annotations_ok;
    // 实时状态现在属于**另一份文档**了 —— 推一格代次，
    // 让上一份文档还在飞的缩略图任务作废（否则会把它的第 N 页塞进这个标签的侧栏）。
    *handles.doc_gen.borrow_mut() += 1;
}

/// 回到书架：**保留标签页**，只关掉 pdf.js 的文档并清空实时状态。
///
/// 标签栏会继续显示已打开的文档，点一下就能回来，行为和浏览器一致。
pub(crate) fn go_library(handles: &Handles) {
    if let Some(active) = *handles.active {
        sync_tab_from_live(handles, active);
    }
    pdf::close();
    handles.active.set(None);
    clear_live(handles);
    if let Some(element) = handles.scroll_ref.cast::<web_sys::HtmlElement>() {
        element.set_scroll_top(0);
    }
}

/// 切换到第 `idx` 个标签（从标签栏点击时用）。
///
/// `from` 是切换前正在看的标签下标 —— 必须由调用方显式传入：
/// `set()` 之后立刻回读 `*handles.active` 拿到的仍是旧值，
/// 靠句柄自己判断会算错要保存哪一个。
pub(crate) fn activate_tab(handles: &Handles, idx: usize, from: Option<usize>) {
    let list = (*tabs_now(handles)).clone();
    let Some(tab) = list.get(idx).cloned() else {
        return;
    };
    begin_activation(handles, idx, tab, from);
}

/// 激活的实体。`tab` 由调用方显式给出，**不要**在这里回读 `tabs` 句柄 ——
/// `close_tab` 刚 `set` 过列表，回读拿到的是删之前的那一份，
/// 会把已经关掉的文档又灌回来（页码、页数全错）。
pub(crate) fn begin_activation(handles: &Handles, idx: usize, tab: DocTab, from: Option<usize>) {
    if from == Some(idx) {
        return;
    }
    if let Some(from) = from {
        sync_tab_from_live(handles, from);
    }

    handles.active.set(Some(idx));
    handles.loading.set(true);
    handles.status.set(None);
    if let Some(element) = handles.scroll_ref.cast::<web_sys::HtmlElement>() {
        element.set_scroll_top(0);
    }

    // 记下这一次激活的代次，见字段 `activation_gen` 的注释。
    // 关键点是：**先推进代次，再发异步任务** —— 这样后一次激活一定拿到更大的号，
    // 前一次的任务回来看见号不对就自己退场，绝不碰实时状态。
    let generation = {
        let mut current = handles.activation_gen.borrow_mut();
        *current += 1;
        *current
    };
    let activation_gen = handles.activation_gen.clone();

    let handles = handles.clone();
    spawn_local(async move {
        // 注意：桥接层的 `loadDocument` 是**先销毁旧文档再载入新的**，
        // 所以一旦失败，旧的 pdf.js 文档已经没了 —— 实时状态必然失效，
        // 只能退回书架并报错（标签本身留在 `tabs` 里，点一下可以重试）。
        let failure = match pdf::open_path(&tab.meta.path).await {
            Ok(raw) => match check_open_result(&raw) {
                Ok(()) => {
                    // 成功，但可能已经过时了（用户在这段时间里又点了别的标签）——
                    // 那就什么都别做，让后来那个任务去收尾。
                    if *activation_gen.borrow() != generation {
                        return;
                    }
                    apply_tab_to_live(&handles, &tab);
                    handles.loading.set(false);
                    return;
                }
                Err(err) => format!("切换失败：{err}"),
            },
            Err(err) => format!("切换失败：{err}"),
        };

        // 失败同理：过时了就别清状态、别报错 ——
        // 否则会把**已经成功打开**的那份文档连状态一起抹掉。
        if *activation_gen.borrow() != generation {
            return;
        }
        handles.active.set(None);
        clear_live(&handles);
        handles.status.set(Some(failure));
    });
}

/// 关闭第 `idx` 个标签。关掉正在看的那一个时，自动顶上相邻的一个；
/// 一个都不剩就回到书架。
pub(crate) fn close_tab(handles: &Handles, idx: usize) {
    let list = (*tabs_now(handles)).clone();
    if idx >= list.len() {
        return;
    }
    let was_active = *handles.active == Some(idx);

    // 正在看的这份被关掉时记下位置：之后从书架再打开同一份能续读。
    if was_active {
        *handles.restore.borrow_mut() = Some(RestoreTarget {
            path: list[idx].meta.path.clone(),
            page: *handles.current_page,
            scale: *handles.scale,
        });
    }

    let mut rest = list.clone();
    let closed = rest.remove(idx);
    set_tabs(handles, rest.clone());
    // 这份文档的侧栏缩略图缓存（`DocTab.thumbs`，Rust 侧的 data URL）不再需要了，
    // `closed` 随作用域丢掉即可。
    // 也不用调 `release_thumbnail`：bridge.js 那边给缩略图用的独立实例**用完即放**，
    // 而且当前文档的缩略图走的是主 `pdfDoc`（关文档时由 `closeDocument` 负责）。
    drop(closed);

    if rest.is_empty() {
        pdf::close();
        handles.active.set(None);
        clear_live(handles);
        return;
    }

    match *handles.active {
        // 关掉的是正在看的那份 → 让原位置顶上来的那个接棒。
        // 用 `rest`（刚算出来的新列表）取，不要回读句柄。
        Some(active) if active == idx => {
            let next = idx.min(rest.len() - 1);
            let tab = rest[next].clone();
            handles.active.set(None);
            begin_activation(handles, next, tab, None);
        }
        // 关掉的是前面某个 → 活动下标整体前移
        Some(active) if active > idx => handles.active.set(Some(active - 1)),
        _ => {}
    }
}

/// 把当前状态落盘（异步，不等结果）。
///
/// 阅读区那个「页码变了就存一次」的 effect 只覆盖**开着文档**的情况。
/// 书架上的动作（收藏 / 取消收藏 / 删除记录 / 删除记录和文件 / 打开新文档登记进列表）
/// 全都发生在**首页** —— 那时没有打开的文档，那个 effect 一次都不跑。
/// 少了这一步，"在首页收藏一下、退出应用、再打开"收藏就没了，
/// 用户会以为功能坏了（而这正是**看得见**的坏）。
///
/// `last_path` / `last_page` / `scale` 原样沿用 `session` 里那份，
/// 理由见 `Handles::session` 的注释 —— 千万别在这里现算。
///
/// ⚠️ **`state_ok` 为假时直接不写。** 那意味着磁盘上那份 `state.json` 我们
/// 既读不出来、也没能留档 —— 此时写下去就是把没能保住的用户数据盖掉。
/// 见 [`Handles::state_ok`]。
pub(crate) fn persist_state(handles: &Handles) {
    if !*handles.state_ok.borrow() {
        return;
    }
    let session = handles.session.borrow().clone();
    let state = PersistedState {
        recent_files: handles.recent_store.borrow().clone(),
        last_path: session.as_ref().map(|s| s.path.clone()),
        last_page: session.as_ref().map(|s| s.page),
        scale: session.as_ref().map(|s| s.scale),
    };
    if let Ok(json) = serde_json::to_string(&state) {
        spawn_local(async move {
            let _ = pdf::save_app_state(&json).await;
        });
    }
}

/// 「最近打开」的唯一读写入口：改 store，再把快照推给 state，最后落盘。
///
/// **所有对最近列表的修改都必须走这里。** 直接 `(*handles.recent).clone()` 再 `set`
/// 在「当前帧创建的闭包」里碰巧是对的，但一旦这个 `handles` 是从
/// `use_effect_with((), …)` 那种只跑一次的 effect 里带出来的，读到就是第 0 帧的空列表 ——
/// 详见 `recent_store` 声明处的注释。
///
/// 落盘也放这儿而不是让每个调用方自己记得存：**列表变了就是要存**，
/// 这是同一个不变量。散出去写迟早会漏一处 —— 已经漏过（首页收藏不落盘）。
pub(crate) fn write_recent(handles: &Handles, mutate: impl FnOnce(&mut Vec<RecentFile>)) {
    let (snapshot, evicted) = {
        let mut list = handles.recent_store.borrow_mut();
        // 改之前有哪些 path、改完还剩哪些，做差集 —— 用来找"被挤出列表"的那些。
        let before: HashSet<String> = list.iter().map(|item| item.path.clone()).collect();
        mutate(&mut list);
        let after: HashSet<String> = list.iter().map(|item| item.path.clone()).collect();
        let evicted: Vec<String> = before.difference(&after).cloned().collect();
        (list.clone(), evicted)
    };
    // ⚠️ 从最近列表里消失的条目，必须顺手把 pdf.js 侧那份缩略图文档放掉。
    //
    // 原先只有「删除记录」和「清空最近打开」记得放，而 `record_open` 里那个
    // `list.truncate(10)` 把第 11 条挤出去时**没人放** —— 那份 `PDFDocumentProxy`
    // （扣着整份 PDF 的字节与解析结果）就永久常驻了。更糟的是 Rust 侧那份
    // `thumbs` 快照还留着它的 id，于是**以后也不会再请求它**，等于再也回收不掉。
    // 缓存按"本次会话里出现过的所有文件"增长，而书架只显示 10 个。
    //
    // 放在这个唯一的读写入口上，上限淘汰 / 删除 / 清空三条路一次覆盖 ——
    // 别在调用点各写一遍（`forget_recent` 原来自己放过一遍，是重复的）。
    //
    // ⚠️ 这里**只管 pdf.js 侧那份 `PDFDocumentProxy`**（大头：整份 PDF 的字节
    // 与解析结果）。Rust 侧那份 `thumbs` 的 data URL 归「书架缩略图」effect 管
    // （它跟着 `recent` 重跑）—— 两边管的不是同一层东西，别混在一起。
    for path in evicted {
        pdf::release_thumbnail(&path);
    }
    handles.recent.set(snapshot);
    persist_state(handles);
}

/// 从「最近打开」里摘掉一条。
///
/// ✕ 按钮、右键菜单的「删除记录」、以及「删除记录和文件」都走这里。
///
/// 这里**只管列表**。两份派生缓存各有归属，别在这儿再抄一遍（散着写迟早会漂）：
/// - pdf.js 侧那份 `PDFDocumentProxy` → `write_recent` 按「前后差集」释放；
/// - Rust 侧那份 `thumbs` data URL → 「书架缩略图」effect 跟着 `recent` 重跑时清掉。
///
/// 传 `&Handles` 而不是各个 state 句柄：`recent_store` 是跨帧同一个 `RefCell`，
/// 所以哪怕传进来的是旧帧的 `handles`，读写也落在同一份数据上（见 `Handles::live`）。
pub(crate) fn forget_recent(handles: &Handles, id: &str) {
    write_recent(handles, |list| list.retain(|item| item.id != id));
}

/// 文档加载完成后的收尾：把它登记成一个标签页并激活。
///
/// 两种情况：
/// - **这份文档已经开在某个标签里** —— pdf.js 刚载入的正是它，直接切过去，
///   保留原来的页码 / 缩放 / 检索，不要重新解析（也就不会把进度冲掉）。
/// - **新文档** —— 先把当前标签的进度存回去，再追加一个新标签。
pub(crate) async fn apply_document(handles: Handles, meta: DocMeta) {
    handles.loading.set(true);
    handles.status.set(None);

    // ---- 已经在标签里了：直接激活
    //
    // ⚠️ 换文档之前**必须**先把当前标签的实时状态写回 `tabs` —— 和新文档那条路一样。
    // 实时句柄才是唯一真相，`tabs[idx]` 只是一份「上次切走时」的快照
    // （见 `sync_tab_from_live` 的注释）。少了这一步有两个后果：
    //   ① 用户点回那个标签时 `apply_tab_to_live` 会把**旧快照**灌进实时句柄 ——
    //      页码 / 缩放退回切走之前的位置；
    //   ② 更糟的是批注：旧快照的 `annotations_ok` 仍为真，于是存盘 effect 随即
    //      把**旧快照**写进那份文档的文件 —— **用户刚加的批注从磁盘上静默消失**。
    // 这条路是可达的：把一份**已经开着**的 PDF 再拖进来 / 再走一次「打开方式」/
    // 在「打开其它 PDF」里选中它，`open_path_flow` 没有去重，直接就到这里了。
    //
    // 注意 `prev == idx`（重新打开正在看的这一份）时这次同步是个**无损往返**：
    // 先把实时状态存进快照，再原样灌回来，顺手把下面刚更新的 `meta` 带进去。
    let list = (*tabs_now(&handles)).clone();
    if let Some(idx) = list.iter().position(|tab| tab.meta.id == meta.id) {
        // 用 `sync_tab_from_live` 的**返回值**接着改（理由见它的注释）。
        let mut list = match *handles.active {
            Some(prev) => sync_tab_from_live(&handles, prev),
            None => list,
        };
        list[idx].meta = meta;
        set_tabs(&handles, list.clone());
        handles.active.set(Some(idx));
        apply_tab_to_live(&handles, &list[idx]);
        handles.loading.set(false);
        return;
    }

    // ---- 新文档：等把该读的读完，**提交前**再把当前标签的进度存回去
    //
    // ⚠️ 注意这里**没有**在 await 之前取 `tabs` 快照。快照必须等到提交那一刻
    // 再取（见下面「提交」那一段）—— 中间跨了三次 await，`tabs` 完全可能
    // 已经被别人改过（用户点了别的标签、另一条打开流水线提交了）。
    // 早取的那份一 `set` 回去就把别人的改动整个覆盖掉（标签静默消失）。

    let sizes: Vec<PageSize> = pdf::page_sizes()
        .await
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default();

    // `page_sizes()` 失败时 `sizes` 会是空列表。若 pdf.js 明明报告了页数
    // （`meta.num_pages > 0`），那这就**不是**"这份 PDF 没有页"，而是解析失败了。
    // 不吭声的话用户看到的只有「文档已打开，但没有解析到任何页面」——
    // 既不知道出了什么事，也没法判断是不是文件坏了。见下面设 `status` 的地方。
    let sizes_failed = sizes.is_empty() && meta.num_pages > 0;

    let outline: Vec<OutlineNode> = pdf::outline()
        .await
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_default();

    // 读批注：**必须**把"这次到底读成没读成"一起带出来。
    // 失败时下面那个「持久化批注」effect 会在同一帧被触发 —— 没有这个标志的话，
    // 它会拿着空列表去覆盖磁盘上的真实文件（静默删光用户的批注）。
    let (annotations, annotations_ok) = load_annotations_guarded(&meta.id).await;

    // 注意：下面一律用**局部变量**记录，不要回头读 state handle。
    // `UseStateHandle::set()` 之后句柄里仍是旧值，读回来会拿到上一次渲染的数据
    // （曾经因此把 pages 写成 0，首页的进度条和「继续阅读」卡片全部静默消失）。
    let page_count = if meta.num_pages > 0 {
        // pdf.js 的 numPages 最权威，page_sizes 失败时它依然可靠。
        meta.num_pages
    } else {
        sizes.len() as u32
    };

    // 续读页码的夹取上界。
    //
    // ⚠️ 用 `page_count`（= `meta.num_pages`，pdf.js 的权威值），**不要**用
    // `sizes.len()`。`page_sizes()` 是可能失败的 —— 那正是上面 `page_count` 要
    // 兜底的理由 —— 一旦失败，`sizes` 就是空列表，`sizes.len().max(1)` 恒等于 1，
    // 于是 `clamp(1, 1)` 把续读页码**强制拉回第 1 页**，并把这个 1 当成新的
    // `last_page` 存回去：用户的阅读进度被静默清零。
    let max_page = page_count.max(1);

    // 续读页码：**三档回退**。
    //
    // ① `restore` 槽位命中（上次**关掉**的正是这份）—— 它同时带着 `scale`，最完整；
    // ② 否则看「最近打开」里这份自己的 `last_page` —— 书架卡片上那个 `30/100` 就是它。
    //    光认 ① 是不够的：`restore` 是**单槽**、只记"最后关掉的那一份"，
    //    从书架打开任何**别的**文档都会落到最后的 `_` 分支、回第 1 页 ——
    //    而下面 `entry.last_page = page` 会把这个 1 写回最近列表，
    //    **卡片上记着的真实进度被静默覆盖掉**（`tests/e2e/e2e31` 钉的就是它）。
    // ③ 两处都没记（这份从没读过）—— 才回第 1 页。
    //
    // `resuming` 决定要不要适宽：只要落在"记得住的位置"上就沿用当前缩放、不适宽。
    let (page, scale, resuming) = match handles.restore.borrow().clone() {
        Some(target) if target.path == meta.path => (
            target.page.clamp(1, max_page),
            target.scale.clamp(0.25, 5.0),
            true,
        ),
        // ⚠️ 读 `recent_store`（跨帧同一个 `RefCell`、唯一真相），不要读 `handles.recent`
        // 那个渲染快照 —— 冷启动带待打开文件时它是空的（同 `write_recent` 的注释）。
        _ => {
            let remembered = handles
                .recent_store
                .borrow()
                .iter()
                .find(|item| item.id == meta.id)
                .map(|item| item.last_page)
                .unwrap_or(1);
            if remembered > 1 {
                (remembered.clamp(1, max_page), *handles.scale, true)
            } else {
                (1, *handles.scale, false)
            }
        }
    };

    let tab = DocTab {
        meta: meta.clone(),
        sizes,
        outline,
        annotations,
        annotations_ok,
        page,
        scale,
        query: String::new(),
        hits: Vec::new(),
        active_hit: None,
        thumbs: HashMap::new(),
    };

    // ---- 提交：登记成标签并激活
    //
    // ⚠️ 到这里为止跨了三次 await（`page_sizes` / `outline` / 读批注），期间
    // `tabs` 可能已经被别人改过（用户点了别的标签；另一条打开流水线刚提交完）。
    // 所以**在这里**才把实时状态写回、才重新取列表：
    //   - `sync_tab_from_live` 读的是实时句柄（跨帧同一个 `RefCell`，见
    //     `Handles::live`），现在调才是"把当下这份进度存回去"；
    //   - 列表也必须是**此刻**的那一份（`tabs_now` 读的是活的 `tabs_store`）。
    //     早取的那份一 push 就把别人的改动整个覆盖掉 ——
    //     拖两份 PDF 进来只开出一份（`tests/e2e/e2e38`）。
    let mut list = match *handles.active {
        Some(prev) => sync_tab_from_live(&handles, prev),
        None => (*tabs_now(&handles)).clone(),
    };
    list.push(tab.clone());
    let index = list.len() - 1;
    set_tabs(&handles, list);
    handles.active.set(Some(index));
    apply_tab_to_live(&handles, &tab);
    // 适宽只给「全新打开」的文档；续读的那份沿用存下来的缩放。
    // 必须在 `apply_tab_to_live` 之后设 —— 它内部会把这个标志清成 false。
    *handles.pending_fit.borrow_mut() = !resuming;
    handles.loading.set(false);

    // 放在 `apply_tab_to_live` **之后**：它内部会把 `status` 清成 `None`，
    // 早了就被它盖掉，用户什么也看不到。
    if sizes_failed {
        web_sys::console::error_1(
            &format!(
                "页面尺寸解析失败：pdf.js 报告 {} 页，但 page_sizes() 返回空",
                meta.num_pages
            )
            .into(),
        );
        handles.status.set(Some(format!(
            "页面尺寸解析失败（pdf.js 报告 {} 页）—— 请关掉再重新打开这份文档",
            meta.num_pages
        )));
    }

    // 记入最近打开：补上总页数、上次读到的页码与打开时间，首页的进度条靠这些数据。
    let mut entry = RecentFile::from(&meta);
    entry.pages = page_count;
    entry.last_page = page;
    entry.opened_at = Date::now();

    // ⚠️ 走 `write_recent`，别直接读 `handles.recent` —— 冷启动带待打开文件那条路上，
    // 这个 `handles` 是从只跑一次的启动 effect 里带出来的，直接读会拿到空列表，
    // 一写就把 `load_app_state` 刚读回来的历史全冲掉。
    write_recent(&handles, move |list| {
        // ⚠️ **先把原来的收藏标记接过来。**
        //
        // `RecentFile::from(&meta)` 里 `favorite` 是**写死**的 `false`
        // （`DocMeta` 根本不带收藏信息，见 `types.rs`）。而下面那句 `retain`
        // 会把带 `favorite: true` 的旧条目**删掉**、换成刚构造的这条 ——
        // 不先读出来的话，**打开一次就把用户标的星标抹一次**，而且全程静默。
        //
        // 这条路只在文档**不在标签里**时才走到（函数开头的早退分支不碰最近列表），
        // 所以「收藏 → 退出应用 → 从书架再打开」正是命中的那一步。
        // `tests/e2e/e2e30` 钉的就是它。
        if let Some(old) = list.iter().find(|item| item.id == meta.id) {
            entry.favorite = old.favorite;
        }
        list.retain(|item| item.id != meta.id);
        list.insert(0, entry);

        // ⚠️ **淘汰时豁免收藏条目。**
        //
        // 上限（`RECENT_LIMIT`）管的是"自动历史"，而 `favorite` 是用户显式说的
        // "留着"。原来那句裸的 `list.truncate(10)` 不看 `favorite`：收藏一份、
        // 再打开 10 份别的文档，被收藏的那份就从书架消失，紧接着 `persist_state`
        // 把它的 `favorite: true` 一起写没 —— **收藏静默消失，且不可恢复**
        // （`tests/e2e/e2e32` 钉的就是它）。
        //
        // 名额算法：先数出收藏占了几条，剩下的名额才留给非收藏；收藏本身不占名额、
        // 也永不淘汰。于是列表长度 = max(收藏数, 上限)。收藏超过上限时列表会变长 ——
        // 那是用户自己标的，不该由我们替他删。
        let mut room = RECENT_LIMIT.saturating_sub(list.iter().filter(|i| i.favorite).count());
        list.retain(|item| {
            if item.favorite {
                true
            } else if room > 0 {
                room -= 1;
                true
            } else {
                false
            }
        });
    });
}

/// 把桥接层返回的打开结果判成 `Ok(())` / 错误信息。
///
/// 切标签时用：那份文档之前已经打开成功过，这里只做一次校验，
/// 失败就把错误显示出来，而不是静默停在旧文档上。
pub(crate) fn check_open_result(raw: &str) -> Result<(), String> {
    let parsed: OpenResult = serde_json::from_str(raw).map_err(|err| format!("{err}"))?;
    if let Some(error) = parsed.error {
        return Err(error);
    }
    if parsed.meta.is_none() && !parsed.ok {
        return Err("无法打开该文件".into());
    }
    Ok(())
}

/// 解析桥接层返回的打开结果。
pub(crate) async fn handle_open_result(handles: &Handles, raw: String) {
    let parsed: OpenResult = match serde_json::from_str(&raw) {
        Ok(value) => value,
        Err(err) => {
            handles.status.set(Some(format!("打开失败：{err}")));
            return;
        }
    };
    if parsed.cancelled {
        return;
    }
    if let Some(error) = parsed.error {
        handles.status.set(Some(error));
        return;
    }
    match parsed.meta {
        Some(meta) => apply_document(handles.clone(), meta).await,
        None if !parsed.ok => handles.status.set(Some("无法打开该文件".into())),
        None => {}
    }
}

/// 排队版入口：走系统文件对话框。
pub(crate) fn open_dialog_flow(handles: Handles) {
    request_open(handles, OpenRequest::Dialog);
}

/// 排队版入口：已知路径（拖拽 / 双击 / 最近列表）。
pub(crate) fn open_path_flow(handles: Handles, path: String) {
    request_open(handles, OpenRequest::Path(path));
}

/// 「打开文档」流水线的排队请求。只有两种入口，都从 `app.rs` 来。
#[derive(Clone)]
pub(crate) enum OpenRequest {
    /// 走系统文件对话框。
    Dialog,
    /// 已知路径：拖拽 / 双击 .pdf / 资源管理器「打开方式」/ 书架上的最近列表。
    Path(String),
}

/// 「打开文档」这条流水线的**单槽队列**。
///
/// # 为什么必须串行
///
/// `pdf::open_path()` / `pdf::open_dialog()` 把 PDF 装进 pdf.js 里**那一个**全局
/// 文档，而 `apply_document` 之后还要连着 await 三次（`page_sizes` / `outline` /
/// 读批注）才提交标签 —— 读的全是"当前那份"。两条流水线一重叠就出两件事：
///
/// 1. 第二条的 `open_path` 会在第一条还在读的时候把全局文档换掉 ——
///    第一条拿到的是**别人的**页数 / 目录（还会拿它去夹续读页码）；
/// 2. 两条各自在 `await` **之前**取了一份 `tabs` 快照，最后提交的那条用过期快照
///    `set` 回去，**把另一条刚 push 进来的标签整个覆盖掉** —— 拖两份 PDF 进来，
///    标签栏里只剩一份，全程静默。
///
/// 所以做成"同一时刻只有一条流水线在跑，其余的排队"。`running` 与 `pending` 的
/// 读写之间**没有 await**（wasm 单线程），所以不会丢请求。
#[derive(Default)]
pub(crate) struct OpenQueue {
    pending: VecDeque<OpenRequest>,
    running: bool,
}

/// 请求打开一份文档：排进队列，必要时拉起那条唯一的流水线。
///
/// ⚠️ 调用方**不要再**自己 `spawn_local` 一层：拉起流水线是这里的事，
/// 重复 spawn 就等于把"同一时刻只有一条"这件事让回给调用方去保证。
pub(crate) fn request_open(handles: Handles, request: OpenRequest) {
    {
        let mut queue = handles.open_queue.borrow_mut();
        queue.pending.push_back(request);
        if queue.running {
            return;
        }
        queue.running = true;
    }
    spawn_local(run_open_queue(handles));
}

/// 唯一的流水线：一件一件跑完队列里的请求，跑空了就把 `running` 放掉。
async fn run_open_queue(handles: Handles) {
    loop {
        // ⚠️ 这一段**不能有 await**：「队列空了」和「复位 running」必须是同一个
        // 原子动作，否则中间会出现"已经复位、又有人 push 进来"的窗口 ——
        // 那个请求会一直躺在队列里没人跑（打开文档静默失效）。
        let next = {
            let mut queue = handles.open_queue.borrow_mut();
            match queue.pending.pop_front() {
                Some(request) => request,
                None => {
                    queue.running = false;
                    return;
                }
            }
        };
        match next {
            OpenRequest::Dialog => open_dialog_once(&handles).await,
            OpenRequest::Path(path) => open_path_once(&handles, path).await,
        }
    }
}

async fn open_dialog_once(handles: &Handles) {
    if let Err(err) = pdf::ready().await {
        handles.status.set(Some(err));
        return;
    }
    match pdf::open_dialog().await {
        Ok(raw) => handle_open_result(handles, raw).await,
        Err(err) => handles.status.set(Some(format!("打开失败：{err}"))),
    }
}

async fn open_path_once(handles: &Handles, path: String) {
    match pdf::open_path(&path).await {
        Ok(raw) => handle_open_result(handles, raw).await,
        Err(err) => handles.status.set(Some(format!("打开失败：{err}"))),
    }
}
