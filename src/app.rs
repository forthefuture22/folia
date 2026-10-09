//! Folia 应用主体：状态编排、工具栏、阅读区与交互。

use std::collections::{HashMap, HashSet};
use std::rc::Rc;

use wasm_bindgen::closure::Closure;
use wasm_bindgen::JsCast;
use wasm_bindgen_futures::spawn_local;
use web_sys::{InputEvent, KeyboardEvent, MouseEvent};
use yew::prelude::*;

use crate::components::confirm_delete::confirm_delete_html;
use crate::components::ctx_menu_view::ctx_menu_html;
use crate::components::file_card::file_cards_html;
use crate::components::icon::icon;
use crate::components::page_view::PageView;
use crate::components::sidebar::{Sidebar, SidebarTab};
use crate::ctx_menu::{clamp_menu_pos, viewport_size, CtxMenu, CARD_MENU_ITEMS, MARK_MENU_ITEMS};
use crate::dom::{
    copy_text, is_typing, right_click_is_editable, selection_box, stage_view, visible_pages,
};
use crate::layout::{fit_scale, scroll_to_hit, scroll_to_page, Layout};
use crate::pdf;
use crate::pdf::types::{
    DocMeta, Highlight, MarkKind, OutlineNode, PageSize, PersistedState, RecentFile, SearchHit,
    SelectionInfo,
};
use crate::selbar::{resolve_sel_bar, SelBar, SelBox};
use crate::state::document::{mark_callback, DocTab, MarkCtx, RestoreTarget};
use crate::state::handles::{
    activate_tab, bump_thumbs, close_tab, forget_recent, go_library, open_dialog_flow,
    open_path_flow, put_thumb, tabs_now, thumb_focus, write_recent, Handles, OpenQueue, ThumbStore,
    PAGE_THUMB_LIMIT,
};
use crate::state::history::History;
use crate::ui::format::{color_label, next_zoom};

/// 可选的高亮颜色。
const COLORS: [&str; 4] = ["yellow", "green", "blue", "pink"];

/// 吐司（保存 / 复制 / 撤回的反馈条）停留多久自动收起，毫秒。
///
/// 2 秒：短到不挡事，长到能读完「已保存 12 条批注」这种一句话。
/// 想手动提前关掉也可以直接点它。
const TOAST_MS: i32 = 2000;

/// 书架的两个分页。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LibraryTab {
    Recent,
    Favorites,
}

