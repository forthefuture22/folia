# e2e 测试套件

Folia 的端到端测试。**这些原先散落在 `/tmp/e2e*`，重启机器就全没了**，
2026-10-06 收进仓库 —— 仓库里只有约 1 MB 的代码，运行产物全部现场生成。

## 怎么跑

```bash
# 1) 先构建前端产物。套件会从 dist/ 复制一份站点出来，dist 不存在会直接报错。
trunk build

# 2) 跑某一套（每套都是独立脚本，可以单独跑）
python tests/e2e/e2e25/verify.py
```

前置：Python 3、headless Chrome（路径写死在 `verify.py` 里：
`/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`）。

**⚠️ 前台跑，别放后台。** 后台任务会丢沙箱提权，套件复制 `dist/` 时读
`dist/public/pdfjs/pdf.mjs` 会被拒（`file-read-data`），一批套件会一起变红，
看着像代码坏了。

## 每套是怎么工作的

`verify.py` 里有个 `build_site()`，负责把站点现场搭出来：

1. `rm -rf site/` 然后从 `dist/` 整份复制过来；
2. 删掉 trunk 注入的热重载 `<script>`（否则 headless 里会一直重连）；
3. 调 `theme_pin.pin_light()` **把 `prefers-color-scheme` 钉死成浅色**
   —— 不钉的话宿主机到点自动转深色，用例会「白天绿、晚上红」；
4. 把本套的 `harness.js` 复制成 `site/harness-stub.js`，并在 `bridge.js`
   之后插一行 `<script type="module" src="/harness-stub.js">`；
5. 起一个绑 `127.0.0.1:0`（随机端口）的 `http.server`，跑 headless Chrome。

**所以 `site/`、截图、`chrome-profile/`、`run.log` 之类都是运行产物，
不用入库** —— 见同目录的 `.gitignore`。

`theme_pin.py` 是共用依赖（被 17 个 `verify.py` import），放在本目录下。

**⚠️ 一条 shell 命令里连跑多套会撞上批量删除保护。** 每套的 `build_site()` 开头都是
`rm -rf site/`，而 `site/` 是 `dist/` 的整份副本（数千个文件）—— 一次删这么多会触发
`safe-delete` 的批量确认（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），于是**这一套在建站点
阶段就崩了**（`CalledProcessError: rm -rf ...`），看着像代码坏了。
**一次跑一套**，或者撞上了就单独重跑那一套。

## 有断言的回归套件（25 套 / 479 条判据）

| 套件 | 判据 | 覆盖 |
| --- | --- | --- |
| e2e17 | 73 | 批注 / 文本层 / 选区几何 |
| e2e18 | 32 | 阅读区渲染 + 缩略图 |
| e2e19 | 28 | 字体 / 工具栏 |
| e2e21 | 8 | 跳页（End / 页码框） |
| e2e22 | 30 | 检索 / 冷启动待打开 / 默认程序 / 总览 / 吐司 |
| e2e23 | 26 | 深色模式取色（正文/浮层/右键菜单） |
| e2e24 | 7 | 冷启动待打开文件 + 历史不丢 |
| e2e25 | 23 | 拖入 / `folia://open-path` / 重开同一份 |
| e2e26 | 17 | 书架「设为默认」 |
| e2e27 | 56 | 右键菜单 |
| e2e28 | 43 | 批注右键菜单 |
| e2e29 | 9 | 上限淘汰要释放缩略图缓存 |
| e2e30 | 6 | 重开同一份不抹掉收藏 |
| e2e31 | 7 | 续读进度不被覆盖成第 1 页 |
| e2e32 | 7 | 10 条上限不挤掉收藏 |
| e2e33 | 6 | Esc 回书架不掉标签 |
| e2e34 | 12 | 读不出 `state.json` 时不覆盖磁盘 |
| e2e35 | 6 | 缩略图实例寿命（用完即放 / 当前文档复用主实例，含对照组） |
| e2e36 | 17 | 四处内存上限：页面位图 / pdf.js 算子表 / 检索文本缓存 / 缩略图路径的算子表（②③④ 有对照组） |
| e2e37 | 9 | 150 页文档：总览滚到远离阅读位置处，按需补齐不被淘汰焦点打死（含手工对照） |
| e2e38 | 9 | 并发打开两份文档：串行化 + 两个标签都不丢（含手工对照） |
| e2e39 | 6 | 书架缩略图：删掉一份之后，旧任务不再渲染已不在列表里的文件（含手工对照） |
| e2e40 | 10 | 深色模式：PDF 里的位图图片不该被反相（含 `REGRESS` 对照组） |
| e2e41 | 29 | 图片层：算子表→四边形的单测 + 深色图片层的内存不变量（含 `REGRESS` 对照组） |
| e2e42 | 3 | 缩略图：同一份文件**并发**渲染两次，先回来的那个不能 destroy 掉还在飞的实例（含 `REGRESS` 对照组） |

