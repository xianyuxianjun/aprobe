# aprobe

面向 OpenAPI 的、**有界自主**的接口契约测试 Agent。

> 读规范 → Agent 设计用例（**没有网络出口**）→ 用例文件进 Git 被人工审阅 → 确定性代码执行 → Agent 诊断失败归因 → 产出可回放 Trace 与可量化指标。

>`aprobe` 验证的是接口的**功能与契约正确性**，不是安全漏洞。安全审计（Rule / Finding / Evidence 那一套语义）属于另一个学科，两者术语不通用。

## 核心主张

设计上只做四件别人容易做错的事：

1. **模型不接触网络。** 生成阶段的 Agent 只能读规范、写用例文件；网络出口只存在于确定性的执行阶段，且只接受用例文件里已存在的 `case_id`。见 [ADR-0001](./docs/adr/0001-case-file-as-approval-carrier.md)。
2. **判定不由模型做出。** 通过/失败只来自声明式 Assertion 的确定性求值；无法求值只能是「无法判定」，不允许退化成「通过」。
3. **事实来源是自有 Trace。** 编排层（LangGraph）的中间状态只用于流转，回放与评估读的是本项目持久化的权威轨迹。见 [ADR-0002](./docs/adr/0002-authoritative-trace-owned-by-aprobe.md)。
4. **降级模式与 Agent 模式是同一条流程的两个 driver。** 不调用模型的模式不是"简化版"，而是同一个规划器 seam 的另一个实现：同一套工具、同一份产物结构、同一套判定。两种模式因此可比——质量差值本身就是一个可量化的结果。见 [ADR-0003](./docs/adr/0003-degraded-mode-shares-the-agent-seam.md)。

## 当前进展

本表只区分**已实测**与**开发中**，不把计划写成现状。每一行的验证方式都可以直接运行。

| 能力 | 状态 | 验证方式 |
|---|---|---|
| OpenAPI 3.0/3.1 解析、稳定 Operation 标识、JSON Pointer 保留 | 已实现 | `pytest tests/test_specification.py` |
| 只解析本地 `$ref`，远程引用直接拒绝 | 已实现 | 同上 |
| Operation 路径模板 + 用例参数值构造请求，路径不可自定义 | 已实现 | `pytest tests/test_runner.py` |
| TargetPolicy：允许范围、只读默认、禁止重定向、超时/体积上限、重试上限 | 已实现 | `pytest tests/test_policy.py` |
| 声明式 Assertion：状态码 / JSON Schema / JSONPath（含跨字段比较）/ 响应头 / 响应耗时 | 已实现 | `pytest tests/test_assertions.py` |
| Verdict 合成：失败优先，其次无法判定 | 已实现 | 同上 |
| 证据脱敏（请求头、JSON 敏感键、非 JSON 文本、URL userinfo） | 已实现 | `pytest tests/test_runner.py` |
| 用例文件校验（重复 id、陈旧路径、写声明、requires 环）与确定性执行顺序 | 已实现 | `pytest tests/test_cases.py` |
| 权威 Trace 持久化（SQLite） | 已实现 | `pytest tests/test_cli.py` |
| 报告导出 Markdown / JSON / JUnit | 已实现 | `pytest tests/test_report.py` |
| CLI：`generate` / `validate` / `run` / `report` 与退出码语义 | 已实现 | `pytest tests/test_cli.py` |
| 降级模式（不调用任何模型）跑通全流程 | 已实现 | 全部测试都在无密钥环境运行 |
| 版本化 Mock 评估基准（conformant / violating 两个场景） | 已实现 | `pytest tests/test_runner.py` |
| 生成循环（LangGraph 有界多步 + 注册工具 + 预算） | 已实现（降级模式保留为默认） | `pytest tests/test_agent_loop.py tests/test_agent_integration.py` |
| Agent 工具注册表（6 个只读工具，与传输无关） | 已实现 | `pytest tests/test_tools.py` |
| 模型客户端（OpenAI-compatible + 脚本回放两个 adapter） | 已实现 | `pytest tests/test_agent_integration.py` |
| Agent Step / Tool Call 轨迹与 token 成本 | 已实现 | 同上 |
| 离线评估与量化指标（判定准确率、假阳/假阴、无依据结论率、回放一致率） | 已实现 | `pytest tests/test_evaluation.py`、`aprobe evaluate` |
| 诊断循环（失败归因：接口缺陷 / 用例缺陷 / 环境问题 / 无法判定） | 已实现（需要模型端点，**没有降级替代**） | `pytest tests/test_diagnosis.py` |
| 归因进入报告（与结论分开陈述） | 已实现 | `aprobe report --format md` |

## 实测指标

