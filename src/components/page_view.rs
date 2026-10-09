//! 单页视图：canvas 画面 + 透明文本层 + 批注覆盖层。
//!
//! 只有处于可视区的页面才会真正触发 pdf.js 渲染（虚拟化），
//! 其余页面仅保留一个占位盒，保证长文档的滚动性能。

use wasm_bindgen_futures::spawn_local;
use yew::prelude::*;

use crate::pdf::{
    self,
    types::{Highlight, MarkKind, Rect},
};

#[derive(Properties, PartialEq)]
pub struct PageViewProps {
    pub page: u32,
    pub width: f64,
    pub height: f64,
    pub scale: f64,
    /// 是否在可视区内（决定要不要渲染）。
    pub active: bool,
    /// 是否在**保留窗口**内（决定要不要留着 canvas 与文本层）。
    ///
    /// 保留窗口 = 可视窗口每侧再外扩 [`crate::layout::KEEP_BAND`] 页，所以
    /// `active` 一定是 `keep` 的子集。两者分开是因为代价差得远：一次渲染要
    /// 跑 worker 往返 + 画一张位图，而**留住**那张位图（A4 页约 6.7 MB）
    /// 和 pdf.js 的算子表是纯占内存。
    ///
    /// 出了保留窗口就摘掉 `<canvas>` 与 `.textLayer`，并调 `release_page` ——
    /// 位图、DOM、算子表一起还给系统。
    pub keep: bool,
    /// 当前检索词，用于在文本层里高亮命中。
    pub query: String,
    /// 当前选中的命中 key。
    pub active_key: String,
    pub highlights: Vec<Highlight>,
    pub on_focus_highlight: Callback<String>,
    /// 在一条批注上**右击**了：`(批注 id, 样式, 视口 x, 视口 y)`。
    ///
    /// 只在真的命中标注时触发 —— 右击正文空白处不会发，也不会拦掉系统菜单。
    pub on_mark_context: Callback<(String, MarkKind, f64, f64)>,
}

/// 本页一条标注的命中索引。
///
/// 把 `Highlight` 摊平成「id + 批注原文 + 样式 + 矩形」，让 `mousemove` 和
/// `contextmenu` 两个处理里只剩纯几何 —— 两处各自去 `props.highlights` 里翻，
/// 迟早会漂（比如一处忘了 `item.page == props.page` 的过滤）。
#[derive(Clone, PartialEq)]
struct MarkHit {
    id: String,
    /// 用户写的批注原文。**空 = 这条标注没批注**（见 [`MarkHit::tip`]）。
    note: String,
    kind: MarkKind,
    rects: Vec<Rect>,
}

impl MarkHit {
    /// 页面内归一化坐标是否落在这条标注上 —— **纯几何**，与有没有批注无关。
    ///
    /// ⚠️ 这里**故意**不看 `note`。右击菜单必须在「只有样式、还没写批注」的
    /// 标注上也能弹出来 ——「删除高亮 / 下划线」这类动作正是给它们准备的，
    /// 而刚标完一条标注时它**必然**还没有批注。
    /// 悬停提示另走 [`MarkHit::tip`]，两者**不能**共用一个"算不算命中"的判据。
    fn contains(&self, px: f64, py: f64) -> bool {
        self.rects
            .iter()
            .any(|r| px >= r.x && px <= r.x + r.w && py >= r.y && py <= r.y + r.h)
    }

    /// 悬停提示要显示的内容 —— **只有写过批注才有**。
    ///
    /// 没批注就返回 `None`，鼠标划过时**什么都不显示**。
    /// （早先这里会退回被标注的那段原文，于是每条标注划过去都冒一个黑框，
    /// 用户明确要求去掉：提示框只在真有话要说的时候出现。）
    fn tip(&self) -> Option<&str> {
        let note = self.note.trim();
        if note.is_empty() {
            None
        } else {
            Some(note)
        }
    }
}

/// 把鼠标的视口坐标换算成「页面内的归一化坐标」——
/// 和 `Highlight::rects` 是同一套坐标系。
///
/// 拿不到页面尺寸（还没布局出来）时返回 `None`，调用方直接放弃这一帧。
fn normalize_point(page_el: &web_sys::Element, event: &MouseEvent) -> Option<(f64, f64)> {
    let box_ = page_el.get_bounding_client_rect();
    if box_.width() <= 0.0 || box_.height() <= 0.0 {
        return None;
    }
    Some((
        (event.client_x() as f64 - box_.left()) / box_.width(),
        (event.client_y() as f64 - box_.top()) / box_.height(),
    ))
}

