//! 书架卡片的封面：由文档 id 派生稳定的颜色，以及缩略图框的比例适配。

use wasm_bindgen::JsCast;
use web_sys::Event;

/// 由文档 ID 派生一个稳定的色相，让每本「书」的封面颜色不同但每次打开一致。
///
/// 用 FNV-1a：够散、够短、不依赖 `std::hash` 的随机种子
/// （`DefaultHasher` 每次进程启动的种子不同，颜色会每次变）。
pub fn cover_hue(id: &str) -> u32 {
    let mut hash: u32 = 2166136261;
    for byte in id.as_bytes() {
        hash ^= u32::from(*byte);
        hash = hash.wrapping_mul(16777619);
    }
    hash % 360
}

/// 封面底色：三段式渐变比两段更有体积感。
pub fn cover_style(hue: u32) -> String {
    format!(
        "background:linear-gradient(158deg,hsl({hue} 52% 94%),hsl({hue} 38% 82%) 62%,hsl({hue} 33% 76%));"
    )
}

/// 书架卡片：让缩略图框跟着**图片自己的**比例走。
///
/// 卡片这里拿不到页面尺寸 —— `RecentFile` 里只有页数，没有每页宽高，
/// 而为了排版去把每份文档都加载一遍 pdf.js 显然不划算。
/// 好在缩略图是 pdf.js 按页面**真实 viewport** 画的，
/// 所以**图自己的宽高比就是页面比例**：解码完把它写到父元素上，
/// 「框的比例 == 图的比例」就必然成立 —— 不管是 A4、Letter 还是横向页，
/// 也不管是这次新画的还是上次留下的旧缩略图。
///
/// CSS 里那个 `1 / 1.414` 只是图片加载完成前的占位（A4），
/// 所以最坏情况是加载完前后差几像素，而不是永久裁掉一块。
pub fn fit_thumb_frame(event: Event) {
    let Some(img) = event
        .target()
        .and_then(|target| target.dyn_into::<web_sys::HtmlImageElement>().ok())
    else {
        return;
    };
    let (width, height) = (img.natural_width(), img.natural_height());
    if width == 0 || height == 0 {
        return;
    }
    // `parent_element()` 给的是 `Element`，而 `style()` 只在 `HtmlElement` 上
    // —— 卡片的框就是普通 `<div>`，转过去一定成功。
    if let Some(frame) = img
        .parent_element()
        .and_then(|parent| parent.dyn_into::<web_sys::HtmlElement>().ok())
    {
        let _ = frame
            .style()
            .set_property("aspect-ratio", &format!("{width} / {height}"));
    }
}
