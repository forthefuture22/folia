//! 批注编辑的撤销栈。

use std::cell::RefCell;
use std::rc::Rc;

use yew::prelude::*;

use crate::pdf::types::Highlight;

/// 批注撤销栈最多记多少步。
///
/// 存的是整份快照，所以这里同时是内存上限：50 份 × 几十条批注，可以忽略。
pub(crate) const HISTORY_LIMIT: usize = 50;

/// 批注编辑的撤销栈。
///
/// 存的是**每次改动之前的整份快照**，不是 diff：批注最多几十条，一次 `Vec` 克隆的
/// 代价可以忽略，而快照永远不会出现「撤回完状态对不上」那种 diff 才有的问题。
///
/// 栈本身放 `RefCell`（它变的时候不需要渲染），但「能不能撤回」要驱动按钮的
/// `disabled`，所以额外用一个 state 镜像栈的深度。
#[derive(Clone)]
pub(crate) struct History {
    pub(crate) stack: Rc<RefCell<Vec<Vec<Highlight>>>>,
    pub(crate) depth: UseStateHandle<usize>,
}

impl History {
    /// 记下改动**之前**的样子。所有会改批注的地方都要先调它。
    pub(crate) fn push(&self, before: &[Highlight]) {
        let len = {
            let mut stack = self.stack.borrow_mut();
            stack.push(before.to_vec());
            // 只留最近 `HISTORY_LIMIT` 步：再往回撤也没人会用到，白占内存。
            if stack.len() > HISTORY_LIMIT {
                stack.remove(0);
            }
            stack.len()
        };
        self.depth.set(len);
    }

    /// 撤回一步，返回上一步的批注列表；栈空时返回 `None`。
    pub(crate) fn undo(&self) -> Option<Vec<Highlight>> {
        let (previous, len) = {
            let mut stack = self.stack.borrow_mut();
            let previous = stack.pop();
            (previous, stack.len())
        };
        self.depth.set(len);
        previous
    }

    /// 清空。换文档 / 切标签时必须调 —— 历史属于上一份文档，跟着过来就撤串了。
    pub(crate) fn clear(&self) {
        self.stack.borrow_mut().clear();
        self.depth.set(0);
    }
}
