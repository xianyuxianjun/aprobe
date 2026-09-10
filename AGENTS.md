# aprobe Agent 工作守则

面向 OpenAPI 的、有界自主的接口契约测试 Agent。验证接口的**功能与契约正确性**，不是安全漏洞。

接手任何任务前，先读这四份，再动代码：

- `CONTEXT.md` —— 术语表与领域边界。**术语以它为准**，不要另造词。
- `README.md` —— 定位、进展表、命令与退出码、已知限制。
- `docs/adr/` —— 难以反悔的决策。改到相关区域前必须读对应 ADR。
- 本文件 —— 架构地图与设计理念。

---

## 一、不可协商的边界

这七条是项目的全部价值所在。**放松其中任何一条，项目就退化成"会发请求的 LLM 脚本"**。

1. **模型不接触网络。** 生成阶段只能读规范、写用例文件；网络出口只存在于确定性的执行阶段，且只接受用例文件里已存在的 `case_id`。（ADR-0001）
2. **判定不由模型做出。** 通过/失败只来自声明式 Assertion 的确定性求值。无法求值只能是「无法判定」，**不允许退化成「通过」**。
3. **权威事实来源是自有 Trace。** 编排层（LangGraph）的中间状态只用于流转控制，不参与回放、评估或结论推导。（ADR-0002）
4. **降级模式与 Agent 模式共用同一条流程。** 同一个规划器 seam、同一套注册工具、同一份 Test Case 结构。（ADR-0003）
5. **允许范围是唯一的访问控制。** `target.allow` 为空即拒绝一切；没有隐式允许。
6. **不信任代理。** HTTP 客户端必须 `trust_env=False`。否则请求会被系统代理静默转发到允许范围之外，第 5 条直接失效。
7. **凭据只从环境变量读取。** 配置文件里只出现引用名。明文凭据不落库、不写日志、不进报告。

路径模板、方法、允许范围、上限、脱敏，全部由确定性代码控制。**不要为了让某个演示跑通而放松任何一条。**

---

## 二、架构地图

### Seam（一条 seam 上只有一个 adapter 时，它还是假想的）

| Seam | 位置 | 现有 adapter | 状态 |
|---|---|---|---|
| 规划器 | `generator.generate_cases` | 确定性生成 | **假想中**。M1 的 LangGraph 循环成为第二个 adapter 时才变真 |
| 网络出口 | `runner.TestRunner` | httpx | 真实且唯一。**任何新增网络调用都必须经由它** |
| 用例文件 | `cases.load_case_file` / `dump_case_file` | YAML 文件 | 真实。它是审批载体（ADR-0001），格式变更必须先读该 ADR |
| 权威轨迹 | `trace.TraceStore` | SQLite | 真实 |
| 工具注册表 | M1 引入 | 暂无 | 必须与传输无关，两种模式共用（ADR-0003） |

### 必须保持深的模块

改这些模块时，**优先加实现、不要加接口**：

- `assertions.AssertionEvaluator`：接口基本只有 `evaluate_all(assertions, observation)`，背后是所有算子语义与"永不抛异常"的契约。新增断言类型是对的，新增公开方法要犹豫。
- `cases.validate_cases`：一个函数返回问题列表，吞掉全部跨对象校验。**不要拆成多个 `validate_x`**——调用方需要一次看到全部问题。
- `specification`：`load_specification` + `Specification.resolve` / `schema_document`。JSON Pointer、转义规则、`$ref` 打包全在这里，是**唯一**的 schema 解析入口。
- `runner.TestRunner.execute`：一个方法产出完整 `TestRun`，永不抛异常。**这是刻意的深度，不是疏忽**——见下面「禁止的模式」。
- `policy.TargetPolicy`：两个方法，背后是允许范围语义、只读默认、userinfo 与协议校验。

### 适配器与词汇

- `models.py`：领域词汇的所在地。它的价值是 locality，不是 depth。**不要往里面加行为**。
- `report.py`、`cli.py`：最外层适配器，本来就该薄。**但反过来说：真实逻辑不许住在这里**（见「已知的债」第 3 条）。
- `sanitizer.py`：一组独立小规则，浅是合适的。

---

## 三、设计理念

### 用词固定

**module / interface / implementation / depth / seam / adapter / leverage / locality**。不要替换成 component、service、API、boundary。一致性本身就是价值。

- **Module**：任何有 interface 和 implementation 的东西（函数、类、包、跨层切片）。
- **Interface**：调用方为了正确使用它必须知道的**全部事实**——签名，也包括不变式、顺序约束、错误模式、必需配置。
- **Depth**：接口上的 leverage，即"调用方每学一个单位接口，能换来多少行为"。
- **Seam**：可以不修改该处代码就改变行为的位置。

需要更细的词汇时，加载 `codebase-design` skill。

### 四条判据

