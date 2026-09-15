# TestMatrix 每日任务指令生产规范

> 本文件定义每天产出「整块可复制实现任务指令」的固定结构、风格铁律与 Git 提交 SOP。
> 任务指令的消费者是 AI 编程工具：任务指令只负责精确指定改哪个文件、加什么类/函数签名、埋点位置、测试断言、验收标准，不负责写实现代码。
> 所有每天变化的数字一律使用占位符（见第四节），禁止硬编码。

---

## 一、任务指令固定段落结构

每天的任务指令必须按以下顺序组织，段落不可省略：

### 0. 开场对齐
```
先读取项目根目录的 PROJECT_CONTEXT.md 和 PROJECT_PLAN.md，对齐进度与本地约定。
今天是 Day{DAY}，任务是：{当日任务标题}。
{前一日已落地内容一句话概述}，今天只做{当日范围}，{明确排除项}不做。
```

### 1. 【当前现状（已确认）】
列出与当日任务相关的已有文件、类、函数签名、测试基线、已知约束。格式：
- 文件路径: 已有类/函数/常量
- 测试基线 {旧基线}，今天后 {新基线}（+{增量}条）
- 项目有无相关先例（全新设计 / 基于已有模块扩展）

### 2. 【任务一】~【任务四】
每个任务独立编号，必须包含：
- **目标文件**：精确到文件路径
- **改动性质**：新建 / 修改 / 零改动确认
- **设计要点**：类名、函数签名（含参数类型与返回值）、关键数据结构、边界处理
- **约束与铁律**：不抛异常、不阻断主流程、不修改既有契约等
- **零新第三方依赖**（除非当日任务明确要求）

### 3. 【验收标准】
量化条目，至少包含：
- `py -m pytest` 全量 {旧基线} → {新基线}，0 failed 0 warning
- 核心功能断言（帧格式、事件序列、字段齐全等）
- 既有模块零回归
- 代码质量（完整 docstring + 类型注解、中文逐行注释、无 print）
- commit message 摘要

### 4. 【收尾】
- PROJECT_CONTEXT.md：第 6 章新增 6.x（编号以 PROJECT_CONTEXT 现有小节顺延为准）设计决策、第 7 章追加新踩坑记录、第 5 节模块表加行、第 10 节变更日志加行、测试基线 {旧基线}→{新基线}
- PROJECT_PLAN.md：Day{DAY} 行 `[ ]`→`[✓]`、进度统计更新（提交数以 `git rev-list --count HEAD` 实测为准）
- git add 范围：只加源码和测试，禁止 add 本地文档与运行产物
- 全量 pytest 复核

### 5. Git 提交 SOP
固定附在任务指令末尾，见第三节。

---

## 二、风格铁律

1. **只写任务书，不贴完整实现代码**：任务指令指定「改哪个文件、函数签名是什么、埋点在哪、断言测什么」，实现代码由编程工具产出；贴完整代码既浪费上下文又会让编程工具机械照抄。
2. **具体到可执行**：每个任务必须精确到文件路径、类名、函数签名（含参数与返回值类型）、常量名、测试断言内容；禁止「完善一下」「优化一下」等泛泛表述。
3. **测试只增不减**：当日新增测试数与基线增量严格对应（如 +8 条），既有测试因行为变更需修改断言时必须明确指出改哪条、改成什么，不得静默删除。
4. **零非技术表述**：公开仓库全程禁止任何非技术、职业规划类表述，词表以本地私有约定为准，代码、注释、文档、commit message 均适用。
5. **中文逐行注释**：新增代码的关键逻辑行必须有中文注释解释意图；docstring 覆盖参数、返回值、异常。
6. **Windows + PowerShell 环境**：测试命令统一 `py -m pytest`，多命令用分号 `;` 分隔，禁止 bash 语法（`&&`、`export` 等）；git 命令加 `--no-pager`。
7. **不越界**：当日任务范围外的改动一律不做；需要修改既有契约时必须在任务指令中明确标注「有意行为变更」并说明影响面。
8. **过渡方案标注**：临时实现（如内存通道替代 Redis、裸线程替代任务队列）必须在 docstring 和设计决策中明写「过渡方案，Day{DAY} 替换」，防后期误读。
9. **测试命名与基建复用**：测试文件命名 `test_xxx_demo.py`，用例名 `test_场景_预期结果`；markers（如 `@pytest.mark.api` / `@pytest.mark.regression`）、fixture（临时 SQLite 库 + `DatabaseSession.reset()` + 种子造数 + autouse 清洁）遵循既有测试文件与 PROJECT_CONTEXT 约定，不另起一套命名体系。
10. **运行时坑必须带入**：任务指令涉及具体模块时，必须带上 PROJECT_CONTEXT 第 7 章中对应的运行时坑（如 SQLite `:memory:` 经 Path 拼接变文件、bool 是 int 子类、created_at 秒级精度倒序断言需跨秒、后台线程不得借用请求 session、流式读取须设帧数上限等），在相关任务的设计要点或测试约束中显式引用，防同类错误重复发生。

---

## 三、Git 提交 SOP（五段，任务指令末尾固定附）

### 第 0 步 先读远程，确认本地不落后

