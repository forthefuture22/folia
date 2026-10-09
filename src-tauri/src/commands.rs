//! Folia 后端命令：文件打开/读取、批注与应用状态的本地持久化。
//!
//! 设计原则：
//! - 所有文件系统访问都收敛在 Rust 侧，前端只通过 IPC 拿到元数据与原始字节。
//! - 原始字节用 `tauri::ipc::Response` 返回，避免 JSON 数组/base64 带来的体积膨胀。

use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::hash::{Hash, Hasher};
use std::io::Write as _;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};

use serde::{Deserialize, Serialize};
use tauri::ipc::Response;
use tauri::{AppHandle, Manager};
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

/// 打开一份 PDF 后返回给前端的元数据。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OpenedPdf {
    /// 由绝对路径派生的稳定文档 ID，用于关联批注与阅读进度。
    pub id: String,
    pub path: String,
    pub name: String,
    pub size: u64,
}

/// 由路径派生稳定 ID（同一条路径每次运行结果一致）。
fn doc_id_for(path: &Path) -> String {
    let mut hasher = DefaultHasher::new();
    path.to_string_lossy().hash(&mut hasher);
    format!("{:016x}", hasher.finish())
}

/// 校验路径并构造元数据。
fn describe(path: &Path) -> Result<OpenedPdf, String> {
    let meta = fs::metadata(path).map_err(|e| format!("无法读取文件信息：{e}"))?;
    if !meta.is_file() {
        return Err("目标不是一个文件".into());
    }
    let name = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "未命名.pdf".into());
    Ok(OpenedPdf {
        id: doc_id_for(path),
        path: path.to_string_lossy().to_string(),
        name,
        size: meta.len(),
    })
}

/// 弹出系统文件选择框，返回所选 PDF 的元数据；用户取消时返回 `None`。
#[tauri::command]
pub async fn open_pdf(app: AppHandle) -> Result<Option<OpenedPdf>, String> {
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog()
        .file()
        .set_title("打开 PDF")
        .add_filter("PDF 文档", &["pdf"])
        .pick_file(move |picked| {
            let _ = tx.send(picked);
        });

    let picked = rx.await.map_err(|e| e.to_string())?;
    let Some(file_path) = picked else {
        return Ok(None);
    };
    let path = file_path.into_path().map_err(|e| e.to_string())?;
    describe(&path).map(Some)
}

/// 拖拽/外部传入路径时使用：只校验并返回元数据，不弹框。
#[tauri::command]
pub fn read_pdf_path(path: String) -> Result<OpenedPdf, String> {
    describe(Path::new(&path))
}

/// 读取 PDF 原始字节，以二进制形式回传（JS 侧收到 ArrayBuffer）。
#[tauri::command]
pub fn read_pdf_bytes(path: String) -> Result<Response, String> {
    let data = fs::read(&path).map_err(|e| format!("读取文件失败：{e}"))?;
    Ok(Response::new(data))
}

/// 应用数据目录（不存在时创建）。
fn data_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let dir = app.path().app_data_dir().map_err(|e| e.to_string())?;
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    Ok(dir)
}

/// 给临时文件名去重用的自增序号（同一次运行里两次写不会撞同一个名字）。
static TMP_SEQ: AtomicU64 = AtomicU64::new(0);