/// 悬停提示：显示什么内容、摆在哪儿（**viewport 坐标**）。
///
/// 坐标用 viewport 而不是页面内百分比，是因为提示框用 `position: fixed` ——
/// 这样它不会被 `.page` 的 `overflow: hidden` 裁掉（`.page` 要裁是为了
/// 让 canvas 的圆角干净）。
#[derive(Clone, PartialEq)]
struct Tip {
    text: String,
    x: f64,
    y: f64,
    /// 页面太靠上、上面放不下时，改挂在标注**下方**。
    below: bool,
}

/// 当前有没有正在拖选文字。
///
/// 有的话就不弹提示：拖选经常要划过已经标过的文字，一边选一边闪提示很吵。
fn has_text_selection() -> bool {
    web_sys::window()
        .and_then(|window| window.document())
        .and_then(|document| document.get_selection().ok().flatten())
        .map(|selection| !selection.is_collapsed())
        .unwrap_or(false)
}

#[function_component(PageView)]
pub fn page_view(props: &PageViewProps) -> Html {
    let canvas_id = format!("folia-canvas-{}", props.page);
    let text_id = format!("folia-text-{}", props.page);

    // `.page` 自己。悬停命中测试要用它的 bounding rect 把鼠标坐标换算成
    // 「页面内的归一化坐标」—— 和 `Highlight::rects` 是同一套坐标系。
    let page_ref = use_node_ref();

    // 本页渲染任务的代次。见下面那个 effect —— 缩放 / 检索词连续变化时，
    // 会有多个 `render_page` 任务同时在飞，"先发的后回"会把 canvas 画回**旧缩放**，
    // 或者把**旧文本层**贴到刚画好的新画面上（文本层和 canvas 必须同源同缩放，
    // 不然选中位置会整体偏掉）。
    let render_gen = use_mut_ref(|| 0_u32);

    // 这一页的 canvas 有没有真的画过东西。
    //
    // 只有画过才需要"释放" —— 首次挂载时绝大多数页 `keep = false`，
    // 没有这个标志就会对整本 PDF 每页发一次 `release_page`（1000 页 = 1000 次
    // 跨 wasm 边界的调用），而它们本来就没占用什么。
    let painted = use_mut_ref(|| false);

    {
        let canvas_id = canvas_id.clone();
        let text_id = text_id.clone();
        let render_gen = render_gen.clone();
        let painted = painted.clone();
        let deps = (
            props.active,
            props.keep,
            props.page,
            props.scale,
            props.query.clone(),
            props.active_key.clone(),
        );
        use_effect_with(
            deps,
            move |(active, keep, page, scale, query, active_key)| {
                // 每跑一次就推一格代次，让上一轮那个还在飞的渲染任务作废。
                let generation = {
                    let mut current = render_gen.borrow_mut();
                    *current += 1;
                    *current
                };
                if *active {
                    *painted.borrow_mut() = true;
                    let canvas_id = canvas_id.clone();
                    let text_id = text_id.clone();
                    let page = *page;
                    let scale = *scale;
                    let query = query.clone();
                    let active_key = active_key.clone();
                    let render_gen = render_gen.clone();
                    spawn_local(async move {
                        if *render_gen.borrow() != generation {
                            return;
                        }
                        let _ = pdf::render_page(&canvas_id, page, scale).await;
                        // 画完 canvas 再对照一次 —— 这中间可能又来了新的一轮。
                        // 少了这一句，旧的文本层会盖在**新缩放**的 canvas 上。
                        if *render_gen.borrow() != generation {
                            return;
                        }
                        let _ = pdf::render_text_layer(&text_id, page, scale, &query, &active_key)
                            .await;
                    });
                } else if !*keep && *painted.borrow() {
                    // 滚出保留窗口：把这一页在 pdf.js 侧的算子表放掉。
                    // canvas 与文本层已经跟着 `keep` 从渲染树里摘掉了 ——
                    // 位图和 DOM 由浏览器回收，不需要手工 clear。
                    *painted.borrow_mut() = false;
                    let page = *page;
                    spawn_local(async move {
                        let _ = pdf::release_page(page).await;
                    });
                }
                || {}
            },
        );
    }

    // ---- 悬停提示 + 右击菜单 ----
    //
    // 为什么不给标注挂 `onmouseenter`：`.hl-layer` 是 `pointer-events: none`，
    // 而且**文本层（z-index 2）压在标注层（z-index 1）上面** ——
    // 想收鼠标事件就得给标注开 `pointer-events: auto`，那样已经标过的文字
    // 就没法拖选、没法再标第二遍了。所以改成在 `.page` 上听 mousemove，
    // 拿坐标自己做矩形命中测试：选字完全不受影响。
    //
    // 右击（contextmenu）走**同一套几何命中**，理由一模一样 ——
    // 同样不能给标注开 `pointer-events`。
    // 但"算不算命中"之后的**判据不同**：悬停还要再看有没有批注（没批注不弹框），
    // 右击只要命中就弹菜单。见 `MarkHit::contains` 与 `MarkHit::tip`。
    let tip = use_state(|| None::<Tip>);

    let hits: Vec<MarkHit> = props
        .highlights
        .iter()
        .filter(|item| item.page == props.page)
        .map(|item| MarkHit {
            id: item.id.clone(),
            note: item.note.clone(),
            kind: item.kind,
            rects: item.rects.clone(),
        })
        .collect();

    // 这一页有没有**带批注**的标注。一条都没有的话悬停永远不该出提示框，
    // 后面那套坐标换算也就可以整个省掉。
    let has_any_note = hits.iter().any(|hit| hit.tip().is_some());

    // 标注被删掉之后，已经挂在屏幕上的提示框还写着那条批注的内容 ——
    // 下一次 mousemove 才会重算。手动把它收掉，否则会留一条"幽灵提示"。
    {
        let tip = tip.clone();
        let dep = props.highlights.clone();
        use_effect_with(dep, move |_| {
            tip.set(None);
            || {}
        });
    }

    let on_mousemove = {
        let tip = tip.clone();
        let page_ref = page_ref.clone();
        let hits = hits.clone();

        Callback::from(move |event: MouseEvent| {
            if !has_any_note || has_text_selection() {
                if tip.is_some() {
                    tip.set(None);
                }
                return;
            }
            let Some(page_el) = page_ref.cast::<web_sys::Element>() else {
                return;
            };
            let Some((px, py)) = normalize_point(&page_el, &event) else {
                return;
            };
            // 命中了**并且**写过批注才提示 —— 只命中样式、还没批注的不提示。
            let hit = hits
                .iter()
                .filter(|hit| hit.contains(px, py))
                .find_map(|hit| hit.tip().map(|text| (hit, text)));

            let box_ = page_el.get_bounding_client_rect();
            let next = hit.map(|(hit, text)| {
                // 挂在**第一个矩形**的上方中点；拿不到矩形就退回鼠标位置。
                let (top, bottom, cx) = match hit.rects.first() {
                    Some(r) => (
                        box_.top() + r.y * box_.height(),
                        box_.top() + (r.y + r.h) * box_.height(),
                        box_.left() + (r.x + r.w / 2.0) * box_.width(),
                    ),
                    None => {
                        let y = event.client_y() as f64;
                        (y, y, event.client_x() as f64)
                    }
                };
                // 贴着视口顶边时上面放不下（会顶出屏幕），改挂下方。
                let below = top < 64.0;
                Tip {
                    text: text.to_string(),
                    x: cx,
                    y: if below { bottom } else { top },
                    below,
                }
            });

            // 鼠标在同一段标注里移动时不要每帧都 set —— 值没变就别触发重渲染。
            if (*tip).as_ref() != next.as_ref() {
                tip.set(next);
            }
        })
    };

    // 右击一条批注 → 交给上层弹菜单（「编辑批注」/「删除<样式>」）。
    //
    // ⚠️ 这一路**不看 `note`**（只走 `contains` 的纯几何命中）：
    // 没写批注的标注也要能右击删掉。悬停提示那边是另一套判据，见 `on_mousemove`。
    let on_contextmenu = {
        let on_mark_context = props.on_mark_context.clone();
        let page_ref = page_ref.clone();
        let hits = hits.clone();
        Callback::from(move |event: MouseEvent| {
            if hits.is_empty() {
                return;
            }
            let Some(page_el) = page_ref.cast::<web_sys::Element>() else {
                return;
            };
            let Some((px, py)) = normalize_point(&page_el, &event) else {
                return;
            };
            let Some(hit) = hits.iter().find(|hit| hit.contains(px, py)) else {
                // 右击正文空白处：这里什么都不做。
                // （系统默认菜单由 `app.rs` 里那个 document 级兜底统一关掉了 ——
                //  这一层的职责只有"要不要开自己的菜单"。）
                return;
            };
            // 命中了才开菜单。这里的 `prevent_default` 和上面那个兜底是**双保险**：
            // 兜底保证默认菜单不会冒出来，这一句保证即使兜底被收窄也不会叠一层。
            event.prevent_default();
            on_mark_context.emit((
                hit.id.clone(),
                hit.kind,
                event.client_x() as f64,
                event.client_y() as f64,
            ));
        })
    };

    let on_mouseleave = {
        let tip = tip.clone();
        Callback::from(move |_: MouseEvent| {
            if tip.is_some() {
                tip.set(None);
            }
        })
    };

    let page_style = format!("width:{:.2}px;height:{:.2}px;", props.width, props.height);

    let overlays: Html = props
        .highlights
        .iter()
        .filter(|item| item.page == props.page)
        .map(|item| {
            // 标记自己的类：`.folia-mark` 负责定位与 `mix-blend-mode: multiply`，
            // `folia-mark-{kind}` 决定画成填充还是线，`folia-hl-{color}` 决定颜色。
            let class = format!(
                "folia-mark folia-mark-{} folia-hl-{}",
                item.kind.css(),
                item.color
            );
            // 徽标**不能**跟着挂 `.folia-hl`：那会把 `mix-blend-mode: multiply` 也继承过去，
            // 而徽标正好压在高亮框左上角（`translate(-50%, -50%)`），
            // 于是左半边跟淡色高亮相乘、右半边跟白页相乘 —— 同一个圆点显出两种深浅。
            let badge_class = format!("folia-hl-badge folia-hl-{}", item.color);
            let rects: Html = item
                .rects
                .iter()
                .map(|rect| {
                    let style = format!(
                        "left:{:.4}%;top:{:.4}%;width:{:.4}%;height:{:.4}%;",
                        rect.x * 100.0,
                        rect.y * 100.0,
                        rect.w * 100.0,
                        rect.h * 100.0
                    );
                    html! { <div class={class.clone()} style={style} /> }
                })
                .collect();

            let badge: Html = match item.rects.first() {
                Some(rect) => {
                    let style = format!("left:{:.4}%;top:{:.4}%;", rect.x * 100.0, rect.y * 100.0);
                    let id = item.id.clone();
                    let on_focus = props.on_focus_highlight.clone();
                    let onclick = Callback::from(move |_: MouseEvent| on_focus.emit(id.clone()));
                    // 这个小圆点的 `title` 是**原生**悬浮提示（浏览器画的，不是 `.mark-tip`）。
                    // 没批注时说「添加批注」而不是「查看批注」—— 点下去打开的是**空**输入框，
                    // 说「查看」会让人以为里面有东西。判空用 `trim()`，和 `MarkHit::tip` 一致。
                    let note = item.note.trim();
                    let title = if note.is_empty() {
                        "添加批注".to_string()
                    } else {
                        note.to_string()
                    };
                    html! {
                        <button class={badge_class.clone()} {style} {onclick} {title} />
                    }
                }
                None => html! {},
            };

            html! { <>{ rects }{ badge }</> }
        })
        .collect();

    // 提示框。`pointer-events: none` 保证它不会把鼠标从页面上抢走，
    // 否则框一出现就会触发 mouseleave → 闪一下没了。
    let tip_html: Html = match &*tip {
        Some(tip) => {
            let class = if tip.below {
                "mark-tip below"
            } else {
                "mark-tip"
            };
            html! {
                <div class={class} style={format!("left:{:.1}px;top:{:.1}px;", tip.x, tip.y)}>
                    { &tip.text }
                </div>
            }
        }
        None => html! {},
    };

    // 只有「保留窗口」内的页才挂 canvas 与文本层 —— 这是本文件里最大的一处内存开关。
    //
    // 两样都是大头：canvas 的位图 = 宽 × 高 × dpr² × 4（A4 页在缩放 1.25、dpr 1.5
    // 下约 6.7 MB），文本层是每页上千个 `<span>`。原来**每一页都挂**，于是滚过去的
    // 页位图再也不还 —— 翻 30 页就是几百 MB，而且还在长。
    //
    // 摘掉元素就等于把位图与 DOM 一起交给浏览器回收，比手工 `canvas.width = 0`
    // 干净：后者还要自己维护"CSS 尺寸留着、位图清掉"的不一致状态。
    let page_body: Html = if props.keep || props.active {
        html! {
            <>
                <canvas id={canvas_id.clone()} class="page-canvas" />
                // 深色模式下「保持原色」的图片层：空容器，内容由 `bridge.js`
                // 在渲染完之后填（把图片那几块的原色像素从主 canvas 抠过来）。
                // 主 canvas 会被 CSS 整体反相，位图图片只有挪进这一层才躲得开 ——
                // 详见 `public/pdfjs/bridge.js` 的「深色模式下的图片保护」。
                <div class="page-image-layer" />
                <div id={text_id.clone()} class="textLayer" />
            </>
        }
    } else {
        html! {}
    };

    html! {
        <div ref={page_ref} class="page" data-page={props.page.to_string()} style={page_style}
            onmousemove={on_mousemove} onmouseleave={on_mouseleave}
            oncontextmenu={on_contextmenu}>
            { page_body }
            <div class="hl-layer">{ overlays }</div>
            { tip_html }
        </div>
    }
}