确认远程指向、拉取远程引用、查看与 origin/main 的关系：

```powershell
git remote -v
```

```powershell
git fetch origin
```

```powershell
git --no-pager status
```

```powershell
git --no-pager log --oneline -5 origin/main
```

```powershell
git --no-pager diff --stat origin/main
```

- `git fetch origin` 只拉远程引用，不动工作区和本地代码。
- `git status` 看与 origin/main 的关系：up to date / ahead / behind。
- `git log origin/main` 确认远程最新一条是 {前一日提交标题} 的 {REMOTE_HASH}。
- 若显示 **behind**（远程有本地没有的提交）：先 `git pull --rebase origin main`，有冲突解决完再继续。
- 若 **up to date** 或 **ahead**：直接往下。

### 第 1 步 不绿不提交

全量测试必须通过后才允许提交：

```powershell
py -m pytest -v
```

全量必须 {新基线} passed / 0 failed / 0 warning，否则先修代码再提交。

### 第 2 步 只暂存源码和测试

仅暂存当日改动的源码与测试文件：

```powershell
git add {当日改动的源码文件列表} {当日改动的测试文件列表}
```

```powershell
git --no-pager status
```

复查暂存区：`PROJECT_PLAN.md` / `PROJECT_CONTEXT.md` / `output/` 绝不能出现（已在 `.git/info/exclude`，也禁止显式 add）。

### 第 3 步 提交（第 {COMMIT_NO} 次，Conventional Commits）

```powershell
git commit -m "{type}({scope}): {当日技术摘要}" `
    -m "- {关键点1}" `
    -m "- {关键点2}" `
    -m "- {关键点3}" `
    -m "- 新增{增量}条测试，{旧基线}→{新基线} passed"
```

- type 取值：`feat` / `fix` / `docs` / `refactor` / `test` / `chore`
- scope 取值：模块名（如 `web` / `core` / `db` / `common`）
- 只写技术事实，不写非技术表述

### 第 4 步 推送

```powershell
git --no-pager push origin main
```

**成功口径**：看到 `{REMOTE_HASH}..xxxxxxx  main -> main` 即为成功。

- PowerShell 下 push 刷红色 `NativeCommandError`、退出码显示 1 是 stderr 误报，不算失败。
- 只有出现 `! [rejected]` / `non-fast-forward` 才是真失败——回第 0 步 `git pull --rebase origin main` 后重推。
- **严禁 `git push -f` / `--force` 强推。**

### 第 5 步 推送后双向验证

```powershell
git rev-list --count HEAD
```

```powershell
git --no-pager log --oneline -3
```

- `git rev-list --count HEAD` 应为 {COMMIT_NO}。
- 打开 GitHub 仓库网页，确认最新提交已出现在远程。

---

## 四、占位符对照表

| 占位符 | 含义 | 示例 |
|---|---|---|
| `{DAY}` | Day 编号 | Day27 |
| `{COMMIT_NO}` | 提交次数序号 | 第 32 次 |
| `{当日任务标题}` | PROJECT_PLAN 当日行的任务简述 | 通知集成 |
| `{旧基线}` | 当日开始前的 pytest 通过数 | 326 |
| `{新基线}` | 当日目标 pytest 通过数 | 334 |
| `{增量}` | 当日新增测试条数 | 8 |
| `{REMOTE_HASH}` | 远程 origin/main 最新提交短 hash | d308280 |
| `{前一日提交标题}` | 远程最新提交的 message 标题 | SSE增强多订阅者广播 |
| `{type}` | Conventional Commits 类型 | feat / fix / docs |
| `{scope}` | 模块范围 | web / core / db |
| `{当日技术摘要}` | commit 标题的技术概括 | 通知集成Web触发执行完成后自动推送 |
| `{关键点1/2/3}` | commit body 的技术要点 | EventChannel重构为有界历史环 |

---

## 五、产出前自检清单

任务指令产出前逐项核对：

- [ ] 开场已读 PROJECT_CONTEXT.md + PROJECT_PLAN.md，Day{DAY} 行任务内容与行数/测试数预估已确认
- [ ] 【当前现状】列出的文件/类/函数与本地实际一致（已 Read 核对，非凭记忆）
- [ ] 每个任务精确到文件路径 + 函数签名 + 测试断言，无泛泛表述
- [ ] 测试只增不减，新增条数 = {新基线} - {旧基线}
- [ ] 既有测试如需改断言，已明确指出改哪条、改成什么
- [ ] 所有每天变化的数字均为占位符，无硬编码
- [ ] 全文无非技术表述（代码、注释、文档、commit message）
- [ ] 涉及具体模块时已带入 PROJECT_CONTEXT 第 7 章对应的运行时坑
- [ ] 测试命名与 fixture/markers 遵循既有约定
- [ ] Git SOP 五段完整附在末尾，占位符已按当日实际替换
- [ ] 禁止 git add 的三类文件（PROJECT_PLAN.md / PROJECT_CONTEXT.md / output/）已在任务指令中标注
- [ ] 过渡方案已标注「Day{DAY} 替换」

---

*文件版本：v1.1 | 适用阶段：Day27 起全程*