/// **原子写**：先写同目录下的临时文件并 `sync_all`，再 `rename` 覆盖目标。
///
/// ⚠️ **不要用 `fs::write` 落盘用户数据。** 它是 `O_TRUNC` 之后直接写 ——
/// 写一半断电 / 磁盘写满，磁盘上就留一个**截断的文件**。
/// 对 `state.json` 后果特别重：下次启动 JSON 解析失败 → 内存里是空状态 →
/// 用户随手打开一份文档就把**最近列表、收藏、阅读进度一起覆盖掉**
/// （见 `load_app_state` 的注释与 `tests/e2e/e2e34`）。
///
/// `rename` 在**同一个文件系统内**是原子的：读的人要么看到完整的旧文件，
/// 要么看到完整的新文件，不会看到写了一半的。临时文件必须放在**同一个目录**里，
/// 否则 `rename` 会退化成跨设备拷贝，就不原子了。
///
/// `sync_all` 不能省：只 `rename` 不保证内容已经落盘，断电后仍可能是个空文件。
fn write_atomic(path: &Path, data: &str) -> Result<(), String> {
    let dir = path
        .parent()
        .ok_or_else(|| "目标路径没有父目录".to_string())?;
    let stem = path
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "data".into());
    // 带 pid + 序号：多个写任务同时在飞时也不会互相截断同一个临时文件。
    let tmp = dir.join(format!(
        ".{stem}.tmp-{}-{}",
        std::process::id(),
        TMP_SEQ.fetch_add(1, Ordering::Relaxed)
    ));

    let result = (|| -> Result<(), String> {
        let mut file = fs::File::create(&tmp).map_err(|e| format!("写入临时文件失败：{e}"))?;
        file.write_all(data.as_bytes())
            .map_err(|e| format!("写入临时文件失败：{e}"))?;
        file.sync_all()
            .map_err(|e| format!("同步到磁盘失败：{e}"))?;
        Ok(())
    })();
    if let Err(err) = result {
        let _ = fs::remove_file(&tmp);
        return Err(err);
    }

    fs::rename(&tmp, path).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        format!("替换文件失败：{e}")
    })
}

/// 读取某份文档的批注 JSON；文件不存在时返回空数组。
///
/// ⚠️ **只有 `NotFound` 才当成"这份文档还没有批注"**，其它读取错误（权限、I/O）
/// 一律返回 `Err`。
///
/// 早期版本是 `Err(_) => Ok("[]")` —— 把"读不出来"和"没有批注"混成同一个结果。
/// 前端拿到空列表后又会立刻把它存盘回写，于是**一次读取失败就把用户攒下的批注
/// 全删了**。错误必须在类型上分得开，不能靠调用方去猜。
#[tauri::command]
pub fn load_annotations(app: AppHandle, id: String) -> Result<String, String> {
    let file = data_dir(&app)?
        .join("annotations")
        .join(format!("{id}.json"));
    match fs::read_to_string(&file) {
        Ok(content) => Ok(content),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok("[]".into()),
        Err(err) => Err(format!("读取批注失败：{err}")),
    }
}

/// 覆盖写入某份文档的批注 JSON（**原子写**，见 [`write_atomic`]）。
#[tauri::command]
pub fn save_annotations(app: AppHandle, id: String, data: String) -> Result<(), String> {
    let dir = data_dir(&app)?.join("annotations");
    fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    write_atomic(&dir.join(format!("{id}.json")), &data)
}

/// 读取全局应用状态（最近文件、阅读进度等）。
///
/// ⚠️ **只有 `NotFound` 才当成"还没存过"**，其它读取错误（权限、I/O）一律返回 `Err`。
///
/// 这条原来写的是 `Err(_) => Ok("{}")` —— 和 [`load_annotations`] 早期那个
/// `Err(_) => Ok("[]")` 是**同一个错误**：把"读不出来"和"没有数据"混成同一个结果。
/// 前端拿到 `{}` 会当成"最近列表是空的"，紧接着的存盘就把**用户的整个最近列表、
/// 所有收藏、阅读进度**一次写没（`tests/e2e/e2e34` 钉的就是它）。
///
/// 错误必须在类型上分得开，不能靠调用方去猜 —— 上面那条已经吃过一次亏了。
#[tauri::command]
pub fn load_app_state(app: AppHandle) -> Result<String, String> {
    let file = data_dir(&app)?.join("state.json");
    match fs::read_to_string(&file) {
        Ok(content) => Ok(content),
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => Ok("{}".into()),
        Err(err) => Err(format!("读取应用状态失败：{err}")),
    }
}