⚠️ **`e2e37` 钉的是一个真实回归**（已修）：缩略图缓存的淘汰焦点取的是「阅读位置」，
而总览是**所有页**的网格 —— 用户在网格里滚到远离阅读位置处时，刚渲染完的那张自己
就是「最远」的，于是**渲染 → 淘汰 → 推版本号 → 补齐 effect 重跑 → 再渲染**，转不停
（实测 80 秒渲染 15,823 次），可视区的格子永远没图，进度文案永远停在 `N / 150`。
修法：淘汰焦点换成 `thumb_focus()`（总览开着时取网格可视区中心页），且 `put_thumb()`
返回「这张有没有留下来」——**插进去立刻被淘汰就不推版本号**。这套还专门加了
「缓存确实被推到了上限」的前置判据，否则只请求 48 张 < 上限 60，主判据会**假绿**。

⚠️ **e2e27 / e2e28 打的是 `OK` 前缀，不是 `[PASS]`** —— 用 `grep '\[PASS\]'` 数会得到 0，
看着像挂了两套。按汇总行数，或者直接看结尾。

⚠️ **`e2e17` / `e2e23` 需要 `PIL`（Pillow）** —— 默认的 `python3` 没有装，只有
`~/.workbuddy-ai/binaries/python/envs/default/bin/python` 有。这两套用默认解释器跑会
在 `from PIL import Image` 直接 `ModuleNotFoundError`，看着像代码坏了。

⚠️ `e2e23` 早期被记成"探针套件"，其实它的 `verify.py` 有 26 条真断言、会打
`[PASS]`/`[FAIL]`。这里已改归到上面的表里。

## 对照组（`REGRESS=1`）能覆盖到哪一层

带对照组的套件（`e2e17` / `e2e18` / `e2e35` / `e2e36` / `e2e40` / `e2e41` / `e2e42`）在 `REGRESS=1`
时会把站点里那份源码**按锚点字符串替换**回修好之前的样子，然后重跑一遍：**只有"主角那几条"变红、
对照组保持绿**，才算证明根因找对了（否则红的是别的东西，结论不成立）。

⚠️ `REGRESS=1` **不是所有套件都认** —— `e2e33` / `e2e34` 的 `verify.py` 里没有这段代码，
给它们加环境变量只会原样跑一遍主跑（拿到的分数和主跑一样是**正常**，别当成对照组失效）。

`e2e36` 当前是 **3 条补丁 → 4 条红**（`TEXT_CACHE_LIMIT` 那条同时打掉 `textCache` 与
`viewCache` 两条判据），跑出来应该是 **13/17**：

| 补丁锚点 | 变红的判据 | 保持绿 |
| --- | --- | --- |
| `MAX_DPR = 1.5` → `2` | dpr 上限 | 虚拟化那 6 条 |
| `TEXT_CACHE_LIMIT = 16` → `Infinity` | textCache / viewCache | 检索命中、`<mark>` 高亮 |
| `if (!isInReader(pageNumber \|\| 1))` → `if (false)` | `thumbCleanups > 0` | 缩略图本身还画得出来 |

⚠️ ③ 的锚点跟着实现走：守卫从 `if (borrowed)` 挪到了内层 `if (!isInReader(...))`
（阅读区正在看的那几页要**跳过** —— `page.cleanup()` 清的是整个 `_intentStates`，
连阅读区那条 DISPLAY 记录一起清掉，而侧栏会自动滚到当前页、和阅读区看的是同一批页，
反复清等于让每次滚动都重解析一遍内容流）。锚点对不上时补丁会**静默失效**、对照组全绿。

