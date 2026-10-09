//! 文档标签页的领域类型：一份已打开文档的全部状态，
//! 以及「把当前选区写成一条批注」这条路径。
//!
//! 这里只有数据与纯逻辑，不碰 pdf.js 之外的外部世界，也不做渲染。

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::atomic::{AtomicU64, Ordering};

use web_sys::MouseEvent;
use yew::prelude::*;

use crate::components::sidebar::SidebarTab;
use crate::pdf;
use crate::pdf::types::{
    DocMeta, Highlight, MarkKind, OutlineNode, PageSize, SearchHit, SelectionInfo,
};
use crate::selbar::{SelBar, SelBox};
use crate::state::history::History;

/// 上次退出时记录的阅读位置，用于重新打开同一份文档时恢复。
#[derive(Clone)]
pub(crate) struct RestoreTarget {
    pub(crate) path: String,
    pub(crate) page: u32,
    pub(crate) scale: f64,
}

/// 写入一条批注要用到的一堆句柄。
///
/// 打包起来是因为四种样式（高亮 / 下划线 / 删划线 / 波浪线）走的是同一条路 ——
/// 唯一的区别就是 `Highlight.kind`。不打包就得把同一段闭包抄四遍。
#[derive(Clone)]
pub(crate) struct MarkCtx {
    pub(crate) annotations: UseStateHandle<Vec<Highlight>>,
    /// 见 `Handles::annotations_ok`。用户亲手加的批注一定是可信的。
    pub(crate) annotations_ok: Rc<RefCell<bool>>,
    pub(crate) history: History,
    pub(crate) color: UseStateHandle<String>,
    pub(crate) status: UseStateHandle<Option<String>>,
    pub(crate) sidebar_tab: UseStateHandle<SidebarTab>,
    pub(crate) sidebar_open: UseStateHandle<bool>,
    pub(crate) has_selection: UseStateHandle<bool>,
    pub(crate) sel_box: Rc<RefCell<Option<SelBox>>>,
    pub(crate) sel_bar: UseStateHandle<Option<SelBar>>,
}

/// 生成标注 id 用的自增序号。
///
/// 只靠 `Date::now()` 会撞：同一毫秒内在同一页连标两下，两个 id 完全相同 ——
/// 而「删除」是按 id 过滤的，于是一删删两条，`active_highlight` 也会一起指错。
/// 加一个单调递增的序号，成本为零，且不依赖时钟精度。
pub(crate) static MARK_SEQ: AtomicU64 = AtomicU64::new(0);

pub(crate) fn next_mark_id(page: u32, now: f64) -> String {
    let seq = MARK_SEQ.fetch_add(1, Ordering::Relaxed);
    format!("hl-{page}-{}-{seq}", now as u64)
}

/// 「把当前选区标成 `kind`」的回调。
pub(crate) fn mark_callback(ctx: MarkCtx, kind: MarkKind) -> Callback<MouseEvent> {
    Callback::from(move |_: MouseEvent| {
        let Some(raw) = pdf::selection_info() else {
            ctx.status.set(Some("请先在正文中选中要标注的文字".into()));
            return;
        };
        let Ok(info) = serde_json::from_str::<SelectionInfo>(&raw) else {
            return;
        };
        let now = js_sys::Date::now();
        let mut list = (*ctx.annotations).clone();
        ctx.history.push(&list);
        list.push(Highlight {
            id: next_mark_id(info.page, now),
            page: info.page,
            color: (*ctx.color).clone(),
            text: info.text,
            note: String::new(),
            kind,
            rects: info.rects,
            created_at: now,
        });
        ctx.annotations.set(list);
        // 用户动过批注了 —— 这份列表现在一定可信，可以往磁盘写。
        *ctx.annotations_ok.borrow_mut() = true;
        pdf::clear_selection();
        // 选区已经没了：浮层要收起来，工具栏的按钮也要跟着复位，
        // 不然它们会一直亮着，看起来像还能再标一次。
        ctx.has_selection.set(false);
        *ctx.sel_box.borrow_mut() = None;
        ctx.sel_bar.set(None);
        ctx.sidebar_tab.set(SidebarTab::Annotations);
        ctx.sidebar_open.set(true);
    })
}

/// 一个标签页：一份已打开的文档 + 它自己的阅读状态。
///
/// pdf.js 侧同一时刻只有**一份**文档是活的，所以切标签的做法是：
/// 把当前标签的实时状态存回这里 → 把目标标签的状态灌回实时句柄 →
/// 重新把目标 PDF 载入 pdf.js。
///
/// `sizes` / `outline` / `annotations` 在打开时取一次就缓存住，
/// 切回来时不必重新解析（`getPageSizes` 要遍历上百页，是打开流程里最贵的一步）。
#[derive(Clone)]
pub(crate) struct DocTab {
    pub(crate) meta: DocMeta,
    pub(crate) sizes: Vec<PageSize>,
    pub(crate) outline: Vec<OutlineNode>,
    pub(crate) annotations: Vec<Highlight>,
    /// `annotations` 是不是**可信**的（读成功过，或被用户改过）。
    ///
    /// 读失败 / 解析失败时我们只能拿一个空列表去渲染，但这个空列表**绝不能**写回磁盘
    /// —— 那等于把用户的批注一次性抹掉。这个标志一路传到存盘那一环，见
    /// [`Handles::annotations_ok`]。
    pub(crate) annotations_ok: bool,
    /// 阅读位置
    pub(crate) page: u32,
    pub(crate) scale: f64,
    /// 检索状态
    pub(crate) query: String,
    pub(crate) hits: Vec<SearchHit>,
    pub(crate) active_hit: Option<usize>,
    /// 侧栏缩略图缓存（按页码），切回来不用重新渲染。
    ///
    /// 条数受 `handles::PAGE_THUMB_LIMIT` 约束（那是**全局**上限，切标签时整份
    /// 搬过来，所以这里不会比它更大）—— 一个标签最多留 60 张 ≈ 750 KB。
    pub(crate) thumbs: HashMap<u32, String>,
}
