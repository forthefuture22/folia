# Folia

**专注精读的桌面 PDF 阅读器。** 面向需要长时间盯着一份文档的场景：
连续滚动阅读、目录导航、全文检索、高亮批注，多份文档用标签页同时开着来回对照。

用 **Tauri 2 + Yew（Rust / WebAssembly）** 写：外壳是原生的 Rust 进程，
界面和阅读逻辑编译成 wasm 跑在系统 WebView2 里，PDF 解析交给 pdf.js。

> 这个仓库是 **Windows 版**。窗口、文件关联、回收站、默认打开方式都是按
> Windows 实现的，不含 macOS 分支。

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [从源码构建](#从源码构建)
- [项目结构](#项目结构)
- [架构](#架构)
- [功能说明](#功能说明)
- [快捷键](#快捷键)
- [数据存放位置](#数据存放位置)
- [测试](#测试)
- [Windows 上的几个关键设计](#windows-上的几个关键设计)
- [已知边界](#已知边界)
- [第三方组件](#第三方组件)
- [许可](#许可)

---

## 特性

**阅读**

- 连续滚动阅读，虚拟化渲染 —— 只画可视区附近的页，150 页的文档也不卡
- 目录（Outline）导航，支持层级与粗体 / 斜体标记
- 缩放：适应宽度、手动档位加减、`Ctrl + / - / 0` 重置
- 全部缩略图网格：一层盖出全文所有页，滚到哪补到哪，点哪儿跳哪儿
- 深色模式：跟随系统，**正文页面按主题重新合成**（文字反相、图片保持原色，
  见 [`composeDarkThumbnail`](public/pdfjs/bridge.js)）

**检索与批注**

- 全文检索（pdf.js 的 `getTextContent`），命中结果高亮 + 侧栏列表，可逐个跳转
- 选中文字浮出工具栏：4 种标注样式（高亮 / 下划线 / 删划线 / 波浪线）× 4 种颜色
- 批注可再编辑、可删除；侧栏「批注」页签集中列出
- 复制：选中文字直接复制，或从批注菜单复制原文

**书架**

- 最近打开列表（上限 10 条）+ 收藏分页
- 卡片显示首页缩略图（按主题烘焙，深浅两套）
- 右键菜单：收藏 / 取消收藏、在资源管理器中显示、删除记录、删除记录**和文件**（进回收站，删前二次确认）
- 「设为默认 PDF 阅读器」一键把 Folia 挂进系统「打开方式」

**其他**

- 多标签：同时开多份文档，各自保留缩放 / 滚动位置 / 批注状态
- 外部打开：双击 `.pdf`、「打开方式 → Folia」、把 PDF 拖到 `folia.exe` 图标上
- 拖放：把 PDF 拖进窗口即可打开
- 状态持久化：最近列表、收藏、阅读进度、批注，全部落盘且**原子写**

---

## 快速开始

仓库里 `windows-build/` 放了一份**编译好的产物**，拷到 Windows 10/11 机器上双击即可：

| 文件 | 说明 |
| --- | --- |
| `folia.exe` | 主程序（约 7.7 MB，前端资源已内嵌） |
| `WebView2Loader.dll` | **必须同目录**，Tauri 靠它加载 WebView2 |

> 前端资源是编进 exe 的，所以**不需要**起任何本地服务器。
> 如果启动后看到「localhost 拒绝连接」，说明这份 exe 是**开发模式**构建的
> （漏了 `tauri/custom-protocol`，见[下面](#3-手写-cargo-build-时必须带-custom-protocol)）。

`WebView2 Runtime`：Windows 11 和较新的 Win10 已预装。没有的话从
[微软官网](https://developer.microsoft.com/microsoft-edge/webview2/)装一次即可。

---

## 从源码构建

### 依赖

| 需要 | 用途 | 备注 |
| --- | --- | --- |
| **Rust 1.90+** | 前端和后端都是 Rust | [rustup.rs](https://rustup.rs)。MSRV 由 `tauri 2.12` 决定（它自己声明 `rust-version = "1.90"`） |
| `wasm32-unknown-unknown` target | 编译前端 wasm | `rustup target add wasm32-unknown-unknown` |
| **Trunk** | 打包前端到 `dist/` | `cargo install trunk --locked`，或用[预编译版](https://github.com/trunk-rs/trunk/releases) |
| **MSVC 构建工具** 或 **MinGW-w64** | 链接原生 exe | 见下方说明 |
| **WebView2 Runtime** | 运行 | 一般系统自带 |

**关于链接器**：官方推荐 MSVC（装 Visual Studio Build Tools 的「使用 C++ 的桌面开发」）。
如果**没有管理员权限**装不了 MSVC，可以走 MinGW 路线 —— 宿主工具链换成
`stable-x86_64-pc-windows-gnu`，用 [WinLibs 的 MinGW-w64](https://winlibs.com/)
（zip 包解到用户目录即可，不需要提权），编译时把 `gcc` 放进 `PATH`。
本仓库的 `windows-build/` 就是这样编出来的。

### 构建

```bash
# 1) 前端 -> dist/
trunk build --release

# 2) 后端 -> folia.exe
cargo build --release --target x86_64-pc-windows-gnu \
  --features tauri/custom-protocol \
  --manifest-path src-tauri/Cargo.toml
```

产物在 `<target>/x86_64-pc-windows-gnu/release/`：
`folia.exe` + `WebView2Loader.dll`（Tauri 构建时已复制过去，两个一起拷走才能跑）。

用 MSVC 工具链的话把 `--target x86_64-pc-windows-gnu` 去掉即可。

### 开发

```bash
cargo tauri dev      # 起 trunk serve + Tauri 窗口，改代码热重载
trunk build          # 只构建前端
cargo tauri build    # 打 NSIS 安装包（需要 tauri-cli）
```

---

## 项目结构

```
.
├── index.html                 入口：挂主题脚本 + 声明 trunk 资源
├── styles.css                 全部样式（含浅色 / 深色两套 tokens）
├── Cargo.toml                 workspace 根：前端 crate + [profile.release]
├── Trunk.toml                 trunk 配置（dev server 端口 1420）
├── preview-home.html          首页设计的独立预览页，浏览器直接打开即可
│
├── src/                       前端（Yew / Rust -> wasm）
│   ├── main.rs                入口：打平台标记 + 挂载 App
│   ├── app.rs                 应用主体：状态编排、工具栏、阅读区、交互（最大的一个文件）
│   ├── selbar.rs              「选中文字浮出工具栏」的落点计算（纯数字，带单测）
│   ├── layout.rs              滚动 / 缩放 / 翻页的几何计算
│   ├── dom.rs                 DOM 相关的零碎工具（选区、可见页、是否在输入框里…）
│   ├── ctx_menu.rs            右键菜单的几何与条目定义
│   ├── components/            UI 组件：侧栏、页面视图、书架卡片、图标、确认框
│   ├── state/                 状态：标签页、文档、最近列表、缩略图缓存
│   └── pdf/                   pdf.js 桥接（Rust 侧的调用封装 + 数据类型）
│
├── src-tauri/                 后端（Tauri / Rust）
│   ├── src/lib.rs             启动流程、单实例、命令行 argv、待打开槽位
│   ├── src/commands.rs        所有 IPC 命令：读写文件、持久化、回收站、默认程序
│   ├── tauri.conf.json        窗口 / 打包 / 文件关联
│   └── capabilities/          权限白名单（保持最小）
│
├── public/pdfjs/              pdf.js 4.10.38（运行时 + CMaps + 标准字体）
│   └── bridge.js              ★ 前端与 pdf.js 之间的桥接层（JS）
│
├── tests/e2e/                 端到端测试套件
└── windows-build/             编译好的产物
```

---

## 架构

三层，边界很清楚：

```
┌─────────────────────────────────────────────────────────────┐
│  后端（原生 Rust 进程）  src-tauri/                          │
│  · 文件读写、应用数据目录、原子写                             │
│  · 回收站、资源管理器定位                                     │
│  · 默认打开方式（注册表）                                     │
│  · 单实例 + 命令行 argv                                       │
└──────────────────────────┬──────────────────────────────────┘
                           │ Tauri IPC（invoke / emit）
┌──────────────────────────┴──────────────────────────────────┐
│  前端（wasm）  src/                                          │
│  · Yew 组件树、状态管理、交互逻辑                             │
│  · 书架 / 阅读器 / 侧栏 / 右键菜单                            │
└──────────────────────────┬──────────────────────────────────┘
                           │ window.FoliaBridge.*（JS 调用）
┌──────────────────────────┴──────────────────────────────────┐
│  桥接层（JS）  public/pdfjs/bridge.js                        │
│  · pdf.js 文档加载、页面渲染、文字层、检索                    │
│  · 缩略图合成（含深色烘焙）                                   │
│  · 图片层：把页面里的位图单独抠出来，深色下不被反相            │
└─────────────────────────────────────────────────────────────┘
```

**为什么中间要夹一层 JS**：pdf.js 是 JS 库，而界面是 Rust 编译的 wasm。
两边靠 `window.FoliaBridge` 这个全局对象通信 —— `bridge.js` 是
`<script type="module">`，与 wasm 初始化存在竞态，所以 Rust 侧有
`pdf::ready()` 做一次有限等待（见 [`src/pdf/mod.rs`](src/pdf/mod.rs)）。

**原生能力一律在 Rust 侧调用**：比如「在资源管理器中显示」走
`tauri-plugin-opener` 的 **Rust API**，而不是它的 JS API ——
前端根本不碰那个插件，`capabilities/default.json` 也就不用为它开权限。

---

## 功能说明

### 书架

启动后的第一屏。两个分页：

- **最近**：最近打开过的文档，上限 **10 条**，新的挤掉旧的，但**收藏不会被挤掉**
- **收藏**：手动收藏的文档

卡片显示首页缩略图（JPEG data URL）。缩略图用**独立于阅读文档的 pdf.js 实例**渲染，
所以没打开任何文档时也能显示。书架顶部有搜索、排序、网格 / 列表视图切换，
以及「清空记录」（只清记录，不动文件）。

**右键菜单**（4 个动作 + 一条分隔线）：

```
收藏 / 取消收藏
──────────────────
在资源管理器中显示
删除记录            ← 只从「最近」里拿掉，文件还在
删除记录和文件       ← 红色，进回收站，删前二次确认
```

### 阅读器

- **连续滚动 + 虚拟化**：只有可视区附近的页真正渲染，滚出窗口的页会释放
  canvas 位图和 pdf.js 的解析产物（算子表）
- **全部缩略图网格**：点顶栏的「全部缩略图」按钮盖出一层所有页的网格。
  缩略图**按需补齐** —— 只请求可视区那一片，滚到哪补到哪
  （早期是开的时候一口气渲染 150 张，而缓存上限只有 60 张，等于白做 90 次；
  详见 [`tests/e2e/README.md`](tests/e2e/README.md) 里 `e2e37` 那段）
- **缩放**：适应宽度（`fit_scale` 按阅读区可用宽度反算，夹在 0.25×–5×）、
  手动加减档、重置 100%。新文档打开时**自动适宽**
- **翻页**：方向键 / PageUp / PageDown / 空格 / Home / End，见[快捷键](#快捷键)
- **工具栏**：加高亮、放大 / 缩小 / 适宽、**撤回上一步批注改动**、立即保存到本地

### 侧栏

四个页签：

| 页签 | 内容 |
| --- | --- |
| **缩略图** | 每页预览，当前页高亮，点哪儿跳哪儿 |
| **目录** | PDF 的 Outline，支持层级 / 粗体 / 斜体，点击跳转 |
| **批注** | 当前文档所有标注，可定位、可删除 |
| **搜索结果** | 检索命中列表，带上下文片段，逐个跳转 |

### 批注与高亮

选中文字后浮出工具栏：**4 种样式**（高亮 / 下划线 / 删划线 / 波浪线）×
**4 种颜色**（黄 / 绿 / 蓝 / 粉）+ 复制按钮。

标注按页存储，坐标是**相对页面尺寸的归一化值**，所以换缩放级别不会漂。
标注可再次右键编辑样式或删除。

### 检索

`Ctrl + F` 会**同时**展开侧栏并切到「搜索结果」页签再聚焦输入框
（输入框长在那个面板里，面板没挂载时光 focus 是空的）。
输入有去抖，命中结果在正文里用 `<mark>` 标出，当前那条样式不同。

### 深色模式

主题只有**一个入口**：`html.theme-dark` 这个类。由 `index.html` 里一段
**同步脚本**按 `prefers-color-scheme` 挂上 —— 放在 wasm 起来之前，
是为了不闪一下白屏。

写成类而不是 `@media (prefers-color-scheme: dark)`，好处是系统偏好和
以后可能加的手动开关走**同一份颜色定义**，不会各漂各的。

主题切换会广播 `folia-theme` 事件 —— 因为有些缩略图是**按主题烘焙**的
（深色那张是把整页反相、再把图片按原色贴回去合成出来的），主题一变就过期，
需要作废重烘。

### 多标签

每份文档是一个 `DocTab`，各自保存：缩放级别、滚动位置、批注列表、
侧栏状态。切标签时整体替换「当前文档的实时状态」。

同时打开两份文档时**串行化**处理，保证两个标签都不丢。

---

## 快捷键

| 动作 | 快捷键 |
| --- | --- |
| 聚焦检索框（并展开侧栏「搜索结果」） | `Ctrl + F` |
| 放大 | `Ctrl + =` 或 `Ctrl + +` |
| 缩小 | `Ctrl + -` |
| 缩放重置为 100% | `Ctrl + 0` |
| 下一页 | `→` / `PageDown` / `空格` |
| 上一页 | `←` / `PageUp` |
| 第一页 | `Home` |
| 最后一页 | `End` |
| 关闭右键菜单 / 关闭总览 / 退出输入 / 返回书架 | `Esc`（按层级从最上面一层开始关） |

翻页那组按键只在**没在输入框里**、且**已经打开文档**时才生效
（`is_typing()` + `meta.is_some()` 两道闸门），所以在检索框里按空格不会跳页。

---

## 数据存放位置

```
%APPDATA%\com.anemone.folia\
├── state.json                  全局状态：最近列表、收藏、阅读进度
└── annotations\
    └── <doc-id>.json           每份文档的批注
```

`<doc-id>` 由**文件路径**派生，所以同一条路径每次运行结果一致。

### 落盘是「不能出错」的路径

这部分代码比看上去啰嗦，但每一条都是踩出来的：

**① 原子写，不用 `fs::write`。**
`fs::write` 是 `O_TRUNC` 之后直接写 —— 写一半断电或磁盘写满，磁盘上就留一个
**截断的文件**。所以统一走「写同目录临时文件 → `sync_all` → `rename` 覆盖」。
`rename` 在同一文件系统内是原子的，读的人要么看到完整的旧文件、要么看到完整的新文件。

**② 「读不出来」和「没有数据」必须分得开。**
早期版本是 `Err(_) => Ok("[]")` —— 把读取失败（权限 / I/O）和「这份文档还没批注」
混成同一个结果。前端拿到空列表后又会立刻存盘回写，于是**一次读取失败就把用户
攒下的批注全删了**。现在只有 `NotFound` 才算「没有」，其它错误一律返回 `Err`。

**③ 解析不了的状态文件要留档，不能丢也不能卡死。**
`load_app_state` 只判断得了「读得出来」，判断不了「解析得了」——
而截断的 JSON 恰恰是读得出来、解析不了。所以前端一旦发现自己解析失败，
就调 `quarantine_app_state` 把旧文件改名成 `state.json.corrupt-<毫秒>` 保住，
再当「没有状态」继续用。两头都不亏：不静默丢弃，也不把应用卡死。

---

## 测试

### 单元测试

**`selbar.rs` 的落点计算** —— 纯数字模块，不碰 DOM，所以不需要 wasm target、
也不用起 Tauri：

```bash
rustc --test src/selbar.rs -o "$TEMP/selbar_test.exe" && "$TEMP/selbar_test.exe"
```

覆盖 7 个分支：贴在选区上方、上方没空间时翻到下方、滚动导致翻面、
两侧都放不下时贴顶边、左右夹取、水平滚动下的夹取、退化容器不返回 NaN。

**后端命令的单测**（原子写、回收站）：

```bash
cargo test --manifest-path src-tauri/Cargo.toml
```

### 端到端测试

`tests/e2e/` 下有 **41 个编号目录**，其中 **28 个带 `verify.py`**（入口脚本），
有断言的回归套件共 **25 套 / 479 条判据**。

每套都是一条完整流水线：把 `trunk build` 的**真实产物** `dist/` 复制成
`site/`，注入桩（只桩掉 pdf.js 和 Tauri 这两个**外部依赖**，其余全是真的），
起一个临时静态服务器，用**无头 Chrome** 打开，合成事件、采样 DOM / 像素，
逐条打印 `PASS` / `FAIL` 并截图。

详见 [`tests/e2e/README.md`](tests/e2e/README.md) —— 里面有完整的套件表、
每组判据覆盖什么、以及 `REGRESS=1` 对照组怎么用（把源码按锚点字符串替换回
修好之前的样子，确认「主角那几条变红、对照组保持绿」，否则结论不成立）。

> ⚠️ **这套脚手架目前还是 macOS 的，在 Windows 上跑不了。** `verify.py` 里
> Chrome 路径写死成 `/Applications/Google Chrome.app/...`，还有 `/tmp/e2eNN`
> 之类的路径。应用本身已经是 Windows 版，**要改的只是这层脚手架** ——
> 换成 Windows 的 Chrome 路径、`shutil.rmtree`、以及当前机器的 cargo registry 路径。

---

## Windows 上的几个关键设计

这一节记的是「为什么代码长这样」，都是移植时踩过的。

### 1. 外部打开：命令行 argv + 单实例转发

macOS 上「双击 PDF」「拖到 Dock 图标」「打开方式」走的是 LaunchServices，
在 Rust 侧表现为 `RunEvent::Opened` 事件。

**Windows 没有这个机制** —— 这三种情况系统都是**再启动一次进程**，
把路径塞进命令行参数。所以分两条路：

| 场景 | 走法 |
| --- | --- |
| **冷启动**（应用没在跑） | 启动时 `first_pdf_arg(std::env::args())` 把路径存进 `PendingOpen` 槽位，前端起来后主动调 `take_pending_open` 取走并直接打开 |
| **热启动**（应用已经在跑） | `tauri-plugin-single-instance` 接住第二个进程的 argv，`emit("folia://open-path", path)` 推给前端，顺手 `unminimize` + `set_focus` |

⚠️ **槽位是「冷启动专用」的，热启动不能往里写。**
`take_pending_open` 全程只在启动 effect 里被调一次。热启动时如果往槽里写，
路径会**永久留在槽里没人取**，直到**下一次冷启动**才被取走 ——
表现为「正常开 App 却自动弹出一份上次已经关掉的文档」。

⚠️ 单实例插件**必须第一个注册**（Tauri 的硬要求）。

### 2. 默认打开方式：只能注册 + 跳设置，不能静默改

点「设为默认」按钮做两件事：

1. 把 Folia 注册进 `.pdf` 的**候选打开方式**（写 `HKCU\Software\Classes`，
   不需要管理员权限）：
   - `Folia.PDF` —— ProgId 本体（名称、图标、打开命令）
   - `.pdf\OpenWithProgids` —— 挂进候选列表
   - `Applications\folia.exe\SupportedTypes` —— 按可执行文件登记一份
2. 打开系统的「默认应用」设置页（`ms-settings:defaultapps`），由用户确认

**为什么不能直接设成默认**：Windows 8 起，`UserChoice` 键带**微软私有哈希校验**，
程序写入会被判定为篡改并**忽略**。所以「静默设默认」这条路在新系统上根本走不通，
骗人的按钮不如不给。查询（`current_pdf_handler`）是只读的，读
`FileExts\.pdf\UserChoice` 的 `ProgId`，回退到 `HKCU\Software\Classes\.pdf`。

### 3. 手写 `cargo build` 时必须带 `custom-protocol`

`tauri/custom-protocol` 是「告诉 Tauri 现在是**生产**环境、用编进 exe 的资源、
别去连 dev server」的开关。**`cargo tauri build` 会自动加上它**，
只有手写 `cargo build` 才会漏 —— 漏了的话 exe 会去请求 `http://localhost:1420`
（那是 `tauri dev` 起的开发服务器），报「拒绝连接」。

```bash
cargo build --release --features tauri/custom-protocol --manifest-path src-tauri/Cargo.toml
```

**验证办法**：先在 1420 端口起一个 TCP 监听，再启动 exe。
**一条连接都收不到**才说明走的是内嵌资源。

### 4. `[profile.release]` 必须写在 workspace 根

写在成员 crate（`src-tauri/Cargo.toml`）里 cargo 会**直接忽略**并警告
`profiles for the non root package will be ignored`。搬到根 `Cargo.toml` 之后
LTO + `strip = true` 才真正生效 —— 产物从 26.4 MB 掉到 7.7 MB 就是这个原因。

### 5. 两个编译期坑（`Cargo.toml` 里有详细注释）

**① `trash` 在 Windows 上会「故意」编不过。**
`default-features = false` 会连 `coinit_apartmentthreaded` 一起关掉，而 `trash`
必须先确定调用线程的 COM 初始化模式，于是留了一句
`0 = "THIS IS AN ERROR ON PURPOSE..."` 强制你二选一。
这里选 **`apartmentthreaded`**：Tauri 的窗口线程本来就是 STA（wry 建 webview 时
已经 `CoInitializeEx` 成 `APARTMENTTHREADED`），同模式再调一次是安全的；
反过来选 `multithreaded` 会撞 `RPC_E_CHANGED_MODE`，而 `trash` 对它直接 `panic!`。

**② `schemars 0.8` + `indexmap 1.x` 编不过（`E0107`）。**
`tauri-utils` 打开了 `schemars` 的 `preserve_order` 特性 → `schemars` 的
`Map<K, V>` 被定义成 `indexmap::IndexMap<K, V>`（只给两个参数）→ 而
`indexmap 1.9.3` 的 `IndexMap` 有没有第三个泛型参数的默认值，取决于它的
`has_std` cfg，这个 cfg 由 build.rs 决定：**开了 `std` 特性就直接 emit**，
没开就用 autocfg **探测** sysroot。麻烦在于 `indexmap 1.9.3` 的 `Cargo.toml` 里
**没有 `default` 特性**，`std` 默认是关的 —— 只能走探测，而探测可能静默失败。

修法是显式给 `indexmap` 开 `std`，**而且 `[dependencies]` 和
`[build-dependencies]` 两处都要写**：前者管**目标单元**，后者管**宿主单元**
（`schemars` 同时被 `tauri-utils` 当普通依赖、被 `tauri-build` 当构建依赖用），
cargo 的 host / target 特性是分开解析的。

试过但**没用**的路子（别再试）：用 `--cfg has_std` 走 rustflags ——
`[build]` / `[target.<host>]` / `RUSTFLAGS` 三种写法**都传不进宿主单元**；
把 `indexmap` 降到 1.8.2 —— 同一个 `has_std` 门控，没用。

### 6. 构建目录别放在会被同步 / 监听的文件目录里

如果仓库放在微信文件目录、网盘同步目录这类地方，文件监听和杀毒软件会**锁住**
刚写出的 `.rlib` 和 `dlltool` 生成的 import library，报的是
`os error 5 拒绝访问` 这种看起来像权限问题的错。把构建目录挪出去：

```bash
export CARGO_TARGET_DIR="C:/Users/<you>/.build/folia-target"
```

---

## 已知边界

- **e2e 脚手架还没移植到 Windows** —— `verify.py` 里 Chrome 路径和若干临时路径
  是 macOS 的（见[测试](#端到端测试)一节）。所以这个仓库目前**没有在 Windows 上
  跑过回归测试**，只有启动冒烟验证。
- **没打 NSIS 安装包** —— `tauri.conf.json` 里 `targets: ["nsis"]` 已经配好，
  但打安装包需要 `tauri-cli`。`windows-build/` 里是直接可运行的 exe，不装包也能用。
- **「设为默认」不能一键生效** —— 见[上面](#2-默认打开方式只能注册--跳设置不能静默改)，
  系统不允许程序静默改默认程序，点完按钮还要在设置页里确认一次。
- **没有代码签名** —— 首次运行可能弹 SmartScreen 警告。
- **深色模式下 PDF 里的位图图片**是单独一层处理的（把图片抠出来按原色贴回），
  逻辑在 `bridge.js`，有对应的 e2e 套件（`e2e40` / `e2e41`），但同样受上面第一条限制。

---

## 第三方组件

| 组件 | 版本 | 许可 |
| --- | --- | --- |
| [pdf.js](https://github.com/mozilla/pdf.js) | 4.10.38 | Apache-2.0（见 `public/pdfjs/LICENSE`） |
| pdf.js CMaps | 同 pdf.js | 见 `public/pdfjs/cmaps/LICENSE` |
| 标准字体（Foxit / Liberation） | — | 见 `public/pdfjs/standard_fonts/LICENSE_*` |
| [Tauri](https://tauri.app) | 2.12.1 | Apache-2.0 / MIT |
| [Yew](https://yew.rs) | 0.21 | MIT / Apache-2.0 |
| [tauri-plugin-single-instance](https://github.com/tauri-apps/plugins-workspace) | 2.5.2 | Apache-2.0 / MIT |
| [trash](https://github.com/Byron/trash-rs) | 5.2.9 | MIT |
| [winreg](https://github.com/gentoo90/winreg-rs) | 0.56 | MIT |

pdf.js 是**完整 vendored** 在仓库里的（`public/pdfjs/`），不走 npm ——
所以 clone 下来不需要 `npm install`，但升级 pdf.js 要手动替换那几个文件。

---

## 许可

**本仓库目前没有 LICENSE 文件。** 没有许可证默认意味着「保留所有权利」，
他人不能合法地使用、修改或分发。

如果你打算让这个项目开源，需要自己加一份 —— 常见选择：

- **MIT** / **Apache-2.0** —— 宽松，允许商用和闭源二次开发
- **GPL-3.0** —— 传染性，衍生作品也必须开源

注意 `public/pdfjs/` 下是 Mozilla 的 Apache-2.0 代码，保留原有许可证声明即可。
