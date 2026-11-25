# PosterForest 怎么做的 —— 实现向拆解

来源：`extracted/arXiv-2508.21720v3/`（ACL 2026，arXiv 2508.21720v3 完整 LaTeX 源码，含 5 个 agent 的逐字 prompt）。
官方代码：https://github.com/kaist-cvml/poster-forest

下面每条标注 **[原文]**（源码里明确写了）或 **[推断]**（从 prompt / 图里反推，论文没写）。

---

## 1. 两阶段流水线

### 阶段 A：建树（一遍过，无循环）

| # | 阶段 | 输入 | 输出 | 用 LLM? |
|---|---|---|---|---|
| 0 | Docling 抽取 | 论文 PDF | markdown + `figures_info` / `tables_info` | 否 |
| 1 | **Parser Agent** | markdown + 图表清单 | **Raw Doc Tree** `T_Raw` | 文本 LLM |
| 2 | **Summarizing Agent** | （见 §3 的坑） | **Content Tree** `T_Content` | 文本 LLM |
| 3 | **Layout Init** | `T_Content` | **Layout Tree** `T_Layout` | **否，纯算法** |
| 4 | **Merge** | 二者 | **Poster Tree** `T_Poster^(0)` | 否 |
| 5 | Render | `T_Poster^(0)` | 海报文件 + 预览图 | 否 |

第 3 步明确**不是** agent：*"The initial allocation of regions is deterministically derived from content statistics"*（`03_method.tex:148`）**[原文]**。附录里原本有个 "Layout Init Agent" 小节，被注释掉了（`06_appendix.tex:464-468`）——他们把它从 agent 降级成了算法。这是个承重的设计决定。

### 阶段 B：精修循环

附录里被注释掉但内容完整的算法块（`06_appendix.tex:539-601`）是全文对这个循环最精确的描述：

```
T <- T_poster^(0)
for t = 0 .. T_max-1:                 # 树级迭代
    Q <- {Root(T)}                    # BFS
    while Q:
        n <- Q.pop()
        O_c <- A_Content(n);  O_l <- A_Layout(n)      # Stage 1 分析
        for k in range(K):                            # Stage 2 协作
            O_c, O_l <- A_Content(O_l), A_Layout(O_c)
        c*, l* <- A_Content(O_c,O_l), A_Layout(O_c,O_l)  # Stage 3 定稿
        UpdateNode(n, c*, l*);  Enqueue(children(n))
    if Eval(T): break                 # <- Feedback Agent，看渲染图
```

**BFS 根→叶，每个节点每轮访问一次；渲染 + Feedback 只在整轮遍历之间做，不是每节点做。**

节点更新签名注意上下文是不对称的（`03_method.tex:226-252`）：
- Layout 节点：`A_Layout(自身, 更新后的父, 所有后代)`
- Content 节点：`A_Content(自身, 更新后的父, 父的后代)` ← 也就是**兄弟**。这解释了为什么 Content prompt 有 `sibling_contents` 槽而没有 descendant 槽。

**迭代次数论文自相矛盾**：`03_method.tex:279` 说 *"we set K = 2"*；`06_appendix.tex:301` 说 *"T_max=2"*；被注释的 `06_appendix.tex:300` 说 *"T_max=2, K=1"*。取 **T_max=2, K=1**。字面实现是 6 次 LLM 调用/节点/轮，20 节点的树 ≈ 240 次调用/海报——demo 必须把 Stage 1–3 压成各一次调用。

---

## 2. 核心洞察：布局是递归二分切分，LLM 永远不写坐标

**这是整篇论文最值得抄的一点。** 三处独立证据：

1. `03_method.tex:141`：*"the canvas is hierarchically partitioned into regions organized by rows and columns"*
2. `assets/main_2.pdf`：每个内部节点只带**一个标量**（`row 0.7` / `col 0.5`），恰好 2 个子节点，画布上每次只有一条红色切分线。四帧之间**只有比例在变，拓扑从不变**：`0.7→0.6`、`0.5→0.3`、`0.6→0.7`、`0.5→0.7`
3. Layout Agent 的输出词汇表里只有「方向 + 幅度」，根本没有坐标字段

