//! 书架卡片：一张卡 = 一份最近打开的 PDF。
//!
//! 卡片正面只有「打开」一个动作 —— 收藏 / 删除记录都搬进了右键菜单
//! （那两个悬浮按钮曾经贴在卡片右下角，点得到的区域很窄，还和文件名行重叠过）。
//!
//! 这个模块是**纯渲染**：给它一串条目、缩略图缓存和两个回调，它只吐 HTML。

use std::collections::HashMap;

use web_sys::MouseEvent;
use yew::prelude::*;

use crate::pdf::types::RecentFile;
use crate::ui::cover::{cover_hue, cover_style, fit_thumb_frame};
use crate::ui::format::{file_meta, relative_time};

/// 把一串条目渲染成卡片列表。
///
/// - `on_open`：点卡片打开那份文档（载荷是文件路径）。
/// - `on_menu`：在卡片上右击（载荷是条目本身 + 光标的视口坐标）。
pub fn file_cards_html(
    items: &[RecentFile],
    thumbs: &HashMap<String, String>,
    on_open: Callback<String>,
    on_menu: Callback<(RecentFile, f64, f64)>,
) -> Html {
    items
        .iter()
        .map(|item| {
            let onopen = {
                let on_open = on_open.clone();
                let path = item.path.clone();
                Callback::from(move |_: MouseEvent| on_open.emit(path.clone()))
            };
            // 右键：拦掉系统菜单，在光标处开自己的菜单。
            //
            // 这里的 `prevent_default` 现在是**双保险**：document 上那个捕获阶段的
            // 兜底监听已经把默认菜单整个关掉了（见 `app.rs` 里「默认右键菜单」那段）。
            // 留着它是因为它不花钱，而且万一以后把那个兜底收窄（比如只关书架），
            // 卡片自己的菜单也不会突然叠一层系统菜单。
            // `stop_propagation` 则是另一件事：不让事件冒到书架容器上，
            // 免得触发"点空白处收起"之类的逻辑。
            let onmenu = {
                let on_menu = on_menu.clone();
                let item = item.clone();
                Callback::from(move |event: MouseEvent| {
                    event.prevent_default();
                    event.stop_propagation();
                    on_menu.emit((
                        item.clone(),
                        event.client_x() as f64,
                        event.client_y() as f64,
                    ));
                })
            };
            // 卡片整体的提示：名字 + 相对时间。
            // 相对时间算不出来（时间戳缺失，比如从旧 `state.json` 读回来的条目）时
            // **不要**拼那个换行 —— 否则提示框里会多出一条空行，看着像坏了。
            let when = relative_time(item.opened_at);
            let tip = if when.is_empty() {
                item.name.clone()
            } else {
                format!("{}\n{}", item.name, when)
            };
            let fallback_style = cover_style(cover_hue(&item.id));
            let thumb = thumbs.get(&item.id).cloned();
            html! {
                <div class="file-card" onclick={onopen} oncontextmenu={onmenu}
                    data-card-id={item.id.clone()} title={tip}>
                    // 卡片整体挂的是「名字 + 相对时间」；文件名自己再挂一层 `title`，
                    // 这样 hover 在（被省略号截断的）名字上时，提示框里就是**完整文件名**，
                    // 不用夹带时间。见 styles.css 里 `.file-title` 的单行截断。
                    <div class="file-title" title={item.name.clone()}>{ &item.name }</div>
                    <div class="file-meta">{ file_meta(item) }</div>
                    <div class="file-thumb">
                        { match thumb {
                            Some(url) => html! {
                                <img src={url} alt="" onload={Callback::from(fit_thumb_frame)} />
                            },
                            None => html! {
                                <div class="file-thumb-fallback" style={fallback_style}>
                                    <span class="fallback-badge">{"PDF"}</span>
                                </div>
                            },
                        } }
                    </div>
                </div>
            }
        })
        .collect()
}
