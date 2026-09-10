"""aprobe 的错误类型与进程退出码语义。

退出码是 CI 门禁的一部分（见 README「退出码」）：
0 通过 / 1 断言失败 / 2 存在无法判定 / 3 用例或配置非法 / 4 目标被策略拒绝。
"""

from __future__ import annotations

import enum


class ExitCode(enum.IntEnum):
    OK = 0
    ASSERTION_FAILED = 1
    INCONCLUSIVE = 2
    INVALID_INPUT = 3
    POLICY_DENIED = 4


class AprobeError(Exception):
    """aprobe 所有可预期错误的基类。"""

    exit_code = ExitCode.INVALID_INPUT


class SpecError(AprobeError):
    """OpenAPI 规范无法解析或使用了不受支持的构造。"""


class CaseFileError(AprobeError):
    """用例文件缺失、结构非法或引用了不存在的 Operation。"""


class ConfigError(AprobeError):
    """aprobe.yaml 缺失或非法。"""


class PolicyDeniedError(AprobeError):
    """目标、方法或动作被确定性策略拒绝。"""

    exit_code = ExitCode.POLICY_DENIED
