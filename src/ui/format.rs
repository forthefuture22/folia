//! 把内部标识 / 数值换算成界面上要显示的文字。
//!
//! 全是纯函数：给定同样的输入一定给出同样的输出，不读全局状态、不碰 DOM
//! （唯一的外部依赖是 `Date::now()`，只在 `relative_time` 里用）。

use js_sys::Date;

use crate::pdf::types::RecentFile;

/// 缩放档位。
pub const ZOOM_STEPS: [f64; 9] = [0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0];

/// 色板的中文名，用作 tooltip。
///
/// 原来直接把 `yellow` / `green` 这些内部标识当提示语显示，界面其它地方全是中文，
/// 唯独鼠标停在色板上会跳出一个英文单词。
pub fn color_label(color: &str) -> &'static str {
    match color {
        "green" => "绿色",
        "blue" => "蓝色",
        "pink" => "粉色",
        _ => "黄色",
    }
}

/// 在当前档位的基础上往上 / 往下走一格（`direction > 0` 为放大）。
///
/// 找不到更近的档位就顶到两端 —— 不做"无级缩放"，因为档位制才能保证
/// 每次点「+ / −」的视觉变化一致。
pub fn next_zoom(current: f64, direction: f64) -> f64 {
    if direction > 0.0 {
        ZOOM_STEPS
            .iter()
            .copied()
            .find(|step| *step > current + 0.001)
            .unwrap_or(ZOOM_STEPS[ZOOM_STEPS.len() - 1])
    } else {
        ZOOM_STEPS
            .iter()
            .rev()
            .copied()
            .find(|step| *step < current - 0.001)
            .unwrap_or(ZOOM_STEPS[0])
    }
}

/// 人类可读的体积：`1.2 MB` / `340 KB` / `512 B`。
pub fn format_size(bytes: u64) -> String {
    const MB: f64 = 1024.0 * 1024.0;
    const KB: f64 = 1024.0;
    let value = bytes as f64;
    if value >= MB {
        format!("{:.1} MB", value / MB)
    } else if value >= KB {
        format!("{:.0} KB", value / KB)
    } else {
        format!("{bytes} B")
    }
}

/// 把时间戳格式化成「今天 / 昨天 / N 天前 / N 个月前 / N 年前」。
///
/// 刻意不用绝对日期：`2026-08-20` 这种 10 个字符太长，
/// 相对时间既短又更符合「最近打开」的语境。用在卡片的 title 提示里。
///
/// 时间戳缺失（`<= 0`，比如从旧 `state.json` 读回来的条目）返回**空串** ——
/// 调用方靠它决定要不要拼这一段，别改成返回 "未知"。
pub fn relative_time(timestamp_ms: f64) -> String {
    if timestamp_ms <= 0.0 {
        return String::new();
    }
    const DAY: f64 = 86_400_000.0;
    let diff = Date::now() - timestamp_ms;
    if diff < DAY {
        "今天".into()
    } else if diff < 2.0 * DAY {
        "昨天".into()
    } else if diff < 30.0 * DAY {
        format!("{} 天前", (diff / DAY) as u32)
    } else if diff < 365.0 * DAY {
        format!("{} 个月前", (diff / (30.0 * DAY)) as u32)
    } else {
        format!("{} 年前", (diff / (365.0 * DAY)) as u32)
    }
}

/// 卡片上那行次要信息：`1/130 | 13.2 MB`。
/// 页数未知（还没打开过）时退化成只显示体积。
pub fn file_meta(item: &RecentFile) -> String {
    let size = format_size(item.size);
    if item.pages > 0 {
        let page = item.last_page.clamp(1, item.pages);
        format!("{page}/{} | {size}", item.pages)
    } else {
        size
    }
}
