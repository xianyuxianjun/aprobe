"""aprobe.yaml 与凭据来源。

边界：配置文件里只出现凭据的**引用名**（环境变量名），值永远不在文件里。

`.env` 只是"环境变量的另一个来源"：真实环境优先，`.env` 只补空缺。
它进 `.gitignore`，模板是 `.env.example`；值不会被打印、不会被写进 Trace。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError

DEFAULT_CONFIG_NAME = "aprobe.yaml"
ENV_FILE_NAME = ".env"

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str
    allow: list[str] = Field(default_factory=list)


class LimitsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_ms: int = 5000
    max_response_bytes: int = 65536
    retries: int = 0


class AuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scheme: str | None = None
    env: str | None = None
    header: str | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    spec: str
    cases: str
    trace_db: str = ".aprobe/trace.db"
    target: TargetConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    allow_write: bool = False

    def resolve(self, base: Path, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else (base / path)


def load_config(path: str | Path) -> Config:
    source = Path(path)
    if not source.is_file():
        raise ConfigError(f"配置文件不存在: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML: {source}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {source}")
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"配置文件结构非法: {source}\n{exc}") from exc


def parse_env_file(text: str) -> dict[str, str]:
    """解析 `.env` 文本。这是一个纯函数，因此可以单独测。

    容忍：空行、`#` 注释、`export KEY=VALUE`、值两侧的单双引号、`=` 两侧空格。
    忽略：没有 `=` 的行、键名非法的行。同名键后者覆盖前者。
    """
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not _ENV_KEY.match(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def effective_environment(directory: Path, environ: Mapping[str, str]) -> dict[str, str]:
    """真实环境优先，`directory/.env` 只补空缺。

    优先级做成这样是刻意的：临时 `FOO=bar aprobe ...` 必须能覆盖 `.env`，
    否则 CI 与本地调试会互相打架。
    """
    values = dict(environ)
    path = directory / ENV_FILE_NAME
    if path.is_file():
        for key, value in parse_env_file(path.read_text(encoding="utf-8")).items():
            values.setdefault(key, value)
    return values
