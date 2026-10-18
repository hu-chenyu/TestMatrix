# ADR-002: pytest 插件（pytest11 entry point）vs conftest 钩子的选型取舍

- 状态：已接受
- 日期：2026-10-17（Day57，打包验证日即时写）
- 更新：2026-10-18（Day58 落地 xdist 兼容，决策 7 由"边界声明"改为"已支持"；Day58-fix 补记已知限制）
- 相关实现：`src/testmatrix_plugin/__init__.py`、`pyproject.toml`（build-system + entry point + 包发现）
- 相关前置：`docs/adr/ADR-001-real-executor-integration.md`（真实执行器选型）、项目根 `conftest.py`（Day55 收集期钩子）
- 命名说明：本文件按当日任务指定的 `ADR-002-*.md` 落地。与 ADR-001 同源，`docs/adr/` 既有的四位小写编号体系（`0001-` / `0002-`）待 D59 复盘统一处置，本日不改名、不动索引文件。

---

## 背景与问题

Day55 在项目根 `conftest.py` 中实现了收集期钩子（`pytest_collection_modifyitems` 三级字典序稳定排序、
收集统计写入 `config.stash`、标记完整性检查）与会话收尾摘要（`pytest_sessionfinish`）。
Day56 需要新增两项执行期能力：**结果上报**（把"本次会话跑了什么、结果如何"汇总成结构化 JSON）
与**日志钩子**（逐用例记录 setup / call / teardown 三阶段结果与耗时）。

面临二选一：

- **方案甲**：继续在 `conftest.py` 里扩展钩子（追加 `pytest_sessionfinish` 的第二段逻辑与
 `pytest_runtest_logreport`）；
- **方案乙**：抽离为独立的 `pytest11` entry point 插件，经 `pyproject.toml` 注册、安装后由 pytest 自动加载。

七个决策点按影响面排序，每个都给出选定方案、被否决的备选与当时的判断依据。

---

## 打包验证实测（本决策的事实基础，Day57）

本决策不只是"写起来好看"，它必须能通过标准 Python 打包流程分发出去。当日实测记录：

### 1. 产物

| 项 | 实测结果 |
| --- | --- |
| 构建命令 | `.venv/Scripts/python -m build`（build 1.6.1，临时安装，不写入 `requirements.txt`） |
| sdist | `dist/testmatrix-0.1.0.tar.gz`（485,852 字节，90 个条目） |
| wheel | `dist/testmatrix-0.1.0-py3-none-any.whl`（10,855 字节，7 个条目） |
| wheel 内容 | `testmatrix_plugin/__init__.py` + `testmatrix-0.1.0.dist-info/*`（含 `entry_points.txt`，内容为 `testmatrix = testmatrix_plugin`） |

**白名单式包发现生效**：`src/` 下的 `common` / `core` / `db` / `web` / `scripts` 全部不在 wheel 内；
`top_level.txt` 只有 `testmatrix_plugin` 一个顶层名。

### 2. sdist 内容与构建期 setuptools 版本相关（当日新发现）

| 构建环境 | 条目数 | 是否含 `tests/` | 发行包文件名 |
| --- | --- | --- | --- |
| 构建隔离（`requires = ["setuptools>=64"]` 当日解析到 84.0.0） | 90 | **含整个 `tests/`（74 个文件）** | 小写归一 `testmatrix-0.1.0.tar.gz` |
| 本地 venv（setuptools 65.5.0，`--no-isolation`） | 15 | 不含 | 保留原名 `TestMatrix-0.1.0.tar.gz` |

两种环境都**不含任何业务代码**（`src/common` 等从不进入 sdist），差异只在 `tests/` 与 `src/TestMatrix.egg-info/`。
已用 `git archive HEAD` 导出干净树复现，确认这不是本地残留 `egg-info/SOURCES.txt` 造成的假象
（干净树同样含 `tests/`）。结论：只声明 `setuptools>=64` 等于把"产物形态与文件名"交给"当天解析到的最新版"。