四个样例集，在仓库自带的两个基准上实跑（`bash scripts/evaluate_all.sh`，回放 2 轮）：

| 样例集 | 用例 | 判定准确率 | 假阴性 | 无法判定 | 回放一致率 |
| --- | --- | --- | --- | --- | --- |
| petstore-conformant | 7 | 100.0% | 0 | 0 | 100.0% |
| petstore-violating | 7 | 100.0% | 0 | 0 | 100.0% |
| edgecases-conformant | 12 | 100.0% | 0 | 0 | 100.0% |
| edgecases-violating | 12 | 100.0% | 0 | 1 | 100.0% |

标注的来源是"我在基准里注入了什么偏差"，而不是"aprobe 说了什么"——期望值必须来自独立的真相来源，否则指标只是自证。

`edgecases-violating` 里**每个接口各注入一种不同种类**的偏差，按用例来源分组之后是这张表：

| 用例来源 | 抓住的偏差 |
| --- | --- |
| 确定性生成（9 条） | 6 类：缺必填且多出 `additionalProperties` 禁止的字段、枚举越界、嵌套类型错、数值形状错、204 被返回 200、302 被返回 301 |
| 人工补写（3 条） | 2 条业务规则（`total` 必须等于 `items` 条数，含空列表）＋ 1 条重定向语义 |
| 两者都只能给出 | 1 条「无法判定」：声明 `application/json` 但响应不是 JSON |

**这张表才是这个项目想说的事**：schema 驱动的自动生成能覆盖结构与状态码，但**业务规则类违约只有会读语义的用例设计者能发现**——这就是 Agent（或人）在这条流程里不可替代的位置，也是 M1 那套工具集存在的理由。

**这些数字不说明什么**，必须一并读：

- 38 条标注、两个自建基准，而且用例、基准、标注出自同一个作者与同一轮工作。100% 是预期结果，不是准确率声明。
- 它证明的是**管线可复现、可度量、能抓住注入的每一类偏差，且漏报会被单独拦下**。
- 想要有说服力的数字，需要基准由第三方提供、标注由另一个人写。

## 规划器对比（项目最核心的数字）

同一基准（`edgecases-violating`，注入了 9 处违约）、同一套标注，**只换用例集**：

| 指标 | 仅确定性生成 | Agent 补齐后 |
| --- | --- | --- |
| 覆盖的用例 | 9 | 12 |
| 准确率（分母是各自的覆盖数） | 100.0% | 100.0% |
| 发现违约 / 注入违约 | 6/9 | 9/9 |
| **违约发现率** | **66.7%** | **100.0%** |
| 未被覆盖的标注 | 3 | 0 |
| Agent 步数 / token | — | 3 / 450 |

**先看左边那列**：准确率 100%，违约发现率只有 66.7%。覆盖得少的用例集准确率反而更好看——所以准确率不能用来做对比，`defect_detection_rate` 可以，因为它的分母是固定的标注集合。这就是为什么度量模型要单独有这一个指标。

补上来的 3 条全部是**业务规则与语义**：`total` 必须等于 `items` 条数（含空列表），以及"重定向没有被跟随"。schema 校验对它们无能为力。

**这个数字的限度，必须一并读：**

- 右侧的"Agent"是 `mock/model_server.py` 这个**脚本替身**，不是真实模型。它度量的是这条管线**能把"会读语义的用例设计"带进来多少价值**，即用例设计能力在这套基准上的上界——不是某个模型的真实水平。
- 450 token 是替身编造的，不代表真实成本。

复现：

```bash
python mock/edge_service.py --scenario violating --port 8160
aprobe evaluate --config aprobe.yaml --suite eval/edgecases-violating.yaml \
  --target http://127.0.0.1:8160 \
  --cases cases/edgecases-degraded.yaml --against-cases cases/edgecases-agent.yaml
```

CI 把这条对比作为常驻项：`scripts/evaluate_all.sh` 会断言这组差值，退化了就红。

## 持续集成

`.github/workflows/ci.yml` 跑三件事：`pytest`、`ruff --select F`（只看未定义名与未使用导入/变量，不引入风格门禁）、以及在全部基准上回放全部样例集。

门禁规则的实体是 `evaluation.gate()`，可以直接测：

- **假阴性单独构成失败**，不参与准确率平均——漏报违约比误报危险得多。
- 指定 `--min-accuracy` 就按门槛判；不指定时任何不一致都算失败（更严格）。
- 有 `--against-cases` 时门禁只作用于右侧（要交付的那套用例），左侧只作参照。

本地跑同一条命令即可：

```bash
bash scripts/evaluate_all.sh
```

## 快速开始

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'

# 1) 启动版本化评估基准（另一个终端）
python mock/mock_service.py --port 8080

