# aprobe

面向 OpenAPI 的、**有界自主**的接口契约测试 Agent。

> 读规范 → Agent 设计用例（**没有网络出口**）→ 用例文件进 Git 被人工审阅 → 确定性代码执行 → Agent 诊断失败归因 → 产出可回放 Trace 与可量化指标。

>`aprobe` 验证的是接口的**功能与契约正确性**，不是安全漏洞。术语与领域边界见 [CONTEXT.md](./CONTEXT.md)。

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
| 生成循环（LangGraph 有界多步 + 注册工具 + 预算） | **开发中**（M1，接在降级模式同一个 seam 之后，见 [ADR-0003](./docs/adr/0003-degraded-mode-shares-the-agent-seam.md)） | — |
| 诊断循环（失败归因：接口缺陷 / 用例缺陷 / 环境问题 / 无法判定） | **开发中**（M3） | — |
| Agent Step / Tool Call 轨迹与 token 成本 | **开发中**（M1） | — |
| 评估集与量化指标（判定准确率、无依据结论率、回放一致率） | **开发中**（M2） | — |

## 快速开始

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'

# 1) 启动版本化评估基准（另一个终端）
python mock/mock_service.py --port 8080

# 2) 生成用例文件（降级模式，不调用模型）
aprobe generate --spec examples/petstore.yaml --out cases/generated.yaml

# 3) 离线校验用例文件（不联网、不调用模型）
aprobe validate --spec examples/petstore.yaml --cases cases/petstore.yaml

# 4) 确定性地执行并记录 Trace，导出报告
aprobe run --config aprobe.yaml --json reports/report.json --junit reports/junit.xml --markdown reports/report.md

# 5) 从 Trace 重新导出报告
aprobe report --config aprobe.yaml --format json
```

跑一遍"故意违约"的场景，可以看到契约违约会被判定为失败：

```bash
python mock/mock_service.py --scenario violating --port 8081
aprobe run --config aprobe.yaml --target http://127.0.0.1:8081 --fail-on none
```

## 命令与退出码

| 命令 | 作用 | 是否联网 | 是否调用模型 |
|---|---|---|---|
| `generate` | 从规范生成用例文件 | 否 | 否（M1 起可切换为 Agent 循环） |
| `validate` | 校验用例文件与规范一致 | 否 | 否 |
| `run` | 确定性地执行用例并记录 Trace | 是（仅允许范围内的目标） | 否 |
| `report` | 从 Trace 导出报告 | 否 | 否 |

退出码（CI 门禁语义）：

| 码 | 含义 |
|---|---|
| 0 | 全部通过 |
| 1 | 至少一条 Assertion 失败 |
| 2 | 没有失败，但存在「无法判定」（可用 `--fail-on inconclusive` 让它变成 1） |
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
  models.py          领域模型：Operation / TestCase / Assertion / TestRun / Verdict
  cases.py           用例文件读写、校验、确定性排序
  generator.py       降级模式的确定性用例生成
  assertions.py      声明式断言求值（不抛异常、不调用模型）
  policy.py          被测目标策略
  runner.py          唯一的网络出口
  trace.py           权威轨迹持久化
  report.py          Markdown / JSON / JUnit 导出
  cli.py             命令行与退出码
mock/mock_service.py 版本化评估基准
cases/petstore.yaml  示例用例文件（含人工补写的路径参数用例）
examples/petstore.yaml
docs/adr/            架构决策记录（0001 审批载体、0002 权威轨迹、0003 模式共用同一条流程）
AGENTS.md            架构地图与设计理念：接手任务前先读
CONTEXT.md           术语表与领域边界
```

## 已知限制（M0）

- 只支持本地 OpenAPI 文件，不读取代码仓库或自然语言需求。
- 嵌套 `$ref` 仅打包 `#/components/schemas/*`；指向其他位置的引用会导致「无法判定」，而不是静默通过。
- 请求体只支持 JSON；表单、multipart、二进制上传暂不支持。
- 尚无语义化的失败归因（诊断循环在 M3），报告只列出断言层面的观察事实。
- 尚无 Agent 循环与预算，因此也还没有 token 成本与步数指标。

## 开发

**改动前先读 [AGENTS.md](./AGENTS.md)**：它写了 seam 在哪、哪些模块必须保持深、禁止的模式，以及当前三笔已知的债。

```bash
uv pip install -e '.[dev]'
.venv/bin/pytest
```

Agent 循环所需的可选依赖单独声明，M0 不依赖它们：

```bash
uv pip install -e '.[agent]'
```