### 3. 干净环境安装验证

| 步骤 | 实测结果 |
| --- | --- |
| `py -3.11 -m venv .venv_packagetest` + 安装 wheel 与 pytest | 成功（该环境的 pytest 为当日最新 9.1.1） |
| entry point 注册 | `importlib.metadata` 读到 `testmatrix = testmatrix_plugin` |
| pytest 采集头 | `plugins: TestMatrix-0.1.0`（**自动加载，未传任何 `-p` 参数**） |
| 真跑一条用例 | `1 passed`，终端出现项目根 conftest 的收集摘要行与插件会话摘要行各一行，报告 JSON 正确落盘 |
| 环境差异提示 | 该 venv 未装 `pytest-rerunfailures`，而 `pytest.ini` 配了 `reruns`，故出现一次 `PytestConfigWarning: Unknown config option: reruns`——属临时环境依赖不全，与插件行为无关 |

本步骤在**修复后的最终产物**上复验（Day57 补测发现并修掉一处终端降级缺陷后重新构建）。
临时 venv 与探针目录在验证完成后即删除，不留在项目目录内。

### 4. 元数据校验

`python -m twine check dist/*`：wheel 与 sdist 均 `PASSED with warnings`，warning 为
`long_description` / `long_description_content_type` 缺失（本项目 `[project]` 未声明 readme）。

---

## 决策摘要

| # | 决策点 | 选定方案 | 否决的主要备选 |
| --- | --- | --- | --- |
| 1 | 分发方式 | **独立发行包 + `pytest11` entry point** | 继续在 conftest 里加钩子；插件不进 entry point、靠 `-p` / `sys.path` 注入 |
| 2 | 激活方式 | **安装即自动激活**（pytest 自动发现） | 显式 `-p` 加载；由调用方 import conftest 触发 |
| 3 | 与项目耦合度 | **单向、无 import 依赖**：插件不 import conftest，按 `Protocol` 读 stash，收集口径不重算 | 插件 import conftest 复用常量与数据结构；插件自己重算收集统计 |
| 4 | 可复用性 | **与业务代码物理隔离**（独立 src 包 + 白名单式包发现） | 插件放进 `src/common` 或 `src/core` 一起分发 |
| 5 | 测试隔离 | **双路径验证**：纯函数直接驱动 + 嵌套 pytest 真跑子进程 | 只做嵌套端到端；只做单元驱动 |
| 6 | 打包验证 | **标准 `build` + 干净 venv 安装 + `twine check`** | 只验证 `pip install -e .` 可用 |
| 7 | xdist 兼容性 | **Day58 已支持**：worker 零副作用 + master 唯一落盘/摘要 + 收集统计由 worker 上报合并（Day57 曾把"不支持"作为边界） | 继续声明不支持；Day57 时的"现在就预埋多进程支持" |

---

## 决策详情

### 决策 1：分发方式用独立发行包 + `pytest11` entry point

- **选择**：`[project.entry-points."pytest11"]` 注册 `testmatrix = "testmatrix_plugin"`，
 插件源码放独立包 `src/testmatrix_plugin/`，与业务子包并列但独立分发。
- **备选与取舍**：

| 方案 | 问题 |
| --- | --- |
| 继续在 `conftest.py` 里加钩子 | **加载范围限制**：conftest 只对"被收集路径的祖先链"生效。真实执行器拉起的 pytest 子进程收集的是 `output/` 下的文件（`tests/conftest.py` 不在其祖先链上），项目根 conftest 虽在链上，但把"平台自有的结果上报"绑在某一份 conftest 上，等于要求"任何一次运行都必须恰好加载到那一份" |
| 插件不进 entry point、靠 `-p` 或 `sys.path` 注入 | 每个调用方（Web 触发、队列 worker、CLI、人工排障）都必须记得挂载参数；漏一处就是"报告静默不生成"，而静默是该类故障最贵的形态 |
| 独立发行包 + entry point（选定） | 装一次对**整个解释器**生效，含所有子进程；分发能力由 `python -m build` 的产物天然承载 |

