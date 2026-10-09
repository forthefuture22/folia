//! 「选中文字后浮出的工具栏」该落在哪儿。
//!
//! 这里刻意不碰 DOM：输入全是数字，于是「贴上还是贴下」「别贴边溢出去」这些
//! 容易写错的分支可以用普通 `rustc --test src/selbar.rs` 直接跑，
//! 不必把整个 wasm 应用连同 Tauri 后端一起拉起来。
//!
//! 坐标一律是**正文滚动容器的内容坐标**（不是屏幕坐标）——
//! 工具栏是容器的绝对定位子元素，于是它跟着正文一起滚，
//! 滚动时只需要重算「翻不翻面」，不需要重新定位。

/// 浮层工具栏的尺寸估算。
///
/// 只用来决定「贴选区上方还是下方」和「别贴着容器边缘溢出去」，
/// 差几个像素在视觉上看不出来。这里的数字是**实测值**：
/// 无头 Chrome 里量出来的 `.sel-bar` 就是 173 × 34（见 README「选中文字的浮层工具栏」），
/// 所以贴边时的夹取是精确的、不留余量。改 `.sel-bar` 的样式要回来重测。
///
/// 尺寸随按钮数走：工具栏上是 4 个样式按钮 + 1 个复制按钮（每个 28px 宽、
/// 26px 高）外加分隔线与内边距，所以比只有一个按钮时宽不少。
pub const SEL_BAR_W: f64 = 173.0;
pub const SEL_BAR_H: f64 = 34.0;
/// 工具栏与选区之间的空隙，以及离容器可视边缘的安全边距。
pub const SEL_BAR_GAP: f64 = 8.0;
pub const SEL_BAR_EDGE: f64 = 8.0;

/// 浮层工具栏的落点。
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct SelBar {
    /// 工具栏**中心**的横坐标。
    ///
    /// 给中心而不是左上角：CSS 侧一句 `translateX(-50%)` 就能把它折回去，
    /// 于是「以选区中心对齐」只需要一个数字，不必先知道工具栏的真实宽度。
    pub left: f64,
    pub top: f64,
}

/// 当前选区在容器内容坐标里的包围盒。
#[derive(Debug, Clone, Copy)]
pub struct SelBox {
    pub left: f64,
    pub right: f64,
    pub top: f64,
    pub bottom: f64,
}

/// 正文滚动容器此刻「看得见」的那一块（内容坐标原点 + 可视尺寸）。
#[derive(Debug, Clone, Copy)]
pub struct StageView {
    pub top: f64,
    pub left: f64,
    pub width: f64,
    pub height: f64,
}

