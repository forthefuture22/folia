//! 前后端共享的数据模型。
//!
//! 与 `public/pdfjs/bridge.js` 返回的 JSON 字段一一对应，注意 camelCase 的 serde 重命名。

use serde::{Deserialize, Serialize};

/// 打开一份 PDF 后的元数据（由 Rust 命令返回）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DocMeta {
    pub id: String,
    pub path: String,
    pub name: String,
    pub size: u64,
    #[serde(rename = "numPages")]
    pub num_pages: u32,
}

/// `openDocumentDialog` / `openDocumentPath` 的统一返回。
#[derive(Debug, Clone, Deserialize)]
pub struct OpenResult {
    pub ok: bool,
    #[serde(default)]
    pub cancelled: bool,
    #[serde(default)]
    pub error: Option<String>,
    #[serde(default)]
    pub meta: Option<DocMeta>,
}

/// 单页在 scale = 1 时的尺寸。
#[derive(Debug, Clone, Copy, PartialEq, Deserialize)]
pub struct PageSize {
    pub width: f64,
    pub height: f64,
}

/// 目录树节点。
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct OutlineNode {
    pub title: String,
    #[serde(rename = "pageNumber")]
    pub page_number: Option<u32>,
    #[serde(default)]
    pub bold: bool,
    #[serde(default)]
    pub italic: bool,
    #[serde(default)]
    pub children: Vec<OutlineNode>,
}

/// 一条搜索命中。
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct SearchHit {
    pub page: u32,
    #[serde(rename = "itemIndex")]
    pub item_index: u32,
    #[serde(rename = "charIndex")]
    pub char_index: u32,
    pub length: u32,
    /// 与文本层 `<mark data-key>` 一一对应的标识。
    pub key: String,
    /// 命中在页面内的归一化纵向位置（0..1），用于滚动定位。
    pub y: f64,
    pub snippet: String,
}

/// 归一化矩形（相对页面宽高的 0..1 比例，缩放无关）。
#[derive(Debug, Clone, Copy, PartialEq, Serialize, Deserialize)]
pub struct Rect {
    pub x: f64,
    pub y: f64,
    pub w: f64,
    pub h: f64,
}

/// 批注的**样式**：同一段选区可以画成四种样子。
///
/// 只有高亮是整块淡色填充，另外三种是画在文字上的线 —— 但四者共用同一套几何
/// （归一化矩形）、同一个 `color`、同一条增删改链路，所以没必要拆成四个类型。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum MarkKind {
    /// 高亮：整块淡色填充。
    #[default]
    Highlight,
    /// 下划线：矩形底边上一条实线。
    Underline,
    /// 删划线：矩形中线上一条实线。
    Strike,
    /// 波浪线：矩形底边上一条波浪。
    Wavy,
}

impl MarkKind {
    /// 写进 CSS 类名的名字。
    ///
    /// 必须和上面 `serde(rename_all = "lowercase")` 的结果一致 ——
    /// 前者决定落盘 JSON 里存什么，后者决定页面上画成什么，两边错开就会出现
    /// 「存进去了但画不出来」。四个变体都是单个单词，所以 lowercase 就是全小写。
    pub fn css(self) -> &'static str {
        match self {
            Self::Highlight => "highlight",
            Self::Underline => "underline",
            Self::Strike => "strike",
            Self::Wavy => "wavy",
        }
    }

    /// 给人看的中文名。
    ///
    /// 用在两处：批注列表每条的样式名（左侧圆点只说明**颜色**，看不出画的是哪种），
    /// 以及正文里右键菜单的「删除下划线」这类文案。
    /// **两边必须是同一份**，否则同一个东西在侧栏叫"删划线"、在右键菜单叫"删除线"。
    pub fn label(self) -> &'static str {
        match self {
            Self::Highlight => "高亮",
            Self::Underline => "下划线",
            Self::Strike => "删划线",
            Self::Wavy => "波浪线",
        }
    }
}

/// 一条批注（高亮 / 下划线 / 删划线 / 波浪线）。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Highlight {
    pub id: String,
    pub page: u32,
    /// 颜色名：yellow / green / blue / pink。
    pub color: String,
    pub text: String,
    #[serde(default)]
    pub note: String,
    /// 样式。旧数据没有这个字段，一律按「高亮」读（那时候也只有高亮）。
    #[serde(default)]
    pub kind: MarkKind,
    pub rects: Vec<Rect>,
    #[serde(default)]
    pub created_at: f64,
}

/// 选区信息（由 JS 侧计算几何）。
#[derive(Debug, Clone, PartialEq, Deserialize)]
pub struct SelectionInfo {
    pub page: u32,
    pub text: String,
    pub rects: Vec<Rect>,
}

/// 最近打开记录。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RecentFile {
    pub id: String,
    pub path: String,
    pub name: String,
    pub size: u64,
    /// 总页数（0 表示未知）。
    #[serde(default)]
    pub pages: u32,
    /// 上次读到的页码，用于首页进度条与续读。
    #[serde(default)]
    pub last_page: u32,
    /// 上次打开的时间（Unix 毫秒），用于显示「昨天」这类相对时间。
    #[serde(default)]
    pub opened_at: f64,
    /// 是否已收藏（书架的「收藏」分页用它过滤）。
    #[serde(default)]
    pub favorite: bool,
}

impl From<&DocMeta> for RecentFile {
    fn from(meta: &DocMeta) -> Self {
        Self {
            id: meta.id.clone(),
            path: meta.path.clone(),
            name: meta.name.clone(),
            size: meta.size,
            pages: meta.num_pages,
            last_page: 1,
            opened_at: 0.0,
            favorite: false,
        }
    }
}

/// 落盘的全局状态。
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct PersistedState {
    #[serde(default, rename = "recentFiles")]
    pub recent_files: Vec<RecentFile>,
    #[serde(default, rename = "lastPath")]
    pub last_path: Option<String>,
    #[serde(default, rename = "lastPage")]
    pub last_page: Option<u32>,
    #[serde(default)]
    pub scale: Option<f64>,
}