`e2e42` 是 **1 条补丁 → 1 条红**，跑出来应该是 **2/3**：

| 补丁锚点 | 变红的判据 | 保持绿 |
| --- | --- | --- |
| `if (!borrowed) releaseThumbDoc(key);` → `dropThumbDoc(key)` | 并发两次都拿到真 JPEG | 只读一次文件、收尾后实例被放掉 |

⚠️ `e2e42` 的判据**与"谁先回来"无关**：并发那一对里页 1 用 40px、页 4 用 1200px，
面积差约 600 倍，先回来的必然是小的那次 —— 但即使调度翻转，旧实现下**总有一次**
拿到空串，所以断言写成"两个都得是真 JPEG"，不依赖虚拟时钟下的具体顺序。

`e2e40` 是 **1 条补丁 → 3 条红**，跑出来应该是 **7/10**：

| 补丁锚点 | 变红的判据 | 保持绿 |
| --- | --- | --- |
| `quads.push(unitSquareQuad(m));` → 短路 | 深色图片变粉 / 图片层没建 / 切回深色没重建 | 浅色那 3 条配对判据、文字反相 |

⚠️ `e2e40` 的锚点必须打在**收集图片四边形**那一步（源头），不能只关 `renderPage` 里
那句 `paintImageLayer` —— 图片层有**两条**建立路径（渲染完成时、主题切回深色时），
只关前者的话，harness 里"切浅色再切回深色"会把它重新建起来，主判据**假绿**。踩过。

`e2e41` 是 **4 条补丁 → 6 条红**，跑出来应该是 **23/29**：

| 补丁锚点 | 变红的判据 | 保持绿 |
| --- | --- | --- |
| `} else if (fn === OPS.paintImageXObjectRepeat) {` → `else if (false)` | `units.repeat`（88 号分支） | 85/86 号那几条、`group`(87) |
| `} else if (fn === OPS.paintInlineImageXObjectGroup) {` → `else if (false)` | `units.group`（87 号分支） | `repeat`(88)、85/86 号那几条 |
| `  forgetPageImages(pageNumber);` → 注释掉 | 滚动后 `imageQuads` / `canvases` 涨到 24、`quads` 涨到 30 | DOM 的 `layers`（4，被 Yew 摘掉）、配对判据 |
| `      if (darkImagesOn) {` → `if (true)` | 浅色开局 `imageQuads == 0` | 浅色各拍 `layers == 0`（切浅色时会 `clearImageLayer`） |

⚠️⚠️ **e2e41 踩过的坑：补丁打在一条走不到的分支上 = 假绿。** 这一套最早的三条判据是
"数第 1 页同一行上有几块"，本意是覆盖 pdf.js 的图片合并算子 `paintImageXObjectRepeat`(88)，
对照组补丁也打在那条分支上 —— 结果**主跑绿、对照也绿**。查下来是：本版本 pdf.js 的
`PDFPageProxy.getOperatorList()` **硬编码传 `isOpList = true`**，而 `OperatorList` 构造时
`streamSink && !(intent & RenderingIntentFlag.OPLIST)` 才用 `QueueOptimizer`
→ **走 `NullOptimizer`，永远不合并**（只有 `render()` 那条路才合并）。
于是那 6 个戳记在算子表里就是 6 条独立的 `paintImageXObject`(85)，补丁根本打不到它们。

教训两条：

- **写判据前先确认"被测的那条分支真的会被走到"** —— 用真样本凑不出来的时候，
  就把逻辑抽成**纯函数**直接喂合成输入（`quadsFromOps` 就是这么抽出来的，
  `?mode=units` 用 `import("/public/pdfjs/pdf.mjs")` 拿真的 `OPS`/`Util` 喂进去）。
- **对照组的分数和主跑一样时，第一反应是"补丁没打到"，不是"这个不变量本来就成立"。**

