//! 连续滚动的页面布局计算（虚拟化的基础）。

use yew::NodeRef;

use crate::pdf::types::{PageSize, SearchHit};

/// 页面之间的垂直间距（px）。
pub const PAGE_GAP: f64 = 18.0;

/// 「保留窗口」比「可视窗口」每侧多出的页数。
///
/// 可视窗口（[`Layout::visible_range`]）决定**渲染**哪几页；保留窗口决定
/// **留住**哪几页的 canvas 位图与 pdf.js 算子表。两者分开是因为代价完全不同：
///
/// - canvas 位图 = 宽 × 高 × dpr² × 4，A4 页在缩放 1.25、dpr 1.5 下约 6.7 MB；
/// - pdf.js 的算子表按页常驻在 worker 里（只有 `destroy()` 才会清）。
///
/// 原来只有"可视窗口"一个概念，于是**每一页都挂着 canvas**、滚过去的页位图
/// 再也不还 —— 翻 30 页就是几百 MB。
///
/// 多留 2 页是给"来回滚"用的：往回滚一两页时位图还在，不用重渲染；再远一点
/// 才需要重新跑一遍 `getOperatorList` + 画 canvas。而 `visible_range` 本身
/// 已经带了 600px 的缓冲区，所以保留窗口里那些**没在渲染**的页至少离视口
/// 600px —— 摘掉它的 canvas 不会被人看见。
pub const KEEP_BAND: usize = 2;

/// 预计算的页面偏移表，用于快速定位可视页与滚动跳转。
#[derive(Debug, Clone, PartialEq)]
pub struct Layout {
    pub heights: Vec<f64>,
    pub offsets: Vec<f64>,
    pub total: f64,
}

impl Layout {
    /// 由「可视窗口」推出「保留窗口」（闭区间，0-based）。
    ///
    /// 调用方保证 `visible` 已经是闭区间且有序；空文档返回 `(0, 0)`。
    pub fn keep_range(&self, visible: (usize, usize)) -> (usize, usize) {
        if self.offsets.is_empty() {
            return (0, 0);
        }
        let last = self.offsets.len() - 1;
        (
            visible.0.saturating_sub(KEEP_BAND),
            visible.1.saturating_add(KEEP_BAND).min(last),
        )
    }
    pub fn new(sizes: &[PageSize], scale: f64) -> Self {
        let mut heights = Vec::with_capacity(sizes.len());
        let mut offsets = Vec::with_capacity(sizes.len());
        let mut cursor = 0.0;
        for size in sizes {
            offsets.push(cursor);
            let height = size.height * scale;
            heights.push(height);
            cursor += height + PAGE_GAP;
        }
        let total = if sizes.is_empty() {
            0.0
        } else {
            (cursor - PAGE_GAP).max(0.0)
        };
        Self {
            heights,
            offsets,
            total,
        }
    }

    pub fn len(&self) -> usize {
        self.offsets.len()
    }

    /// 页面的顶部偏移（1-based 页码）。
    pub fn offset_of(&self, page: u32) -> f64 {
        let index = page.saturating_sub(1) as usize;
        self.offsets.get(index).copied().unwrap_or(0.0)
    }

    /// 某个纵向位置落在第几页（1-based）。
    pub fn page_at(&self, y: f64) -> u32 {
        for (index, offset) in self.offsets.iter().enumerate() {
            if y < offset + self.heights[index] + PAGE_GAP * 0.5 {
                return (index + 1) as u32;
            }
        }
        self.offsets.len().max(1) as u32
    }

    /// 返回需要渲染的页面索引区间（闭区间，0-based）。
    pub fn visible_range(&self, top: f64, viewport: f64, buffer: f64) -> (usize, usize) {
        if self.offsets.is_empty() {
            return (0, 0);
        }
        let low = top - buffer;
        let high = top + viewport + buffer;
        let mut start: Option<usize> = None;
        let mut end = 0usize;
        for index in 0..self.offsets.len() {
            let page_top = self.offsets[index];
            let page_bottom = page_top + self.heights[index];
            if page_bottom <= low {
                continue;
            }
            if page_top > high {
                break;
            }
            if start.is_none() {
                start = Some(index);
            }
            end = index;
        }
        match start {
            Some(start) => (start, end),
            None => {
                // 视口完全落在页间空隙时，退化为最近的一页。
                let nearest = self.page_at(top).saturating_sub(1) as usize;
                (nearest, nearest)
            }
        }
    }
}

/// 阅读区左右两侧留给页面阴影与呼吸感的总宽度（px）。
pub const FIT_PADDING: f64 = 64.0;

/// 按「这一页的宽度铺满阅读区」反算缩放值；拿不到可用宽度时返回 `None`。
///
/// 工具栏的「适宽」按钮和「新文档打开自动适宽」共用它 ——
/// 两处各写一份迟早会漂移。
pub fn fit_scale(scroll_ref: &NodeRef, page_width: f64) -> Option<f64> {
    if page_width <= 0.0 {
        return None;
    }
    let available = scroll_ref
        .cast::<web_sys::Element>()
        .map(|element| element.client_width() as f64 - FIT_PADDING)
        .unwrap_or(760.0);
    if available <= 60.0 {
        return None;
    }
    Some((available / page_width).clamp(0.25, 5.0))
}

/// 把阅读区滚动到指定页面的顶部。
pub fn scroll_to_page(scroll_ref: &NodeRef, layout: &Layout, page: u32) {
    if let Some(element) = scroll_ref.cast::<web_sys::Element>() {
        let top = (layout.offset_of(page) - 12.0).max(0.0);
        element.set_scroll_top(top as i32);
    }
}

/// 滚动到某条检索命中的位置（保留一点上方留白）。
pub fn scroll_to_hit(scroll_ref: &NodeRef, layout: &Layout, hit: &SearchHit) {
    if let Some(element) = scroll_ref.cast::<web_sys::Element>() {
        let index = hit.page.saturating_sub(1) as usize;
        let page_height = layout.heights.get(index).copied().unwrap_or(0.0);
        let top = (layout.offset_of(hit.page) + hit.y * page_height - 90.0).max(0.0);
        element.set_scroll_top(top as i32);
    }
}
