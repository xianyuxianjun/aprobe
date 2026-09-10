# 用例文件即审批载体，生成阶段没有网络出口

Status: accepted

我们要在"Agent 自主设计用例"和"CI 里没有人点确认"之间做取舍。决定：**Test Case 以版本化文件形式落在仓库里，它就是审批载体**——人在 Git/PR 里审阅，"运行期不得执行用例文件之外的请求"；因此生成阶段的 Agent 完全没有网络出口，网络只存在于确定性的执行阶段，且只接受文件中已存在的 `case_id`。

被否决的替代方案：

- **交互式确认（每次执行前人工点批准）**：本地可用，但 CI 里无人在场，只能退化成"自动执行"，等于没有审批；而且审批动作本身不可审计。
- **运行时自动探测（允许 Agent 自行发请求以了解接口行为）**：这是最自然的做法，也是本项目刻意放弃的做法。它会让"模型接触网络"成为可能，从而无法证明任何安全边界。

Consequences：

- 审批的载体是 Git 历史，可 diff、可 review、可回滚。
- 生成阶段的安全性可以一句话证明：*模型只能提出用例，不能向被测目标发请求*。
- 代价：在用例文件被接受之前，系统无法通过"试一下"来获得关于接口实际行为的知识；用例设计的准确性只能靠规范本身，泛化能力因此受限。这是明知的取舍。

## 补充（M1）

接入模型客户端后需要把"网络出口"说得更精确：项目的网络出口有两条，作用域不同且互不交叉。

- `runner.TestRunner` → **被测目标**，受 TargetPolicy 约束；
- `model_client` → **模型端点**，由 `APROBE_MODEL_BASE_URL` 配置，拿不到目标地址。

因此本 ADR 的原意不变：生成阶段没有通往**被测目标**的出口。已被测试守住：
`tests/test_agent_loop.py::test_loop_never_receives_the_target_address` 与
`tests/test_agent_integration.py::test_model_endpoint_receives_tool_declarations_and_no_target`。