# 2) 生成用例文件（默认降级模式，不调用模型）
aprobe generate --spec examples/petstore.yaml --out cases/generated.yaml

# 或：用有界 Agent 循环生成（需要 APROBE_MODEL_* 环境变量）
aprobe generate --config aprobe.yaml --mode agent --force

# 3) 离线校验用例文件（不联网、不调用模型）
aprobe validate --spec examples/petstore.yaml --cases cases/petstore.yaml

# 4) 确定性地执行并记录 Trace，导出报告
aprobe run --config aprobe.yaml --json reports/report.json --junit reports/junit.xml --markdown reports/report.md

# 5) 在评估基准上回放标注样例集，得到可比较的指标
aprobe evaluate --config aprobe.yaml --suite eval/petstore-conformant.yaml --repeat 2

# 6) 一次性在全部基准上回放全部四个样例集（CI 用的同一条命令）
bash scripts/evaluate_all.sh

# 7) 从 Trace 重新导出报告（含失败归因段落）
aprobe report --config aprobe.yaml --format json
```

失败归因的完整演示（违约基准 + 模型端点替身，不需要真实密钥）：

```bash
python mock/mock_service.py --scenario violating --port 8081
python mock/edge_service.py --scenario violating --port 8082     # 第二个基准：契约边界
python mock/model_server.py --script eval/diagnose-demo.json --port 8090
aprobe run --config aprobe.yaml --target http://127.0.0.1:8081 --fail-on none
APROBE_MODEL_BASE_URL=http://127.0.0.1:8090 APROBE_MODEL=mock-model aprobe diagnose --config aprobe.yaml
aprobe report --config aprobe.yaml --format md
```

`mock/model_server.py` 只是 chat/completions 的替身，**它不是被测目标**；把两者混为一谈会破坏 ADR-0001 的边界。

跑一遍"故意违约"的场景，可以看到契约违约会被判定为失败：

```bash
python mock/mock_service.py --scenario violating --port 8081
python mock/edge_service.py --scenario violating --port 8082     # 第二个基准：契约边界
aprobe run --config aprobe.yaml --target http://127.0.0.1:8081 --fail-on none
```

## 命令与退出码

| 命令 | 作用 | 是否联网 | 是否调用模型 |
|---|---|---|---|
| `generate` | 从规范生成用例文件（`--mode degraded\|agent\|auto`） | 仅 `agent` 模式访问模型端点 | `agent` 模式调用模型 |
| `validate` | 校验用例文件与规范一致 | 否 | 否 |
| `run` | 确定性地执行用例并记录 Trace | 是（仅允许范围内的目标） | 否 |
| `evaluate` | 在评估基准上回放标注样例集并输出指标 | 是（并先确认基准身份） | 否 |
| `diagnose` | 对失败或无法判定的运行做归因 | 否 | 是（必须） |
| `report` | 从 Trace 导出报告（含归因段落） | 否 | 否 |

需要模型的两处，只有这里：`generate --mode agent` 与 `diagnose`。`run`、`validate`、`evaluate`、`report` **永远不调模型**。

Agent 模式的环境变量（不配置就一律走降级模式）：

```bash
APROBE_MODEL_BASE_URL=https://your-endpoint/v1   # OpenAI-compatible
APROBE_MODEL=your-model
APROBE_MODEL_API_KEY=...                         # 只从环境变量读，不落库也不进日志
```

退出码（CI 门禁语义）：

| 码 | 含义 |
|---|---|
| 0 | 全部通过 |
| 1 | 至少一条 Assertion 失败 |
| 2 | 没有失败，但存在「无法判定」、规划循环未完成，或部分运行没有得到归因 |
| 3 | 用例文件或配置非法，未发出任何请求 |
| 4 | 目标被策略拒绝（不在允许范围、非 http/https、或未开启的写操作） |

`--fail-on none` 只改变退出码，不改变报告中记录的结论。

## 设计边界

- **允许范围就是访问控制。** `aprobe.yaml` 里的 `target.allow` 为空时，一切目标都被拒绝；没有隐式允许。
- **不信任代理。** HTTP 客户端忽略系统与环境代理，允许范围必须是唯一的访问控制，请求不允许被静默转发。
- **配置里的相对路径以配置文件所在目录为基准**，与 `docker-compose` / `pyproject.toml` 的习惯一致。
- **凭据只从环境变量读取。** 配置文件里只有凭据的**引用名**（`auth.env`）；明文凭据不落库、不写日志、不进报告。
- **用例不得设置 `Authorization`、`Cookie`、`Host` 等请求头**，这些由配置注入，避免用例伪造身份或改写路由。
- **写操作默认拒绝**，需要同时满足用例 `write: true` 与全局 `allow_write: true`。
- **JSONPath 只支持受限子集**：`$`、`.key`、`['key']`、`[n]`。不支持通配、递归、过滤表达式。跨字段的业务规则用 `equals_path` / `length_equals_path` 表达，例如 "`total` 必须等于 `items` 的条数"。
- **`generate` 不覆盖已存在的用例文件**（除非 `--force`）。用例文件是审批载体，不能被静默改写。
- **构造不出安全请求的 Operation 一律记入 `needs_input`**，绝不退化成"随便发一个 GET"。

## 目录结构

```
src/aprobe/
  specification.py   OpenAPI 解析与 $ref 打包（唯一 schema 解析入口）
  models.py          领域模型：Operation / TestCase / Assertion / TestRun / AgentRun / FailureAttribution
  cases.py           用例文件读写、校验、确定性排序（审批载体）
  generator.py       降级模式的确定性用例生成
  planner.py         规划器 seam：确定性生成与 Agent 循环两个 adapter
  tools.py           与传输无关的工具注册表（生成用的 6 个 + 共享的只读工具）
  model_client.py    模型端点端口与两个 adapter（OpenAI-compatible / 脚本回放）
  agent_loop.py      有界多步循环（LangGraph 薄图，生成与诊断共用）
  diagnosis.py       失败归因：只读工具、证据强制、四个归因类别
  evaluation.py      离线评估与指标（纯计算，不碰 I/O）
  assertions.py      声明式断言求值（不抛异常、不调用模型）
  policy.py          被测目标策略
  runner.py          唯一通往被测目标的网络出口
  trace.py           权威轨迹持久化（runs / agent_runs / attributions）
  report.py          Markdown / JSON / JUnit 导出与 CI 门禁退出码
  cli.py             命令行与退出码
