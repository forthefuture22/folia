//! 与 `public/pdfjs/bridge.js` 及 Tauri 命令层的绑定。
//!
//! 桥接层以 `window.FoliaBridge` 全局对象暴露，因此这里用 `js_namespace` 绑定，
//! 不依赖 wasm-bindgen 的 module 路径解析，打包方式更稳。
//!
//! 所有 `extern` 声明都带 `catch`：JS 侧一旦抛异常，会变成 `Err(JsValue)` 而不是
//! 直接 panic 掉整个 wasm 应用。

pub mod types;

use wasm_bindgen::prelude::*;
use wasm_bindgen_futures::JsFuture;

#[wasm_bindgen]
extern "C" {
    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = openDocumentDialog, catch)]
    fn bridge_open_dialog() -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = openDocumentPath, catch)]
    fn bridge_open_path(path: &str) -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = closeDocument, catch)]
    fn bridge_close_document() -> Result<(), JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = renderThumbnail, catch)]
    fn bridge_render_thumbnail(
        path: &str,
        page: u32,
        max_width: f64,
    ) -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = releaseThumbnail, catch)]
    fn bridge_release_thumbnail(path: &str) -> Result<(), JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = getPageSizes, catch)]
    fn bridge_page_sizes() -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = renderPage, catch)]
    fn bridge_render_page(
        canvas_id: &str,
        page: u32,
        scale: f64,
    ) -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = releasePage, catch)]
    fn bridge_release_page(page: u32) -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = renderTextLayer, catch)]
    fn bridge_render_text_layer(
        container_id: &str,
        page: u32,
        scale: f64,
        query: &str,
        active_key: &str,
    ) -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = getOutline, catch)]
    fn bridge_outline() -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = search, catch)]
    fn bridge_search(query: &str) -> Result<js_sys::Promise, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = getSelectionInfo, catch)]
    fn bridge_selection_info() -> Result<JsValue, JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = clearSelection, catch)]
    fn bridge_clear_selection() -> Result<(), JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "FoliaBridge"], js_name = onFileDrop, catch)]
    fn bridge_on_file_drop(callback: &js_sys::Function) -> Result<(), JsValue>;

    #[wasm_bindgen(js_namespace = ["window", "__TAURI__", "core"], js_name = invoke, catch)]
    fn tauri_invoke(cmd: &str, args: JsValue) -> Result<js_sys::Promise, JsValue>;
}

/// 判断全局对象上是否已挂载某个名字。
fn global_has(name: &str) -> bool {
    js_sys::Reflect::get(&js_sys::global(), &JsValue::from_str(name))
        .map(|value| !value.is_undefined() && !value.is_null())
        .unwrap_or(false)
}

async fn sleep_ms(ms: i32) {
    let promise = js_sys::Promise::new(&mut |resolve, _reject| {
        if let Some(window) = web_sys::window() {
            let _ = window.set_timeout_with_callback_and_timeout_and_arguments_0(&resolve, ms);
        }
    });
    let _ = JsFuture::from(promise).await;
}

/// 等待桥接层就绪。
///
/// `<script type="module">` 的执行与 wasm 初始化存在竞态，这里做一次有限等待。
pub async fn ready() -> Result<(), String> {
    for _ in 0..240 {
        if global_has("FoliaBridge") {
            return Ok(());
        }
        sleep_ms(25).await;
    }
    Err("pdf.js 桥接层未加载，请确认 public/pdfjs/bridge.js 已随应用一起打包".into())
}

/// 等待若干毫秒（用于检索去抖）。
pub async fn sleep(ms: i32) {
    sleep_ms(ms).await;
}

/// 等待一个 JS Promise 并把结果按字符串取回。
async fn call(result: Result<js_sys::Promise, JsValue>) -> Result<String, String> {
    let promise = result.map_err(|err| format!("{err:?}"))?;
    let value = JsFuture::from(promise)
        .await
        .map_err(|err| format!("{err:?}"))?;
    Ok(value.as_string().unwrap_or_default())
}

/// 通用 Tauri 命令调用，参数以对象形式传入。
async fn invoke(cmd: &str, args: &[(&str, JsValue)]) -> Result<JsValue, String> {
    let object = js_sys::Object::new();
    for (key, value) in args {
        js_sys::Reflect::set(&object, &JsValue::from_str(key), value)
            .map_err(|_| "构造调用参数失败".to_string())?;
    }
    let promise = tauri_invoke(cmd, object.into()).map_err(|err| format!("{err:?}"))?;
    JsFuture::from(promise)
        .await
        .map_err(|err| format!("{err:?}"))
}

// ---------------------------------------------------------------- 文档

pub async fn open_dialog() -> Result<String, String> {
    ready().await?;
    call(bridge_open_dialog()).await
}

pub async fn open_path(path: &str) -> Result<String, String> {
    ready().await?;
    call(bridge_open_path(path)).await
}

/// 释放 pdf.js 侧的文档对象与缓存。
///
/// 返回书架时必须调用，否则反复开关文档会让旧文档一直留在内存里。
/// 这是同步调用（JS 侧返回 `undefined`），所以不需要 await。
pub fn close() {
    let _ = bridge_close_document();
}