⚠️ 顺带纠正一个曾经的性能假设：**`page.getOperatorList()` 不是 `render()` 的缓存命中。**
两者 `renderingIntent` 不同（前者多一个 `OPLIST` 位），`cacheKey` 不同 →
`_intentStates` 里是**两条独立记录** → worker 会**再解析一遍内容流**、并**多留一份算子表**
直到 `page.cleanup()`。所以图片层只在**深色**下收集（浅色是大多数人的默认状态，
不该付这份钱），浅色期间渲染的页靠 `pageCanvases` 记下 transform，切深色时补收。

⚠️ **锚点补丁必须直接改站点里的源码。** 在外面包一层函数只能盖住"外面看得见"的
那半边（`e2e35` 的 `OLD_BRIDGE_PATCHES` 就是直接改 `site/public/pdfjs/bridge.js`）。

⚠️ **判断落在 Rust/wasm 里的那条，没法用 `REGRESS` 验证。** 站点里是**编译好的 wasm**，
文本替换改不动它 —— 比如 `e2e36` 的「页面位图虚拟化」（`layout.rs` 的 `keep_range`）
和 `e2e34` 的「读失败不当没有数据」（`handles.rs`）。这类判据只能用
**绝对判据**（`canvas 数 < 页数`）+ **配对判据**（当前页照样画得出来）兜住。
要真验证它会红，得回到改动前重新 `trunk build` 再跑。

**`e2e37` / `e2e38` / `e2e39` 的判据全在 Rust/wasm 里**（淘汰焦点、打开队列、任务代次），
所以这三套**没有 `REGRESS=1` 自动化对照**，它们的对照实验是手工做的：把 Rust 源码里
那一处临时改回旧行为 → `trunk build` → 重跑 → 看是否只红主角那几条 → 还原。
三套都做过，结果如下（`6/9`、`6/9`、`5/6` 就是对照态）：

| 套件 | 临时改回 | 对照结果 | 红在哪 |
| --- | --- | --- | --- |
| `e2e37` | `thumb_focus` 只认 `current_page` | 6/9 | 可视区 24 格全空、等也等不回来 |
| `e2e38` | `request_open` 不排队（直接 `spawn_local`） | 6/9 | `sizes:B` 出现两次、A 的标签显示 `/ 37` |
| `e2e39` | 书架 effect 里 `superseded = \|\| false` | 5/6 | 删掉的那份又被渲染 1 次、配对判据里两个任务重复渲染 |

⚠️ **对照实验之后一定要全文扫描补丁残留**：本会话出过一次事故 —— 用 Python 改文件时
一个 `AssertionError` 吞掉了写入，源码里残留着 `let superseded = || false; // ==== 临时对照补丁 ====`，
表现为「对照组效果」被当成真实现跑，直到配对判据露出马脚才发现。扫描关键字：
`对照补丁` / `if false` / `|| false` / `&& false`（注意排除正常的 `use_state(|| false)`）。

⚠️ 反过来说：**判据在 `bridge.js` 里、但值由 Rust 侧驱动的那条也钉不住** ——
`e2e36` 的 `thumbCleanups` 分支在 `bridge.js`（能改），但它涨不涨取决于侧栏
（Rust/wasm）有没有来要缩略图。所以它**同时**配了"缩略图照样画得出来"这条配对判据：
补丁只让计数归零，不会连带把缩略图搞坏。

## 探针套件（有 verify.py，但只打印、没有 PASS/FAIL）

`e2e13` / `e2e14` / `e2e20` —— 保留是为了留证据，**不是回归门禁**，
跑完 `grep "通过"` 一无所获别当成失败。

## 写"真渲染"的套件时，虚拟时钟有三个坑（e2e35 / e2e40 踩全了）

套件用 `--dump-dom --virtual-time-budget=N` 拿结果，代价是**虚拟时钟在主线程空闲时
会一路快进**。要真跑 pdf.js 的渲染（`page.render()`）就会连踩三下：

1. **`requestAnimationFrame` 不触发。** pdf.js 的 canvas 渲染器靠 rAF 调度，
   虚拟时钟下 rAF 永远不回调 → `page.render()` 的 promise **永远不 resolve**，
   表现为"卡在取到 page 之后"。补一句 `window.requestAnimationFrame = cb => setTimeout(() => cb(performance.now()), 16)`
   即可（`e2e18` / `e2e35` / `e2e36` / `e2e40` 都有这一对补丁）。