- **实测证据**：干净 venv 中安装 wheel 后，未传任何 `-p` 参数即出现 `plugins: TestMatrix-0.1.0`。

### 决策 2：激活方式是"安装即自动激活"，代价是环境差异

- **选择**：依赖 pytest 对 `pytest11` 组的自动发现；`-p no:testmatrix` 作为显式禁用手段。
- **备选与取舍**：

| 方案 | 问题 |
| --- | --- |
| 显式 `-p testmatrix_plugin` | 需要在 `pytest.ini` 的 `addopts` 里写死；而 `pytest.ini` 的配置面被多处文档与用例引用，且 `-p` 的模块解析发生在 ini 的 `pythonpath` 处理之前，还得额外保证 `PYTHONPATH` |
| 由 conftest import 插件模块 | conftest 加载即生效，但把插件的生命周期绑回 conftest——回到决策 1 的问题 |
| 自动激活（选定） | **代价必须明写**：本地开发环境装的是 editable 包，CI 不安装本项目，于是"本地自动加载、CI 不加载"，同一份用例在两种环境下跑的不是同一条链路 |

- **代价的处置**：测试子进程一律用 `-p src.testmatrix_plugin -p no:testmatrix` 固定链路——
 前者显式加载源码路径下的那一份（CI 与本地都存在），后者挡住 entry point 那一份
 （本地已安装时它会自动加载）。若不做这两步，同一份 `__init__.py` 会作为两个模块对象同时存在，
 注册两套钩子（摘要打印两次、报告写两遍）、持有两把 `StashKey`。

### 决策 3：与项目耦合度——单向依赖、不 import conftest、收集口径不重算

- **选择**：插件对 conftest 零 import 依赖；只遍历 `config.pluginmanager.get_plugins()`，
 对带 `COLLECTION_STATS_STASH_KEY` 属性的插件用**它自己的那把键**读 stash，
 按结构化子类型（`Protocol`）只取四个字段：`total` / `marker_counts` /
 `module_counts` / `unregistered_marker_counts`。收集侧统计**一律不重算**。
- **备选与取舍**：

| 方案 | 问题 |
| --- | --- |
| `import conftest` 复用常量与数据结构 | `StashKey` **以对象身份为键**：import 得到的是**另一份模块对象**、另一把键，`config.stash.get(键)` 永远返回 `None` **且不报错**（Day55/7.79 已踩过同族坑），排障时表现为"插件没写报告"这类与真因无关的表象 |
| 插件自己重算收集统计 | 同一口径两份实现必然漂移（Day45 三处各写一套脱敏逻辑就是前车之鉴）；且插件在子进程里收集的 items 与外层会话未必同一批 |
| 不 import + 按 Protocol 读 stash（选定） | 代价是插件声明的字段面与 conftest 的实际结构之间没有编译期约束，靠 `hasattr` 判据与 `null` 降级兜住 |

- **空值语义**：读不到时报告里 `collection_stats` 为 `null`，**不是缺键**——
 "读不到"与"字段不存在"在消费侧处置完全不同，缺键会让下游静默取到 `None`。

### 决策 4：可复用性——与业务代码物理隔离、白名单式包发现

- **选择**：`[tool.setuptools] package-dir = {"" = "src"}` +
 `[tool.setuptools.packages.find] where = ["src"], include = ["testmatrix_plugin*"]`。
- **备选与取舍**：

| 方案 | 问题 |
| --- | --- |
| 插件放 `src/common/` 或 `src/core/` 一起分发 | `common` / `core` / `db` / `web` 是极易与外部库相撞的通用顶层名，装进 site-packages 是一次不可控的命名污染；且"装一个插件"变成"必须带着整个平台的依赖闭包" |
| 不声明包发现规则，交给 setuptools 自动发现 | 自动发现要么找不到（`src` 布局需 `package-dir`），要么把 `src` 下所有子包都装成顶层包 |
| 独立包 + 白名单 include（选定） | 实测 wheel 只含 `testmatrix_plugin/__init__.py` 与 `.dist-info`，`top_level.txt` 仅一个顶层名 |