/// 把选区包围盒换算成工具栏的落点。
///
/// 默认贴在选区**上方**（阅读器的惯例：手和光标都在选区下面，别挡住刚选完的字）；
/// 上方放不下就翻到下方；两边都放不下（选区高过一屏）就贴住可视区顶部。
pub fn resolve_sel_bar(box_: SelBox, view: StageView) -> SelBar {
    let above = box_.top - SEL_BAR_GAP - SEL_BAR_H;
    let below = box_.bottom + SEL_BAR_GAP;
    let top = if above >= view.top + SEL_BAR_EDGE {
        above
    } else if below + SEL_BAR_H <= view.top + view.height - SEL_BAR_EDGE {
        below
    } else {
        view.top + SEL_BAR_EDGE
    };

    // 横向以选区中心对齐，再夹进可视范围 —— 夹取用的宽度是估算值，
    // 但能保证放大到需要横向滚动时，工具栏不会跟着选区跑到屏幕外面去。
    let half = SEL_BAR_W / 2.0;
    let centre = (box_.left + box_.right) / 2.0;
    let left = centre
        .min(view.left + view.width - SEL_BAR_EDGE - half)
        .max(view.left + SEL_BAR_EDGE + half);

    SelBar { left, top }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 一个 800×600 的容器，没滚动。
    fn view() -> StageView {
        StageView {
            top: 0.0,
            left: 0.0,
            width: 600.0,
            height: 800.0,
        }
    }

    /// 页面正中一段单行选区。
    fn box_at(top: f64, height: f64) -> SelBox {
        SelBox {
            left: 100.0,
            right: 200.0,
            top,
            bottom: top + height,
        }
    }

    #[test]
    fn sits_above_the_selection() {
        let bar = resolve_sel_bar(box_at(300.0, 18.0), view());
        // 上边 = 选区上边 - 空隙 - 工具栏高
        assert_eq!(bar.top, 300.0 - SEL_BAR_GAP - SEL_BAR_H);
        // 横向以选区中心对齐
        assert_eq!(bar.left, 150.0);
    }

    #[test]
    fn flips_below_when_there_is_no_room_above() {
        // 选区贴着可视区顶部：上方只剩 20px，放不下一个 34px 高的工具栏。
        let bar = resolve_sel_bar(box_at(20.0, 18.0), view());
        assert_eq!(bar.top, 38.0 + SEL_BAR_GAP);
        // 而且不能盖住选区
        assert!(bar.top >= 38.0);
        // 顺带把「34px 放不下」这件事钉住：上面的边距 20 < 工具栏高 + 空隙。
        // 写成 `const` 块，编译期就断言掉，省一条运行时指令。
        const { assert!(20.0 < SEL_BAR_H + SEL_BAR_GAP) };
    }

    #[test]
    fn scrolling_can_make_it_flip() {
        let box_ = box_at(100.0, 18.0);
        // 没滚动时上方有 100px，够放。
        assert_eq!(
            resolve_sel_bar(box_, view()).top,
            100.0 - SEL_BAR_GAP - SEL_BAR_H
        );
        // 往下滚 80px：选区在屏幕上只剩 20px 的上方空间，得翻到下面。
        let scrolled = StageView {
            top: 80.0,
            ..view()
        };
        assert_eq!(resolve_sel_bar(box_, scrolled).top, 118.0 + SEL_BAR_GAP);
    }

    #[test]
    fn falls_back_to_the_top_edge_when_neither_side_fits() {
        // 选区比一屏还高：上下都没地方，贴住可视区顶部而不是跑到屏幕外。
        let tall = SelBox {
            left: 100.0,
            right: 200.0,
            top: 0.0,
            bottom: 900.0,
        };
        assert_eq!(resolve_sel_bar(tall, view()).top, SEL_BAR_EDGE);
    }

    #[test]
    fn clamps_into_view_on_both_sides() {
        let half = SEL_BAR_W / 2.0;

        // 选区贴着左边缘：工具栏不能跟着越界，夹到「半个宽度 + 边距」。
        let left_edge = SelBox {
            left: 0.0,
            right: 20.0,
            top: 300.0,
            bottom: 318.0,
        };
        assert_eq!(resolve_sel_bar(left_edge, view()).left, SEL_BAR_EDGE + half);

        // 贴着右边缘同理。
        let right_edge = SelBox {
            left: 580.0,
            right: 600.0,
            top: 300.0,
            bottom: 318.0,
        };
        assert_eq!(
            resolve_sel_bar(right_edge, view()).left,
            600.0 - SEL_BAR_EDGE - half
        );
    }

    #[test]
    fn clamp_follows_horizontal_scrolling() {
        // 横向滚到 300：可视的内容横区间是 [300, 900]，
        // 选区在可视区最左边时工具栏要夹到 300 + 半个宽度 + 边距。
        let scrolled = StageView {
            left: 300.0,
            ..view()
        };
        let box_ = SelBox {
            left: 310.0,
            right: 330.0,
            top: 300.0,
            bottom: 318.0,
        };
        assert_eq!(
            resolve_sel_bar(box_, scrolled).left,
            300.0 + SEL_BAR_EDGE + SEL_BAR_W / 2.0
        );
    }

    #[test]
    fn never_returns_nan_on_a_degenerate_container() {
        // 容器宽度小于工具栏宽度时「上界 < 下界」，夹取会失效 ——
        // 至少不能变成 NaN 或把工具栏扔到负坐标去。
        let tiny = StageView {
            top: 0.0,
            left: 0.0,
            width: 100.0,
            height: 800.0,
        };
        let bar = resolve_sel_bar(box_at(300.0, 18.0), tiny);
        assert!(bar.left.is_finite() && bar.left > 0.0);
    }
}
