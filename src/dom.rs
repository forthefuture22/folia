//! 直接操作 DOM 的小工具：剪贴板、滚动容器几何、可编辑区域判定。
//!
//! 这些都是「拿 `NodeRef` / 事件 → 算出一个值」的叶子函数，不读应用状态。
//! 抽出来是为了让 `app.rs` 里的状态编排读起来不被打断。

use wasm_bindgen::{JsCast, JsValue};
use web_sys::{KeyboardEvent, MouseEvent};
use yew::NodeRef;

use crate::pdf;
use crate::pdf::types::SelectionInfo;
use crate::selbar::{SelBox, StageView};

/// 把文本写进系统剪贴板，返回是否成功。
///
/// 用 `document.execCommand("copy")` 而不是 `navigator.clipboard`：后者要求
/// **安全上下文**，而 Tauri 生产环境走 `tauri://` 自定义协议，那里
/// `navigator.clipboard` 直接是 `undefined`（开发时 localhost 反而有，容易漏掉）。
///
/// `execCommand("copy")` 复制的是**当前选区**，所以得先拿一个临时 textarea
/// 把内容放进去并选中它 —— 这会顶掉正文里的选区，所以调用方要把「有选区」
/// 那套状态一起收掉。
pub fn copy_text(text: &str) -> bool {
    let Some(document) = web_sys::window().and_then(|window| window.document()) else {
        return false;
    };
    let Ok(element) = document.create_element("textarea") else {
        return false;
    };
    let Ok(area) = element.dyn_into::<web_sys::HtmlTextAreaElement>() else {
        return false;
    };
    area.set_value(text);
    // 必须真的挂进文档 `execCommand` 才认，但不能让页面闪一下 —— 挪到屏幕外。
    let style = area.style();
    let _ = style.set_property("position", "fixed");
    let _ = style.set_property("top", "-1000px");
    let _ = style.set_property("opacity", "0");
    let Some(body) = document.body() else {
        return false;
    };
    if body.append_child(area.as_ref()).is_err() {
        return false;
    }
    area.select();
    // 用反射调 `execCommand`，不用 web-sys 的方法：它在 web-sys 里没有暴露
    // （`Document` 上根本没有 `exec_command`），而且它本身是 deprecated 的 ——
    // 反正这是「没别的办法」才走的一条路，显式一点反而看得清。
    let copied = js_sys::Reflect::get(&document, &JsValue::from_str("execCommand"))
        .ok()
        .and_then(|method| method.dyn_into::<js_sys::Function>().ok())
        .and_then(|method| method.call1(&document, &JsValue::from_str("copy")).ok())
        .and_then(|value| value.as_bool())
        .unwrap_or(false);
    // 无论成败都要摘掉，别在 `body` 里留一个幽灵节点。
    let _ = body.remove_child(area.as_ref());
    copied
}

/// 读出正文滚动容器此刻「看得见」的那一块。
///
/// 单纯是 `StageView` 的 DOM 入口 —— 几何计算本身在 `selbar.rs` 里，不碰 DOM，
/// 那样才能用普通 `rustc --test` 直接跑。
pub fn stage_view(scroll_ref: &NodeRef) -> Option<StageView> {
    let element = scroll_ref.cast::<web_sys::Element>()?;
    Some(StageView {
        top: f64::from(element.scroll_top()),
        left: f64::from(element.scroll_left()),
        width: f64::from(element.client_width()),
        height: f64::from(element.client_height()),
    })
}