- **实测验收**：wheel 内容清单与 `top_level.txt` 两项均已在 Day57 亲验（见上文"打包验证实测"）；
 测试侧另有静态契约断言——用 `src/` 下的**真实目录名**逐一匹配 include 规则，
 命中集合必须恰好是插件包（只断言配置文本的话，"配置没动但 src 下新增了业务包"会被漏掉）。

### 决策 5：测试隔离——双路径验证，缺一不可

- **选择**：
 1. **直接驱动**：用替身 `config` / `session` / `report` 调用插件的纯函数与钩子；
 2. **嵌套 pytest 端到端**：真起子进程跑探针用例，断言"注册 → 加载 → 钩子生效 → 报告落盘"。
- **备选与取舍**：

| 方案 | 问题 |
| --- | --- |
| 只做嵌套端到端 | 降级路径不可达也不可稳定构造: 报告写入失败（目标路径被目录占用 / 父路径被同名文件占用）、stash 读不到、ini 不可读、统计容器缺失——这些恰恰是"旁路铁律"的承重分支 |
| 只做单元驱动 | "entry point 声明正确"与"pytest 真的能加载它"是两件事；不做子进程验证时，"插件没被加载"会被误判成"插件没问题" |
| 双路径（选定） | 端到端证明链路真实可用，直接驱动把每条降级分支钉在单条断言上 |

- **该决策当场兑现了价值**：Day57 补测用直接驱动构造"writer 存在却抛错"（终端关闭 / 输出管道断开），
 实测该异常会从 `pytest_sessionfinish` 穿透出去——把展示层故障升级成会话故障（pytest 记 INTERNALERROR、
 退出码被改写），与"绝不改变退出码与任何用例结果"的承诺相反。
 修复前该用例实测变红、修复后转绿。

### 决策 6：打包验证用标准构建流程，不用 editable 安装代替

- **选择**：`python -m build` 生成 sdist + wheel → 校验 wheel/sdist 内容清单 →
 在**独立临时 venv** 中安装 wheel 并真跑一次会话 → `twine check` → 删除临时 venv。
- **备选与取舍**：

| 方案 | 问题 |
| --- | --- |
| 只验证 `pip install -e .` 可用 | editable 的简单模式写一个 `.pth` 把 `src` 目录整个加进 `sys.path`，**恰好绕过白名单式包发现**——它验证不到 wheel 的真实内容，也验证不到 sdist 能不能装 |
| 用 `--config-settings editable_mode=strict` 收紧 | setuptools 65 的 strict 模式把包复制到 `build/` 下，模块从副本加载（改源码不生效、必须重装），属"为干净引入更难维护的形态" |
| 标准构建 + 干净环境安装（选定） | 唯一能同时验证"产物内容"与"装完能用"的路径；代价是每次多一个 venv 与约 15 秒构建时间 |

### 决策 7：xdist 兼容性——Day58 已支持（原为边界声明）

- **状态**：Day57 声明"当前不支持"作为边界；**Day58 已完成改造，插件支持 xdist 多进程并发
 （`-n`），串行（不传 `-n`）行为与 Day57 完全一致**。
- **Day57 的边界声明与两条冲突机制（存档）**：
 1. **模块级 `_STATE` 单例是"单进程单会话"假设**：每个 worker 是独立进程，各自持一份
   `_STATE`，因此各进程的统计只覆盖自己执行的用例；
 2. **报告路径固定**（默认 `output/testmatrix_report.json`）：多进程写同一个文件，
   最终内容取决于写盘先后。
 当时的实测（`-n 2` 跑 4 条用例，pytest-xdist 3.5.0）：stdout 只出现 1 行会话摘要、
 数值为主进程汇总口径，最终报告 `collection_stats` 为 `null`、`execution_stats` 为完整汇总