mock/
  http_kit.py        两个基准共用的 HTTP 管道（机制与意图分开）
  mock_service.py    评估基准 #1：Petstore（被测目标）
  edge_service.py    评估基准 #2：契约边界，每接口一种偏差
  model_server.py    模型端点替身（不是被测目标）
eval/                四个评估样例集、归因演示脚本、Agent 用例脚本替身
cases/
  petstore.yaml          示例用例（确定性 + 人工）
  edgecases.yaml         同上，用于契约边界基准
  edgecases-degraded.yaml  仅确定性生成的快照（对比的左侧）
  edgecases-agent.yaml     Agent 补齐之后的快照（对比的右侧）
scripts/
  evaluate_all.sh    在全部基准上回放全部样例集（CI 用的同一条命令）
.github/workflows/   CI：pytest + ruff -F + 评估门禁
cases/petstore.yaml  示例用例文件（含人工补写的路径参数用例）
docs/adr/            架构决策记录（0001 审批载体、0002 权威轨迹、0003 模式共用同一条流程）
```

## 已知限制（M0）

- 只支持本地 OpenAPI 文件，不读取代码仓库或自然语言需求。
- 嵌套 `$ref` 仅打包 `#/components/schemas/*`；指向其他位置的引用会导致「无法判定」，而不是静默通过。
- 请求体只支持 JSON；表单、multipart、二进制上传暂不支持。
- 尚无语义化的失败归因（诊断循环在 M3），报告只列出断言层面的观察事实。
- 响应声明的 media type 是 JSON 但实际不是 JSON 时，契约无法校验，结论是「无法判定」而不是「失败」。这是刻意的边界（无法求值只能是无法判定），代价是这类违约需要人再看一眼。
- token 成本只在真的接了模型端点之后才有数；仓库里的数字是 `mock/model_server.py` 换来的，不代表真实成本。

## 开发

本仓库有三条不变量，改动前请确认没有破坏它们（每条决策的来由都记在 `docs/adr/`）：

1. **模型不接触被测目标**——生成阶段没有通往目标的网络出口。
2. **判定只来自确定性 Assertion**——模型只参与用例设计与失败归因，不参与判定。
3. **权威事实来自自有 Trace**——编排层的中间状态不参与回放与评估。

```bash
uv pip install -e '.[dev]'          # 核心：不需要模型，降级模式完整可用
uv pip install -e '.[dev,agent]'    # 加上 Agent 循环（LangGraph）
.venv/bin/pytest
```

ADR-0003 说"降级模式不是简化版"，这句话是可执行的：

| 安装方式 | `pytest` 实测 |
| --- | --- |
| 只装 `[dev]` | **130 passed, 5 skipped**（跳过的是必须用循环的用例，不是失败） |
| `[dev,agent]` | **162 passed** |

CI 两个都跑：`degraded-only` 这个 job 只装核心依赖，用来保证这条性质不会悄悄退化。

Agent 循环所需的可选依赖单独声明，M0 不依赖它们：

```bash
uv pip install -e '.[agent]'
```