/// 读出当前选区在正文滚动容器内容坐标里的包围盒。
///
/// 几何完全复用桥接层已经算好的那一份：`getSelectionInfo` 返回的是**页面内归一化**
/// 的矩形，只要乘回页面元素自己的尺寸就能还原成屏幕坐标。
/// 这样「选区长什么样」在前后端只有一份定义，不会各算各的。
///
/// 用内容坐标（屏幕坐标 − 容器 border box + scrollTop/Left）而不是屏幕坐标，
/// 是为了让工具栏**跟着正文一起滚**：位置只在选区变化时算一次，
/// 滚动时不需要重新定位（只有「贴上方/贴下方」偶尔要翻面，见 `on_scroll`）。
pub fn selection_box(scroll_ref: &NodeRef) -> Option<SelBox> {
    // 没有选区时桥接层返回字符串 `"null"`，反序列化会失败 —— 正好当 `None` 用。
    let info: SelectionInfo = serde_json::from_str(&pdf::selection_info()?).ok()?;
    let container = scroll_ref.cast::<web_sys::Element>()?;
    let base = container.get_bounding_client_rect();
    if base.width() <= 0.0 || base.height() <= 0.0 {
        return None;
    }

    // 按页码找到那一页。找不到说明选区不在任何一页里，也就没有工具栏可言。
    let page = container
        .query_selector(&format!(".page[data-page=\"{}\"]", info.page))
        .ok()
        .flatten()?;
    let frame = page.get_bounding_client_rect();
    if frame.width() <= 0.0 || frame.height() <= 0.0 {
        return None;
    }

    // 逐条还原成屏幕坐标再取并集：选区跨行时 `rects` 是每行一条。
    let mut box_ = SelBox {
        left: f64::MAX,
        right: f64::MIN,
        top: f64::MAX,
        bottom: f64::MIN,
    };
    for rect in &info.rects {
        let left = frame.left() + rect.x * frame.width();
        let top = frame.top() + rect.y * frame.height();
        box_.left = box_.left.min(left);
        box_.right = box_.right.max(left + rect.w * frame.width());
        box_.top = box_.top.min(top);
        box_.bottom = box_.bottom.max(top + rect.h * frame.height());
    }
    if box_.left >= box_.right || box_.top >= box_.bottom {
        return None;
    }

    // 换到内容坐标。容器没有边框和内边距，所以 border box 的左上角就是原点。
    let origin_x = base.left() - f64::from(container.scroll_left());
    let origin_y = base.top() - f64::from(container.scroll_top());
    box_.left -= origin_x;
    box_.right -= origin_x;
    box_.top -= origin_y;
    box_.bottom -= origin_y;
    Some(box_)
}

/// 键盘事件是否发生在输入控件内（用于跳过翻页快捷键）。
pub fn is_typing(event: &KeyboardEvent) -> bool {
    event
        .target()
        .and_then(|target| target.dyn_into::<web_sys::Element>().ok())
        .map(|element| matches!(element.tag_name().as_str(), "INPUT" | "TEXTAREA"))
        .unwrap_or(false)
}

/// 右键落点是不是**可编辑区域**（输入框 / 文本域 / contenteditable）。
///
/// 和 `is_typing` 的区别是这里用 `closest` **往上找祖先**，而不是只看 `event.target()`
/// 自己：`contenteditable` 是**继承**的，光标常常落在里面某个 `<span>` 上，
/// 只看 target 会漏。
///
/// 排除 `contenteditable="false"` —— 那是"显式关掉继承"，不该算可编辑。
pub fn right_click_is_editable(event: &MouseEvent) -> bool {
    event
        .target()
        .and_then(|target| target.dyn_into::<web_sys::Element>().ok())
        .and_then(|element| {
            element
                .closest("input, textarea, [contenteditable]:not([contenteditable=\"false\"])")
                .ok()
                .flatten()
        })
        .is_some()
}

/// 在一个可滚动容器里，列出**当前可视区内**（上下各外扩 `band` px）那些条目的页码。
///
/// 页码从元素自己的 `data-page` 属性读，**不是** DOM 下标 —— 容器里的条目可能
/// 因为虚拟化 / 缓存淘汰而不连续，下标和页码对不上。
///
/// 只做几何，不判断"这一条是不是已经有图了"：那是业务条件，归调用方
/// （`app.rs` 里传给 `need_thumb`，它自己会去重）。
pub fn visible_pages(container: &web_sys::Element, item_selector: &str, band: f64) -> Vec<u32> {
    let rect = container.get_bounding_client_rect();
    let top = rect.top() - band;
    let bottom = rect.bottom() + band;
    let Ok(items) = container.query_selector_all(item_selector) else {
        return Vec::new();
    };
    let mut pages = Vec::new();
    for index in 0..items.length() {
        let Some(node) = items.item(index) else {
            continue;
        };
        let Ok(element) = node.dyn_into::<web_sys::Element>() else {
            continue;
        };
        let item = element.get_bounding_client_rect();
        if item.bottom() < top || item.top() > bottom {
            continue;
        }
        if let Some(page) = element
            .get_attribute("data-page")
            .and_then(|value| value.parse::<u32>().ok())
        {
            pages.push(page);
        }
    }
    pages
}