——"最终留下的是哪个进程的那一份"是时序产物而非设计保证。
- **Day58 的实现要点**：
 1. **worker 零副作用**：worker 的 `pytest_sessionfinish` 直接返回（不写报告、不输出摘要、
   不清状态）。进程角色判定用 `workerinput` / `workeroutput` 两个 xdist 只注入给 worker 的
   通道属性，`PYTEST_XDIST_WORKER` 环境变量仅作替身配置兜底——该变量会被子进程继承，
   单凭它判定会把嵌套 pytest / 执行器子进程误判成 worker（报告静默不落盘）。
 2. **master 唯一写报告 + 唯一输出摘要**：报告仍是单一固定路径，多进程共写冲突在结构上消除；
   执行侧统计天然是全量口径（worker 把每条阶段报告回传 master，master 的
   `pytest_runtest_logreport` 累加到的就是整场会话）。
 3. **worker → master 的收集统计上报通道**：xdist 的 master **不做收集**（`DSession.pytest_collection`
   直接 `return True`），故 master 不会执行根 conftest 的 `pytest_collection_modifyitems`、
   stash 里也没有统计；真正持有完整统计的是 worker。worker 在 `pytest_collection_finish`
   把统计写进 `config.workeroutput`，master 在 `pytest_testnodedown`
   （`@pytest.hookimpl(optionalhook=True)`，未装 xdist 时不报"未知钩子"）收下并合并。
 4. **合并口径是"取 total 更大的一份"而非相加**：各 worker 收集的是整场会话的完整用例集
   （只执行其中一部分），N 份同口径统计相加会把总数放大 N 倍；total 更大者优先还能在
   "某 worker 收集不完整"时取到最接近完整会话的那一份，且结果确定。
- **已知限制（Day58-fix 记录，Day58-fix3 校正机制描述）**：`-n` 模式下**项目根 conftest 的
 收集摘要行不输出**，两侧成因不同、结果相同:
 1. **master 侧——没收集**：master 没有 stash 统计（`stats is None` 即返回）；
 2. **worker 侧——写了但到不了 master 终端**：worker 进程**确实有** `terminalreporter`
   （pytest 7.4.4 存在 `Config.InvocationParams`，xdist `remote.py` 因此走
   `_prepareconfig(args, None)` 分支，那个只在旧 pytest 上执行的 `remote_initconfig`
  ——它会往 worker 的启动参数里追加"禁用终端插件"一项——不会被调用），
   `pytest_sessionfinish` 照常执行、`write_line` 也真的写；但写的是
   **worker 自己的标准输出**，xdist 不把它转发到 master 终端（只在失败报告里回显）。
 （Day58-fix2 曾把成因 2 记成"worker 进程里没有终端插件、摘要从未产生"，Day58-fix3 以
 源码分支 / worker 内插件实例 / 标记行行为三重取证证伪后改回。）
 Day58-fix 因此在 conftest 的 `pytest_sessionfinish` 入口显式加
 worker 守卫（早退，用 `workerinput` 单判据、不用会被子进程继承的 `PYTEST_XDIST_WORKER`
 兜底），把这个事实写成代码而不是留给"恰好读不到"；`-n` 模式的会话级信息由插件
 "会话完成"摘要承担。若要恢复该摘要，需要 conftest 侧也接一条 worker→master 上报
 （不在当前范围）。
- **为什么 Day57 不预埋**（存档）：多进程下的正确形态依赖实际的 xdist 接入方案，
 提前设计等于为不确定需求付代价；先声明边界、再按实际接入改造。

---

## 后果与边界

### 收益

- **可打包分发**：`python -m build` 产出 wheel/sdist，安装后对整个解释器生效，
 包含真实执行器拉起的每一个 pytest 子进程——"平台自有的结果上报"不再依赖调用方记得挂载。
