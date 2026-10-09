//! 侧栏：页面缩略图 / 目录树 / 批注列表。
//!
//! 用哪个面板由左侧图标轨决定，所以这里不再自带标签页，
//! 只在头部显示当前面板名。

use std::cell::Cell;
use std::rc::Rc;

use wasm_bindgen::closure::Closure;
use wasm_bindgen::JsCast;
use web_sys::InputEvent;
use yew::prelude::*;

use crate::components::icon::icon;
use crate::pdf::types::{Highlight, MarkKind, OutlineNode, PageSize, SearchHit};
use crate::state::handles::ThumbStore;

/// 页面尺寸还没解析出来时用的兜底比例（A4）。
const FALLBACK_RATIO: f64 = 595.0 / 842.0;

/// 批注样式的中文名，显示在批注列表里。
///
/// 列表左侧的圆点只说明**颜色**，看不出画的是哪种样式 ——
/// 四种样式共存之后必须补一个名字，否则「这条是高亮还是下划线」只能靠点进去看。
///
/// 真正的定义在 `MarkKind::label`（正文右键菜单的「删除下划线」也要用同一份文案）。
/// 这里留个薄壳是为了让调用点读起来还是"侧栏的标签"。
fn kind_label(kind: MarkKind) -> &'static str {
    kind.label()
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SidebarTab {
    Thumbnails,
    Outline,
    Annotations,
    Search,
}

#[derive(Properties, PartialEq)]
pub struct SidebarProps {
    pub tab: SidebarTab,
    pub outline: Vec<OutlineNode>,
    pub annotations: Vec<Highlight>,
    pub current_page: u32,
    pub total_pages: usize,
    /// 每页在 scale = 1 时的尺寸。缩略图的框要按**这一页自己的比例**来撑，
    /// 否则页面会被裁掉一块。
    pub page_sizes: Vec<PageSize>,
    pub active_highlight: Option<String>,
    /// 页码 → 缩略图 data URL。
    ///
    /// ⚠️ 传的是**共享** store 的句柄（`ThumbStore`）而不是一份 owned 的 `HashMap`
    /// —— 原来父级每次渲染都要 `(*page_thumbs).clone()` 把整张表（几百 KB 的
    /// data URL 字符串）复制一遍。`ThumbStore` 的 `PartialEq` 只比指针，
    /// 否则 Yew 每次渲染还要逐条比对那几百 KB。
    pub thumbs: ThumbStore,
    /// 全文检索：关键词、命中列表、当前命中下标。
    pub query: String,
    pub hits: Vec<SearchHit>,
    pub active_hit: Option<usize>,
    /// 搜索输入框挂在面板顶部，ref 由父级持有（图标轨的放大镜要 focus 它）。
    pub search_ref: NodeRef,
    pub on_query_input: Callback<InputEvent>,
    pub on_clear_query: Callback<()>,
    pub on_jump: Callback<u32>,
    pub on_delete: Callback<String>,
    pub on_note: Callback<(String, String)>,
    pub on_close: Callback<()>,
    /// 请求某一页的缩略图（父级负责去重与缓存）。
    pub on_need_thumb: Callback<u32>,
    /// 「页面总览」浮层是不是盖在上面。
    ///
    /// 盖着的时候侧栏整片都在遮罩底下看不见，而总览是一张**所有页**的网格、
    /// 淘汰焦点跟着网格跑 —— 这时候还去补侧栏那 13 张等于每来一张总览的图就
    /// 白渲染 13 次，而且一张也留不住（见下面缩略图那条 effect）。
    pub overview_open: bool,
    /// 点某一条命中（直接跳过去）。
    pub on_pick_hit: Callback<usize>,
    pub on_prev_hit: Callback<()>,
    pub on_next_hit: Callback<()>,
}