#[function_component(App)]
pub fn app() -> Html {
    // ---- 标签页（多份文档）
    //
    // 唯一真相是 `tabs_store` 那个活格子（见 `Handles::tabs_store`：`UseStateHandle`
    // 不是活的，而这里是典型的读-改-写）；`tabs_gen` 只是它的"变了"信号。
    let tabs_store = use_mut_ref(|| Rc::new(Vec::<DocTab>::new()));
    let tabs_gen = use_state(|| 0_u64);
    let tabs_counter = use_mut_ref(|| 0_u64);
    let active = use_state(|| Option::<usize>::None);

    // ---- 当前文档的实时状态（切标签时会被 `apply_tab_to_live` 整体替换）
    let meta = use_state(|| Option::<DocMeta>::None);
    let page_sizes = use_state(Vec::<PageSize>::new);
    let outline = use_state(Vec::<OutlineNode>::new);
    let annotations = use_state(Vec::<Highlight>::new);
    // 当前这份批注能不能往磁盘写（读成功过 / 用户改过）。见 `Handles::annotations_ok`。
    let annotations_ok = use_mut_ref(|| false);
    let recent = use_state(Vec::<RecentFile>::new);
    // 「最近打开」的**唯一真相**在这里，`recent` 只是渲染用的快照。
    //
    // 为什么不能用 `recent` 直接做「读-改-写」：`UseStateHandle::set()` 换的是它内部那个
    // `Rc`，而不是原地改值 —— 所以**第 0 帧克隆出去的 handle，永远指向第 0 帧那份数据**。
    // Yew 源码的 `use_state` 文档里就写着这句话。启动 effect 正是
    // `use_effect_with((), …)`：它只在第 0 帧跑一次，拿到的是第 0 帧的 `handles`
    //（那时 `recent` 还是空 `Vec`），再把它传给 `open_path_flow`。
    // 于是「冷启动 + 带着一个 PDF 路径启动」时，`apply_document` 读到的是**空列表**，
    // 写完就把刚 `load_app_state` 读回来的几十条历史**整个冲掉**。
    //
    // 修法沿用本文件里缩略图那套（`page_thumbs_store` + `page_thumbs`）：
    // 读写都走 store，写完再 `set` 一份快照触发重渲染。
    let recent_store = use_mut_ref(Vec::<RecentFile>::new);
    // 磁盘上那份 `state.json` 现在能不能往写。默认 **false** —— 没读到之前一律不许写，
    // 否则「读不出来」会被当成「没有数据」写回去。见 `Handles::state_ok`。
    let state_ok = use_mut_ref(|| false);

    // ---- 视图状态
    let scale = use_state(|| 1.25_f64);
    let current_page = use_state(|| 1_u32);
    let visible = use_state(|| (0_usize, 0_usize));
    let pending_scroll = use_state(|| Option::<u32>::None);
    let pending_fit = use_mut_ref(|| false);
    let sidebar_open = use_state(|| true);
    let sidebar_tab = use_state(|| SidebarTab::Thumbnails);
    // 「页面总览」浮层是否打开（点顶栏的 ▦）。
    //
    // 为什么不是「把侧栏切到缩略图页」：侧栏**默认就是开着的、默认就停在缩略图页**，
    // 所以那样做用户点下去屏幕上什么都不变 —— 会被当成"按钮坏了"。
    // 总览是盖住整个窗口的一层网格，点下去一定有反应，也真的能一屏看全所有页。
    let overview_open = use_state(|| false);
    let highlight_color = use_state(|| "yellow".to_string());
    let active_highlight = use_state(|| Option::<String>::None);
    let library_tab = use_state(|| LibraryTab::Recent);
    let library_filter = use_state(String::new);
    let library_sort_by_name = use_state(|| false);
    let show_filter = use_state(|| false);
    // Folia 现在是不是系统默认的 PDF 阅读器。`None` = 还没查出来（或系统没登记）。
    //
    // 只用来决定首页那个「设为默认」按钮**要不要显示**：已经是默认了还摆着它，
    // 点下去屏幕上什么都不会变，用户只会当成"按钮坏了"（本项目已经栽过一次 ——
    // 见「一个按钮如果和默认状态重合，就等于不存在的按钮」）。
    //
    // 启动时查一次是**只读**操作（`LSCopyDefaultRoleHandlerForContentType`），
    // 不会碰系统设置；真正会改设置的 `set_default_pdf_handler` 仍然只在点击时才调。
    let is_default_pdf = use_state(|| Option::<bool>::None);
    // 右键菜单（见 `CtxMenu`）：书架卡片和正文批注共用一份状态。
    // 存的是打开那一刻的快照。
    let menu = use_state(|| Option::<CtxMenu>::None);
    // 「删除记录和文件」的二次确认：`Some(_)` = 确认框开着，里面是要删的那一份。
    //
    // 单独存一份、不复用菜单里的 —— 确认框弹出来的时候菜单已经关掉了。
    // 删用户的文件是不可逆的（虽然进了回收站），**必须先问一遍**，
    // 而且确认框里要把文件名原样写出来，别让用户对着一个"确定吗？"瞎猜。
    let pending_delete = use_state(|| Option::<RecentFile>::None);

    // ---- 缩略图：书架卡片（按文档 id）与侧栏/总览（按页码）分开缓存
    let thumbs = use_state(HashMap::<String, String>::new);
    // ⚠️ 页码缩略图这一边，data URL **只**存在 `page_thumbs_store` 一份里；
    // `thumbs_gen` 只是一个"变了"的信号（`thumbs_counter` 是它的活来源）。
    //
    // 原来 store 和 state 各存一份，而"每渲染完一页就把整张表克隆给 state"
    // 在整本生成时是 **O(N²) 的字符串拷贝** —— 500 页累计约 1.5 GB 的瞬时分配，
    // 外加常驻两份 data URL。上限与淘汰策略见 `state/handles.rs` 的 `PAGE_THUMB_LIMIT`。
    let thumbs_gen = use_state(|| 0_u64);
    let thumbs_counter = use_mut_ref(|| 0_u64);
    let page_thumbs_store = use_mut_ref(HashMap::<u32, String>::new);
    // 总览开着时缩略图淘汰的**焦点页**（= 网格可视区中心页）。见 `Handles::overview_focus`
    // 与 `thumb_focus`。0 = 还没算过，`thumb_focus` 会回落到阅读位置。
    let overview_focus = use_mut_ref(|| 0_u32);
    // 书架缩略图任务的代次（见下面那条 effect）：`recent` 一变就 +1，
    // 只有最新那条任务会干活、会写 state，旧的直接退场。
    //
    // ⚠️ 这里**不再有**一把"正在渲染中的 id"的锁（原 `thumbs_busy`）：那把锁
    // 跨代次，会让接班的新任务跳过"上一代正在渲染"的 id，而上一代过期后又不写
    // 结果 —— 那一份就永远没有缩略图。理由见那条 effect 的注释。
    let shelf_gen = use_mut_ref(|| 0_u64);
    // 主题代次：主题一变，**按主题烘焙**的缩略图全部过期（深色那张是把整页反相、
    // 再把图片按原色贴回去合成出来的，见 `bridge.js` 的 `composeDarkThumbnail`）——
    // 一张位图只对一种主题成立，所以只能重烘。
    //
    // ⚠️ 它同时是书架那条 effect 的**依赖之一**：页码缩略图那边清空 store 就够
    // （侧栏/总览是按需请求的，缺了会自己再要），但书架那条 effect 的依赖是
    // "列表里有哪几份" —— 光清空 map 它不会重跑。
    //
    // 形状跟 `thumbs_gen` 一致：`theme_counter` 是**活的**来源，理由见 `bump_thumbs`
    // （闭包捕获的那个句柄永远停在注册那一帧）。
    let theme_gen = use_state(|| 0_u64);
    let theme_counter = use_mut_ref(|| 0_u64);
    let pages_busy = use_mut_ref(HashSet::<u32>::new);
    // 标签激活代次（见 `begin_activation`）：连点标签时作废先发的那个 `open_path` 任务。
    let activation_gen = use_mut_ref(|| 0_u32);
    // 实时状态所属文档的代次（见 `Handles::doc_gen`）：作废切换前发出的缩略图任务。
    let doc_gen = use_mut_ref(|| 0_u32);
    // 「打开文档」流水线的队列（见 `OpenQueue`）：拖两份 PDF 进来时让它们排队，
    // 而不是两条流水线互相覆盖（只开出一份）。
    let open_queue = use_mut_ref(OpenQueue::default);

    // ---- 检索状态
    let query = use_state(String::new);
    let hits = use_state(Vec::<SearchHit>::new);
    let active_hit = use_state(|| Option::<usize>::None);
    let search_gen = use_mut_ref(|| 0_u32);

    // ---- 其它
    let status = use_state(|| Option::<String>::None);
    let loading = use_state(|| false);
    let has_selection = use_state(|| false);
    // 选中文字后浮出的工具栏。`sel_box` 是原始几何（内容坐标，滚动时用来重算），
    // `sel_bar` 是解算好的落点（`None` = 不显示）。
    let sel_box = use_mut_ref(|| Option::<SelBox>::None);
    let sel_bar = use_state(|| Option::<SelBar>::None);
    // 撤销栈的深度单独存一份 state：栈本身在 `RefCell` 里，变的时候不会重渲染，
    // 而「撤回」按钮的 disabled 得跟着变。
    let undo_depth = use_state(|| 0_usize);
    let history = History {
        stack: use_mut_ref(Vec::<Vec<Highlight>>::new),
        depth: undo_depth.clone(),
    };
    let restore = use_mut_ref(|| Option::<RestoreTarget>::None);
    // 上次落盘时的 `last_path` / `last_page` / `scale`，供首页动作落盘时沿用。
    // 见 `Handles::session` / `persist_state` —— 别在别处现算这三个值。
    let session = use_mut_ref(|| Option::<RestoreTarget>::None);
    // 「最新那一帧的 handles」的格子。见 `Handles::live` / `Handles::now` ——
    // 只在第 0 帧注册一次的回调靠它拿到当下的状态。
    //
    // ⚠️ 强的 `Rc` 由**这里**（`app.rs`）持有，`Handles` 里那个 `live` 只拿 `Weak`。
    // 不能两边都拿 `Rc`：格子里装的就是一份 `Handles`，那份 `Handles` 自己也指向这个
    // 格子，两个强引用就构成自引用环，整个状态图永远释放不掉（见 `Handles::live`）。
    let live = use_mut_ref(|| Option::<Handles>::None);

    let scroll_ref = use_node_ref();
    let search_ref = use_node_ref();
    let page_input_ref = use_node_ref();
    // 页面总览的网格（滚动时按需补缩略图，见 `overview_top_up`）。
    let overview_grid_ref = use_node_ref();
    // 请求把焦点送进搜索框。
    // 不能「切页签 + 立刻 focus」：搜索框长在「搜索结果」面板里，
    // 切页签只是 `set` 了一下状态，DOM 要等这一帧渲染完才挂上去，
    // 同步 focus 拿到的是 `null`。这里先记下意图，交给渲染后的 effect 执行。
    let search_focus = use_mut_ref(|| false);

    let handles = Handles {
        tabs_store: tabs_store.clone(),
        tabs_gen: tabs_gen.clone(),
        tabs_counter: tabs_counter.clone(),
        active: active.clone(),
        meta: meta.clone(),
        page_sizes: page_sizes.clone(),
        outline: outline.clone(),
        annotations: annotations.clone(),
        annotations_ok: annotations_ok.clone(),
        recent: recent.clone(),
        recent_store: recent_store.clone(),
        state_ok: state_ok.clone(),
        is_default_pdf: is_default_pdf.clone(),
        scale: scale.clone(),
        current_page: current_page.clone(),
        visible: visible.clone(),
        pending_scroll: pending_scroll.clone(),
        pending_fit: pending_fit.clone(),
        status: status.clone(),
        loading: loading.clone(),
        query: query.clone(),
        hits: hits.clone(),
        active_hit: active_hit.clone(),
        active_highlight: active_highlight.clone(),
        has_selection: has_selection.clone(),
        sel_box: sel_box.clone(),
        sel_bar: sel_bar.clone(),
        history: history.clone(),
        restore: restore.clone(),
        session: session.clone(),
        thumbs_gen: thumbs_gen.clone(),
        thumbs_counter: thumbs_counter.clone(),
        page_thumbs_store: page_thumbs_store.clone(),
        overview_focus: overview_focus.clone(),
        pages_busy: pages_busy.clone(),
        activation_gen: activation_gen.clone(),
        doc_gen: doc_gen.clone(),
        open_queue: open_queue.clone(),
        overview_open: overview_open.clone(),
        menu: menu.clone(),
        scroll_ref: scroll_ref.clone(),
        // 只给弱引用：强的那个在 `live`（见上面那段注释），这里拿 `Rc` 就是自引用环。
        live: Rc::downgrade(&live),
    };

    // 把"当下这一帧的 handles"写进自引用格子里，供只注册一次的回调取用。
    // 必须在 effect 注册**之前**——effect 要等这一帧渲染完才跑，那时这里已经写好了。
    *live.borrow_mut() = Some(handles.clone());

    // 渲染用的标签列表：按 `tabs_gen` 这个版本号 memo 一份 `Rc`。
    //
    // ⚠️ 必须**读一下版本号**（这里是 memo 的依赖）：列表本身在 `tabs_store` 那个
    // 活格子里，`set_tabs` 只推版本号 —— 这次渲染要是跟版本号没有依赖关系，
    // 标签栏就可能画不出来。memo 返回的 `Rc` 让每次渲染都只是指针克隆，
    // 不会去深拷贝 `DocTab` 里那几十张缩略图。
    let tabs = {
        let handles = handles.clone();
        use_memo(*tabs_gen, move |_| tabs_now(&handles))
    };

    // ---- 布局表（随页面尺寸与缩放重算）
    let layout = {
        let sizes = (*page_sizes).clone();
        let scale_value = *scale;
        use_memo((sizes, scale_value), |(sizes, scale)| {
            Layout::new(sizes, *scale)
        })
    };

    // ---- 启动：等待桥接层就绪，载入历史状态
    {
        let handles = handles.clone();
        use_effect_with((), move |_| {
            let handles = handles.clone();
            spawn_local(async move {
                if let Err(err) = pdf::ready().await {
                    handles.status.set(Some(err));
                    return;
                }
                // 读回上次的最近列表与阅读进度。
                //
                // ⚠️ 这里必须**分清三件事**：读成功 / 读得出来但解析不了 / 根本读不出来。
                // 早期把后两种都当成"没有数据"（`if let Ok(raw)` + `if let Ok(state)`
                // 两层静默跳过），于是内存里是空列表，紧接着的存盘就把用户磁盘上的
                // 最近列表 / 收藏 / 阅读进度整个覆盖掉 —— 而 `PersistedState` 每个字段
                // 都是 `#[serde(default)]`，解析失败连个响都没有。
                // `tests/e2e/e2e34` 钉的就是它。
                //
                // 现在：读不出来 / 解析不了就**先留档再继续**（`quarantine_app_state`
                // 把旧文件改名保住），留档成功才放开落盘。见 `Handles::state_ok`。
                let mut loaded = false;
                match pdf::load_app_state().await {
                    Ok(raw) => match serde_json::from_str::<PersistedState>(&raw) {
                        Ok(state) => {
                            // 先把历史灌进 store，再推快照 —— 下面 `take_pending_open`
                            // 那条路会去改最近列表，而它拿到的 `handles` 正是第 0 帧那个，
                            // 只有 store 能让它看见这里刚读回来的内容。
                            *handles.recent_store.borrow_mut() = state.recent_files.clone();
                            handles.recent.set(state.recent_files);
                            if let Some(path) = state.last_path {
                                let target = RestoreTarget {
                                    path,
                                    page: state.last_page.unwrap_or(1),
                                    scale: state.scale.unwrap_or(1.25),
                                };
                                *handles.restore.borrow_mut() = Some(target.clone());
                                // 记下"上次存进去的就是这三个值" —— 首页上的动作
                                // （收藏 / 删除记录）落盘时要原样写回去，不能凭空造。
                                // 见 `Handles::session` / `persist_state`。
                                *handles.session.borrow_mut() = Some(target);
                            }
                            loaded = true;
                        }
                        Err(err) => web_sys::console::error_1(
                            &format!("阅读记录解析失败（原文件会先留档再重来）：{err}").into(),
                        ),
                    },
                    Err(err) => web_sys::console::error_1(
                        &format!("读取阅读记录失败（原文件会先留档再重来）：{err}").into(),
                    ),
                }
                if loaded {
                    *handles.state_ok.borrow_mut() = true;
                } else {
                    match pdf::quarantine_app_state().await {
                        // 旧文件已经改名保住了 —— 之后从干净状态开始写就没什么可丢的。
                        Ok(Some(name)) => {
                            *handles.state_ok.borrow_mut() = true;
                            handles.status.set(Some(format!(
                                "阅读记录读不出来，已把原文件留档为 {name}，本次从空白开始"
                            )));
                        }
                        // 本来就没有 state.json —— 正常的首次启动，不是错误。
                        Ok(None) => *handles.state_ok.borrow_mut() = true,
                        // 留档也失败：**这次会话一律不落盘**，免得把没能保住的那份盖掉。
                        Err(err) => {
                            web_sys::console::error_1(
                                &format!("留档阅读记录失败，本次会话不会写回：{err}").into(),
                            );
                            handles.status.set(Some(format!(
                                "阅读记录读不出来，且无法留档（本次改动不会保存）：{err}"
                            )));
                        }
                    }
                }
                // 查一次「Folia 是不是已经是系统默认的 PDF 阅读器」，决定首页那个
                // 「设为默认」按钮要不要出现。纯读取，不改系统设置。
                if let Ok(current) = pdf::default_pdf_handler().await {
                    handles.is_default_pdf.set(current);
                }
                // 双击 .pdf / 资源管理器「打开方式 → Folia」/ 把 .pdf 拖到 exe 上 ——
                // 冷启动时后端比前端先拿到路径（命令行参数），那时还没有监听器，
                // 所以它先存着，这里主动取一次。取到了就**直接打开它**，
                // 而不是只记成"待续读"。
                if let Some(pending) = pdf::take_pending_open().await {
                    // ⚠️ 用 `now()` 而不是手上这份 `handles` —— 它是第 0 帧的，
                    // 里面 `tabs` 还是空 `Vec`。见 `Handles::live` 的注释。
                    let target = handles.now();
                    open_path_flow(target, pending);
                }
            });
            || {}
        });
    }

    // ---- 注册拖拽打开
    //
    // 这个 effect 的依赖是 `()`：监听器只注册一次，闭包捕获的 `handles`
    // 永远停在**第 0 帧**。所以回调里必须用 `handles.now()` 取当下的那份 ——
    // 否则拖入第二份 PDF 时，`apply_document` 读到的 `tabs` 是空的，
    // 标签栏里原来那份文档会被静默吃掉（`/tmp/e2e25` 的 drop2 用例钉的就是这个）。
    {
        let handles = handles.clone();
        use_effect_with((), move |_| {
            let target = handles.clone();
            let callback = Closure::<dyn Fn(String)>::new(move |path: String| {
                let handles = target.now();
                open_path_flow(handles, path);
            });
            pdf::on_file_drop(callback.as_ref().unchecked_ref());
            callback.forget();
            || {}
        });
    }

    // ---- 主题变了：把"按主题烘焙"的缩略图整体作废
    //
    // 深色缩略图是**烘焙进位图**的（整页反相 + 图片按原色贴回，见 `bridge.js` 的
    // `composeDarkThumbnail`），一张位图只对一种主题成立 —— 主题一变就得重烘。
    //
    // ⚠️ 事件由 `index.html` 里那段**主题脚本自己**派发。在这里另挂一个
    // `MutationObserver` 盯 `html.theme-dark` 也能做，但那是**第二条**监听同一件事
    // 的路；主题的入口只能有一个（见 `MEMORY.md` §五）。
    //
    // ⚠️ 清空之后**不用**挨个催渲染：
    //   · 页码缩略图（侧栏 / 总览）是按需请求的 —— store 里没有，组件下一次渲染
    //     就会再要一次（`need_thumb`）；
    //   · 书架那边不同，它那条 effect 的依赖是"列表里有哪几份"，所以要靠
    //     `theme_gen` 把它唤起来（见那条 effect 的依赖）。
    {
        let handles = handles.clone();
        let thumbs = thumbs.clone();
        let theme_gen = theme_gen.clone();
        let theme_counter = theme_counter.clone();
        use_effect_with((), move |_| {
            let target = handles.clone();
            let shelf = thumbs.clone();
            let gen = theme_gen.clone();
            let counter = theme_counter.clone();
            let callback = Closure::<dyn Fn()>::new(move || {
                // 代次取自**活的**计数器，不是闭包捕获的那个句柄 —— 这个监听器
                // 只注册一次，句柄永远停在第 0 帧，`set(*gen + 1)` 会连着算出同一个数。
                let next = {
                    let mut value = counter.borrow_mut();
                    *value += 1;
                    *value
                };
                let live = target.now();
                live.page_thumbs_store.borrow_mut().clear();
                bump_thumbs(&live);
                shelf.set(HashMap::new());
                gen.set(next);
            });
            if let Some(window) = web_sys::window() {
                let _ = window.add_event_listener_with_callback(
                    "folia-theme",
                    callback.as_ref().unchecked_ref(),
                );
            }
            callback.forget();
            || {}
        });
    }

    // ---- 吐司（保存 / 复制 / 撤回…的反馈条）2 秒后自动收起
    //
    // 依赖是 `(*status).clone()`：换一条消息会重新计时（所以连着保存两次，
    // 第二条仍然能完整显示 2 秒），清空则把定时器丢掉。
    // 不用 `gloo_timers` —— 项目里没引这个依赖，`set_timeout` 就够了。
    {
        let status = status.clone();
        use_effect_with((*status).clone(), move |current| {
            let handle = current.as_ref().and_then(|_| {
                let status = status.clone();
                let callback = Closure::once_into_js(move || status.set(None));
                web_sys::window().and_then(|window| {
                    window
                        .set_timeout_with_callback_and_timeout_and_arguments_0(
                            callback.unchecked_ref(),
                            TOAST_MS,
                        )
                        .ok()
                })
            });
            // 重新计时 / 卸载时要把上一个定时器清掉，否则旧的那个会提前把新消息收掉。
            move || {
                if let Some(id) = handle {
                    if let Some(window) = web_sys::window() {
                        window.clear_timeout_with_handle(id);
                    }
                }
            }
        });
    }

    // ---- 选中文字的底色跟随「当前所选批注颜色」
    //
    // `::selection` 原来写死品牌紫 `rgba(108,92,231,.28)` ——
    // 于是选一段文字准备加批注时，盖在字上的那层是淡紫的，
    // 跟紧接着生成的高亮色完全不是一回事，看着像「批注的颜色错了」。
    //
    // 这里只把**用哪个色**写到根元素的 `--hl-sel` 上（值形如 `var(--hl-blue)`），
    // 具体 rgba 仍由 styles.css 的 `--hl-<color>` 定义 ——
    // 和高亮矩形共用一份，两处不会各自漂。
    {
        let color = (*highlight_color).clone();
        use_effect_with(color, move |color| {
            if let Some(root) = web_sys::window()
                .and_then(|window| window.document())
                .and_then(|document| document.document_element())
                .and_then(|element| element.dyn_into::<web_sys::HtmlElement>().ok())
            {
                let _ = root
                    .style()
                    .set_property("--hl-sel", &format!("var(--hl-{color})"));
            }
            || {}
        });
    }

    // ---- 缩放会让页面重排、文本层整层重绘，选区随之失效：把浮层收起来。
    //
    // 不做这件事的话 `has_selection` 会一直停在 `true` —— 工具栏的「注释」按钮
    // 看着能点、浮层也还飘在旧位置上，但底下已经没东西可选了。
    {
        let has_selection = has_selection.clone();
        let sel_box = sel_box.clone();
        let sel_bar = sel_bar.clone();
        use_effect_with(*scale, move |_| {
            if *has_selection || sel_bar.is_some() {
                has_selection.set(false);
                sel_bar.set(None);
            }
            *sel_box.borrow_mut() = None;
            || {}
        });
    }

    // ---- 键盘快捷键
    //
    // ⚠️ 这个 effect 的依赖列表**不是**空的，是有原因的：
    // 它只注册一次的话，闭包捕获的 `UseMemo`（`layout`）是**注册那一帧的快照**，
    // 而这里第一帧是"文档还没载入"：`meta` 是 `None`、`layout` 是空的（`len() == 0`）。
    // 于是 `is_typing || meta.is_none()` 直接 return、`total` 又恒等于 1 ——
    // **方向键 / PageUp / PageDown / 空格 / Home / End 全都按不动**，
    // Esc 也退不回书架。把这几项放进依赖里，状态一变就重新注册，
    // 闭包拿到的 `layout` 就是当下这一帧的了。
    //
    // ⚠️ 但**光靠依赖不够** —— `handles` 必须走 `now()`，理由见下面闭包里的注释。
    // （`layout` 只能靠依赖：它是 `use_memo` 的产物，不在 `Handles` 里。）
    {
        let handles = handles.clone();
        let layout = layout.clone();
        let scroll_ref = scroll_ref.clone();
        let search_ref = search_ref.clone();
        let search_focus = search_focus.clone();
        let sidebar_open = sidebar_open.clone();
        let sidebar_tab = sidebar_tab.clone();
        let deps = (
            (*meta).is_some(),
            *scale,
            *current_page,
            (*layout).clone(),
            *overview_open,
            (*menu).clone(),
        );
        use_effect_with(deps, move |_| {
            let document = web_sys::window().unwrap().document().unwrap();
            let closure = Closure::<dyn Fn(KeyboardEvent)>::new(move |event: KeyboardEvent| {
                // ⚠️ **必须取"当下这一帧"的 handles，不能用闭包捕获的那个。**
                //
                // 这个监听器是按依赖重注册的，而依赖里的 `layout` 是
                // `use_memo((page_sizes, scale))` 的产物 —— 对**页面尺寸与页数都相同**
                // 的两份 PDF，`layout` 完全相等，于是打开第二份时依赖元组一字不变、
                // effect **不会重注册**，捕获的 `handles` 就停在"只开着第一份"那一帧。
                //
                // 而 `UseStateHandle` 的 deref 是**快照**（Yew 文档的 Caution 段：
                // handle 只反映它被返回时的值；`UseReducerHandle` 里存的是 `Rc<T>`，
                // `set` 是换一个新 `Rc` 进去，老 handle 永远看不到）。
                // 于是下面那句 `go_library(&handles)` 会拿**旧帧的 `handles.tabs`**
                // 当基准改完再 `set` 回去 —— 第二个标签被静默抹掉。
                // 复现：打开两份规格相同的 PDF，按 Esc 回书架，标签栏只剩第一个。
                // `tests/e2e/e2e33` 钉的就是它（对照组点 home 按钮则正常，
                // 因为那条走的是每帧新建的回调）。
                //
                // `now()` 读的是渲染时写进 `live` 的"最新那一帧的 handles"，
                // 和拖拽 / `folia://open-path` 那两条一次性监听器用的是同一个机制。
                let handles = handles.now();

                if event.meta_key() || event.ctrl_key() {
                    match event.key().as_str() {
                        "f" | "F" => {
                            event.prevent_default();
                            // 搜索框现在长在侧栏的「搜索结果」面板里：面板没收起 /
                            // 没切到这个页签时它根本没挂载，光 focus 是空的。
                            sidebar_open.set(true);
                            sidebar_tab.set(SidebarTab::Search);
                            *search_focus.borrow_mut() = true;
                            // 面板本来就在「搜索结果」上时上面几个 `set` 不会引起重渲染，
                            // 于是那个 effect 也不会跑 —— 这里补一次同步聚焦兜底。
                            if let Some(input) = search_ref.cast::<web_sys::HtmlInputElement>() {
                                let _ = input.focus();
                            }
                        }
                        "=" | "+" => {
                            event.prevent_default();
                            handles.scale.set(next_zoom(*handles.scale, 1.0));
                        }
                        "-" => {
                            event.prevent_default();
                            handles.scale.set(next_zoom(*handles.scale, -1.0));
                        }
                        "0" => {
                            event.prevent_default();
                            handles.scale.set(1.0);
                        }
                        _ => {}
                    }
                    return;
                }

                // Esc：卡片右键菜单开着就先关它 —— 它盖在最上面，
                // 而且它出现在**首页**（那时 `meta` 是 `None`），
                // 所以这一条必须放在下面那个 `meta.is_some()` 闸门**之前**。
                if event.key() == "Escape" && handles.menu.is_some() {
                    event.prevent_default();
                    handles.menu.set(None);
                    return;
                }

                // Esc：总览开着就先关总览；在输入框里只退出输入（避免误触返回书架）；
                // 其余情况才返回书架。
                if event.key() == "Escape" && handles.meta.is_some() {
                    // 总览是盖在最上面的一层，"最上面那层先关"才符合直觉 ——
                    // 不然按一下 Esc 整份文档就退回书架了，用户只是想退出总览。
                    if *handles.overview_open {
                        event.prevent_default();
                        handles.overview_open.set(false);
                        return;
                    }
                    if is_typing(&event) {
                        if let Some(target) = event
                            .target()
                            .and_then(|node| node.dyn_into::<web_sys::HtmlElement>().ok())
                        {
                            let _ = target.blur();
                        }
                    } else {
                        event.prevent_default();
                        go_library(&handles);
                    }
                    return;
                }

                if is_typing(&event) || handles.meta.is_none() {
                    return;
                }
                let total = layout.len().max(1) as u32;
                let current = *handles.current_page;
                let target = match event.key().as_str() {
                    "ArrowRight" | "PageDown" | " " => Some((current + 1).min(total)),
                    "ArrowLeft" | "PageUp" => Some(current.saturating_sub(1).max(1)),
                    "Home" => Some(1),
                    "End" => Some(total),
                    _ => None,
                };
                if let Some(page) = target {
                    event.prevent_default();
                    handles.current_page.set(page);
                    scroll_to_page(&scroll_ref, &layout, page);
                }
            });
            let _ = document
                .add_event_listener_with_callback("keydown", closure.as_ref().unchecked_ref());
            move || {
                let _ = document.remove_event_listener_with_callback(
                    "keydown",
                    closure.as_ref().unchecked_ref(),
                );
                drop(closure);
            }
        });
    }

    // ---- 滚动：更新可视页范围与当前页码
    let on_scroll = {
        let scroll_ref = scroll_ref.clone();
        let layout = layout.clone();
        let visible = visible.clone();
        let current_page = current_page.clone();
        let sel_box = sel_box.clone();
        let sel_bar = sel_bar.clone();
        Callback::from(move |_: Event| {
            let Some(element) = scroll_ref.cast::<web_sys::Element>() else {
                return;
            };
            let top = element.scroll_top() as f64;
            let height = element.client_height() as f64;
            let range = layout.visible_range(top, height, 600.0);
            if *visible != range {
                visible.set(range);
            }
            let page = layout.page_at(top + height * 0.35);
            if *current_page != page {
                current_page.set(page);
            }

            // 浮层工具栏本身是内容坐标、位置不动，但「贴上方还是贴下方」取决于
            // 选区此刻在**屏幕**上的位置 —— 滚到顶部时上方就没地方了，得翻到下面去。
            // 几何没变，拿存下来的那份重算即可，不用再问一次桥接层。
            if let (Some(box_), Some(view)) = (*sel_box.borrow(), stage_view(&scroll_ref)) {
                let next = resolve_sel_bar(box_, view);
                if *sel_bar != Some(next) {
                    sel_bar.set(Some(next));
                }
            }
        })
    };

    // ---- 检索：输入变化后去抖执行（用代次避免旧结果覆盖新结果）
    {
        let hits = hits.clone();
        let active_hit = active_hit.clone();
        let search_gen = search_gen.clone();
        let meta_id = meta.as_ref().map(|item| item.id.clone());
        use_effect_with(((*query).clone(), meta_id), move |(query, _)| {
            let query = query.clone();
            // ⚠️ **无条件**推进代次，而不是只在"新查询非空"时推进。
            //
            // 这个 effect 只要跑了，就意味着"用户换了查询词"或"换了文档"——
            // 两种情况下那个还在 `sleep(200)` 里的旧检索任务都必须作废。
            // 早期版本把 `+= 1` 放进了 `if !query.trim().is_empty()` 里面，
            // 于是**清空查询**、以及**切到一个查询为空的标签**这两条路径
            // 代次原封不动，旧任务过了两道检查后照样 `hits.set(...)` ——
            // 表现为：清掉搜索框，命中结果又自己冒出来；或者切到另一份文档，
            // 上面贴的是**上一份文档**的命中（点进去跳的是错页）。
            let generation = {
                let mut current = search_gen.borrow_mut();
                *current += 1;
                *current
            };
            hits.set(Vec::new());
            active_hit.set(None);
            if !query.trim().is_empty() {
                let hits = hits.clone();
                let active_hit = active_hit.clone();
                let search_gen = search_gen.clone();
                spawn_local(async move {
                    pdf::sleep(200).await;
                    if *search_gen.borrow() != generation {
                        return;
                    }
                    let raw = pdf::search(&query).await.unwrap_or_else(|_| "[]".into());
                    if *search_gen.borrow() != generation {
                        return;
                    }
                    let parsed: Vec<SearchHit> = serde_json::from_str(&raw).unwrap_or_default();
                    hits.set(parsed);
                    active_hit.set(None);
                });
            }
            || {}
        });
    }

    // ---- 文档就绪后：新文档先适宽，再恢复滚动位置
    //
    // 两件事必须绑在同一个 effect 里、且**适宽在前**：
    // 适宽改的是 `scale`，而 `layout` 是 `scale` 的派生值 —— 先滚动再适宽的话，
    // 用的还是旧缩放的偏移表，落点会偏。
    {
        let scroll_ref = scroll_ref.clone();
        let layout = layout.clone();
        let page_sizes = page_sizes.clone();
        let pending_scroll = pending_scroll.clone();
        let pending_fit = pending_fit.clone();
        let scale = scale.clone();
        let meta_id = meta.as_ref().map(|item| item.id.clone());
        use_effect_with((meta_id, page_sizes.len()), move |(id, count)| {
            if id.is_some() && *count > 0 {
                // 只有「全新打开」的文档才自动适宽；续读、切标签都带着自己的缩放。
                if *pending_fit.borrow() {
                    *pending_fit.borrow_mut() = false;
                    if let Some(width) = page_sizes.first().map(|size| size.width) {
                        if let Some(value) = fit_scale(&scroll_ref, width) {
                            scale.set(value);
                        }
                    }
                }
                if let Some(page) = *pending_scroll {
                    scroll_to_page(&scroll_ref, &layout, page);
                    pending_scroll.set(None);
                }
            }
            || {}
        });
    }

    // ---- 搜索面板就位后，把焦点送进搜索框
    //
    // 只有「面板真的切到了搜索结果」这一帧之后 DOM 里才有那个 input，
    // 所以聚焦必须放在渲染后的 effect 里，不能在触发切页签的同一个回调里做。
    {
        let search_ref = search_ref.clone();
        let search_focus = search_focus.clone();
        use_effect_with((*sidebar_tab, *sidebar_open), move |(tab, open)| {
            if *open && *tab == SidebarTab::Search && *search_focus.borrow() {
                *search_focus.borrow_mut() = false;
                if let Some(input) = search_ref.cast::<web_sys::HtmlInputElement>() {
                    let _ = input.focus();
                }
            }
            || {}
        });
    }

    // ---- 持久化批注
    //
    // ⚠️ **只有 `annotations_ok` 为真才写。**
    // 读失败时 `annotations` 会被置成一个空列表，而这个 effect 的依赖里就有它 ——
    // 也就是说**同一帧**它就会被触发。少了这个判断，"读不出来"会立刻升级成
    // "把用户磁盘上的批注覆盖成 []"。这是整个项目里最危险的一条路径：
    // 它删的是用户自己攒出来的数据，而且没有任何提示。
    {
        let meta_id = meta.as_ref().map(|item| item.id.clone());
        let annotations_ok = annotations_ok.clone();
        use_effect_with(((*annotations).clone(), meta_id), move |(items, id)| {
            if let Some(id) = id {
                if *annotations_ok.borrow() {
                    if let Ok(json) = serde_json::to_string(items) {
                        let id = id.clone();
                        spawn_local(async move {
                            let _ = pdf::save_annotations(&id, &json).await;
                        });
                    }
                }
            }
            || {}
        });
    }

    // ---- 持久化应用状态（最近文件 / 阅读进度）
    {
        let meta_path = meta.as_ref().map(|item| item.path.clone());
        let recent = recent.clone();
        let recent_store = recent_store.clone();
        let session = session.clone();
        let state_ok = state_ok.clone();
        use_effect_with(
            ((*recent).clone(), meta_path, *current_page, *scale),
            move |(files, path, page, scale_value)| {
                if let Some(path) = path {
                    // 把当前页码同步进书架条目，首页的进度条才是实时的。
                    // 只在真的变化时才 set，否则会不断自我触发。
                    let mut list = files.clone();
                    let mut changed = false;
                    for item in list.iter_mut() {
                        if &item.path == path && item.last_page != *page {
                            item.last_page = *page;
                            changed = true;
                        }
                    }
                    if changed {
                        // store 和 state 一起写，否则 store 会落后于渲染，
                        // 后面任何走 `write_recent` 的修改都会把这次页码更新吞掉。
                        *recent_store.borrow_mut() = list.clone();
                        recent.set(list.clone());
                    }

                    // 同步「上次落盘的三个值」。**必须在存盘的同时更新** ——
                    // 不然之后在首页收藏一下，`write_recent` 里的 `persist_state`
                    // 会把**启动时**那份 `last_page` 又写回去，阅读进度倒退。
                    *session.borrow_mut() = Some(RestoreTarget {
                        path: path.clone(),
                        page: *page,
                        scale: *scale_value,
                    });

                    let state = PersistedState {
                        recent_files: list,
                        last_path: Some(path.clone()),
                        last_page: Some(*page),
                        scale: Some(*scale_value),
                    };
                    // ⚠️ 磁盘上那份读不出来、又没能留档时**一个字都不写** ——
                    // 否则这个空列表会把没能保住的用户数据盖掉。见 `Handles::state_ok`。
                    // （内存里的 `recent_store` / `session` 照常更新，界面不受影响。）
                    if *state_ok.borrow() {
                        if let Ok(json) = serde_json::to_string(&state) {
                            spawn_local(async move {
                                let _ = pdf::save_app_state(&json).await;
                            });
                        }
                    }
                }
                || {}
            },
        );
    }

    // ---- 书架卡片缩略图：为每份最近文件渲染首页预览
    //
    // 串行生成 + 逐个 set，避免同时解码一堆 PDF 把主线程堵死。
    //
    // ⚠️ **`recent` 每变一次就起一条新任务，而旧任务不会自己停。** 旧任务手里
    // 那份 `known` 是它启动那一刻的快照，里面可能有**已经被上限挤掉**的 id ——
    // 它接着 `thumbs.set(known.clone())` 就把那些条目的 data URL（约 20–30 KB
    // 一张）又塞回 map 里，而这时"丢掉不在列表里的条目"那句 `retain` 早就跑完
    // 了：要等**下一次** `recent` 变化才会再清一遍（自愈，但中间一直在漏，
    // 而且旧任务还在白渲染已经删掉的文件的首页）。
    // 所以每条任务认一个代次，只有最新那条干活、写结果（`tests/e2e/e2e39`）。
    //
    // ⚠️ **这里原来还有一把 `thumbs_busy` 的"正在渲染"锁，代次化之后必须去掉。**
    // 那把锁是**跨代次**的：旧任务在 `await` 里握着某个 id，新任务一看"有人在
    // 渲染"就跳过 —— 而旧任务回来时发现自己过期、不写结果，于是那一份**永远
    // 没有缩略图**。有了代次，"新一代是唯一干活的人"本身就替代了那把锁；
    // 代价只是"旧任务在飞的那一张会被新一代再渲染一遍"，多一次渲染而已。
    //
    // 启动阶段 `recent` 会连着变好几次（`load_app_state` 一次、续读进度回填
    // 一次、打开文档一次），所以"两条任务重叠"是常态而不是边界情况。
    //
    // ⚠️ **依赖是「列表里有哪几份」，不是整个列表。**
    // 用 `(*recent).clone()` 当依赖的话，上面那几次变化**每一次都会起一条新任务**，
    // 而它们会**同时**去渲染同一份文件的首页 —— 同一份 PDF 被并发载入两三次。
    // 真 pdf.js 渲染本来就吃虚拟时钟预算，再加一倍并发就开始"时过时不过"
    // （`tests/e2e/e2e18` 就是这么变得不稳的）。而这条 effect 真正关心的只有
    // "有哪几份"：id 是路径哈希出来的，id 不变就意味着路径不变，正在跑的那条
    // 任务照样会把它渲染完。
    {
        let thumbs = thumbs.clone();
        let job = shelf_gen.clone();
        // 任务要的是完整条目（带 path），所以另外捕获一份当下这一帧的列表 ——
        // id 不变时 `use_effect_with` 会留着旧的那条任务（连同它捕获的旧列表），
        // 而两者的路径是同一批。
        let ids: Vec<String> = recent.iter().map(|item| item.id.clone()).collect();
        let files_now = (*recent).clone();
        // ⚠️ 依赖里带上 `theme_gen`：主题一变，缩略图整体作废、要重烘。
        // 光清空 `thumbs` 是唤不动这条 effect 的 —— 它的依赖是"列表里有哪几份"，
        // 而主题切换时那份列表一个字都没变。
        use_effect_with((ids, *theme_gen), move |_| {
            let files = files_now.clone();
            let thumbs = thumbs.clone();
            let job = job.clone();
            // 代次在 effect 体里**同步**推 —— 不能放进任务里：任务是排队跑的，
            // 谁先执行由调度决定，而"哪一次 run 最新"由这里决定，不能混。
            let my_job = {
                let mut current = job.borrow_mut();
                *current += 1;
                *current
            };
            spawn_local(async move {
                // 有人接班了就整条退场：新任务会用新的列表重做一遍 retain，
                // 旧任务继续跑只会把过期条目写回去。
                let superseded = || *job.borrow() != my_job;
                // 用局部变量记已生成的，不要回头读 state handle（set 之后句柄里还是旧值）。
                let mut known = (*thumbs).clone();
                // ⚠️ 先丢掉「已经不在最近列表里」的那些条目。
                //
                // 这份 map 是按 **id** 存缩略图 data URL 的，而最近列表有 10 条上限：
                // 被挤掉的条目如果只删列表、不删这里，每挤掉一份就**永久**留下一张
                // ~320px 的 JPEG data URL（约 20–30 KB）。`forget_recent` 那条路会自己
                // `map.remove(id)`，但 **上限淘汰那条路不会** ——
                // 放在这个「跟着 `recent` 重跑」的 effect 里，三条路一次覆盖。
                //
                // 注意这里**不释放** pdf.js 侧那份 `PDFDocumentProxy`：那个由
                // `write_recent` 按「前后差集」负责（见那里）。两边管的不是一层东西。
                let before = known.len();
                known.retain(|id, _| files.iter().any(|item| &item.id == id));
                if known.len() != before && !superseded() {
                    thumbs.set(known.clone());
                }
                for item in files {
                    // 每一轮都重新问一次：接班的任务已经拿新列表跑过了，
                    // 这里再往下走只会把过期条目写回 map。
                    if superseded() {
                        return;
                    }
                    if known.contains_key(&item.id) {
                        continue;
                    }
                    let url = pdf::thumbnail(&item.path, 1, 320.0)
                        .await
                        .unwrap_or_default();
                    if url.is_empty() {
                        continue;
                    }
                    known.insert(item.id.clone(), url);
                    if superseded() {
                        return;
                    }
                    thumbs.set(known.clone());
                    pdf::sleep(16).await;
                }
            });
            || {}
        });
    }

    // 换文档时清空侧栏缩略图这件事**不能**做成「监听 meta.id 的 effect」：
    // 切标签时 `apply_tab_to_live` 会先把缓存灌进 state，effect 紧接着又把它清掉。
    // 所以清空统一由 `clear_live` / `apply_tab_to_live` 显式负责。

    // ---------------------------------------------------------------- 交互回调

    let open_file = {
        let handles = handles.clone();
        Callback::from(move |_: MouseEvent| open_dialog_flow(handles.clone()))
    };

    let open_recent = {
        let handles = handles.clone();
        Callback::from(move |path: String| open_path_flow(handles.clone(), path))
    };

    let remove_recent = {
        let handles = handles.clone();
        Callback::from(move |id: String| forget_recent(&handles, &id))
    };

    // 收藏 / 取消收藏（书架的「收藏」分页靠它过滤）。
    let toggle_favorite = {
        let handles_for_favorite = handles.clone();
        Callback::from(move |id: String| {
            // ⚠️ 走 `write_recent`，不要自己 `recent_store.borrow_mut()` 再 `recent.set()`。
            // 一是落盘（首页收藏也得存得住，见 `persist_state`），
            // 二是这个回调的 `handles` 可能是旧帧的，只有 store 跨帧共享。
            write_recent(&handles_for_favorite, move |list| {
                for item in list.iter_mut() {
                    if item.id == id {
                        item.favorite = !item.favorite;
                    }
                }
            });
        })
    };

    // ---------------------------------------------------------------- 卡片右键菜单
    //
    // 四个动作：收藏 / 取消收藏、在资源管理器中显示、删除记录、删除记录和文件。
    //
    // ⚠️ 菜单项一律用 `onmousedown` 而不是 `onclick`：菜单开着时 document 上挂着
    // 一个"点哪儿都关菜单"的 mousedown 监听，`mousedown` 早于 `click` ——
    // 用 `onclick` 的话，等它轮到的时候菜单已经重渲染没了，动作根本不会触发。
    // （和「选中态浮层上的按钮必须 mousedown」是同一个道理。）

    // 打开卡片菜单：把卡片快照 + 光标位置存进去。
    let open_card_menu = {
        let menu = menu.clone();
        Callback::from(move |(item, x, y): (RecentFile, f64, f64)| {
            // 夹进可视区 —— 右下角的卡片右击时，不夹的话"删除记录和文件"
            // 正好落在屏幕外面，等于点不到。
            let (view_w, view_h) = viewport_size();
            let (x, y) = clamp_menu_pos(x, y, view_w, view_h, CARD_MENU_ITEMS);
            menu.set(Some(CtxMenu::Card { item, x, y }));
        })
    };

    // 打开批注菜单：把批注 id + 样式 + 光标位置存进去。
    let open_mark_menu = {
        let menu = menu.clone();
        Callback::from(move |(id, kind, x, y): (String, MarkKind, f64, f64)| {
            let (view_w, view_h) = viewport_size();
            let (x, y) = clamp_menu_pos(x, y, view_w, view_h, MARK_MENU_ITEMS);
            menu.set(Some(CtxMenu::Mark { id, kind, x, y }));
        })
    };

    let close_menu = {
        let menu = menu.clone();
        Callback::from(move |_: ()| menu.set(None))
    };

    // 「收藏 / 取消收藏」：直接复用书架那颗星标的逻辑，改完顺手关菜单。
    let menu_toggle_favorite = {
        let toggle_favorite = toggle_favorite.clone();
        let close_menu = close_menu.clone();
        Callback::from(move |id: String| {
            toggle_favorite.emit(id);
            close_menu.emit(());
        })
    };

    // 「在资源管理器中显示」：交给后端（`explorer /select` 那套，走 opener 插件的 Rust API）。
    let menu_reveal = {
        let status = status.clone();
        let close_menu = close_menu.clone();
        Callback::from(move |path: String| {
            close_menu.emit(());
            let status = status.clone();
            spawn_local(async move {
                if let Err(err) = pdf::reveal_in_explorer(&path).await {
                    status.set(Some(err));
                }
            });
        })
    };

    // 「删除记录」：只摘掉这条记录，文件不动。
    let menu_remove_record = {
        let remove_recent = remove_recent.clone();
        let close_menu = close_menu.clone();
        Callback::from(move |id: String| {
            remove_recent.emit(id);
            close_menu.emit(());
        })
    };

    // 「删除记录和文件」：先弹确认框 —— 这一步不可逆，不能点一下就把用户的文件弄走。
    let menu_ask_delete_file = {
        let pending_delete = pending_delete.clone();
        let close_menu = close_menu.clone();
        Callback::from(move |item: RecentFile| {
            close_menu.emit(());
            pending_delete.set(Some(item));
        })
    };

    let cancel_delete_file = {
        let pending_delete = pending_delete.clone();
        Callback::from(move |_: ()| pending_delete.set(None))
    };

    // 确认删除：关掉标签（如果它正开着）→ 移进回收站 → 摘掉记录。
    let confirm_delete_file = {
        let handles = handles.clone();
        let pending_delete = pending_delete.clone();
        Callback::from(move |item: RecentFile| {
            let handles = handles.clone();
            let pending_delete = pending_delete.clone();
            // 先把确认框收掉：动作是异步的，框一直杵着会让人以为没点上。
            pending_delete.set(None);
            spawn_local(async move {
                // 这份文档要是正开在某个标签里，先把它关掉 ——
                // 文件马上就不在了，留着一个指向空文件的标签，用户再点一下只会报错。
                if let Some(idx) = tabs_now(&handles)
                    .iter()
                    .position(|tab| tab.meta.id == item.id)
                {
                    close_tab(&handles, idx);
                }
                match pdf::trash_file(&item.path).await {
                    Ok(()) => {
                        forget_recent(&handles, &item.id);
                        handles
                            .status
                            .set(Some(format!("已把「{}」移到回收站", item.name)));
                    }
                    // 失败就**别动记录**：文件还在，记录没了才是最糟的状态。
                    Err(err) => handles.status.set(Some(err)),
                }
            });
        })
    };

    // 菜单开着的时候，在页面任何地方按下鼠标都该把它关掉。
    //
    // 用 document 上的 `mousedown` 而不是"铺一层透明遮罩"：遮罩会把**右键**也吃掉，
    // 于是"在一个卡片上右击、再直接右击另一个卡片"要按两次才换得过去。
    // 挂在 document 上就不拦事件，右键照样能落到另一张卡片上（它会自己重开菜单）。
    //
    // 依赖是 `(*menu).clone()` —— 开着才挂、关了立刻摘掉，不做常驻监听。
    {
        let menu = menu.clone();
        use_effect_with((*menu).clone(), move |current| {
            if current.is_none() {
                return Box::new(|| {}) as Box<dyn FnOnce()>;
            }
            let Some(document) = web_sys::window().and_then(|window| window.document()) else {
                return Box::new(|| {}) as Box<dyn FnOnce()>;
            };
            let closure = Closure::<dyn Fn(MouseEvent)>::new(move |_: MouseEvent| {
                menu.set(None);
            });
            let _ = document
                .add_event_listener_with_callback("mousedown", closure.as_ref().unchecked_ref());
            let document_for_cleanup = document.clone();
            Box::new(move || {
                let _ = document_for_cleanup.remove_event_listener_with_callback(
                    "mousedown",
                    closure.as_ref().unchecked_ref(),
                );
            }) as Box<dyn FnOnce()>
        });
    }

    // 把 WebKit 的**默认右键菜单**整个关掉。
    //
    // 卡片和批注各有自己的菜单（见下面 `CtxMenu`），但默认菜单还会在别的地方冒出来 ——
    // 正文空白处、侧栏、书架背景 —— 弹的是「重新载入 / 检查元素」那一套浏览器菜单，
    // 在桌面应用里很出戏。所以在 document 上挂一个兜底。
    //
    // 挂在**捕获**阶段有两个理由：
    // ① `prevent_default` 不阻断传播，元素自己的 `oncontextmenu` 照常执行
    //    （该开菜单的照样开），只是"默认行为"被提前判死；
    // ② 捕获跑在冒泡之前，不会被某个 `stop_propagation`（比如卡片那个）绕过去。
    //
    // ⚠️ 唯一的例外是**可编辑区域**（输入框 / 文本域 / contenteditable）：
    // 那里用户需要系统菜单里的「粘贴」—— 批注的备注框尤其明显，
    // 写笔记时粘贴是常用动作，拦掉就等于顺手删了一个功能。
    {
        use_effect_with((), move |_| {
            let Some(document) = web_sys::window().and_then(|window| window.document()) else {
                return Box::new(|| {}) as Box<dyn FnOnce()>;
            };
            let closure = Closure::<dyn Fn(MouseEvent)>::new(move |event: MouseEvent| {
                if !right_click_is_editable(&event) {
                    event.prevent_default();
                }
            });
            let _ = document.add_event_listener_with_callback_and_bool(
                "contextmenu",
                closure.as_ref().unchecked_ref(),
                true,
            );
            let document_for_cleanup = document.clone();
            Box::new(move || {
                let _ = document_for_cleanup.remove_event_listener_with_callback_and_bool(
                    "contextmenu",
                    closure.as_ref().unchecked_ref(),
                    true,
                );
            }) as Box<dyn FnOnce()>
        });
    }

    // 侧栏要某一页的缩略图。父级负责去重、并发上限与缓存。
    let need_thumb = {
        let handles = handles.clone();
        let store = page_thumbs_store.clone();
        let pages_busy = pages_busy.clone();
        let doc_gen = handles.doc_gen.clone();
        let path = meta.as_ref().map(|item| item.path.clone());
        Callback::from(move |page: u32| {
            let Some(path) = path.clone() else {
                return;
            };
            if page == 0
                || store.borrow().contains_key(&page)
                || pages_busy.borrow().contains(&page)
            {
                return;
            }
            // 记下"这一刻实时状态属于哪份文档"，回来时对照（见 `Handles::doc_gen`）。
            let generation = *doc_gen.borrow();
            pages_busy.borrow_mut().insert(page);
            let handles = handles.clone();
            let store = store.clone();
            let pages_busy = pages_busy.clone();
            let doc_gen = doc_gen.clone();
            spawn_local(async move {
                let url = pdf::thumbnail(&path, page, 170.0).await.unwrap_or_default();
                pages_busy.borrow_mut().remove(&page);
                if url.is_empty() {
                    return;
                }
                // 文档已经换了（切标签 / 打开新文档 / 回书架）→ 这张图属于**上一份**。
                // 这份 store 是按页码共享的、切标签时整体换过，写进去会顶掉
                // 新标签侧栏里同一页码的那张 —— 丢掉。
                if *doc_gen.borrow() != generation {
                    return;
                }
                // 只写共享 store，再推一格版本号触发渲染。
                //
                // ⚠️ 焦点必须取**活的**那一份（`handles.now()`）：这个任务可能
                // 晚于好几帧才回来，拿创建回调那一帧的页码会把淘汰算错，
                // 把用户正看着的那几页挤出去。
                //
                // ⚠️ 而且焦点是 [`thumb_focus`]（"用户正在看的地方"），
                // **不是** `current_page` —— 总览滚到远处时后者会让这一张
                // 刚插进去就被当成"最远"删掉，然后无限重试（`e2e37`）。
                let live = handles.now();
                let focus = thumb_focus(&live);
                let kept = put_thumb(&mut store.borrow_mut(), page, url, focus);
                // 没留下来 = store 跟插入前一模一样（插进去又删掉同一个 key）→
                // 推版本号只会让补齐 effect 白跑一轮、再请求一次、再被淘汰。
                // 这是防死循环的兜底（见 `put_thumb` 的注释）。
                if kept {
                    bump_thumbs(&live);
                }
            });
        })
    };

    let select_rail = {
        let sidebar_tab = sidebar_tab.clone();
        let sidebar_open = sidebar_open.clone();
        Callback::from(move |tab: SidebarTab| {
            // 再点一次当前面板就收起，符合图标轨的直觉。
            if *sidebar_open && *sidebar_tab == tab {
                sidebar_open.set(false);
            } else {
                sidebar_tab.set(tab);
                sidebar_open.set(true);
            }
        })
    };

    // 回到书架（标签栏左端的房子按钮、Esc 都是它）。
    // 只关掉 pdf.js 的文档并切回书架视图，**标签页保留**。
    let go_home = {
        let handles = handles.clone();
        Callback::from(move |_: MouseEvent| go_library(&handles))
    };

    let toggle_sidebar = {
        let sidebar_open = sidebar_open.clone();
        Callback::from(move |_: MouseEvent| sidebar_open.set(!*sidebar_open))
    };

    let on_query_input = {
        let query = query.clone();
        let sidebar_tab = sidebar_tab.clone();
        let sidebar_open = sidebar_open.clone();
        Callback::from(move |event: InputEvent| {
            if let Some(target) = event
                .target()
                .and_then(|node| node.dyn_into::<web_sys::HtmlInputElement>().ok())
            {
                let value = target.value();
                // 一边打字一边把结果面板顶出来，省得再点一次图标轨。
                if !value.trim().is_empty() {
                    sidebar_tab.set(SidebarTab::Search);
                    sidebar_open.set(true);
                }
                query.set(value);
            }
        })
    };

    let clear_query = {
        let query = query.clone();
        Callback::from(move |_: ()| query.set(String::new()))
    };

    let goto_hit = {
        let hits = hits.clone();
        let active_hit = active_hit.clone();
        let layout = layout.clone();
        let scroll_ref = scroll_ref.clone();
        Callback::from(move |delta: i32| {
            if hits.is_empty() {
                return;
            }
            let len = hits.len() as i32;
            let next = match *active_hit {
                Some(index) => {
                    let candidate = index as i32 + delta;
                    if candidate < 0 {
                        len - 1
                    } else if candidate >= len {
                        0
                    } else {
                        candidate
                    }
                }
                None if delta < 0 => len - 1,
                None => 0,
            } as usize;
            active_hit.set(Some(next));
            let hit = hits[next].clone();
            scroll_to_hit(&scroll_ref, &layout, &hit);
        })
    };

    let prev_hit = {
        let goto_hit = goto_hit.clone();
        Callback::from(move |_: ()| goto_hit.emit(-1))
    };
    let next_hit = {
        let goto_hit = goto_hit.clone();
        Callback::from(move |_: ()| goto_hit.emit(1))
    };

    // 侧栏结果列表里点某一条：选中它并滚过去。
    let pick_hit = {
        let hits = hits.clone();
        let active_hit = active_hit.clone();
        let layout = layout.clone();
        let scroll_ref = scroll_ref.clone();
        Callback::from(move |index: usize| {
            let Some(hit) = hits.get(index).cloned() else {
                return;
            };
            active_hit.set(Some(index));
            scroll_to_hit(&scroll_ref, &layout, &hit);
        })
    };

    // ---- 四种「把选区标成某种样式」的回调
    //
    // 四者走的是同一条路（`mark_callback`），区别只有 `kind`。
    // 颜色统一取顶部工具栏当前所选的那个 —— 浮层里不再放色板。
    let mark_ctx = MarkCtx {
        annotations: annotations.clone(),
        annotations_ok: annotations_ok.clone(),
        history: history.clone(),
        color: highlight_color.clone(),
        status: status.clone(),
        sidebar_tab: sidebar_tab.clone(),
        sidebar_open: sidebar_open.clone(),
        has_selection: has_selection.clone(),
        sel_box: sel_box.clone(),
        sel_bar: sel_bar.clone(),
    };
    let mark_highlight = mark_callback(mark_ctx.clone(), MarkKind::Highlight);
    let mark_underline = mark_callback(mark_ctx.clone(), MarkKind::Underline);
    let mark_strike = mark_callback(mark_ctx.clone(), MarkKind::Strike);
    let mark_wavy = mark_callback(mark_ctx, MarkKind::Wavy);
    // 顶部工具栏排在源码更前面，会先把 `mark_highlight` move 走 —— 浮层留一份。
    let mark_highlight_bar = mark_highlight.clone();

    // 浮层上的四个样式按钮，和上面四个回调一一对应。
    // 写成一张表，以后加样式不必再抄一遍 `html!`。
    let mark_buttons: Html = [
        ("高亮", "marker", mark_highlight_bar),
        ("下划线", "underline", mark_underline),
        ("删划线", "strike", mark_strike),
        ("波浪线", "wavy", mark_wavy),
    ]
    .into_iter()
    .map(|(name, glyph, onclick)| {
        html! {
            <button class="sel-bar-btn" {onclick} title={name}>{ icon(glyph) }</button>
        }
    })
    .collect();

    // ---- 复制选中的文字
    let copy_selection = {
        let status = status.clone();
        let has_selection = has_selection.clone();
        let sel_box = sel_box.clone();
        let sel_bar = sel_bar.clone();
        Callback::from(move |_: MouseEvent| {
            let Some(raw) = pdf::selection_info() else {
                status.set(Some("请先在正文中选中要复制的文字".into()));
                return;
            };
            let Ok(info) = serde_json::from_str::<SelectionInfo>(&raw) else {
                return;
            };
            let copied = copy_text(&info.text);
            // 临时 textarea 已经把正文的选区顶掉了，状态跟着收 ——
            // 不收的话浮层会停在一段已经不存在的选区上。
            has_selection.set(false);
            *sel_box.borrow_mut() = None;
            sel_bar.set(None);
            status.set(Some(if copied {
                format!("已复制 {} 个字", info.text.chars().count())
            } else {
                "复制失败".into()
            }));
        })
    };

    // ---- 撤回：退回上一次批注改动
    let undo_click = {
        let history = history.clone();
        let annotations = annotations.clone();
        let annotations_ok = annotations_ok.clone();
        let status = status.clone();
        Callback::from(move |_: MouseEvent| match history.undo() {
            Some(previous) => {
                annotations.set(previous);
                // 撤回同样是用户动作 —— 撤回后的这份列表一样可信，要能落盘。
                *annotations_ok.borrow_mut() = true;
                status.set(Some("已撤回上一步批注改动".into()));
            }
            // 按钮空栈时是 disabled 的，正常点不到；留着兜底。
            None => status.set(Some("没有可撤回的批注改动了".into())),
        })
    };

    // ---- 保存：把批注立刻落盘
    //
    // 平时批注是**自动保存**的（见下面那个 effect），这个按钮不是唯一入口，
    // 它的价值是「立刻写一次并明确告诉你写完了」—— 关窗口前想确认一下的时候用。
    let save_now = {
        let annotations = annotations.clone();
        let status = status.clone();
        let doc_id = meta.as_ref().map(|item| item.id.clone());
        Callback::from(move |_: MouseEvent| {
            let Some(id) = doc_id.clone() else {
                return;
            };
            let items = (*annotations).clone();
            let status = status.clone();
            spawn_local(async move {
                let Ok(json) = serde_json::to_string(&items) else {
                    status.set(Some("保存失败：批注无法序列化".into()));
                    return;
                };
                match pdf::save_annotations(&id, &json).await {
                    Ok(()) => status.set(Some(format!("已保存 {} 条批注", items.len()))),
                    Err(err) => status.set(Some(format!("保存失败：{err}"))),
                }
            });
        })
    };

    // 浮层工具栏上的按钮都要在 `mousedown` 时阻止默认行为。
    //
    // 按下鼠标会把焦点挪走、顺手**收掉当前选区** —— 选区一没，
    // 紧接着那次 click 里的 `selection_info()` 就是空的：批注加不上，
    // 而且光点一下按钮浮层就会自己消失（`on_mouseup` 发现选区没了）。
    // 这是「选中态浮层」的标准做法，不是可有可无的讲究。
    let keep_selection = Callback::from(|event: MouseEvent| event.prevent_default());

    // 鼠标松开时重算「有没有选中文字」和浮层工具栏的落点。
    //
    // 挂在最外层容器上、而不是正文区上：点侧栏、点标签栏、点工具栏这些
    // 「让选区消失」的操作也要能立刻把浮层收掉。挂在正文区上时，
    // 点在别处的那一下没人处理，工具栏会一直飘在那儿。
    let on_mouseup = {
        let has_selection = has_selection.clone();
        let sel_box = sel_box.clone();
        let sel_bar = sel_bar.clone();
        let scroll_ref = scroll_ref.clone();
        Callback::from(move |_: MouseEvent| {
            let selected = web_sys::window()
                .and_then(|window| window.get_selection().ok().flatten())
                .map(|selection| {
                    let text = String::from(selection.to_string());
                    !selection.is_collapsed() && !text.trim().is_empty()
                })
                .unwrap_or(false);
            // 先比较再 `set`：`UseStateHandle::set` 不看新旧值，每次都触发重渲染，
            // 而这里每次点击都会跑到 —— 不比较的话全应用每次点击都白渲染一遍。
            if *has_selection != selected {
                has_selection.set(selected);
            }

            let raw = if selected {
                selection_box(&scroll_ref)
            } else {
                None
            };
            *sel_box.borrow_mut() = raw;
            let next = match (raw, stage_view(&scroll_ref)) {
                (Some(box_), Some(view)) => Some(resolve_sel_bar(box_, view)),
                _ => None,
            };
            if *sel_bar != next {
                sel_bar.set(next);
            }
        })
    };

    let zoom_in = {
        let scale = scale.clone();
        Callback::from(move |_: MouseEvent| scale.set(next_zoom(*scale, 1.0)))
    };
    let zoom_out = {
        let scale = scale.clone();
        Callback::from(move |_: MouseEvent| scale.set(next_zoom(*scale, -1.0)))
    };
    let fit_width = {
        let scroll_ref = scroll_ref.clone();
        let page_sizes = page_sizes.clone();
        let current_page = current_page.clone();
        let scale = scale.clone();
        Callback::from(move |_: MouseEvent| {
            let index = current_page.saturating_sub(1) as usize;
            let Some(size) = page_sizes.get(index) else {
                return;
            };
            if let Some(value) = fit_scale(&scroll_ref, size.width) {
                scale.set(value);
            }
        })
    };

    let goto_page = {
        let current_page = current_page.clone();
        let layout = layout.clone();
        let scroll_ref = scroll_ref.clone();
        let page_input_ref = page_input_ref.clone();
        Callback::from(move |_: ()| {
            let Some(input) = page_input_ref.cast::<web_sys::HtmlInputElement>() else {
                return;
            };
            if let Ok(value) = input.value().trim().parse::<u32>() {
                let total = layout.len().max(1) as u32;
                let page = value.clamp(1, total);
                current_page.set(page);
                scroll_to_page(&scroll_ref, &layout, page);
            }
        })
    };

    let on_page_keydown = {
        let goto_page = goto_page.clone();
        Callback::from(move |event: KeyboardEvent| {
            if event.key() == "Enter" {
                event.prevent_default();
                goto_page.emit(());
            }
        })
    };

    let on_note = {
        let annotations = annotations.clone();
        let history = history.clone();
        let annotations_ok = annotations_ok.clone();
        Callback::from(move |(id, note): (String, String)| {
            // 只写了空白的批注 = 没写。归一成空串，让三个消费方（正文的悬停提示框 /
            // 徽标上的 `title` / 侧栏的备注行）对"这条到底有没有批注"给出一致的答案；
            // 否则磁盘上会留一个 `" "`，侧栏据此画出一行**空的**备注。
            let note = if note.trim().is_empty() {
                String::new()
            } else {
                note
            };
            let mut list = (*annotations).clone();
            let Some(item) = list.iter_mut().find(|item| item.id == id) else {
                return;
            };
            // 内容没变就别记历史：编辑框点开又原样保存，不该占一步撤回。
            if item.note == note {
                return;
            }
            history.push(&annotations);
            item.note = note;
            annotations.set(list);
            // 用户动过批注了 —— 这份列表现在一定可信，可以往磁盘写。
            *annotations_ok.borrow_mut() = true;
        })
    };

    let on_delete_annotation = {
        let annotations = annotations.clone();
        let history = history.clone();
        let annotations_ok = annotations_ok.clone();
        Callback::from(move |id: String| {
            // 先确认真的存在这么一条 —— 不存在就什么都不做。
            // 否则会往撤销栈里压一步**空操作**：用户按撤回时界面纹丝不动，
            // 得连按好几次才能退回真正想要的那一步。
            if !annotations.iter().any(|item| item.id == id) {
                return;
            }
            let list: Vec<Highlight> = annotations
                .iter()
                .filter(|item| item.id != id)
                .cloned()
                .collect();
            history.push(&annotations);
            annotations.set(list);
            // 用户动过批注了 —— 这份列表现在一定可信，可以往磁盘写。
            *annotations_ok.borrow_mut() = true;
        })
    };

    let on_focus_highlight = {
        let active_highlight = active_highlight.clone();
        let sidebar_tab = sidebar_tab.clone();
        let sidebar_open = sidebar_open.clone();
        Callback::from(move |id: String| {
            active_highlight.set(Some(id));
            sidebar_tab.set(SidebarTab::Annotations);
            sidebar_open.set(true);
        })
    };

    // ---- 批注右键菜单的两个动作
    //
    // 放在这里而不是和卡片菜单那几段挨着：它们依赖上面刚定义的
    // `on_delete_annotation` / `on_focus_highlight`，Rust 的 `let` 得先用后定义不了。

    // 「编辑批注」：跳到侧栏的批注页并选中这一条 ——
    // 和点标注左上角那个小圆点是同一条路（复用 `on_focus_highlight`）。
    let menu_edit_note = {
        let on_focus_highlight = on_focus_highlight.clone();
        let close_menu = close_menu.clone();
        Callback::from(move |id: String| {
            on_focus_highlight.emit(id);
            close_menu.emit(());
        })
    };

    // 「删除」：走已有的 `on_delete_annotation`（带撤销栈 + 自动落盘）。
    //
    // 删批注**不做二次确认** —— 和"删除记录和文件"不一样：
    // 那个删的是用户的文件，删错了是真损失；删一条批注可以撤回，
    // 实在不行重新选一遍文字再标一次就行，成本几乎为零。
    // 为这种可逆操作弹确认框只会让人烦。
    let menu_delete_mark = {
        let on_delete_annotation = on_delete_annotation.clone();
        let close_menu = close_menu.clone();
        Callback::from(move |id: String| {
            on_delete_annotation.emit(id);
            close_menu.emit(());
        })
    };

    let dismiss_status = {
        let status = status.clone();
        Callback::from(move |_: MouseEvent| status.set(None))
    };

    let on_close_sidebar = {
        let sidebar_open = sidebar_open.clone();
        Callback::from(move |_: ()| sidebar_open.set(false))
    };
    let on_jump = {
        let current_page = current_page.clone();
        let layout = layout.clone();
        let scroll_ref = scroll_ref.clone();
        Callback::from(move |page: u32| {
            current_page.set(page);
            scroll_to_page(&scroll_ref, &layout, page);
        })
    };

    // ---------------------------------------------------------------- 派生展示值

    // 阅读器视图 = 有激活的标签；标签列表本身可能非空（回到书架后仍保留）。
    let doc_open = (*active).is_some();
    let total_pages = page_sizes.len();
    let active_key = active_hit
        .and_then(|index| hits.get(index).map(|hit| hit.key.clone()))
        .unwrap_or_default();
    let pages_width = page_sizes
        .iter()
        .map(|size| size.width)
        .fold(0.0_f64, f64::max)
        * *scale;

    let colors_html: Html = COLORS
        .iter()
        .map(|color| {
            let is_active = highlight_color.as_str() == *color;
            let class = if is_active {
                format!("swatch folia-hl-{color} active")
            } else {
                format!("swatch folia-hl-{color}")
            };
            let onclick = {
                let highlight_color = highlight_color.clone();
                let color = color.to_string();
                Callback::from(move |_: MouseEvent| highlight_color.set(color.clone()))
            };
            html! { <button class={class} {onclick} title={color_label(color)} /> }
        })
        .collect();

    // 保留窗口比可视窗口每侧多 `KEEP_BAND` 页：窗口内的页留住 canvas 位图与
    // pdf.js 算子表，出了窗口就释放（见 `layout.rs::KEEP_BAND` 与 `page_view.rs`）。
    let keep_range = layout.keep_range(*visible);
    let pages_html: Html = (0..total_pages)
        .map(|index| {
            let Some(size) = page_sizes.get(index) else {
                return html! {};
            };
            let page = (index + 1) as u32;
            let page_highlights: Vec<Highlight> = annotations
                .iter()
                .filter(|item| item.page == page)
                .cloned()
                .collect();
            html! {
                <PageView
                    page={page}
                    width={size.width * *scale}
                    height={size.height * *scale}
                    scale={*scale}
                    active={index >= visible.0 && index <= visible.1}
                    keep={index >= keep_range.0 && index <= keep_range.1}
                    query={(*query).clone()}
                    active_key={active_key.clone()}
                    highlights={page_highlights}
                    on_focus_highlight={on_focus_highlight.clone()}
                    on_mark_context={open_mark_menu.clone()}
                />
            }
        })
        .collect();

    // ---- 书架：分页过滤 / 排序 / 名称筛选
    let filter = (*library_filter).trim().to_lowercase();
    let mut visible_recent: Vec<RecentFile> = match *library_tab {
        LibraryTab::Recent => (*recent).clone(),
        LibraryTab::Favorites => recent
            .iter()
            .filter(|item| item.favorite)
            .cloned()
            .collect(),
    };
    if !filter.is_empty() {
        visible_recent.retain(|item| item.name.to_lowercase().contains(&filter));
    }
    if *library_sort_by_name {
        visible_recent.sort_by(|a, b| a.name.cmp(&b.name));
    } else {
        visible_recent.sort_by(|a, b| b.opened_at.total_cmp(&a.opened_at));
    }

    // 标签栏：每个已打开的文档一格，点标签切换、点 × 关闭。
    let tabs_html: Html = {
        let active_index = *active;
        (*tabs)
            .iter()
            .enumerate()
            .map(|(index, tab)| {
                let is_active = active_index == Some(index);
                let class = if is_active { "tab active" } else { "tab" };

                let on_activate = {
                    let handles = handles.clone();
                    Callback::from(move |_: MouseEvent| {
                        // 显式传当前下标：`set()` 之后回读句柄拿到的是旧值。
                        activate_tab(&handles, index, active_index);
                    })
                };
                let on_close = {
                    let handles = handles.clone();
                    Callback::from(move |event: MouseEvent| {
                        event.stop_propagation();
                        close_tab(&handles, index);
                    })
                };
                // 中键（滚轮按下去）关标签 —— 跟浏览器一致。
                //
                // 挂在**容器**上而不是两个按钮上：标签的任何位置按中键都该关。
                // 用 `mousedown` 而不是 `auxclick`，一是 Yew 的事件表里没有
                // `onauxclick`，二是 `mousedown` 更早，能赶在中键触发"自动滚动"之前拦掉。
                let on_middle = {
                    let handles = handles.clone();
                    Callback::from(move |event: MouseEvent| {
                        if event.button() != 1 {
                            return;
                        }
                        event.prevent_default();
                        event.stop_propagation();
                        close_tab(&handles, index);
                    })
                };

                html! {
                    // `"false"` = 在这个子树里明确禁止拖拽。
                    // 标签栏用 `deep` 之后，标签容器自己的 padding（左右各 4px）也算拖拽区，
                    // 于是「贴着标签边缘点一下想切过去」会变成拖窗口。
                    // 标签本身是控件（点了要切换），整块划成非拖拽区更符合直觉。
                    <div class={class} key={tab.meta.id.clone()} data-tauri-drag-region="false"
                        onmousedown={on_middle}>
                        <button class="tab-label" onclick={on_activate}
                            title={tab.meta.name.clone()}>
                            { &tab.meta.name }
                        </button>
                        <button class="tab-close" onclick={on_close} title="关闭标签页">
                            {"×"}
                        </button>
                    </div>
                }
            })
            .collect()
    };

    let seg_recent = if matches!(*library_tab, LibraryTab::Recent) {
        "seg active"
    } else {
        "seg"
    };
    let seg_fav = if matches!(*library_tab, LibraryTab::Favorites) {
        "seg active"
    } else {
        "seg"
    };

    // ---- 书架卡片
    let cards_html = file_cards_html(
        &visible_recent,
        &thumbs,
        open_recent.clone(),
        open_card_menu.clone(),
    );

    // 书架空态的提示文案。
    //
    // 「收藏」分页空着时是 `None` —— **整块都不渲染**（连那个云朵图标一起）。
    // 原来这里写的是「还没有收藏的文档 —— hover 卡片点星标即可收藏」，
    // 用户明确要求删掉：那句是给"不知道怎么收藏"的人看的，
    // 但一个空页面摆着一段说明+图标，反而比空着更吵。
    let empty_hint: Option<&str> = match *library_tab {
        LibraryTab::Recent if !filter.is_empty() => Some("没有匹配的文档"),
        LibraryTab::Recent => Some("还没有打开过 PDF"),
        LibraryTab::Favorites => None,
    };

    // ---- 书架工具栏
    let seg_recent_click = {
        let library_tab = library_tab.clone();
        Callback::from(move |_: MouseEvent| library_tab.set(LibraryTab::Recent))
    };
    let seg_fav_click = {
        let library_tab = library_tab.clone();
        Callback::from(move |_: MouseEvent| library_tab.set(LibraryTab::Favorites))
    };
    let toggle_filter = {
        let show_filter = show_filter.clone();
        Callback::from(move |_: MouseEvent| show_filter.set(!*show_filter))
    };
    let on_filter_input = {
        let library_filter = library_filter.clone();
        Callback::from(move |event: InputEvent| {
            if let Some(target) = event
                .target()
                .and_then(|node| node.dyn_into::<web_sys::HtmlInputElement>().ok())
            {
                library_filter.set(target.value());
            }
        })
    };
    let toggle_sort = {
        let library_sort_by_name = library_sort_by_name.clone();
        let status = status.clone();
        Callback::from(move |_: MouseEvent| {
            let next = !*library_sort_by_name;
            library_sort_by_name.set(next);
            status.set(Some(if next {
                "按名称排序".into()
            } else {
                "按最近打开排序".into()
            }));
        })
    };
    // 「网格视图」按钮点了还没实现，先弹一句吐司告诉用户。
    let not_yet = {
        let status = status.clone();
        Callback::from(move |which: &'static str| status.set(Some(format!("{which}尚未开放"))))
    };
    let on_grid_btn = {
        let not_yet = not_yet.clone();
        Callback::from(move |_: MouseEvent| not_yet.emit("列表视图"))
    };
    let clear_recent = {
        let handles = handles.clone();
        let status = status.clone();
        Callback::from(move |_: MouseEvent| {
            // ⚠️ **必须**走 `write_recent`，不能自己 `recent_store.borrow_mut().clear()`
            // 再 `recent.set(Vec::new())`。后者在屏幕上看着一样，但**退出后就回来了**：
            // 清空发生在书架上，此时没有打开的文档，进度 effect 根本不会跑，
            // 少这一次 `persist_state` 就永远存不下去 —— 用户以为清空了，重启全回来。
            //
            // 两份派生缓存都不用在这儿动手：`write_recent` 按「前后差集」释放 pdf.js
            // 那份 `PDFDocumentProxy`，`thumbs` 的 data URL 由书架缩略图 effect 跟着
            // `recent` 重跑时清掉（见各自的注释）。
            write_recent(&handles, |list| list.clear());
            status.set(Some("已清空最近打开记录".into()));
        })
    };

    // ---- 「设为默认 PDF 阅读器」
    //
    // ⚠️ Windows 上这个动作**不会直接改默认程序** —— 系统把 `.pdf` 的归属收在
    // `UserChoice` 里，那个键带微软私有的哈希校验，程序写进去会被判定为篡改并忽略。
    // 所以后端只做两件事：把 Folia 注册进「打开方式」候选 + 打开系统「默认应用」页，
    // 由用户自己点一下确认（见后端 `set_default_pdf_handler`）。
    //
    // 反馈走吐司（2 秒后自动消失），并且把"已经就是默认"和"刚注册好"分开说 ——
    // 两种情况用户该做的事不一样。
    let set_default_app = {
        let status = status.clone();
        let is_default_pdf = is_default_pdf.clone();
        Callback::from(move |_: MouseEvent| {
            let status = status.clone();
            let is_default_pdf = is_default_pdf.clone();
            spawn_local(async move {
                match pdf::default_pdf_handler().await {
                    // 已经是默认了就别再折腾（省得又弹一次设置页）。
                    Ok(Some(true)) => {
                        is_default_pdf.set(Some(true));
                        status.set(Some("Folia 已经是默认的 PDF 阅读器了".into()));
                        return;
                    }
                    // 查不出来（读注册表出错）也继续走注册流程，别卡住用户。
                    _ => {}
                }
                match pdf::set_default_pdf_handler().await {
                    Ok(()) => {
                        // ⚠️ **不**把 `is_default_pdf` 置真：这个命令只是把 Folia 加进
                        // 「打开方式」并打开系统设置页，真正的默认还得用户在设置里点确认。
                        // 按钮留着（下次启动会重新查一次），吐司告诉他去哪点。
                        status.set(Some(
                            "已把 Folia 加入「打开方式」，请在系统设置里把它选为默认".into(),
                        ));
                    }
                    Err(err) => status.set(Some(format!("设置失败：{err}"))),
                }
            });
        })
    };

    // ---- 阅读器：左侧图标轨
    let rail_class = |tab: SidebarTab| {
        if *sidebar_open && *sidebar_tab == tab {
            "rail-btn active"
        } else {
            "rail-btn"
        }
    };
    let rail_thumbs = {
        let select_rail = select_rail.clone();
        Callback::from(move |_: MouseEvent| select_rail.emit(SidebarTab::Thumbnails))
    };
    let rail_outline = {
        let select_rail = select_rail.clone();
        Callback::from(move |_: MouseEvent| select_rail.emit(SidebarTab::Outline))
    };
    let rail_annotations = {
        let select_rail = select_rail.clone();
        Callback::from(move |_: MouseEvent| select_rail.emit(SidebarTab::Annotations))
    };
    // 图标轨上的放大镜：切到「搜索结果」面板，并把工具栏的搜索框聚焦。
    let rail_search = {
        let sidebar_tab = sidebar_tab.clone();
        let sidebar_open = sidebar_open.clone();
        let search_ref = search_ref.clone();
        let search_focus = search_focus.clone();
        Callback::from(move |_: MouseEvent| {
            // 放大镜只负责「把搜索打开」，不像别的图标那样再点一次就收起 ——
            // 所以这里不用 `select_rail`（它是切换语义）。
            sidebar_tab.set(SidebarTab::Search);
            sidebar_open.set(true);
            *search_focus.borrow_mut() = true;
            // 面板本来就在「搜索结果」上时，上面两个 `set` 不引起重渲染，
            // 那个 effect 也不会跑，这里同步补一次。
            if let Some(input) = search_ref.cast::<web_sys::HtmlInputElement>() {
                let _ = input.focus();
            }
        })
    };
    let close_rail = {
        let on_close_sidebar = on_close_sidebar.clone();
        Callback::from(move |_: MouseEvent| on_close_sidebar.emit(()))
    };
    // ---- 「页面总览」：点顶栏的 ▦，盖出一层网格，把**整份文档**的每一页排开。
    //
    // 为什么不是"把侧栏切到缩略图页"：侧栏默认就是开着的、默认就停在缩略图页，
    // 那样点下去屏幕上什么都不变 —— 用户会直接判成"按钮坏了"。
    //
    // ⚠️ **这里只掀浮层，不再"逐页渲染整本"。**
    //
    // 原来这里起一个串行循环 `for page in 1..=total` 把整本的缩略图都渲染一遍。
    // 缓存上限（`PAGE_THUMB_LIMIT`）加进来之后这个循环就没意义了：150 页的文档
    // 渲染 150 张、只有 60 张留得住（500 页就是 440 次白做），而且**渲染顺序是
    // 从第 1 页开始的**，跑完留下的那 60 张恰好是"离阅读位置最近"的那一批 ——
    // 用户在网格里滚到哪、哪一片就是空的。
    //
    // 现在交给下面那条「按需补齐」effect + 网格的 `onscroll`：请求的永远只是
    // **可视区那一片**，滚到哪补到哪。小文档（页数 < 上限）效果一样是"全都有图"，
    // `tests/e2e/e2e19` 的「30 页全部填好」照样成立。
    let open_all_thumbs = {
        let overview_open = overview_open.clone();
        Callback::from(move |_: MouseEvent| overview_open.set(true))
    };

    let tool_pill_class = if *sidebar_open {
        "pill-tool active"
    } else {
        "pill-tool"
    };

    // ---- 页面总览的几个小回调
    let close_overview = {
        let overview_open = overview_open.clone();
        Callback::from(move |_: MouseEvent| overview_open.set(false))
    };
    // 点网格里的某一页：跳过去 + 顺手把总览收掉（跳完还盖着就没意义了）。
    let overview_jump = {
        let on_jump = on_jump.clone();
        let overview_open = overview_open.clone();
        Callback::from(move |page: u32| {
            on_jump.emit(page);
            overview_open.set(false);
        })
    };

    // ---- 总览网格：缩略图按需补齐
    //
    // 缓存有条数上限（`state/handles.rs` 的 `PAGE_THUMB_LIMIT`），装不下的文档
    // 只能"滚到哪补到哪"。装得下的（页数 ≤ 上限）就整本都要 —— 那正是用户点
    // 「展开全部缩略图」时期待的，也轮不到淘汰出手（`e2e19` 认这一句）。
    //
    // 依赖里有 `thumbs_gen`：每次新图进来就重算一次可视区 —— 不然网格刚滚到
    // 一片空地时不会有任何东西触发下一次补齐。
    //
    // ⚠️ **必须先写焦点、再发请求。** `need_thumb` 回来时按 [`thumb_focus`] 淘汰，
    // 焦点取错的话这一轮请求回来的图会被下一轮自己挤掉 —— 插进去立刻被删、
    // 版本号照推、effect 重跑、再请求 …… 转不停（`e2e37`）。
    const OVERVIEW_BAND: f64 = 240.0;
    {
        let on_need = need_thumb.clone();
        let grid_ref = overview_grid_ref.clone();
        let focus_ref = overview_focus.clone();
        let total = total_pages as u32;
        use_effect_with(
            (*overview_open, *current_page, *thumbs_gen),
            move |(open, current, _gen)| {
                if *open && total > 0 {
                    if total as usize <= PAGE_THUMB_LIMIT {
                        for page in 1..=total {
                            on_need.emit(page);
                        }
                    } else {
                        let band = grid_ref
                            .cast::<web_sys::Element>()
                            .map(|grid| visible_pages(&grid, ".po-item", OVERVIEW_BAND))
                            .unwrap_or_default();
                        if let (Some(first), Some(last)) = (band.first(), band.last()) {
                            *focus_ref.borrow_mut() = (first + last) / 2;
                            for page in band {
                                on_need.emit(page);
                            }
                        } else {
                            // 网格还没排完版（打开的第一拍，或者刚被换掉）→ 退回
                            // "当前页附近"。焦点也跟着退回当前页，跟 [`thumb_focus`]
                            // 的兜底一致 —— 等 `scroll_into_view` / `onscroll` 把真正的
                            // 可视区报上来。
                            let start = current.saturating_sub(12).max(1);
                            let end = current.saturating_add(24).min(total);
                            *focus_ref.borrow_mut() = (*current).max(1);
                            for page in start..=end {
                                on_need.emit(page);
                            }
                        }
                    }
                }
                || {}
            },
        );
    }

    let overview_scroll = {
        let grid_ref = overview_grid_ref.clone();
        let on_need = need_thumb.clone();
        let focus_ref = overview_focus.clone();
        Callback::from(move |_: web_sys::Event| {
            let Some(grid) = grid_ref.cast::<web_sys::Element>() else {
                return;
            };
            // 直接操作 DOM、不走状态：网格的 scroll 每帧都来，走 props 等于
            // 把整个应用按帧重渲染一遍（同 `sidebar.rs` 里跟随正文滚动那段）。
            let band = visible_pages(&grid, ".po-item", OVERVIEW_BAND);
            if let (Some(first), Some(last)) = (band.first(), band.last()) {
                *focus_ref.borrow_mut() = (first + last) / 2;
            }
            for page in band {
                on_need.emit(page);
            }
        })
    };
    // 点面板内部不该把浮层关掉 —— 只有点外面那圈遮罩才算"点空白关闭"。
    let swallow_click = Callback::from(|event: MouseEvent| event.stop_propagation());

    // 总览网格的内容 + 头上的进度文案。
    //
    // 缩略图还没生成出来时先放一个占位块 —— 网格的格子数**从一开始就是全的**，
    // 所以用户点下去立刻看到"30 个格子"（而不是等半天才蹦出来），
    // 图片再一张张填进去。
    let overview_done = page_thumbs_store.borrow().len();
    // ⚠️ 别在大文档上写 "x / 150"：`overview_done` 被缓存上限卡住，那个数字会
    // **永远停在上限上**（60 / 150），看上去像卡死了 —— 其实那正是"滚到哪补到哪"
    // 的正常状态。装得下（页数 ≤ 上限）的时候才是真进度，也才敢说"全部已生成"。
    let overview_progress = if total_pages <= PAGE_THUMB_LIMIT {
        if overview_done >= total_pages {
            "全部已生成".to_string()
        } else {
            format!("正在生成缩略图 {} / {}", overview_done, total_pages)
        }
    } else {
        format!(
            "正在生成缩略图（滚到哪补到哪 · 已缓存 {} 张）",
            overview_done
        )
    };
    let overview_items: Html = if *overview_open && total_pages > 0 {
        (1..=total_pages as u32)
            .map(|page| {
                let class = if page == *current_page {
                    "po-item is-current"
                } else {
                    "po-item"
                };
                // 框按这一页的真实比例撑开（A4、Letter、横向页各不相同）。
                let ratio = page_sizes
                    .get((page - 1) as usize)
                    .filter(|size| size.width > 0.0 && size.height > 0.0)
                    .map(|size| size.width / size.height)
                    .unwrap_or(595.0 / 842.0);
                let frame_style = format!("aspect-ratio:{ratio:.6};");
                let on_pick = overview_jump.clone();
                let onclick = Callback::from(move |_: MouseEvent| on_pick.emit(page));
                // 先取出这一页的 URL（一次短借用），别把 `Ref` 留在 `html!` 里。
                let thumb = page_thumbs_store.borrow().get(&page).cloned();
                let frame = match thumb {
                    Some(url) => html! { <img src={url} alt="" /> },
                    None => html! { <span class="file-thumb-loading" /> },
                };
                html! {
                    // `data-page` 是给「滚动时按需补齐」用的：读 DOM 下标不等于页码。
                    <button {class} data-page={page.to_string()} {onclick}
                        title={format!("跳到第 {page} 页")}>
                        <span class="po-frame" style={frame_style}>{ frame }</span>
                        <span class="po-num">{ page }</span>
                    </button>
                }
            })
            .collect()
    } else {
        Html::default()
    };

    // 打开总览时把「当前页」那张滚到视野中间。
    // 100 页的文档一打开总览如果停在第一页，用户还得自己找刚才看到哪了。
    {
        let overview_open = overview_open.clone();
        use_effect_with(*overview_open, move |open| {
            if *open {
                if let Some(element) = web_sys::window()
                    .and_then(|window| window.document())
                    .and_then(|document| {
                        document
                            .query_selector(".po-item.is-current")
                            .ok()
                            .flatten()
                    })
                {
                    element.scroll_into_view();
                }
            }
            || {}
        });
    }

    // ---------------------------------------------------------------- 右键菜单
    //
    // 渲染在 `components/ctx_menu_view.rs` —— 它只吃「开哪一个 + 六个动作」，
    // 不碰状态，所以能独立看、独立改。这里只负责把回调接上去。
    let menu_html = ctx_menu_html(
        (*menu).clone(),
        menu_toggle_favorite.clone(),
        menu_reveal.clone(),
        menu_remove_record.clone(),
        menu_ask_delete_file.clone(),
        menu_edit_note.clone(),
        menu_delete_mark.clone(),
    );

    // 「删除记录和文件」的二次确认 —— 渲染在 `components/confirm_delete.rs`。
    let confirm_html = confirm_delete_html(
        (*pending_delete).clone(),
        cancel_delete_file.clone(),
        confirm_delete_file.clone(),
    );

    // ---------------------------------------------------------------- 渲染

    html! {
        <div class="app" onmouseup={on_mouseup}>
            // ---- 顶部标签栏：两个视图共用。
            // Windows 用的是原生标题栏，拖动本来由标题栏负责；这里再把标签栏
            // 也声明成拖拽区，等于多给用户一条「按住标签条空白处拖窗口」的路
            // （浏览器那种观感）。需要 `core:window:allow-start-dragging` 权限。
            //
            // 用 `deep` 而不是 `true`：`true` 只认「**直接**点在 header 自己身上」
            // （drag.js 里是 `return el === composedPath[0]`），
            // 而标签条右边那一大片空白实际上被 `.tab-strip`（`flex: 1`）占着 ——
            // 点在空白处 composedPath[0] 是它、不是 header，于是拖不动。
            // `deep` = 子树里任何位置都算拖拽区。
            //
            // 交互元素不会被误伤：drag.js 的 `isClickableElement` 遇到
            // BUTTON / A / INPUT / SELECT / TEXTAREA / LABEL / SUMMARY
            // 且自身没带这个属性时会直接 `return false`，
            // 所以 home、标签标题、关闭 × 照样是普通点击。
            <header class="tabbar" data-tauri-drag-region="deep">
                <button class="tabbar-home" onclick={go_home.clone()} title="回到书架">
                    { icon("home") }
                </button>
                <div class="tab-strip">{ tabs_html }</div>
                // 「打开其它 PDF」从阅读器工具栏搬到这里 —— 跟浏览器一样，
                // 「新开一个」属于标签栏，不属于正文工具栏。
                <button class="tabbar-add" onclick={open_file.clone()} title="打开其它 PDF">
                    { icon("plus") }
                </button>
            </header>

            if doc_open {
                // ---- 阅读器工具栏
                <header class="reader-top">
                    <div class="reader-top-left">
                        <button class={tool_pill_class} onclick={toggle_sidebar} title="显示 / 隐藏工具面板">
                            {"工具"}
                        </button>
                        // 「全部缩略图」：展开左侧缩略图面板并把所有页排出来，用来快速跳页。
                        // （原来这里是「打开其它 PDF」，已挪到标签栏右端的 + 号。）
                        <button class="icon-btn" onclick={open_all_thumbs}
                            title="展开全部缩略图（快速跳页）">
                            { icon("pages") }
                        </button>
                    </div>

                    <div class="reader-top-center">
                        <div class="pill-group">
                            <button class="pill-btn" onclick={mark_highlight} disabled={!*has_selection}
                                onmousedown={keep_selection.clone()}
                                title="把选中的文字标为高亮">
                                { icon("marker") }{"高亮"}
                            </button>
                            <span class="swatches" onmousedown={keep_selection.clone()}>{ colors_html }</span>
                            <button class="pill-btn" onclick={zoom_out} title="缩小">{ icon("minus") }</button>
                            <span class="zoom-label">{ format!("{:.0}%", *scale * 100.0) }</span>
                            <button class="pill-btn" onclick={zoom_in} title="放大">{ icon("plus") }</button>
                            <button class="pill-btn" onclick={fit_width} title="适应宽度">
                                { icon("expand") }{"适宽"}
                            </button>
                        </div>
                    </div>

                    <div class="reader-top-right">
                        // 撤回 / 保存：批注编辑的两个兜底动作。
                        // 平时批注是自动落盘的，所以这里不是「唯一保存入口」——
                        // 撤回给改错的人一条退路，保存给「关窗口前确认一下」用。
                        <button class="icon-btn" onclick={undo_click} disabled={*undo_depth == 0}
                            title="撤回上一步批注改动">
                            { icon("undo") }
                        </button>
                        <button class="icon-btn" onclick={save_now} title="立即把批注保存到本地">
                            { icon("save") }
                        </button>
                        // 搜索框和「回到书架」圆按钮都搬走了：
                        // 搜索框进左侧栏的「搜索结果」面板，回书架用标签栏左侧的 home / Esc。
                        <input ref={page_input_ref.clone()} class="page-input"
                            value={current_page.to_string()} onkeydown={on_page_keydown} />
                        <span class="page-total">{ format!("/ {total_pages}") }</span>
                    </div>
                </header>

                <div class="reader-body">
                    <nav class="rail">
                        <button class={rail_class(SidebarTab::Thumbnails)} onclick={rail_thumbs}
                            title="页面缩略图">{ icon("pages") }</button>
                        <button class={rail_class(SidebarTab::Outline)} onclick={rail_outline}
                            title="目录">{ icon("list") }</button>
                        <button class={rail_class(SidebarTab::Annotations)} onclick={rail_annotations}
                            title="批注">{ icon("comment") }</button>
                        <button class={rail_class(SidebarTab::Search)} onclick={rail_search}
                            title="搜索结果">{ icon("search") }</button>
                        <span class="rail-spacer"></span>
                        <button class="rail-btn" onclick={close_rail} title="收起面板">
                            { icon("chevron-left") }
                        </button>
                    </nav>

                    if *sidebar_open {
                        <Sidebar
                            tab={*sidebar_tab}
                            outline={(*outline).clone()}
                            annotations={(*annotations).clone()}
                            current_page={*current_page}
                            total_pages={total_pages}
                            page_sizes={(*page_sizes).clone()}
                            active_highlight={(*active_highlight).clone()}
                            thumbs={ThumbStore(page_thumbs_store.clone())}
                            query={(*query).clone()}
                            hits={(*hits).clone()}
                            active_hit={*active_hit}
                            search_ref={search_ref.clone()}
                            on_query_input={on_query_input.clone()}
                            on_clear_query={clear_query.clone()}
                            on_jump={on_jump}
                            on_delete={on_delete_annotation}
                            on_note={on_note}
                            on_close={on_close_sidebar}
                            on_need_thumb={need_thumb}
                            overview_open={*overview_open}
                            on_pick_hit={pick_hit}
                            on_prev_hit={prev_hit}
                            on_next_hit={next_hit}
                        />
                    }

                    <main class="stage" ref={scroll_ref.clone()} onscroll={on_scroll}>
                        if total_pages == 0 {
                            <div class="empty">
                                <p class="muted">{"文档已打开，但没有解析到任何页面。"}</p>
                            </div>
                        } else {
                            <div class="pages" style={format!("width:{pages_width:.2}px;")}>
                                { pages_html }
                            </div>
                        }
                        // 选中文字后就近浮出的工具栏。
                        //
                        // 它是 `.stage` 的绝对定位子元素，`left/top` 给的是**内容坐标** ——
                        // 于是它跟着正文一起滚，滚动时不需要重新定位。
                        // `left` 是工具栏**中心**，CSS 那句 `translateX(-50%)` 负责折回左上角。
                        if let Some(bar) = *sel_bar {
                            <div class="sel-bar" onmousedown={keep_selection.clone()}
                                style={format!("left:{:.1}px; top:{:.1}px;", bar.left, bar.top)}>
                                { mark_buttons }
                                <span class="divider" />
                                <button class="sel-bar-btn" onclick={copy_selection}
                                    title="复制选中的文字">
                                    { icon("copy") }
                                </button>
                            </div>
                        }
                        // 切标签要重新把 PDF 载入 pdf.js，中间盖一层，避免看到上一份文档。
                        if *loading {
                            <div class="stage-mask">
                                <span class="file-thumb-loading"></span>
                                {"正在载入文档…"}
                            </div>
                        }
                    </main>

                    // ---- 页面总览：盖住整个窗口的一层网格。
                    //
                    // 放 `position: fixed` 而不是 `.stage` 里：`.stage` 有 `overflow`，
                    // 而且正文在滚动 —— 浮层跟着滚或者被裁掉都不对。
                    // 点遮罩（外面那圈）关闭，点面板内部不关（`swallow_click`）。
                    if *overview_open {
                        <div class="page-overview" onclick={close_overview.clone()}>
                            <div class="po-panel" onclick={swallow_click.clone()}>
                                <div class="po-head">
                                    <span class="po-title">
                                        { format!("全部页面 · 共 {} 页", total_pages) }
                                    </span>
                                    <span class="po-progress">{ overview_progress }</span>
                                    <button class="po-close" onclick={close_overview.clone()}
                                        title="关闭（Esc）">
                                        { icon("close") }
                                    </button>
                                </div>
                                <div class="po-grid" ref={overview_grid_ref.clone()}
                                    onscroll={overview_scroll.clone()}>{ overview_items }</div>
                            </div>
                        </div>
                    }
                </div>
            } else {
                // ---- 书架
                <main class="library" ref={scroll_ref.clone()} onscroll={on_scroll}>
                    <div class="lib-head">
                        <div class="segmented">
                            <button class={seg_recent} onclick={seg_recent_click}>{"最近"}</button>
                            <button class={seg_fav} onclick={seg_fav_click}>{"收藏"}</button>
                        </div>
                        <div class="lib-actions">
                            if *show_filter {
                                <input class="search-input" placeholder="按名称筛选…"
                                    value={(*library_filter).clone()} oninput={on_filter_input} />
                            }
                            // 「设为默认 PDF 阅读器」：一次性动作，点完靠吐司反馈。
                            // 放在这里而不是做成图标 —— 图标表达不了"改系统设置"这件事。
                            //
                            // **已经是默认了就不显示**：那种状态下点它什么都不会变，
                            // 用户只会当成"按钮坏了"。`None`（还没查出来）时照常显示，
                            // 免得查得慢的那一下按钮闪进闪出。
                            if !matches!(*is_default_pdf, Some(true)) {
                                <button class="btn" onclick={set_default_app}
                                    title="把 Folia 加入「打开方式」，并在系统设置里选为默认（Windows）">
                                    { icon("settings") }{"设为默认"}
                                </button>
                            }
                            <button class="btn-primary" onclick={open_file.clone()}>
                                { icon("cards") }{"打开文件"}
                            </button>
                        </div>
                    </div>

                    <div class="panel">
                        <div class="panel-head">
                            <div class="panel-tools">
                                <button class={if *show_filter { "icon-btn active" } else { "icon-btn" }}
                                    onclick={toggle_filter} title="搜索">{ icon("search") }</button>
                                <span class="divider" />
                                <button class="icon-btn" onclick={toggle_sort} title="排序">{ icon("sort") }</button>
                                // 「筛选」（sliders 图标）在这里被删掉了 ——
                                // 它一直是个"尚未开放"的占位按钮，点下去只弹一句吐司，
                                // 而它想表达的"按名称筛"其实上面那个放大镜已经能做了。
                                // 一个点了没用的按钮比没有按钮更糟。
                                <button class="icon-btn active" onclick={on_grid_btn} title="网格视图">{ icon("grid") }</button>
                                <span class="divider" />
                                <button class="icon-btn" onclick={clear_recent} title="清空记录">{ icon("trash") }</button>
                            </div>
                        </div>

                        if visible_recent.is_empty() {
                            // 「收藏」分页空着时 `empty_hint` 是 `None` —— 什么都不画。
                            if let Some(hint) = empty_hint {
                                <div class="lib-empty">
                                    <span class="empty-icon">{ icon("cloud") }</span>
                                    <p>{ hint }</p>
                                    if matches!(*library_tab, LibraryTab::Recent) {
                                        <button class="btn-primary" onclick={open_file.clone()}>
                                            { icon("cards") }{"打开文件"}
                                        </button>
                                    }
                                </div>
                            }
                        } else {
                            <div class="file-grid">{ cards_html }</div>
                        }
                    </div>

                    if *loading {
                        <p class="lib-empty muted">{"正在解析文档…"}</p>
                    }
                </main>
            }

            // 右键菜单（卡片 / 批注共用）与它的确认框都挂在 `.app` 的**最外层**，
            // 不在 `.library` 或阅读区里面 —— `position: fixed` 只要碰上带 `transform` /
            // `filter` 的祖先就会退化成相对那个祖先定位，书架将来加个动画就踩上了。
            { menu_html }
            { confirm_html }

            if let Some(message) = (*status).clone() {
                <div class="toast" onclick={dismiss_status}>{ message }</div>
            }
        </div>
    }
}