于是：**重叠在结构上不可能发生**。不需要 IoU 校验，不需要修复循环，没有「模型吐了个重叠的框」这种失败模式；画布全覆盖也是自动的。每次调用的搜索空间是「4 选 1 + 0~5 的序数」，弱模型也够用。packer 可以零 LLM 调用单测。

代价：**拓扑在 init 时就冻结，循环只能重新加权，不能重新编排。** 所以那个两句话带过的 `O_Layout_Init` 其实是你必须自己设计的最高杠杆组件——而论文什么都没给（`03_method.tex:149` 那句 "see supplementary" 是被注释掉的，附录里也没有）。

### Poster Tree 的三种节点（从 `main_1_new.pdf` / `main_2.pdf` 读出）**[推断，高置信]**

严格交替的语法：

```
PosterNode :=
    SplitNode    # 蓝圆，标 "row 0.7"/"col 0.5"  -> 恰好 2 个子节点
  | SemanticNode # 深绿框，标 "sec"/"subsec"     -> 恰好 1 个子节点
  | ContentLeaf  # 浅绿框，标 "cont"             -> 0 个子节点
```

形式化：`l = (r, x)`，`r ∈ {row split, column split, panel}`，`x` = *"normalized position, width, height, aspect ratio"*（`03_method.tex:147`）。

**`box` 必须是从根重算的派生缓存，任何比例变动都要失效重算。绝不让 LLM 写 box——这是整个架构的要点。**

### 两个命名陷阱

- 图里标 `row`/`col`，prompt 用 `split_type ∈ {vertical, horizontal}`。这是两套不同的描述轴（排列方向 vs 切线朝向），从渲染帧里无法无歧义地对上。**以 prompt 为准**：`vertical` = 竖直切线 → LEFT/RIGHT 两个子节点。别让 `row`/`col` 进代码。
- **delta→ratio 的换算论文完全没给。** `main_2.pdf` 里观察到的变动量是 0.1 和 0.2。必须在代码里**硬 clamp**，不能只靠 prompt 里那句 "Shape Sanity" 的恳求——VLM 被告知范围 0–5 就会连着返回 5，两轮下来 `0.7+0.2+0.2 = 1.1`，packer 直接退化。

---

## 3. 三个 Agent 的逐字输出格式

### Parser Agent (`prompts/parser_agent.tex`)

顶层键 `"tree"`，递归 6 字段节点：

```json
{"tree": {
  "id": "1.1",                  // 点分路径，根是 "root"，编码了深度
  "title": "Problem Statement",
  "type": "title|section|subsection|subsubsection",
  "content": "完整原文，不截断不摘要",
  "assets": {"figures": ["1"], "tables": [], "references": []},  // 恒为这三个字符串数组键
  "children": []
}}
```

要点：
- **图表不是节点**，而是 `assets` 里的 ID 引用。这跟正文 `03_method.tex:99` 列的 node types（含 figure/table）**不一致**，prompt 的 example 从不产出它们——以 prompt 为准。
- *"Preserve All Content: Include complete original text without truncation or summarization."*（line 72）——摘要是第 2 步的活。
- **Caption-Guided Strategy**（lines 28–38）：章节是**从图表 caption 诱导出来的**，不是纯靠标题。*"Group content around figures/tables with related captions"*。
- prompt 里把图表清单用 Jinja 循环注入，取 `fig_info.caption` 和 `fig_info.caption_number`——所以抽取适配器得产出 `{id: {caption, caption_number}}`。

### Summarizing Agent (`prompts/summarizing_agent.tex`) —— **不要照抄**

发表的 schema 是 `{metadata{title,author,"publish date",organization}, sections[{title, subsections[{title,content}]}]}`（注意字面键 `"publish date"` 带空格）。三个严重的坑：

