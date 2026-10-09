mod app;
mod components;
mod ctx_menu;
mod dom;
mod layout;
mod pdf;
mod selbar;
mod state;
mod ui;

use app::App;

/// 给 `<html>` 打上平台标记。
///
/// 这个版本只面向 Windows，所以标记是固定的。留着它是因为样式里按平台分叉的
/// 那处（标签栏左侧的红绿灯留白）需要一个统一入口 —— 现在 Windows 不需要留白，
/// 这个类没有对应规则，但把它挂上能让「平台相关的样式」以后有个明确的挂点。
fn tag_platform() {
    let Some(window) = web_sys::window() else {
        return;
    };
    if let Some(root) = window.document().and_then(|d| d.document_element()) {
        let _ = root.class_list().add_1("is-windows");
    }
}

fn main() {
    console_error_panic_hook::set_once();
    tag_platform();
    yew::Renderer::<App>::new().render();
}