/// 把片段里的命中词包一层 `<mark>`，像 Word 那样把关键词标出来。
///
/// 片段的构造见 `bridge.js::snippetOf`，是「命中词前后各 24 字」，
/// 所以正常情况下一定找得到；找不到就原样输出。
fn mark_snippet(snippet: &str, query: &str) -> Html {
    let needle = query.trim().to_lowercase();
    if needle.is_empty() {
        return html! { { snippet.to_string() } };
    }
    let hay = snippet.to_lowercase();
    let mut parts: Vec<Html> = Vec::new();
    let mut cursor = 0usize;
    while let Some(offset) = hay[cursor..].find(&needle) {
        let start = cursor + offset;
        let end = start + needle.len();
        // `to_lowercase()` 不保证等长（比如 'İ'），越界或切在半个字符上就放弃高亮。
        if end > snippet.len() || !snippet.is_char_boundary(start) || !snippet.is_char_boundary(end)
        {
            break;
        }
        if start > cursor {
            parts.push(html! { { snippet[cursor..start].to_string() } });
        }
        parts.push(html! { <mark class="hit-mark">{ snippet[start..end].to_string() }</mark> });
        cursor = end;
    }
    if cursor < snippet.len() {
        parts.push(html! { { snippet[cursor..].to_string() } });
    }
    html! { <>{ for parts }</> }
}

/// 递归渲染目录树，深度决定缩进。
fn render_outline(
    nodes: &[OutlineNode],
    depth: usize,
    current_page: u32,
    on_jump: &Callback<u32>,
) -> Html {
    if nodes.is_empty() {
        return html! {};
    }
    html! {
        <ul class="outline-list">
            { for nodes.iter().map(|node| {
                let active = node.page_number.map(|p| p == current_page).unwrap_or(false);
                let onclick = {
                    let on_jump = on_jump.clone();
                    let page = node.page_number;
                    Callback::from(move |_: MouseEvent| {
                        if let Some(page) = page {
                            on_jump.emit(page);
                        }
                    })
                };
                let class = if active { "outline-link active" } else { "outline-link" };
                let style = format!("padding-left:{}px;", 10 + depth * 12);
                let children = render_outline(&node.children, depth + 1, current_page, on_jump);
                html! {
                    <li class="outline-item">
                        <button {class} {style} {onclick}>{ &node.title }</button>
                        { children }
                    </li>
                }
            }) }
        </ul>
    }
}