/// 渲染某一页的缩略图，返回 JPEG data URL；失败时是空串。
///
/// 用的是桥接层里**独立于阅读文档**的实例，所以可以在没打开任何文档时调用
/// （书架卡片就是靠这个显示首页预览的）。
pub async fn thumbnail(path: &str, page: u32, max_width: f64) -> Result<String, String> {
    call(bridge_render_thumbnail(path, page, max_width)).await
}

/// 释放某份文档的缩略图实例。
pub fn release_thumbnail(path: &str) {
    let _ = bridge_release_thumbnail(path);
}

pub async fn page_sizes() -> Result<String, String> {
    call(bridge_page_sizes()).await
}

pub async fn outline() -> Result<String, String> {
    call(bridge_outline()).await
}

// ---------------------------------------------------------------- 渲染

pub async fn render_page(canvas_id: &str, page: u32, scale: f64) -> Result<String, String> {
    call(bridge_render_page(canvas_id, page, scale)).await
}

/// 放掉某一页在 pdf.js 侧的解析产物（算子表）。页面滚出保留窗口时调用。
pub async fn release_page(page: u32) -> Result<String, String> {
    call(bridge_release_page(page)).await
}

pub async fn render_text_layer(
    container_id: &str,
    page: u32,
    scale: f64,
    query: &str,
    active_key: &str,
) -> Result<String, String> {
    call(bridge_render_text_layer(
        container_id,
        page,
        scale,
        query,
        active_key,
    ))
    .await
}

// ---------------------------------------------------------------- 检索与选区

pub async fn search(query: &str) -> Result<String, String> {
    call(bridge_search(query)).await
}

pub fn selection_info() -> Option<String> {
    let value = bridge_selection_info().ok()?;
    if value.is_null() || value.is_undefined() {
        None
    } else {
        value.as_string()
    }
}

pub fn clear_selection() {
    let _ = bridge_clear_selection();
}

pub fn on_file_drop(callback: &js_sys::Function) {
    let _ = bridge_on_file_drop(callback);
}

// ---------------------------------------------------------------- 持久化

pub async fn load_annotations(id: &str) -> Result<String, String> {
    let value = invoke("load_annotations", &[("id", JsValue::from_str(id))]).await?;
    Ok(value.as_string().unwrap_or_else(|| "[]".into()))
}

pub async fn save_annotations(id: &str, data: &str) -> Result<(), String> {
    invoke(
        "save_annotations",
        &[
            ("id", JsValue::from_str(id)),
            ("data", JsValue::from_str(data)),
        ],
    )
    .await?;
    Ok(())
}

/// 读取全局应用状态。
///
/// ⚠️ 返回值不是字符串时**必须报错**，不能退回 `"{}"` —— 那又是"读失败当成没数据"
/// （见后端 `load_app_state` 的注释）。前端拿不到可信内容时会去留档，不该被糊弄。
pub async fn load_app_state() -> Result<String, String> {
    let value = invoke("load_app_state", &[]).await?;
    value
        .as_string()
        .ok_or_else(|| "应用状态返回了非字符串".to_string())
}

pub async fn save_app_state(data: &str) -> Result<(), String> {
    invoke("save_app_state", &[("data", JsValue::from_str(data))]).await?;
    Ok(())
}

/// 把读不出来 / 解析不了的 `state.json` 改名留档，返回新文件名（没有则 `None`）。
///
/// 前端解析失败时调一次 —— 旧文件原样保住，然后当"没有状态"继续用。
pub async fn quarantine_app_state() -> Result<Option<String>, String> {
    let value = invoke("quarantine_app_state", &[]).await?;
    if value.is_null() {
        return Ok(None);
    }
    Ok(value.as_string())
}

// ---------------------------------------------------------------- 外部打开 / 默认程序

/// 取走「外部送进来的待打开文件」（双击 .pdf / 资源管理器「打开方式」/ 拖到 exe 上）。
///
/// 冷启动时后端比前端先拿到路径，所以启动流程里要主动来取一次；取完就清空。
pub async fn take_pending_open() -> Option<String> {
    let value = invoke("take_pending_open", &[]).await.ok()?;
    value.as_string()
}

/// 把 Folia 加入 `.pdf` 的「打开方式」并打开系统设置（Windows）。
pub async fn set_default_pdf_handler() -> Result<(), String> {
    invoke("set_default_pdf_handler", &[]).await?;
    Ok(())
}

/// 在资源管理器里定位并选中这个文件（书架卡片的右键菜单）。
///
/// 真正的实现走 `tauri-plugin-opener` 的 Rust API，所以这里只是一个普通的命令调用 ——
/// 前端不碰那个插件，capabilities 也就不用为它放权限。
pub async fn reveal_in_explorer(path: &str) -> Result<(), String> {
    invoke("reveal_in_explorer", &[("path", JsValue::from_str(path))]).await?;
    Ok(())
}

/// 把文件移到回收站（书架卡片的右键菜单）。
///
/// **调用方必须先确认过** —— 这个命令自己不会再问一遍。
pub async fn trash_file(path: &str) -> Result<(), String> {
    invoke("trash_file", &[("path", JsValue::from_str(path))]).await?;
    Ok(())
}

/// PDF 现在是不是默认由 Folia 打开。
///
/// `Ok(None)` = 系统里没登记 PDF 的默认程序。
pub async fn default_pdf_handler() -> Result<Option<bool>, String> {
    let value = invoke("default_pdf_handler", &[]).await?;
    if value.is_null() || value.is_undefined() {
        return Ok(None);
    }
    Ok(value.as_bool())
}
