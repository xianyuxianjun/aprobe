from __future__ import annotations

from pathlib import Path

import pytest

from aprobe.errors import SpecError
from aprobe.specification import escape_token, load_specification, resolve_pointer


def test_parses_operations_with_stable_ids(specification) -> None:
    ids = {operation.operation_id for operation in specification.operations}
    assert ids == {
        "getHealth",
        "listPets",
        "createPet",
        "getPetStats",
        "getPetById",
        "getPetOwner",
    }
    assert specification.title == "Petstore Baseline"
    assert specification.openapi_version.startswith("3.")


def test_operation_keeps_method_path_and_pointer(specification) -> None:
    operation = next(item for item in specification.operations if item.operation_id == "getPetById")
    assert operation.method == "GET"
    assert operation.path == "/pets/{petId}"
    assert operation.pointer == "#/paths/~1pets~1{petId}/get"
    assert [parameter.name for parameter in operation.parameters] == ["petId"]
    assert operation.parameters[0].required is True
    assert operation.security == []


def test_operation_records_security_scheme(specification) -> None:
    operation = next(item for item in specification.operations if item.operation_id == "getPetOwner")
    assert operation.security == ["bearerAuth"]


def test_request_body_requirement_is_recorded(specification) -> None:
    operation = next(item for item in specification.operations if item.operation_id == "createPet")
    assert operation.request_body_required is True
    assert operation.request_body_media_type == "application/json"


def test_response_pointer_is_resolvable(specification) -> None:
    operation = next(item for item in specification.operations if item.operation_id == "listPets")
    response = next(item for item in operation.responses if item.status == "200")
    assert response.schema_pointer == "#/paths/~1pets/get/responses/200/content/application~1json/schema"
    resolved = specification.resolve(response.schema_pointer)
    assert resolved["required"] == ["items", "total"]


def test_schema_document_bundles_nested_refs(specification) -> None:
    document = specification.schema_document("#/components/schemas/Pet")
    assert document["properties"]["owner"] == {"$ref": "#/$defs/Owner"}
    assert "Owner" in document["$defs"]
    # 递归 schema 依赖惰性解析，因此这里必须是引用而不是展开后的副本
    assert document["$defs"]["Owner"]["properties"]["token"] == {"type": "string"}


def test_escape_token_follows_rfc6901() -> None:
    assert escape_token("a/b~c") == "a~1b~0c"


def test_resolve_pointer_handles_index_and_escaping(specification) -> None:
    assert resolve_pointer(specification.document, "#/paths/~1pets/get")["operationId"] == "listPets"
    with pytest.raises(ValueError):
        resolve_pointer(specification.document, "#/paths/~1pets/nope")


def test_rejects_remote_ref(tmp_path: Path) -> None:
    spec = tmp_path / "remote.yaml"
    spec.write_text(
        """
openapi: 3.0.3
info: {title: t, version: "1"}
paths:
  /a:
    get:
      responses:
        "200":
          description: ok
          content:
            application/json:
              schema:
                $ref: "https://example.com/schema.json"
""",
        encoding="utf-8",
    )
    with pytest.raises(SpecError, match="远程引用"):
        load_specification(spec)


def test_rejects_non_openapi3(tmp_path: Path) -> None:
    spec = tmp_path / "v2.yaml"
    spec.write_text('swagger: "2.0"\ninfo: {title: t, version: "1"}\npaths: {}\n', encoding="utf-8")
    with pytest.raises(SpecError, match="OpenAPI 3.x"):
        load_specification(spec)


def test_rejects_spec_without_paths(tmp_path: Path) -> None:
    spec = tmp_path / "empty.yaml"
    spec.write_text('openapi: 3.0.3\ninfo: {title: t, version: "1"}\npaths: {}\n', encoding="utf-8")
    with pytest.raises(SpecError, match="没有任何 paths"):
        load_specification(spec)
