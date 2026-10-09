//! 右键菜单的渲染。
//!
//! 卡片和批注共用**同一套**样式与关闭时机，只有条目不同 —— 所以这里按
//! [`CtxMenu`] 的变体各拼一份。`data-ctx-menu` 的值就是变体名，测试靠它区分。
//!
//! 这个组件是**纯函数**：给它「开哪一个菜单 + 六个动作」，它只吐 HTML，
//! 不碰任何状态。所以它既不需要 `Properties`，也不需要 `#[function_component]` ——
//! 直接当函数调更省一层样板。

use web_sys::MouseEvent;
use yew::prelude::*;

use crate::ctx_menu::CtxMenu;
use crate::pdf::types::RecentFile;

/// 渲染当前打开的右键菜单；没开就返回空。
///
/// 六个回调分别是卡片菜单的四个动作和批注菜单的两个动作 ——
/// 关菜单由调用方在这些回调里自己负责（见 `app.rs` 里 `close_menu` 的用法），
/// 这样"菜单项点下去要不要关"这件事只有一个地方决定。
pub fn ctx_menu_html(
    menu: Option<CtxMenu>,
    on_toggle_favorite: Callback<String>,
    on_reveal: Callback<String>,
    on_remove_record: Callback<String>,
    on_ask_delete_file: Callback<RecentFile>,
    on_edit_note: Callback<String>,
    on_delete_mark: Callback<String>,
) -> Html {
    // 菜单里点任何地方都不该弹出系统右键菜单（用户可能想右击菜单项本身）。
    let swallow_context = Callback::from(|event: MouseEvent| event.prevent_default());

    match menu {
        None => Html::default(),
        Some(CtxMenu::Card { item, x, y }) => {
            // 「收藏」那一项按当前状态换文案 —— 已收藏时必须是"取消收藏"，
            // 否则用户点了会以为没生效（收藏状态没变，只是从有变无）。
            let favorite_label = if item.favorite {
                "取消收藏"
            } else {
                "收藏"
            };

            let on_favorite = {
                let on_toggle_favorite = on_toggle_favorite.clone();
                let id = item.id.clone();
                Callback::from(move |_: MouseEvent| on_toggle_favorite.emit(id.clone()))
            };
            let on_reveal_click = {
                let path = item.path.clone();
                Callback::from(move |_: MouseEvent| on_reveal.emit(path.clone()))
            };
            let on_remove_record_click = {
                let on_remove_record = on_remove_record.clone();
                let id = item.id.clone();
                Callback::from(move |_: MouseEvent| on_remove_record.emit(id.clone()))
            };
            let on_delete_file = {
                let on_ask_delete_file = on_ask_delete_file.clone();
                let item = item.clone();
                Callback::from(move |_: MouseEvent| on_ask_delete_file.emit(item.clone()))
            };

            html! {
                <div class="ctx-menu" style={format!("left: {}px; top: {}px;", x, y)}
                    oncontextmenu={swallow_context.clone()} data-ctx-menu="card">
                    <button class="ctx-menu-item" onmousedown={on_favorite}>
                        { favorite_label }
                    </button>
                    <button class="ctx-menu-item" onmousedown={on_reveal_click}>
                        {"在资源管理器中显示"}
                    </button>
                    <div class="ctx-menu-sep" />
                    <button class="ctx-menu-item" onmousedown={on_remove_record_click}>
                        {"删除记录"}
                    </button>
                    <button class="ctx-menu-item danger" onmousedown={on_delete_file}>
                        {"删除记录和文件"}
                    </button>
                </div>
            }
        }
        Some(CtxMenu::Mark { id, kind, x, y }) => {
            // 「删除下划线」「删除删划线」这种 —— 用户右击的是哪条就写哪条，
            // 比笼统的"删除批注"更清楚（同一段文字上可能叠着高亮和下划线两条）。
            // 样式名走 `MarkKind::label()`，和侧栏批注列表用的是同一份文案。
            let delete_label = format!("删除{}", kind.label());

            let on_edit = {
                let on_edit_note = on_edit_note.clone();
                let id = id.clone();
                Callback::from(move |_: MouseEvent| on_edit_note.emit(id.clone()))
            };
            let on_delete = {
                let on_delete_mark = on_delete_mark.clone();
                Callback::from(move |_: MouseEvent| on_delete_mark.emit(id.clone()))
            };

            html! {
                <div class="ctx-menu" style={format!("left: {}px; top: {}px;", x, y)}
                    oncontextmenu={swallow_context.clone()} data-ctx-menu="mark">
                    <button class="ctx-menu-item" onmousedown={on_edit}>
                        {"编辑批注"}
                    </button>
                    <div class="ctx-menu-sep" />
                    <button class="ctx-menu-item danger" onmousedown={on_delete}>
                        { delete_label }
                    </button>
                </div>
            }
        }
    }
}
