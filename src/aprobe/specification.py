"""OpenAPI 3.0 / 3.1 解析。

边界：只解析本地 `$ref`；远程引用直接拒绝，不做静默忽略。
每个 Operation 获得稳定标识，并保留 JSON Pointer 供断言与报告引用。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import SpecError
from .models import Operation, ParameterDecl, ResponseDecl, Specification

HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def escape_token(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def pointer_join(*tokens: str) -> str:
    return "#" + "".join(f"/{escape_token(token)}" for token in tokens)


_COMPONENT_SCHEMA_PREFIX = "#/components/schemas/"


def bundle_local_refs(schema: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    """把 `#/components/schemas/X` 引用重写为 `#/$defs/X`，并打包成自包含 schema。

    这样 JSON Schema 校验器可以惰性解析引用，递归 schema 也不会被展开成死循环。
    指向 components 之外（例如某个 response 的内联 schema）的引用不做重写；
    解析失败时校验器会报错，最终表现为「无法判定」而不是「通过」。
    """
    components = document.get("components")
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        return dict(schema)

    def rewrite(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith(_COMPONENT_SCHEMA_PREFIX):
                name = ref[len(_COMPONENT_SCHEMA_PREFIX) :]
                return {"$ref": f"#/$defs/{escape_token(name)}"}
            return {key: rewrite(value) for key, value in node.items()}
        if isinstance(node, list):
            return [rewrite(item) for item in node]
        return node

    reachable: set[str] = set()
    _collect_component_refs(schema, schemas, reachable)
    bundled: dict[str, Any] = rewrite(schema)
    defs = {escape_token(name): rewrite(schemas[name]) for name in sorted(reachable) if name in schemas}
    existing = bundled.get("$defs")
    if isinstance(existing, dict):
        defs = {**defs, **existing}
    bundled["$defs"] = defs
    return bundled


def _collect_component_refs(node: Any, schemas: dict[str, Any], found: set[str]) -> None:
    """收集从 `node` 出发可达的 component 名字（传递闭包）。

    只打包用得到的部分：接真实模型时发现，把整份文档塞进 $defs 会让每次
    get_response_schema 的返回体大出一个量级，而这些 token 会在后续每一步里重复发送。
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_COMPONENT_SCHEMA_PREFIX):
            name = ref[len(_COMPONENT_SCHEMA_PREFIX) :]
            if name not in found:
                found.add(name)
                _collect_component_refs(schemas.get(name, {}), schemas, found)
        for value in node.values():
            _collect_component_refs(value, schemas, found)
    elif isinstance(node, list):
        for item in node:
            _collect_component_refs(item, schemas, found)


def resolve_pointer(document: Any, pointer: str) -> Any:
    """解析 JSON Pointer。`#` 与空字符串都表示文档根。"""
    if pointer in ("", "#"):
        return document
    if not pointer.startswith("#"):
        raise ValueError(f"只接受以 # 开头的本地 JSON Pointer: {pointer!r}")
    node = document
    for raw in pointer[1:].split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(token)]
            except (ValueError, IndexError) as exc:
                raise ValueError(f"Pointer 无法解析: {pointer}") from exc
        elif isinstance(node, dict):
            if token not in node:
                raise ValueError(f"Pointer 无法解析: {pointer}")
            node = node[token]
        else:
            raise ValueError(f"Pointer 无法解析: {pointer}")
    return node