2. **光补 rAF 还不够，虚拟时钟跑得比真实时间快约 1000:1。** 等 worker 回复时主线程
   恰好空闲 → 虚拟时间瞬间烧掉几万毫秒，而 worker 只拿到几毫秒真实 CPU →
   `page.render()` **时过时不过**（同一份代码上一轮过、这一轮挂）。
   "多给点预算"治不了根。办法是挂一个自续期的定时器，每拍做一小段**真实**忙等，
   把比例压到 ~16:1 —— 见 `e2e35/harness.js` 里的 `paceVirtualClock()`。
   ⚠️ 忙等的量只能写死成循环次数：`Date.now()` / `performance.now()` 在虚拟时钟下
   **也是虚拟的**，页面内没有可信的真实时钟。
3. **不能直接 `await` 那个 promise。** 主线程一空闲，虚拟时钟立刻快进到预算耗尽 ——
   pdf.js 的 worker 是**真实**线程，根本来不及回。症状极具迷惑性：DOM 是好的、
   `document.title` 停在半路（比如 `C:imported`）、截图却已经拍完了（全白/全黑）。
   办法是**持续 tick 的轮询**等它（`e2e18` / `e2e40` 的 `waitFor`）：
   `const tick = () => { if (ok()) return resolve(); setTimeout(tick, 10); }` ——
   每一拍都在给虚拟时钟"喂"一小步，同时留出真实时间让 worker 干活。
   ⚠️ 但**条件本来就不成立时别用 `waitFor`**：它会一直等到预算耗尽，`result` 反而
   写不出去（`e2e40` 的对照实验里踩过：图片层本来就建不起来，"等它出现" = 永远等）。
   那种"不假设结果"的等待要用固定空转的 `settle()`。

⚠️ 顺带：**`--screenshot` 和 `--dump-dom` 各跑一次 Chrome，状态不共享。** 截图拍的
是**脚本跑完时的 DOM 状态** —— 脚本若在中途切过主题，记得切回去（`e2e40` 的浅色那趟
就因此红过一次：它最后切回了深色，配对判据量到的是深色画面）。

另外：**一个套件一次别跑太多阶段**。预算烧完后面的阶段就永远完不成（表现为"卡在第 N 阶段"）。
`e2e18` 因此把书架/阅读器拆成 `?mode=shelf` / `?mode=reader` 两次运行，`e2e35` 拆
`?mode=shelf` / `?mode=main`，`e2e36` 拆 `?mode=page` / `?mode=scroll` / `?mode=search`。

### ⚠️ 判断"这一页画出来了"别用 `canvas.width > 1`

浏览器给一个刚 `createElement("canvas")` 的**默认尺寸就是 300×150** —— `width` 早就是
300 了，`waitFor(() => c.width > 1)` 会**立刻通过**，等于没等（`e2e36` 就因此假绿过：
滚到底之后"最后一页画出来了"那条其实是立刻返回的，样本里是块没画过的默认画布）。

`renderPage` 是**唯一**会写 `canvas.style.width` 的地方，所以正确的判据是
`c.style.width !== ""`。同理，统计"挂着的 canvas 里哪些真画过"也要用它来筛。

### ⚠️ `style.width` 只说明"开始画了"，不说明"画完了"

`renderPage` 是先写 `canvas.width/height/style.width`、再 `page.render()`、**最后**
才去做渲染后那一段（比如 e2e41 的"抠图片层"）。所以 `painted()` 成立时那一段**还没跑** ——
`e2e41` 就因此读到过 `canvases=0` 和 `canvases=1` 两个不同的值（**不稳定**）。

真正的收尾信号是 **`renderTasks` 归零**：`renderPage` 的 `finally` 排在渲染后那一段
**之后**，所以它一归零就说明这一页的整条链走完了。写 `waitFor(() => statNum("renderTasks") === 0)`
（`e2e41` 的 `rendersDone()`）。

### ⚠️ 一步跳到底的滚动，验不出"翻过去的页有没有还账"

