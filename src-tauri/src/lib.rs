mod commands;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;

use commands::{
    default_pdf_handler, load_annotations, load_app_state, open_pdf, quarantine_app_state,
    read_pdf_bytes, read_pdf_path, reveal_in_explorer, save_annotations, save_app_state,
    set_default_pdf_handler, trash_file,
};

/// 从外部送进来、等着被打开的 PDF 路径。
///
/// 什么时候会走到这里（Windows）：
/// - 双击 .pdf（前提是 Folia 已经是默认程序）；
/// - 资源管理器里「打开方式 → Folia」；
/// - 把 .pdf 拖到 `folia.exe` 图标上松手。
///
/// 这三种情况系统都是**再启动一次进程**，把路径塞进命令行参数 —— 和 macOS 的
/// LaunchServices 不同，Windows 没有「把文件交给已运行实例」的内建机制，
/// 所以这里分两条路：
/// - **冷启动**（应用没在跑）：启动时把 argv 里的路径先存进这个槽，
///   等前端起来后主动 `take_pending_open` 取一次；
/// - **热启动**（应用已经在跑）：由 `tauri-plugin-single-instance` 接住第二个
///   进程的 argv，直接 `emit("folia://open-path", …)` 推给前端（见 `run`）。
///
/// ⚠️ **槽位是「冷启动专用」的，热启动不能往里写。**
/// `take_pending_open` 全程只在**启动 effect** 里被调一次；一旦前端已经起来
/// （热启动），往槽里写就会把路径**永久留在槽里** —— 没人取，直到**下一次冷启动**
/// 才被取走，表现为「正常开 App 却自动弹出一份上次已经关掉的文档」。
/// 所以用 `ready` 把「前端是否已就绪」显式记下来（热启动走的是单实例回调，
/// 本来就不会碰这个槽，这里留着标志是为了让语义自洽）。
pub struct PendingOpen {
    /// 冷启动阶段暂存的路径，`take` 语义（取完即清）。
    slot: Mutex<Option<String>>,
    /// 前端是否已经就绪（取过一次 pending）。就绪后不再往槽里写。
    ready: AtomicBool,
}

impl PendingOpen {
    fn new(pending: Option<String>) -> Self {
        Self {
            slot: Mutex::new(pending),
            ready: AtomicBool::new(false),
        }
    }
}

/// 从命令行参数里挑出第一份 PDF 路径。
///
/// `args[0]` 是 exe 自己，跳过；顺带跳过 `-` / `--` 开头的开关（Tauri 自己会塞
/// 一些启动参数）。大小写不敏感地认 `.pdf`。
fn first_pdf_arg<I: IntoIterator<Item = String>>(args: I) -> Option<String> {
    args.into_iter()
        .skip(1)
        .find(|arg| !arg.starts_with('-') && arg.to_ascii_lowercase().ends_with(".pdf"))
}

/// 取走「外部送进来的待打开文件」（取完就清空，只会被消费一次）。
///
/// ⚠️ **顺手置 `ready`**：`take` 与「声明就绪」本来就是同一个时刻，
/// 合成一次调用才没有窗口期（否则要另发一个 `mark_ready`，两者之间热启动事件会挤进来）。
#[tauri::command]
fn take_pending_open(state: tauri::State<'_, PendingOpen>) -> Option<String> {
    state.ready.store(true, Ordering::Release);
    state.slot.lock().ok().and_then(|mut slot| slot.take())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    // 冷启动：命令行里带着 PDF（双击 / 打开方式 / 拖到 exe 上）就先存进槽位，
    // 等前端起来后主动 `take_pending_open` 取走并直接打开。
    let cold_start = first_pdf_arg(std::env::args());

    let app = tauri::Builder::default()
        // ⚠️ 单实例插件**必须第一个注册**（Tauri 的硬要求）。
        // 第二个实例启动时，这段回调在**已经运行的那个实例**里执行：
        // 把 argv 里的 PDF 转成 `folia://open-path` 事件推给前端
        // （`bridge.js` 的 `onFileDrop` 挂了对应监听），再顺手把窗口提到前面。
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            use tauri::{Emitter, Manager};

            if let Some(path) = first_pdf_arg(argv) {
                let _ = app.emit("folia://open-path", path);
            }
            // 从资源管理器双击时窗口多半在后台（甚至最小化），顺手拉回来。
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(PendingOpen::new(cold_start))
        .invoke_handler(tauri::generate_handler![
            open_pdf,
            read_pdf_path,
            read_pdf_bytes,
            load_annotations,
            save_annotations,
            load_app_state,
            save_app_state,
            quarantine_app_state,
            take_pending_open,
            default_pdf_handler,
            set_default_pdf_handler,
            reveal_in_explorer,
            trash_file,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // 没有要观察的运行期事件了（Windows 上不再有 `RunEvent::Opened`），
    // 但 `App::run` 仍然要求传一个回调 —— 交个空的把控制权交出去。
    app.run(|_app_handle, _event| {});
}
