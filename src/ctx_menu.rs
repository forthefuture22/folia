//! 右键菜单的数据模型与定位几何。
//!
//! 书架卡片和正文批注共用**同一套**菜单（样式、夹取、关闭时机、Esc 全都一样），
//! 只是条目不同 —— 所以用一个枚举，而不是两份 `use_state`。
//! 两份的话"Esc 该关哪个""点别处关哪个""两个同时开着怎么办"
//! 每一处都要各写一遍，迟早漏一处（漏的那处表现就是"菜单关不掉"）。
//!
//! 这个模块只负责「菜单开在哪、有多大、是什么」——**不渲染**。
//! 渲染在 `app.rs`（因为要挂一堆回调），尺寸常量在这里（因为定位要先知道它多大）。

use wasm_bindgen::JsValue;

use crate::pdf::types::{MarkKind, RecentFile};

/// 右键菜单当前开着哪一个、开在哪儿；`None` = 没开。
///
/// 打开的那一刻就把要用的数据**快照**进来，而不是只存个 id、渲染时再去查：
/// 菜单项点下去会立刻改数据源，查出来的值会在菜单还开着的时候变，文案跟着闪。
#[derive(Clone, PartialEq)]
pub enum CtxMenu {
    /// 书架卡片。快照整个 `RecentFile`（「收藏 / 取消收藏」的文案要读它）。
    Card { item: RecentFile, x: f64, y: f64 },
    /// 正文里的一条批注。快照 id 与样式（「删除下划线」这种文案要读它）。
    Mark {
        id: String,
        kind: MarkKind,
        x: f64,
        y: f64,
    },
}

// `CtxMenu` 不再有辅助方法：条目数由 `CARD_MENU_ITEMS` / `MARK_MENU_ITEMS`
// 两个常量各管一个（夹取时用），渲染时的条数由各自的 `html!` 决定 ——
// 这两边对不对得上，靠 `/tmp/e2e27` 的"常量守卫"判据（拿渲染出来的真实高度
// 和常量比）钉住，不靠一个只在注释里活着的方法。

/// 右键菜单的尺寸常量。
///
/// 定位时要把菜单夹进可视区（贴着右下角右击不能溢出屏幕），就得先知道它多大。
/// 这几个数**必须和 `styles.css` 里的 `.ctx-menu` / `.ctx-menu-item` 对上** ——
/// `/tmp/e2e27` 有一条"常量守卫"判据会把渲染出来的真实尺寸和它们比对，
/// 改了一边忘了另一边会立刻变红。
pub const MENU_W: f64 = 208.0;
/// 单项高度（`.ctx-menu-item { height: 32px }`，纵向无内边距）。
pub const MENU_ITEM_H: f64 = 32.0;
/// 菜单容器上下内边距之和（`.ctx-menu { padding: 6px }`）。
pub const MENU_PAD: f64 = 12.0;
/// 分隔线的厚度（`.ctx-menu-sep { height: 1px }`）。
pub const MENU_SEP_H: f64 = 1.0;
/// 菜单容器上下边框之和（`.ctx-menu { border: 1px solid }`）。
///
/// ⚠️ 这一项**必须算进去**：全局有 `* { box-sizing: border-box }`，
/// 但那管的是"width 里含不含 padding/border" —— 菜单高度是**内容撑出来**的
/// （没写 height），所以边框照样往外顶 2px。
/// 漏掉它，`clamp_menu_pos` 就会以为菜单比实际矮 2px，底部边距从 8px 变成 6px。
/// `/tmp/e2e27` 的常量守卫判据钉的就是这个数。
pub const MENU_BORDER: f64 = 2.0;
/// 卡片菜单的条目数（收藏 / 在资源管理器中显示 / 删除记录 / 删除记录和文件）。
/// 收藏那一项只换文案，不增删条目，所以这个数是固定的。
pub const CARD_MENU_ITEMS: f64 = 4.0;
/// 批注菜单的条目数（编辑批注 / 删除<样式>）。同样只换文案。
pub const MARK_MENU_ITEMS: f64 = 2.0;

/// 菜单整体高度 = 内边距 + 各项 + 分隔线 + 上下边框。
///
/// 两个菜单都有一条分隔线（卡片那条在"在资源管理器中显示"和"删除记录"之间，
/// 批注那条在"编辑批注"和"删除"之间），所以公式只有 `items` 一个变量。
pub fn menu_height(items: f64) -> f64 {
    MENU_ITEM_H * items + MENU_PAD + MENU_SEP_H + MENU_BORDER
}

/// 把右键菜单的落点夹进可视区。
///
/// 用户很可能在右下角右击 —— 不夹的话菜单会有一半在屏幕外，
/// 而最下面那一项（"删除记录和文件" / "删除批注"）正好落在外面，等于点不到。
pub fn clamp_menu_pos(x: f64, y: f64, view_w: f64, view_h: f64, items: f64) -> (f64, f64) {
    // 留 8px 边距，别贴着屏幕边缘。
    let max_x = (view_w - MENU_W - 8.0).max(8.0);
    let max_y = (view_h - menu_height(items) - 8.0).max(8.0);
    (x.min(max_x).max(8.0), y.min(max_y).max(8.0))
}

/// 取当前窗口的可视区尺寸，用来夹菜单。拿不到就给个保守的兜底值。
pub fn viewport_size() -> (f64, f64) {
    web_sys::window()
        .map(|window| {
            let read = |value: Result<JsValue, JsValue>, fallback: f64| {
                value.ok().and_then(|v| v.as_f64()).unwrap_or(fallback)
            };
            (
                read(window.inner_width(), 1280.0),
                read(window.inner_height(), 800.0),
            )
        })
        .unwrap_or((1280.0, 800.0))
}
