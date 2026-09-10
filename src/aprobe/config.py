"""aprobe.yaml 与凭据来源。

边界：配置文件里只出现凭据的**引用名**（环境变量名），值永远不在文件里。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .errors import ConfigError

DEFAULT_CONFIG_NAME = "aprobe.yaml"


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