/// 写入全局应用状态（**原子写**，见 [`write_atomic`]）。
#[tauri::command]
pub fn save_app_state(app: AppHandle, data: String) -> Result<(), String> {
    write_atomic(&data_dir(&app)?.join("state.json"), &data)
}

/// 把**读不出来 / 解析不了**的 `state.json` 改名留档，返回新文件名。
///
/// 只在**前端解析失败**时调用。`load_app_state` 只判断得了"读得出来"，
/// 判断不了"解析得了"（它不做 JSON 解析）—— 而截断的 JSON 恰恰是读得出来、
/// 解析不了。前端一旦发现自己解析失败，就先来这里把旧文件改名保住，
/// 再当"没有状态"继续用。
///
/// 这样两头都不亏：
/// - **不静默丢弃** —— 旧文件原样躺在旁边（`state.json.corrupt-<毫秒>`），
///   用户或我们事后都能捞回来；
/// - **不把应用卡死** —— 留档之后就是正常流程，用户这次的打开记录 / 收藏照样存得住。
///   （另一条路是"读不出来就整个会话都不落盘"，语义更稳但用户会以为收藏坏了 ——
///   那是**看得见**的坏，见 `persist_state` 的注释。）
///
/// 文件本来就不在时返回 `Ok(None)`：没什么可留档的，这是正常的首次启动。
#[tauri::command]
pub fn quarantine_app_state(app: AppHandle) -> Result<Option<String>, String> {
    let file = data_dir(&app)?.join("state.json");
    if !file.exists() {
        return Ok(None);
    }
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    // 同一毫秒里连着留档两次也别互相覆盖：撞了就往后加序号。
    let mut backup = file.with_file_name(format!("state.json.corrupt-{stamp}"));
    let mut seq = 1;
    while backup.exists() {
        backup = file.with_file_name(format!("state.json.corrupt-{stamp}-{seq}"));
        seq += 1;
    }
    fs::rename(&file, &backup).map_err(|e| format!("留档失败：{e}"))?;
    Ok(Some(
        backup
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default(),
    ))
}

// ---------------------------------------------------------------- 卡片右键菜单的两个动作

/// 在资源管理器里定位并选中这个文件。
///
/// 走 `tauri-plugin-opener` 的 **Rust API**，而不是它的 JS API ——
/// 前端根本不碰这个插件，`capabilities/default.json` 也就不用为它开权限
/// （见项目约定：原生能力一律在 Rust 侧调用，capabilities 保持最小）。
///
/// Windows 上它落到 `explorer /select,"<path>"`，正是资源管理器里
/// 「右键 → 打开文件所在的位置」那种效果（定位到文件、并把它选中）。
#[tauri::command]
pub fn reveal_in_explorer(app: AppHandle, path: String) -> Result<(), String> {
    let target = Path::new(&path);
    if !target.exists() {
        return Err("文件已经不在了".into());
    }
    app.opener()
        .reveal_item_in_dir(target)
        .map_err(|e| e.to_string())
}