- **可独立测试**：插件与业务代码在物理上分离，测试可在干净 venv 中只装 wheel + pytest 就验证加载；
 降级分支也能用纯函数直接驱动，不必构造完整平台环境。
- **与业务代码解耦**：wheel 只含 `testmatrix_plugin`，不引入任何业务顶层名，也不把平台的依赖闭包
 强加给插件使用方。

### 代价

- **需要 `pyproject.toml` 三处配置**：`[build-system]`（缺它时 pip 走 legacy 路径直接报错）、
 `[project.entry-points."pytest11"]`、`[tool.setuptools]` 的 `package-dir` 与白名单包发现。
- **本地需要 editable 安装**才能自动加载；CI 不安装本项目，故测试内固定
 `-p src.testmatrix_plugin -p no:testmatrix` 以保证两端链路一致。
- **sdist 的产物形态与构建期 setuptools 版本绑定**（当日实测见上）：只声明 `setuptools>=64`
 无上界时，sdist 会连同整个 `tests/`（74 个文件）一起打包、发行包文件名被归一为小写。
 **登记为待办**（不在 Day57 白名单内，当日未动）：或固定构建期依赖上界，或在 `pyproject.toml`
 用显式配置收紧 sdist 文件集。此项不影响 wheel 与安装可用性，属"分发产物整洁度"问题。

### 当前边界（plugin 不替代 conftest）

- 插件只承载**执行侧**的两件事：会话结果上报（JSON + 终端摘要）与逐阶段日志钩子。
- **收集期能力仍全部留在项目根 `conftest.py`**（Day55）：三级字典序稳定排序、收集统计入 stash、
 标记完整性检查、收集摘要输出。插件只**消费**这些统计，不重算、不替代。
- 插件不参与任何判定：报告写不出、统计读不到、终端不可用时一律只降级，
 **绝不改变 pytest 退出码与任何用例结果**。
- **xdist 多进程并发已在 Day58 支持**（worker 零副作用 + master 唯一落盘/摘要 + 收集统计由
 worker 上报合并；已知限制见决策 7：`-n` 下根 conftest 的收集摘要不输出）。

### 实测证据清单（可复现）

| 证据 | 位置 / 命令 |
| --- | --- |
| wheel 内容与 entry point | `python -m zipfile -l dist/testmatrix-0.1.0-py3-none-any.whl` |
| sdist 内容与版本差异 | `python -m build --sdist`（隔离）vs `python -m build --sdist --no-isolation`（本地 setuptools 65.5.0） |
| 干净环境自动加载 | 临时 venv 装 wheel 后 `python -m pytest -o addopts= <探针目录>`，采集头出现 `plugins: TestMatrix-0.1.0` |
| 元数据校验 | `python -m twine check dist/*` → 两个产物 `PASSED with warnings` |
| xdist 行为 | `python -m pytest -n 2 <探针目录>`，随后读 `output/testmatrix_report.json` |
| 终端 writer 降级 | `tests/test_pytest_plugin.py::test_emit_message_survives_terminal_writer_failure`（修复前必红） |

---

## 参考

- 项目根 `conftest.py`——Day55 收集期钩子实现（排序 / 统计入 stash / 标记检查 / 摘要输出）
- `src/testmatrix_plugin/__init__.py`——Day56 插件实现（六个钩子 + 报告组装与落盘 + 配置读取）
- `tests/test_pytest_plugin.py`——双路径验收测试（Day56 八条 + Day57 追加四条）
- pytest 官方文档《Writing plugins》与 `pytest11` entry point 约定
- PEP 625（发行包文件名归一化）与 setuptools 关于 sdist 默认文件集的说明
- `docs/adr/ADR-001-real-executor-integration.md`——真实执行器接入选型（本决策的上下文）
- `PROJECT_CONTEXT.md` 6.63（Day56 插件与 conftest 分工）、7.79（entry point 安装与嵌套 pytest 五处坑）