1. 它消费 `{{ markdown_document }}` 而**不是** `T_Raw`，直接违背 Eq.2 `T_Content = A_Refine(T_Raw)`。照实现等于把上一步的树扔了重新解析 markdown。
2. **扁平化成深度 2**，摧毁 Parser 被要求保留的任意嵌套——而论文自己的 ablation (`fig:ablation_study_1`) 说层级结构是必需的（扁平化会把 results 表格塞到 Introduction 底下）。
3. **完全没有 `assets` 字段**，Parser 产出的图表绑定被丢弃。

这明显是个遗留的 slide 生成 prompt：*"for later PPT generation"*、*"each subsection contains approximately 500 words"*（而海报全文平均才 774 词）。

**正确做法**：在 `T_Raw` 上**逐节点**做 prune/merge/summarize，保留 6 字段 schema 和 `assets`，返回 `{id, action: keep|prune|merge_into:<id>, summary}`。正文支持这个读法（`03_method.tex:119-120` 提到 *"node-edge relationships... updated to reflect the revised structure"*）。

### Content Agent (`prompts/content_agent.tex`)

返回**裸 JSON 数组**（不是对象），schema 标注为 mandatory：

```json
[{"alignment": "left", "bullet": true, "level": 0, "runs": [{"text": "..."}]}]
```

这是 **python-pptx 的 paragraph 对象模型**，字段级对应（`paragraph.alignment` / `.level` / `.runs[].text`）——这反过来确认了渲染器是 python-pptx **[推断，高置信]**。

**`current_volume_ratio` 闭环是整个系统最重要的机制，而且很容易漏掉。**

- `current_volume_ratio` = 当前空间填充百分比
- 目标：尽可能接近 100% 但不超过；可接受区间 80–100%
- **必须由渲染器测量，不能让 LLM 猜**：把 bullets 按该 panel 的字号排进 `box`，`used_height / available_height * 100`

一个 agent 背后其实是**两套模板**，按测得的 ratio 路由：

| ratio | 模板 | 模板变量 |
|---|---|---|
| < 95 | **Expand** | `previous_response`, `current_content`, `sibling_contents`, `current_volume_ratio` |
| 95–100 | 不调用（目标区间） | — |
| > 100 | **Condense** | `previous_response`, `sibling_contents`, `current_volume_ratio` ← **故意没有 `current_content`** |

Condense 模板删掉 `current_content` 是用结构而非口头指令来强制「原文只用于扩写」这条规则。值得抄。

两套模板都有一句关键的对冲：*"if still under 95%, leave the rest for later passes"* / *"if still above 105%, leave deeper cuts for later passes"*——这是让它能跨 T_max=2 轮运行而不振荡的原因。95–100 的免调用带是实打实的省钱点（第一轮之后大多数节点就落在带内了）。

### Layout Agent (`prompts/layout_agent.tex`)

是个 **VLM**，看的是**标注过的裁剪图**：*"determine which panel to enlarge and by how much (Δ), based on figure legibility balance within the red analysis box. Inside the red box, green boxes represent panels and blue boxes represent figures."*

所以渲染器必须能出调试overlay：**红框** = 当前 split 节点的区域，**绿框** = 它的两个子 panel，**蓝框** = 里面的图。

唯一的文本输入是 `{{ split_type }}`。输出：

```json
{"direction": "<LEFT|RIGHT|TOP|BOTTOM>", "delta": 0-5, "reason": "..."}
```

- `vertical` → LEFT/RIGHT；`horizontal` → TOP/BOTTOM
- Δ：0 = 不调，1–2 = 很细微，3–4 = 中等，5 = 最大安全放大

判据是**图的可读性，明确不是面积**：
- 图表类看**感知字号**（坐标刻度、标签、图例、数字）
- 示意图/流程图类即使没文字，也按**结构复杂度**判（组件数、分支、箭头密度、嵌套子图）——*"Treat complex diagrams as if they contained implicit small text"*
- *"Do NOT optimize by raw figure size or panel area."*
- Shape Sanity：避免产生极端瘦高的 panel

⚠️ 发表的 few-shot examples 有复制粘贴 bug（RIGHT 那个例子的 reason 写成 *"Action: enlarge UP"*，LEFT 那个把 Rationale 拼成 *"Retionale"*）。别把这些抄进 few-shot，模型会学这个不一致。