1. **删除测试。** 想象删掉这个模块：复杂度消失了 → 它是透传，本就不该存在；复杂度在 N 个调用方重新出现 → 它在赚自己的饭钱。
2. **接口就是测试面。** 调用方和测试穿过同一条 seam。如果你需要绕过接口去测内部，说明模块形状错了。
3. **一个 adapter 是假想 seam，两个才是真 seam。** 不要为"将来可能不同"预先发明抽象接口。
4. **接受依赖，不要自己创建；返回结果，不要产生副作用。**（`TestRunner` 接受 policy / evaluator / credentials 而不自己 new，正是因为如此）

### Clean Code 与 APoSD 的冲突：冲突时全局优先

两本书在**恰好最要命的地方**打架：注释、函数长度、拆类、透传方法。

| 议题 | Clean Code | 本项目采用（APoSD） |
|---|---|---|
| 注释 | 注释是失败，尽量少写 | **注释承载接口契约**。`assertions` 的"永不抛异常"、`policy` 的默认拒绝都属于契约，必须写下来 |
| 函数长度 | 越短越好 | 只在该短的地方短 |
| 拆类 | 单一职责，类要小 | 拆分会把信息泄漏到模块之间 |
| 透传方法 | 分层是好事 | **透传是坏味道** |

**根因是尺度不同**：Clean Code 管"一条语句、一个函数读起来清不清楚"，APoSD 管"复杂度最终住在哪里"。把局部可读性推到底，会**生产**浅模块——这是本项目最该防的失败模式。

### 禁止的模式

- **把编排搬进调用方。** 看到 `TestRunner.execute` 想拆成 RequestBuilder / PolicyChecker / Sender / Evaluator 四个单方法类时，先跑删除测试：复杂度没有消失，只是扩散到了 CLI、测试和未来的诊断循环里。
- **在适配器里放真实逻辑。** 有规则、有分支、需要被直接测到的东西，属于领域模块，不属于 `cli.py`。
- **为了让校验通过而放宽校验。** 尤其是 `policy`、`sanitizer`、"无法判定 → 通过"这三处。
- **把实现细节写进跨模块的格式里。** 典型反例见「已知的债」第 1 条。
- **静默忽略。** 解析不了的东西要记入 `ignored` / `needs_input` / `INCONCLUSIVE`，**绝不静默跳过**。（`specification.ignored`、`generator.needs_input`、`TestRun` 的 `truncated` 标记都是这条的产物）

---

## 四、已知的债（先修，不要在它们上面继续加功能）

1. **RFC6901 指针泄漏进了人工审阅的产物。** `cases/petstore.yaml` 里出现 `#/paths/~1pets/get/responses/200/content/application~1json/schema`。指针是 `specification` 的实现细节，却成了用例文件格式的一部分；而 ADR-0001 要求这个文件是给人审阅的。
   → **M1 之前修**：用例只写 `operation_id` + 响应码，由确定性层解析成指针。否则 Agent 会批量生产这种不可读的字符串。
2. **`ReportMeta` 在两个调用点被重复构造。** 它是 `report` 模块的接口，不该由调用方拼两遍。
3. **`cli._exit_code` 与 `cli._load_context` 是真实逻辑，却住在适配器里。** CI 门禁语义（策略拒绝优先 → failed → inconclusive，`--fail-on none` 只改退出码不改结论）有明确规则，应当能被直接测试。

另有一处纯坏代码：`sanitizer.sanitize_text` 里的嵌套三元 lambda。按两本书的标准都该改，且它换不来任何深度。

---

## 五、工作方式

### 命令

```bash
uv pip install -e '.[dev]'
.venv/bin/pytest                      # 全部测试，目前必须全绿
.venv/bin/aprobe validate --spec examples/petstore.yaml --cases cases/petstore.yaml
python mock/mock_service.py --port 8080            # 评估基准
python mock/mock_service.py --scenario violating --port 8081
```

### 纪律

- **没跑过的命令不许说它能跑；没实测的能力不许写进 README 进展表。** 该表只区分「已实测」与「开发中」。
- **文档与代码冲突时，指出来，不要静默选一个。** 决策解决后更新对应文档。
- **术语变更写 `CONTEXT.md`**（它只是词汇表，不放实现细节、不放规格）。
- **难以反悔 + 缺少上下文会让人困惑 + 存在真实取舍**——三条同时成立才写 ADR。编号取 `docs/adr/` 里最大的 +1。
- **每次改动都要能在无密钥环境下跑通测试。** 降级模式是硬要求（ADR-0003），新功能若只能在有模型时工作，说明它放错了位置。
- 提交信息写"为什么"，不写"改了什么"。

### 非目标（不要顺手做）

- 不做安全测试/漏洞扫描——那是另一个学科，术语不要混用。
- 第一版不读代码仓库、不读自然语言需求，只读 OpenAPI。
- 第一版不做 MCP；工具是与传输无关的注册表就够。
- 不自动修改被测系统，不自动修复代码。
- 不访问生产环境。