`e2e41` 要验"页离开保留窗口时，图片层的记录有没有还回去"。最初是一步
`scrollTop = scrollHeight` —— 结果是**起点那几页 + 终点那几页**，中间十几页
**根本没渲染过**，于是主跑 `imageQuads=2`、对照组 `=5`，都挤在阈值 8 下面，
**判据形同虚设**（对照也绿）。改成分 12 步滚（每步 `waitFor(rendersDone)`）之后，
主跑 `=4`、对照组 `=24`（全书页数），才分得开。

**要验"不随次数累积"，就得真的让它累积足够多次。**

### ⚠️ "有没有释放"这类**不可观测行为**，用副作用计数当探针

`page.cleanup()` 到底放掉了什么，DOM 上看不见，pdf.js 也没有"这一页的算子表还在不在"
的公开口子。硬要断言只能靠猜。做法是：在**真实现**外面包一层计数（`bridge.releasePage`），
或者在被测分支里原地埋一个计数器（`cacheStats().thumbCleanups`）。

两条纪律：

- **必须保留真实现**（`const real = bridge.releasePage.bind(bridge); bridge.releasePage = p => { n.push(p); return real(p); }`）。
  整个换掉就等于在测自己的桩。
- **必须断言"增量"而不是绝对值**。`e2e35` 的判据是"第二次比第一次多读 1 次文件"，
  而不是"一共读了 2 次" —— 后者会被别的路径的读取带偏。

### ⚠️ 大 Map 当 props：`PartialEq` 会逐条比字符串

`Rc<RefCell<HashMap<u32, String>>>` 作为 Yew props 传下去时，每次渲染都要 `PartialEq`
一次 —— `Rc` **没有**指针捷径，会老老实实逐条比对几百 KB 的 data URL（缩略图正是
这种情况）。修法是一层只比指针的 newtype（`state/handles.rs` 的 `ThumbStore`：

```rust
impl PartialEq for ThumbStore {
    fn eq(&self, other: &Self) -> bool { Rc::ptr_eq(&self.0, &other.0) }
}
```

顺带把"整张表 clone 进 state"一起去掉：那在整本生成缩略图时是 **O(N²) 的字符串拷贝**，
store 自己就是唯一真相，state 只留一个版本号信号（`thumbs_gen`）。

## 早期探针（e2e ~ e2e12、e2e15、e2e16）

Folia 早期排查具体问题时写的一次性脚本（拖拽、悬停、标签、选区、布局对比等），
**路径已按仓库结构改写，但没有再逐个跑通验证**。留着当历史参考。

## 跨套件依赖（只有一处）

`e2e23/real_shot.py` 要借 `e2e18` 的真实 pdf.js 管线出深色模式截图。它读两样东西：

- `e2e18/harness.js` —— 在仓库里；
- `e2e18/sample.pdf` —— **生成物，不入库**（`*.pdf` 被 gitignore）。

所以这两套之前要先补生成物：

```bash
python tests/e2e/e2e18/make_pdf.py      # 生成 tests/e2e/e2e18/sample.pdf
python tests/e2e/e2e18/verify.py        # 32 条断言
python tests/e2e/e2e23/real_shot.py     # 出浅色/深色两张图，肉眼比对
```

`e2e18/verify.py` 自己**不会**调 `make_pdf.py`，缺 `sample.pdf` 会直接 `cp` 失败。
其余套件各自独立，互不依赖。

## 搬进来时改了什么

原脚本里硬编码了 `/tmp/e2eNN` 和仓库的绝对路径。搬进来时统一改成从本文件位置推导：

```python
_HERE = _pl.Path(__file__).resolve().parent   # tests/e2e/<suite>
_E2E  = _HERE.parents[0]                       # tests/e2e
_REPO = _HERE.parents[2]                       # 仓库根
```

所以**仓库放在哪儿都能跑**，不再依赖 `/tmp` 的具体路径。

剩下的 `/tmp/e2eNN` 字样都在注释/docstring 里（说明这套原来在哪），不影响运行。
唯一需要留意的例外是 `e2e18/harness.js` 里那个 `path: "/tmp/e2e18/sample.pdf"` ——
它只是桩返回给应用的**标签**（用来推 `doc_id`），字节是从站点自己的
`/sample.pdf` 取的，所以那个路径**不需要真实存在**，改了反而会动到 `doc_id`。