### Feedback Agent (`prompts/feedback_agent.tex`)

**只看渲染出的海报图，别的什么都不给**——*"You are a scientific poster evaluator. You will see a poster image."* 不传树 JSON，不传文本。这是系统里唯一真正全局、真正多模态的检查点。

返回两个键，是**二元控制信号**而不是自由评论：

```json
{"reason": "<1-2 sentences>", "continue": 0}
```

（`continue` 是 Python 保留字，JSON 里保持这个键名，代码里绑到 `cont_flag`。）

三条判据 OR 起来：**visual organization**（对齐/留白/图文平衡/拥挤溢出）、**textual structure**（冗余/截断/缺上下文/各节详略不均）、**hierarchical balance**（节序/父子局部性/标题层级一致/分组连贯）。任一条失败 → `continue=1`。

循环退出条件：`continue==0` 或 `t` 到 **T_max=2**。所以**整个循环最多跑两轮**，预算就是每张海报最多 2 次渲染 + 2 次 feedback 调用。

**一个可以白捡的改进**：`O_Traverse(T_Poster^(t), F̂_Global^(t))`（`03_method.tex:294-298`）说 feedback 要传播进下一轮遍历，但 Content 和 Layout 的 prompt **都没有接收它的槽位**（它们只暴露 `previous_response`/`current_content`/`sibling_contents`/`current_volume_ratio`/`split_type`）。**如发表的实现里，`reason` 生成完就被丢掉了。** 给两个 agent 加一个 `{{ global_feedback }}` 槽，零成本，而且是报告里站得住脚的一条 delta。

另外 `feedback_agent.tex:149-260` 有个被注释掉的早期版本，返回的是**四路 router** 而不是二元 flag：`{"reason": ..., "label": "text"|"layout"|"both"|"good"}`。每次调用的信息量严格更大，能跳过不需要的 agent。值得复活。

---

## 4. 渲染

**[原文]**：
- 画布统一 **48 × 36 英寸**（4:3 横版），`06_appendix.tex:262`
- 基准海报平均 **774 词 / 8.7 张图**（`06_appendix.tex:280`）——用这个标定 `est_area` 常数和字号

**[推断]**：论文从没点名自己的渲染器，但 Content Agent 的输出 schema 就是 python-pptx 的段落模型，加上 Summarizing Agent 说 *"for later PPT generation"*、related work 建在 PPTAgent 上、baseline 表里量的是 *"Python-pptx conversion results of PPTAgent"* → **python-pptx**。图像走 `pptx → LibreOffice headless → pdf → png`。

**渲染器不是个哑序列化器**，它拥有三件 agent 依赖的东西：
1. **几何解析** —— 根向下按 ratio 切分 box，在每个语义节点预留 `header_h`。纯函数。
2. **`current_volume_ratio` 测量** —— 没有它 Content Agent 是瞎的。
3. **标注裁剪图生成** —— 给 Layout Agent 的红/绿/蓝 overlay。

---

## 5. 评测指标

**MLLM-as-a-Judge**（GPT-4o 当六个独立评委，各 1–5 分）：
- Aesthetic：element quality (`Elem.`) / layout balance (`Lay.`) / engagement (`Eng.`) → `Avg.`
- Information：clarity (`Cla.`) / content completeness (`Cont.`) / logical flow (`Logic`) → `Avg.`
- `Overall` = 两个组均值的均值

PosterForest-4o：Elem. 4.02 / Lay. 3.96 / Eng. 2.96 / Cla. 4.00 / Cont. 3.88 / Logic 3.71 / **Overall 3.76**。GT 海报 3.77，原始论文 3.90——**headline claim 是「接近 GT」而不是「最大化」**。Engagement 连人类 GT 都只有 2.70，基本没有判别力，别在它上面下注。

**User study**：25 名研究生，10 组 × 4 题，4 个方法随机序，强制单选，报「排第一的比例」。四个判据：content fidelity / aesthetic quality / structural clarity / overall quality。Ours：56.0 / 53.2 / 59.6 / 59.2%。（注意表头 `Esthetics` 是原文拼错；附录 `06_appendix.tex:328` 又换了一套叫法，全文三种命名变体——自己挑一套保持一致。）