def _collect_remote_refs(node: Any, at: str, found: list[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and not ref.startswith("#"):
            found.append(f"{at}: {ref}")
        for key, value in node.items():
            _collect_remote_refs(value, f"{at}/{escape_token(str(key))}", found)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _collect_remote_refs(value, f"{at}/{index}", found)


def _load_document(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SpecError(f"规范文件不存在: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SpecError(f"规范文件不是合法 YAML/JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise SpecError(f"规范文件顶层必须是对象: {path}")
    return raw


def _parse_parameters(raw: list[Any], pointer: str) -> list[ParameterDecl]:
    parameters: list[ParameterDecl] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SpecError(f"{pointer}/{index} 参数必须是对象")
        name = item.get("name")
        location = item.get("in")
        if not isinstance(name, str) or location not in ("path", "query", "header", "cookie"):
            raise SpecError(f"{pointer}/{index} 参数缺少合法的 name/in")
        schema = item.get("schema")
        parameters.append(
            ParameterDecl(
                name=name,
                location=location,
                required=bool(item.get("required", location == "path")),
                schema=schema if isinstance(schema, dict) else {},
            )
        )
    return parameters


def _parse_responses(raw: Any, pointer: str) -> list[ResponseDecl]:
    if not isinstance(raw, dict):
        return []
    responses: list[ResponseDecl] = []
    for status, body in raw.items():
        if not isinstance(body, dict):
            continue
        content = body.get("content")
        media_type: str | None = None
        schema_pointer: str | None = None
        if isinstance(content, dict):
            for candidate in ("application/json", *sorted(k for k in content if k != "application/json")):
                entry = content.get(candidate)
                if isinstance(entry, dict) and isinstance(entry.get("schema"), dict):
                    media_type = candidate
                    # pointer 已经是转义过的 JSON Pointer，这里只能拼接，不能再转义一次
                    schema_pointer = (
                        f"{pointer.rstrip('/')}/responses/{escape_token(str(status))}"
                        f"/content/{escape_token(candidate)}/schema"
                    )
                    break
        responses.append(
            ResponseDecl(
                status=str(status),
                description=str(body.get("description", "")),
                media_type=media_type,
                schema_pointer=schema_pointer,
            )
        )
    return responses


def _security_names(requirements: Any, schemes: dict[str, Any]) -> list[str]:
    names: list[str] = []
    if isinstance(requirements, list):
        for entry in requirements:
            if isinstance(entry, dict):
                names.extend(str(key) for key in entry)
    return [name for name in dict.fromkeys(names) if name in schemes]


def load_specification(path: str | Path) -> Specification:
    """读取并解析一份 OpenAPI 3.x 规范。"""
    source = Path(path)
    document = _load_document(source)

    version = document.get("openapi")
    if not isinstance(version, str) or not version.startswith("3."):
        raise SpecError(f"只支持 OpenAPI 3.x，收到: {version!r}")

    remote_refs: list[str] = []
    _collect_remote_refs(document, "#", remote_refs)
    if remote_refs:
        raise SpecError("只解析本地 $ref，远程引用会被拒绝: " + "; ".join(remote_refs[:5]))

    info = document.get("info") if isinstance(document.get("info"), dict) else {}
    components = document.get("components") if isinstance(document.get("components"), dict) else {}
    schemes = components.get("securitySchemes") if isinstance(components.get("securitySchemes"), dict) else {}
    global_security = document.get("security")

    paths = document.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise SpecError("规范中没有任何 paths")

    operations: list[Operation] = []
    ignored: list[str] = []
    for path_key, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        path_pointer = pointer_join("paths", str(path_key))
        shared_parameters = path_item.get("parameters")
        for method in HTTP_METHODS:
            node = path_item.get(method)
            if node is None:
                continue
            if not isinstance(node, dict):
                raise SpecError(f"{path_pointer}/{method} 必须是对象")
            pointer = pointer_join("paths", str(path_key), method)

            raw_parameters = list(shared_parameters) if isinstance(shared_parameters, list) else []
            own_parameters = node.get("parameters")
            if isinstance(own_parameters, list):
                raw_parameters.extend(own_parameters)
            parameters = _parse_parameters(raw_parameters, pointer)

            request_body = node.get("requestBody")
            body_required = False
            body_media_type: str | None = None
            if isinstance(request_body, dict):
                body_required = bool(request_body.get("required", False))
                content = request_body.get("content")
                if isinstance(content, dict) and content:
                    body_media_type = "application/json" if "application/json" in content else sorted(content)[0]

            operation_id = node.get("operationId")
            if not isinstance(operation_id, str) or not operation_id:
                operation_id = f"{method}:{path_key}"

            operations.append(
                Operation(
                    operation_id=operation_id,
                    method=method.upper(),
                    path=str(path_key),
                    summary=str(node.get("summary", "")),
                    tags=[str(tag) for tag in node.get("tags", []) if isinstance(tag, (str, int))],
                    parameters=parameters,
                    request_body_required=body_required,
                    request_body_media_type=body_media_type,
                    responses=_parse_responses(node.get("responses"), pointer),
                    security=_security_names(node.get("security", global_security), schemes),
                    pointer=pointer,
                )
            )

        for extra in ("callbacks",):
            if extra in path_item:
                ignored.append(f"{path_pointer}/{extra}: 暂不解析")

    for extra in ("webhooks", "callbacks"):
        if extra in document:
            ignored.append(f"#/{extra}: 暂不解析")

    return Specification(
        document=document,
        openapi_version=version,
        title=str(info.get("title", "(untitled)")),
        version=str(info.get("version", "")),
        source=str(source),
        operations=operations,
        ignored=ignored,
    )
