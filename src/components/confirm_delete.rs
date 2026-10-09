//! 「移到回收站」的二次确认浮层。
//!
//! 删的是**用户的文件**，哪怕进的是回收站，也不该点一下就走 ——
//! 所以这里必须停下来问一句，并且把文件名原样写出来，
//! 别让用户对着一个"确定吗？"瞎猜。
//!
//! 纯渲染：给一个待删条目和两个回调，只吐 HTML。

use web_sys::MouseEvent;
use yew::prelude::*;

use crate::pdf::types::RecentFile;

/// 渲染确认浮层；`pending` 为 `None` 时返回空。
pub fn confirm_delete_html(
    pending: Option<RecentFile>,
    on_cancel: Callback<()>,
    on_confirm: Callback<RecentFile>,
) -> Html {
    let Some(item) = pending else {
        return Html::default();
    };

    let on_cancel_click = {
        let on_cancel = on_cancel.clone();
        Callback::from(move |_: MouseEvent| on_cancel.emit(()))
    };
    let on_confirm_click = {
        let on_confirm = on_confirm.clone();
        let item = item.clone();
        Callback::from(move |_: MouseEvent| on_confirm.emit(item.clone()))
    };
    // 点面板本身不该关掉它（只有点遮罩才算"取消"）。
    let swallow = Callback::from(|event: MouseEvent| event.stop_propagation());

    html! {
        <div class="confirm-backdrop" onclick={on_cancel_click.clone()} data-confirm="1">
            <div class="confirm-panel" onclick={swallow}>
                <p class="confirm-title">{"移到回收站？"}</p>
                <p class="confirm-name" title={item.name.clone()}>{ &item.name }</p>
                <p class="confirm-hint">
                    {"文件会被移到回收站（还能从回收站还原），同时从「最近打开」里移除。"}
                </p>
                <div class="confirm-actions">
                    <button class="btn" onclick={on_cancel_click}>{"取消"}</button>
                    <button class="btn danger" data-confirm-ok="1" onclick={on_confirm_click}>
                        {"移到回收站"}
                    </button>
                </div>
            </div>
        </div>
    }
}