**消融**（可以白捡的报告结构）：
- 层级 vs 扁平 Content Tree
- 四路 agent 消融：(a) base/仅初始树 (b) 仅 Content (c) 仅 Layout (d) 两者 ← 这个 1:1 对上我们 proposal 的 RQ1

---

## 6. 失败模式（设计时要绕开）

**论文明确报告的：**

1. **图表密集会搞崩 parser** —— 头号失败案例（`06_appendix.tex:363-372`）：*"struggles when processing papers containing a large number of figures or tables, especially when such assets are densely clustered or heavily interleaved within the text... errors during asset parsing and content matching, which can **propagate through subsequent stages**"*。**错误会向下游累积，没有恢复机制。** 对策：parse 后断言每个 asset ID 在树里恰好出现在一个节点的 `assets` 下，孤儿/重复的在 UI 里显式暴露而不是静默丢弃。
2. **内容密度次优**（`05_conclusion.tex:21`）—— 80–100% 的宽容带 + "留给后续轮次" 的对冲 + 只有 2 轮的直接后果。想赢过它：把带的下沿提到 90%，或者让第 3 轮由**测得的 ratio** 而不是 VLM 的意见来 gate。
3. **没有可靠的自动指标**（`05_conclusion.tex:22`）：*"Discrepancies between ground truth and generated posters are often better detected by human observers than by automated metrics."* 真正的 claim 靠 user study 撑。
4. **没有建模图的重要性**（`06_appendix.tex:409-415`）—— Layout Agent 只判**可读性**，从不判**重要性**。一张无关紧要的 teaser 和关键结果表被同等对待。便宜的改进：从 Content 阶段带一个 `importance` 分进 layout init 的权重。
5. 单 agent 消融的失败模式（`04_experiments.tex:529-532`）：只有 Content → *"suboptimal panel arrangements and unbalanced layouts"*；只有 Layout → *"suboptimal figure scaling and text overflow"*。两者真的互相依赖，别只上一个。

**论文没承认的结构性缺口：**

6. **拓扑 init 后冻结**，循环只能调比例。如果 `O_Layout_Init` 分错了组，再怎么迭代都救不回来——而论文对 `O_Layout_Init` 零细节。
7. **全局 feedback 生成完就丢**（见 §3）。
8. **Summarizing prompt 跟正文三处矛盾**，照实现会静默关掉论文自己的 ablation 说必需的层级能力。
9. **Layout Agent 从不看文字溢出**，只看图的可读性。没有图的 panel 给不了它任何可推理的东西，prompt 对这种情况行为未定义。要显式决定：无图子树跳过 layout 调用，或者把判据扩展到文字密度。

---

## 7. 我们 demo 的取舍

| 环节 | PosterForest | 我们 | 理由 |
|---|---|---|---|
| PDF 抽取 | Docling | PyMuPDF + caption 锚定裁剪 | 环境没装 Docling；caption 锚定对矢量图和位图都管用 |
| Summarizing | 扁平 depth-2、丢 assets | **在 `T_Raw` 上逐节点选，保留 assets** | 修 §3 的三个坑 |
| 布局表示 | 递归二分切分 + 标量 ratio | **照抄** | 非重叠是定理而非校验 |
| Layout Init | 无细节 | 列优先装箱 + 按 mass 的二分链 | 自己设计，最高杠杆 |
| 预览渲染 | LibreOffice headless | **PIL 直接渲染** | 环境无 LibreOffice；而且我们要精确控制文本测量来算 volume_ratio |
| 导出 | python-pptx | python-pptx（从已解析的 box） | 对上 proposal 承诺的交付物 |
| Feedback | 二元 flag | **复活被弃用的四路 router** | 每次调用信息量更大，能跳过不需要的 agent |
| global_feedback | 生成后丢弃 | **接进 Content/Layout 的 prompt** | 零成本的 delta |
| 多轮对话修改 | 无 | **我们的 RQ2** | PosterForest 完全没有这部分 |