#[function_component(Sidebar)]
pub fn sidebar(props: &SidebarProps) -> Html {
    let editing = use_state(|| Option::<String>::None);
    let draft = use_state(String::new);
    // `.side-body` 是缩略图列表的滚动容器，跟随滚动时要用到。
    let body_ref = use_node_ref();
    // 正在编辑的那条批注的输入框。批注是「失焦即保存」，所以点开就得先聚焦，
    // 否则用户点开又直接点别处（从没聚焦过 → 不会有 blur）会一直卡在编辑态。
    let edit_ref = use_node_ref();

    {
        let edit_ref = edit_ref.clone();
        use_effect_with((*editing).clone(), move |editing| {
            if editing.is_some() {
                if let Some(area) = edit_ref.cast::<web_sys::HtmlTextAreaElement>() {
                    let _ = area.focus();
                }
            }
            || {}
        });
    }

    let on_close = {
        let on_close = props.on_close.clone();
        Callback::from(move |_: MouseEvent| on_close.emit(()))
    };

    // 缩略图按需生成：只请求当前页附近的一段，避免一上来就把 130 页全渲染了。
    //
    // ⚠️ 依赖里有 `overview_open`。它是这一段的**补漏阀**：总览开着时淘汰焦点
    // 跟着网格跑，完全可能把侧栏这一段（当前页附近 13 张）挤出去，而这条
    // effect 原来只认 `(tab, current, total)` —— 总览一关，焦点回到阅读位置，
    // 被挤掉的那几张却没有任何东西去重新请求，侧栏就一直是空格子，直到用户
    // 手动翻页。挂上 `overview_open` 之后总览一关它自己重跑一次，正好补回来。
    {
        let on_need = props.on_need_thumb.clone();
        let tab = props.tab;
        let current = props.current_page;
        let total = props.total_pages;
        let overview_open = props.overview_open;
        use_effect_with(
            (tab, current, total, overview_open),
            move |(tab, current, total, overview_open)| {
                // 总览盖着的时候不补：那 13 张此刻在遮罩底下，而且焦点在网格那边，
                // 请求回来也会立刻被淘汰 —— 纯白渲染。
                if *tab == SidebarTab::Thumbnails && *total > 0 && !*overview_open {
                    let start = current.saturating_sub(6).max(1);
                    let end = (*current + 12).min(*total as u32);
                    for page in start..=end {
                        on_need.emit(page);
                    }
                }
                || {}
            },
        );
    }

    // 缩略图跟随正文滚动。
    //
    // 全程直接操作 DOM，**不走 Yew 状态**：正文的 scroll 事件每帧都来，
    // 走 props 等于把整个应用按帧重渲染一遍。
    //
    // 「丝滑」由两件事凑成：
    //   1. **目标连续** —— 按正文视口中心落在第几页、页内比例多少，
    //      在相邻两个缩略图中心之间插值。旧写法是「当前页缩略图滚出可视区才跳一格」，
    //      只在翻页那一刻动一次，所以一顿一顿的。
    //   2. **位移交给 CSS** —— `.side-body` 上的 `scroll-behavior: smooth`，
    //      浏览器在合成线程上做动画，比自己在 rAF 里逐帧 `setScrollTop` 稳。
    //
    // 用户在缩略图列表里自己滚了（wheel / 触摸）就**冷却一段时间**不跟随。
    // 冷却而不是「滚正文才恢复」：后者等于没有暂停 ——
    // 我们只在正文 scroll 时动手，而正文一 scroll 就会把标志复位。
    // 真正要防的是「用户翻到第 90 页的缩略图在看，正文轻轻动一下就把他拽回第 3 页」。
    {
        let body_ref = body_ref.clone();
        let tab = props.tab;
        let total = props.total_pages;
        use_effect_with((tab, total), move |(tab, total)| -> Box<dyn FnOnce()> {
            if *tab != SidebarTab::Thumbnails || *total == 0 {
                return Box::new(|| {});
            }
            let Some(body) = body_ref.cast::<web_sys::Element>() else {
                return Box::new(|| {});
            };
            let Some(stage) = web_sys::window()
                .and_then(|window| window.document())
                .and_then(|document| document.query_selector(".stage").ok().flatten())
            else {
                return Box::new(|| {});
            };

            // 每个缩略图在**内容坐标**里的纵向中心。只算一次：
            // 缩略图框有固定 `aspect-ratio`，图片后加载不会改变高度。
            let body_top = body.get_bounding_client_rect().top();
            let base = body.scroll_top() as f64;
            let centers: Vec<f64> = body
                .query_selector_all(".thumb-item")
                .ok()
                .map(|list| {
                    (0..list.length())
                        .filter_map(|index| list.item(index))
                        .filter_map(|node| node.dyn_into::<web_sys::Element>().ok())
                        .map(|item| {
                            let rect = item.get_bounding_client_rect();
                            rect.top() - body_top + base + rect.height() * 0.5
                        })
                        .collect()
                })
                .unwrap_or_default();
            if centers.is_empty() {
                return Box::new(|| {});
            }

            let target = Rc::new(Cell::new(-1.0_f64));
            // 冷却截止时刻（`Date::now()` 的毫秒）。0 = 不冷却。
            // 手动滚过缩略图列表后 1.6 秒内不跟随。
            let pause_until = Rc::new(Cell::new(0.0_f64));
            const PAUSE_MS: f64 = 1600.0;

            // 正文视口中心 → 缩略图列表该滚到哪。
            let compute = {
                let stage = stage.clone();
                let body = body.clone();
                let centers = centers.clone();
                let target = target.clone();
                Rc::new(move || {
                    let center =
                        stage.get_bounding_client_rect().top() + stage.client_height() as f64 * 0.5;
                    let Ok(pages) = stage.query_selector_all(".page") else {
                        return;
                    };
                    // 虚拟化下 DOM 里只有可视区附近的页，下标不等于页码 ——
                    // 所以页码从 `data-page` 读。
                    let mut page = 0usize;
                    let mut fraction = 0.0_f64;
                    for index in 0..pages.length() {
                        let Some(element) = pages
                            .item(index)
                            .and_then(|node| node.dyn_into::<web_sys::Element>().ok())
                        else {
                            continue;
                        };
                        let rect = element.get_bounding_client_rect();
                        if center < rect.top() {
                            break;
                        }
                        page = element
                            .get_attribute("data-page")
                            .and_then(|value| value.parse::<usize>().ok())
                            .unwrap_or(index as usize + 1);
                        fraction = if rect.height() > 0.0 {
                            ((center - rect.top()) / rect.height()).clamp(0.0, 1.0)
                        } else {
                            0.0
                        };
                        if center <= rect.bottom() {
                            break;
                        }
                    }
                    if page == 0 {
                        page = 1;
                        fraction = 0.0;
                    }
                    // 页内比例 → 在相邻两个缩略图中心之间插值
                    let index = (page - 1).min(centers.len() - 1);
                    let a = centers[index];
                    let b = centers.get(index + 1).copied().unwrap_or(a);
                    let want = a + (b - a) * fraction - body.client_height() as f64 * 0.5;
                    let max = (body.scroll_height() - body.client_height()).max(0) as f64;
                    target.set(want.clamp(0.0, max));
                })
            };

            // 面板刚挂上：瞬时定位。临时把 `scroll-behavior` 压回 auto，
            // 否则会从列表顶部一路滑过去（比引入 ScrollToOptions 轻）。
            compute();
            if let Ok(html) = body.clone().dyn_into::<web_sys::HtmlElement>() {
                let style = html.style();
                let _ = style.set_property("scroll-behavior", "auto");
                if target.get() >= 0.0 {
                    body.set_scroll_top(target.get().round() as i32);
                }
                let _ = style.remove_property("scroll-behavior");
            }

            let on_scroll = {
                let body = body.clone();
                let target = target.clone();
                let pause_until = pause_until.clone();
                let compute = compute.clone();
                Closure::<dyn FnMut()>::new(move || {
                    // 目标每次都重算（冷却期也一样），这样冷却一过就是准的。
                    compute();
                    if target.get() < 0.0 || js_sys::Date::now() < pause_until.get() {
                        return;
                    }
                    // 只写 `scrollTop`：`.side-body` 上的 `scroll-behavior: smooth`
                    // 会把它变成一段动画，不用自己逐帧插值。
                    body.set_scroll_top(target.get().round() as i32);
                })
            };
            let _ = stage
                .add_event_listener_with_callback("scroll", on_scroll.as_ref().unchecked_ref());

            // 用户自己滚缩略图列表 → 进入冷却，别跟他抢。
            // 只认「滚动手势」：`mousedown` 不算 —— 点缩略图是要跳页，
            // 那种情况下跟随恰恰应该继续。
            // `wheel` 必须是 passive 的，否则浏览器得等 JS 跑完才敢滚列表，反而卡。
            let on_manual = {
                let pause_until = pause_until.clone();
                Closure::<dyn FnMut()>::new(move || {
                    pause_until.set(js_sys::Date::now() + PAUSE_MS);
                })
            };
            let passive = web_sys::AddEventListenerOptions::new();
            passive.set_passive(true);
            for event in ["wheel", "touchstart"] {
                let _ = body.add_event_listener_with_callback_and_add_event_listener_options(
                    event,
                    on_manual.as_ref().unchecked_ref(),
                    &passive,
                );
            }

            Box::new(move || {
                let _ = stage.remove_event_listener_with_callback(
                    "scroll",
                    on_scroll.as_ref().unchecked_ref(),
                );
                for event in ["wheel", "touchstart"] {
                    let _ = body.remove_event_listener_with_callback(
                        event,
                        on_manual.as_ref().unchecked_ref(),
                    );
                }
                drop(on_scroll);
                drop(on_manual);
            })
        });
    }

    let title = match props.tab {
        SidebarTab::Thumbnails => "缩略图".to_string(),
        SidebarTab::Outline => "目录".to_string(),
        SidebarTab::Annotations => format!("批注 {}", props.annotations.len()),
        SidebarTab::Search => "搜索结果".to_string(),
    };

    let body = match props.tab {
        SidebarTab::Thumbnails => {
            if props.total_pages == 0 {
                html! { <p class="side-empty">{"文档尚未解析完成"}</p> }
            } else {
                let items: Html = (1..=props.total_pages as u32)
                    .map(|page| {
                        let active = page == props.current_page;
                        let class = if active {
                            "thumb-item active"
                        } else {
                            "thumb-item"
                        };
                        let on_jump = props.on_jump.clone();
                        let onclick = Callback::from(move |_: MouseEvent| on_jump.emit(page));
                        let thumb = props.thumbs.0.borrow().get(&page).cloned();
                        let frame = match thumb {
                            Some(url) => html! { <img src={url} alt="" /> },
                            None => html! { <span class="file-thumb-loading" /> },
                        };
                        // 框按这一页的真实比例撑开（A4、Letter、横向页各不相同）。
                        // 比例拿不到（尺寸还没解析出来）时用兜底值，总比裁掉一块强。
                        let ratio = props
                            .page_sizes
                            .get((page - 1) as usize)
                            .filter(|size| size.width > 0.0 && size.height > 0.0)
                            .map(|size| size.width / size.height)
                            .unwrap_or(FALLBACK_RATIO);
                        let frame_style = format!("aspect-ratio:{ratio:.6};");
                        html! {
                            <li>
                                <button {class} {onclick}>
                                    <span class="thumb-frame" style={frame_style}>{ frame }</span>
                                    <span class="thumb-num">{ page }</span>
                                </button>
                            </li>
                        }
                    })
                    .collect();
                html! { <ul class="thumb-list">{ items }</ul> }
            }
        }
        SidebarTab::Outline => {
            if props.outline.is_empty() {
                html! { <p class="side-empty">{"这份 PDF 没有目录信息"}</p> }
            } else {
                render_outline(&props.outline, 0, props.current_page, &props.on_jump)
            }
        }
        SidebarTab::Annotations => {
            if props.annotations.is_empty() {
                // 这里原来有一句「选中正文后会浮出一排工具：高亮 / 下划线 / …」。
                // 删掉了：那排工具一选中文字就会自己浮出来，用不着先教一遍。
                html! {}
            } else {
                let items: Html = props
                    .annotations
                    .iter()
                    .map(|item| {
                        let is_active = props.active_highlight.as_deref() == Some(item.id.as_str());
                        let is_editing = editing.as_deref() == Some(item.id.as_str());
                        let class = if is_active { "ann-item active" } else { "ann-item" };

                        let jump = {
                            let on_jump = props.on_jump.clone();
                            let page = item.page;
                            Callback::from(move |_: MouseEvent| on_jump.emit(page))
                        };
                        let start_edit = {
                            let editing = editing.clone();
                            let draft = draft.clone();
                            let id = item.id.clone();
                            let note = item.note.clone();
                            Callback::from(move |_: MouseEvent| {
                                editing.set(Some(id.clone()));
                                draft.set(note.clone());
                            })
                        };
                        let remove = {
                            let on_delete = props.on_delete.clone();
                            let id = item.id.clone();
                            Callback::from(move |_: MouseEvent| on_delete.emit(id.clone()))
                        };
                        let on_input = {
                            let draft = draft.clone();
                            Callback::from(move |event: InputEvent| {
                                if let Some(target) = event
                                    .target()
                                    .and_then(|node| node.dyn_into::<web_sys::HtmlTextAreaElement>().ok())
                                {
                                    draft.set(target.value());
                                }
                            })
                        };
                        // 「失焦即保存」：编辑框一丢焦点就提交草稿并退出编辑态。
                        //
                        // 原来这里是「保存 / 取消」两个按钮。改成 blur 之后两个都得去掉 ——
                        // 「取消」会点不动：点它时 textarea 先失焦、先把内容存下去，
                        // 然后按钮才被点到（那时编辑态已经结束，按钮都没了）。
                        // 所以干脆只留输入框。
                        // （曾经在下面配过一句「点击别处即保存」的小字，用户要求去掉，
                        //  别再画回去 —— 失焦保存这个行为本身没变。）
                        let save = {
                            let on_note = props.on_note.clone();
                            let editing = editing.clone();
                            let draft = draft.clone();
                            let id = item.id.clone();
                            Callback::from(move |_: FocusEvent| {
                                on_note.emit((id.clone(), (*draft).clone()));
                                editing.set(None);
                            })
                        };

                        html! {
                            <li class={class}>
                                <div class="ann-head" onclick={jump}>
                                    <span class={format!("ann-dot folia-hl-{}", item.color)} />
                                    <span class="ann-page">{ format!("P{}", item.page) }</span>
                                    <span class="ann-kind">{ kind_label(item.kind) }</span>
                                    <span class="ann-text">{ &item.text }</span>
                                </div>
                                // 判空要 `trim()` —— 和 `MarkHit::tip` / 徽标的 `title` 一致。
                                // 只写了个空格的批注在正文里既不显示提示框、徽标也说
                                // 「添加批注」，这里却会画出一行**空的**备注，自相矛盾。
                                if !item.note.trim().is_empty() && !is_editing {
                                    <p class="ann-note">{ &item.note }</p>
                                }
                                if is_editing {
                                    <div class="ann-edit">
                                        <textarea ref={edit_ref.clone()} class="ann-input"
                                            value={(*draft).clone()} oninput={on_input} onblur={save}
                                            placeholder="写下你的批注…" />
                                    </div>
                                } else {
                                    <div class="ann-actions">
                                        <button class="btn tiny ghost" onclick={start_edit}>{"批注"}</button>
                                        <button class="btn tiny ghost danger" onclick={remove}>{"删除"}</button>
                                    </div>
                                }
                            </li>
                        }
                    })
                    .collect();
                html! { <ul class="ann-list">{ items }</ul> }
            }
        }
        SidebarTab::Search => {
            if props.query.trim().is_empty() {
                html! { <p class="side-empty">{"在上面的输入框里输入关键词"}</p> }
            } else if props.hits.is_empty() {
                html! { <p class="side-empty">{"没有找到匹配的内容"}</p> }
            } else {
                let items: Html = props
                    .hits
                    .iter()
                    .enumerate()
                    .map(|(index, hit)| {
                        let is_active = props.active_hit == Some(index);
                        let class = if is_active {
                            "hit-item active"
                        } else {
                            "hit-item"
                        };
                        let on_pick = props.on_pick_hit.clone();
                        let onclick = Callback::from(move |_: MouseEvent| on_pick.emit(index));
                        html! {
                            <li>
                                <button {class} {onclick} title={format!("跳到第 {} 页", hit.page)}>
                                    <span class="hit-page">{ format!("P{}", hit.page) }</span>
                                    <span class="hit-text">
                                        { mark_snippet(&hit.snippet, &props.query) }
                                    </span>
                                </button>
                            </li>
                        }
                    })
                    .collect();
                let prev = {
                    let on_prev = props.on_prev_hit.clone();
                    Callback::from(move |_: MouseEvent| on_prev.emit(()))
                };
                let next = {
                    let on_next = props.on_next_hit.clone();
                    Callback::from(move |_: MouseEvent| on_next.emit(()))
                };
                let counter = format!(
                    "{} / {} 处",
                    props.active_hit.map(|index| index + 1).unwrap_or(0),
                    props.hits.len()
                );
                html! {
                    <>
                        <div class="hit-bar">
                            <span class="hit-count">{ counter }</span>
                            <button class="icon-btn" onclick={prev} title="上一处">
                                { icon("chevron-up") }
                            </button>
                            <button class="icon-btn" onclick={next} title="下一处">
                                { icon("chevron-down") }
                            </button>
                        </div>
                        <ul class="hit-list">{ items }</ul>
                    </>
                }
            }
        }
    };

    // 搜索输入框就长在「搜索结果」面板顶部（原来挂在阅读器工具栏上）。
    let search_row = if props.tab == SidebarTab::Search {
        let clear = {
            let on_clear = props.on_clear_query.clone();
            Callback::from(move |_: MouseEvent| on_clear.emit(()))
        };
        html! {
            <div class="side-search">
                { icon("search") }
                <input ref={props.search_ref.clone()} class="search-input" placeholder="搜索全文…"
                    value={props.query.clone()} oninput={props.on_query_input.clone()} />
                if !props.query.is_empty() {
                    <button class="search-clear" onclick={clear} title="清除">{ icon("close") }</button>
                }
            </div>
        }
    } else {
        html! {}
    };

    html! {
        <aside class="sidebar">
            <div class="side-head">
                <div class="side-tabs">
                    <span class="side-tab active">{ title }</span>
                </div>
                <button class="icon-btn" onclick={on_close} title="收起面板">{ icon("chevron-left") }</button>
            </div>
            { search_row }
            <div class="side-body" ref={body_ref.clone()}>{ body }</div>
        </aside>
    }
}