/// 把文件**移到回收站**，不是直接抹掉。
///
/// 为什么不用 `fs::remove_file`：删错了还能从回收站捞回来，
/// 这也正是资源管理器里按 Delete 删文件的行为。
///
/// `trash` crate 在 Windows 上走 `IFileOperation`（`SHFileOperation` 的现代替代），
/// 会正确进回收站、支持「还原」（右键还原或 Ctrl+Z）—— 不需要任何额外权限，
/// 也不弹系统对话框。
///
/// ⚠️ 这个 crate 在 Windows 上**必须先确定调用线程的 COM 初始化模式**，否则
/// **故意**编不过（`trash-5.2.9/src/windows.rs:323` 那句
/// `0 = "THIS IS AN ERROR ON PURPOSE..."`）。本仓库在 `Cargo.toml` 里显式选了
/// `coinit_apartmentthreaded`（Tauri 的窗口线程本来就是 STA），说明见那里的注释。
///
/// ⚠️ 这个命令**自己不做二次确认** —— 调用方（前端）必须先问过用户。
#[tauri::command]
pub fn trash_file(path: String) -> Result<(), String> {
    let target = Path::new(&path);
    if !target.exists() {
        // 已经不在了也当成功：用户的意图是"让它别在那儿"，
        // 报错反而会让前端把记录留着，出现"记录在、文件没了"的僵尸条目。
        return Ok(());
    }

    trash::delete(target).map_err(|e| format!("移到回收站失败：{e}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `trash_file` 必须**真的**把文件弄走。
    ///
    /// 这是整个应用里唯一一条"删用户文件"的路径，所以光"编译过了"不算数 ——
    /// `trash` crate 在 Windows 上走 `IFileOperation`，权限一变就可能静默失败。
    /// 这里拿一个自己造的临时文件试，只断言"文件不在了"，不碰用户任何东西。
    #[test]
    fn trash_file_really_removes_the_file() {
        let path =
            std::env::temp_dir().join(format!("folia-trash-test-{}.txt", std::process::id()));
        fs::write(&path, b"test").expect("造临时文件");
        assert!(path.exists(), "临时文件没造出来：{}", path.display());

        trash_file(path.to_string_lossy().into_owned()).expect("trash_file 应该成功");

        assert!(
            !path.exists(),
            "文件还在，说明 trash_file 没生效：{}",
            path.display()
        );
    }

    /// 文件本来就不在时应当**当成功**，而不是报错。
    ///
    /// 报错的话前端会把记录留着，变成"记录在、文件没了"的僵尸条目。
    #[test]
    fn trash_file_is_ok_when_already_gone() {
        let path = std::env::temp_dir().join("folia-trash-test-definitely-missing.txt");
        let _ = fs::remove_file(&path);
        assert!(trash_file(path.to_string_lossy().into_owned()).is_ok());
    }

    /// 原子写：内容要完整，而且**不能在目录里留下临时文件**。
    ///
    /// 这条是"写坏文件"那条路的正面守卫 —— `fs::write` 是 O_TRUNC 后直接写，
    /// 写一半断电就留一个截断的文件，而 `state.json` 一旦截断，
    /// 下次启动就会被当成"没有数据"整个覆盖掉（见 `load_app_state` 与 e2e34）。
    #[test]
    fn write_atomic_replaces_content_and_leaves_no_litter() {
        let dir = std::env::temp_dir().join(format!("folia-atomic-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("造目录");
        let target = dir.join("state.json");

        write_atomic(&target, "first").expect("首次写");
        assert_eq!(fs::read_to_string(&target).unwrap(), "first");

        // 覆盖写：内容要是**新的**，不能是"旧的 + 新的"或空的。
        write_atomic(&target, "second-longer-payload").expect("覆盖写");
        assert_eq!(
            fs::read_to_string(&target).unwrap(),
            "second-longer-payload"
        );

        let leftovers: Vec<String> = fs::read_dir(&dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .map(|e| e.file_name().to_string_lossy().to_string())
            .filter(|name| name != "state.json")
            .collect();
        assert!(leftovers.is_empty(), "留下了临时文件：{leftovers:?}");

        let _ = fs::remove_dir_all(&dir);
    }

    /// 原子写失败时也不能留下垃圾（父目录不存在 → 直接报错）。
    #[test]
    fn write_atomic_cleans_up_when_target_dir_is_missing() {
        let dir = std::env::temp_dir().join(format!("folia-atomic-missing-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        let target = dir.join("nope").join("state.json");

        assert!(write_atomic(&target, "x").is_err());
        assert!(!dir.exists(), "失败路径不该把目录造出来");
    }
}

// ---------------------------------------------------------------- 默认打开方式（Windows）

/// 我们注册给 `.pdf` 的 ProgId（程序标识符）。
#[cfg(windows)]
const PDF_PROG_ID: &str = "Folia.PDF";

/// ProgId 在「打开方式」列表里显示的名字。
#[cfg(windows)]
const PDF_PROG_NAME: &str = "Folia PDF";

/// 读 `.pdf` 当前的默认打开方式（返回 ProgId）。
///
/// 优先看 Windows 自己维护的 `UserChoice` —— 用户在「默认应用」里选的那份，
/// 从 Windows 8 起它就是**权威来源**。它不存在时退回 `HKCU\Software\Classes\.pdf`
/// 的默认值（老式关联）。
#[cfg(windows)]
fn current_pdf_handler() -> Result<Option<String>, String> {
    use winreg::enums::{HKEY_CURRENT_USER, KEY_READ};
    use winreg::RegKey;

    let hkcu = RegKey::predef(HKEY_CURRENT_USER);

    // 1) 用户显式选过的默认程序。
    if let Ok(key) = hkcu.open_subkey_with_flags(
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf\UserChoice",
        KEY_READ,
    ) {
        if let Ok(prog_id) = key.get_value::<String, _>("ProgId") {
            if !prog_id.is_empty() {
                return Ok(Some(prog_id));
            }
        }
    }

    // 2) 没有 UserChoice 时看 HKCU 的扩展名关联（默认值就是 ProgId）。
    if let Ok(key) = hkcu.open_subkey_with_flags(r"Software\Classes\.pdf", KEY_READ) {
        if let Ok(prog_id) = key.get_value::<String, _>("") {
            if !prog_id.is_empty() {
                return Ok(Some(prog_id));
            }
        }
    }

    Ok(None)
}

/// 把 Folia 注册进 `.pdf` 的「打开方式」候选。
///
/// 写三处（全部在 `HKCU`，**不需要管理员权限**）：
/// 1. `Software\Classes\Folia.PDF` —— 我们自己的 ProgId，带打开命令与图标；
/// 2. `Software\Classes\.pdf\OpenWithProgids` —— 把 ProgId 挂进 `.pdf` 的候选列表；
/// 3. `Software\Classes\Applications\folia.exe\...` —— 按「可执行文件」再登记一份，
///    这样右键「打开方式 → 选择其他应用」里也能直接看到 Folia。
#[cfg(windows)]
fn register_pdf_handler() -> Result<(), String> {
    use winreg::enums::HKEY_CURRENT_USER;
    use winreg::RegKey;

    let exe = std::env::current_exe().map_err(|e| format!("取当前程序路径失败：{e}"))?;
    let exe = exe.to_string_lossy().to_string();
    // 命令里路径要带引号（可能含空格），`%1` 是系统传进来的文件路径。
    let command = format!("\"{exe}\" \"%1\"");

    let hkcu = RegKey::predef(HKEY_CURRENT_USER);
    let classes = r"Software\Classes";

    // 1) ProgId 本体。
    let (prog, _) = hkcu
        .create_subkey(format!(r"{classes}\{PDF_PROG_ID}"))
        .map_err(|e| format!("创建 ProgId 失败：{e}"))?;
    prog.set_value("", &PDF_PROG_NAME)
        .map_err(|e| format!("写 ProgId 名称失败：{e}"))?;
    prog.set_value("FriendlyTypeName", &PDF_PROG_NAME)
        .map_err(|e| format!("写类型名失败：{e}"))?;
    let (cmd, _) = prog
        .create_subkey(r"shell\open\command")
        .map_err(|e| format!("注册打开命令失败：{e}"))?;
    cmd.set_value("", &command)
        .map_err(|e| format!("写打开命令失败：{e}"))?;
    let (icon, _) = prog
        .create_subkey("DefaultIcon")
        .map_err(|e| format!("注册图标失败：{e}"))?;
    icon.set_value("", &format!("\"{exe}\",0"))
        .map_err(|e| format!("写图标失败：{e}"))?;

    // 2) 挂到 `.pdf` 的「打开方式」候选（REG_SZ 空串即可）。
    let (open_with, _) = hkcu
        .create_subkey(format!(r"{classes}\.pdf\OpenWithProgids"))
        .map_err(|e| format!("注册「打开方式」失败：{e}"))?;
    open_with
        .set_value(PDF_PROG_ID, &"")
        .map_err(|e| format!("注册「打开方式」失败：{e}"))?;

    // 3) 按可执行文件登记一份（右键「打开方式」里能看到）。
    let app_name = std::path::Path::new(&exe)
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "folia.exe".into());
    let (app_cmd, _) = hkcu
        .create_subkey(format!(
            r"{classes}\Applications\{app_name}\shell\open\command"
        ))
        .map_err(|e| format!("注册应用程序失败：{e}"))?;
    app_cmd
        .set_value("", &command)
        .map_err(|e| format!("写应用程序命令失败：{e}"))?;
    let (supported, _) = hkcu
        .create_subkey(format!(r"{classes}\Applications\{app_name}\SupportedTypes"))
        .map_err(|e| format!("注册支持类型失败：{e}"))?;
    supported
        .set_value(".pdf", &"")
        .map_err(|e| format!("写支持类型失败：{e}"))?;

    Ok(())
}

/// 打开 Windows「设置 → 应用 → 默认应用」，让用户自己点一下确认。
///
/// 用 `cmd /C start ""` 而不是直接 `Command::new("ms-settings:…")` ——
/// 只有 `start` 才会走 shell 的协议处理。`CREATE_NO_WINDOW` 免得闪一个黑框。
#[cfg(windows)]
fn open_default_apps_settings() -> Result<(), String> {
    use std::os::windows::process::CommandExt;
    // `CREATE_NO_WINDOW`
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    std::process::Command::new("cmd")
        .args(["/C", "start", "", "ms-settings:defaultapps"])
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("打开系统设置失败：{e}"))
}

/// 现在 PDF 是不是默认由 Folia 打开。
///
/// 返回 `Ok(None)` 表示当前没有登记默认程序。
#[tauri::command]
pub fn default_pdf_handler() -> Result<Option<bool>, String> {
    #[cfg(windows)]
    {
        Ok(current_pdf_handler()?.map(|prog_id| prog_id.eq_ignore_ascii_case(PDF_PROG_ID)))
    }
    #[cfg(not(windows))]
    {
        Ok(None)
    }
}

/// 把 Folia 加入 `.pdf` 的「打开方式」，并打开系统设置页。
///
/// ⚠️ **不能直接替用户改默认程序。** Windows 8 起，`.pdf` 的最终归属由
/// `…\FileExts\.pdf\UserChoice` 决定，而这个键带一个**微软私有的哈希校验**：
/// 程序自己写进去会被系统判定为「被篡改」并忽略（这是有意设计，防的就是安装包
/// 静默劫持默认程序）。所以这里只做两件**正当**的事：
///   1. 把 Folia 注册进 `.pdf` 的「打开方式」候选（见 [`register_pdf_handler`]）；
///   2. 打开「设置 → 应用 → 默认应用」，由用户自己选一下。
///
/// 因此这个命令**不会**让 `default_pdf_handler` 立刻变成 `true` —— 真正的
/// 默认要等用户在设置里点完。前端据此把按钮留着，下次启动再查一次。
#[tauri::command]
pub fn set_default_pdf_handler() -> Result<(), String> {
    #[cfg(windows)]
    {
        register_pdf_handler()?;
        open_default_apps_settings()
    }
    #[cfg(not(windows))]
    {
        Err("目前只在 Windows 上支持设置默认打开方式".into())
    }
}
