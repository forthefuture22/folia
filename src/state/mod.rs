//! 应用状态层：领域类型 + 状态机。
//!
//! 这一层**不渲染**，只回答"数据是什么、怎么变"。渲染在 `app.rs`，
//! 纯展示换算在 `ui/` —— 三者之间的依赖是单向的：
//! `app.rs` → `state/` → `pdf/`、`ui/`，反过来没有。

pub mod document;
pub mod handles;
pub mod history;
