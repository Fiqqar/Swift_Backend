"""Generator Postman Collection v2.1 + environment dari schema OpenAPI.

Digunakan oleh tombol "Export to Postman" di Swagger UI:
- `build_postman_collection()`  -> file `postman_collection.json`
- `build_dev_environment()`     -> file `dev.postman_environment.json`

Collection dibuat dengan:
- Base URL via variable `{{baseUrl}}` (default http://localhost:8000).
- Auth bearer level collection memakai `{{access_token}}`.
- Test script pada `POST /api/v1/auth/login` yang otomatis menyimpan token
  dari respons `data.token` ke collection & environment variable.
"""

import json
import re
import uuid
from datetime import datetime, timezone

_COLLECTION_SCHEMA = (
    "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
)
DEFAULT_BASE_URL = "http://localhost:8000"

# Path WS sintetis di OpenAPI (bukan endpoint HTTP sungguhan) -> jangan di-export.
_WS_SYNTHETIC_PATHS = {"/api/v1/ws/driver/position"}

_TAG_ORDER = ["Pathfinding", "Auth", "Shipment", "Traffic", "Tracking"]

_LOGIN_SCRIPT = [
    "var jsonData = pm.response.json();",
    "if (jsonData && jsonData.success && jsonData.data && jsonData.data.token) {",
    "    pm.collectionVariables.set('access_token', jsonData.data.token);",
    "    pm.environment.set('access_token', jsonData.data.token);",
    "    console.log('Login berhasil. Bearer token tersimpan otomatis.');",
    "} else {",
    "    pm.test('Login berhasil', function () {",
    "        pm.expect(jsonData && jsonData.success).to.eql(true);",
    "    });",
    "}",
]


def _resolve(schema, components):
    if not schema:
        return None
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return components.get("schemas", {}).get(name, {})
    return schema


def _stub_value(prop, components):
    if "default" in prop:
        return prop["default"]
    if "$ref" in prop:
        sub = _resolve(prop, components)
        if sub:
            return {k: _stub_value(v, components)
                    for k, v in sub.get("properties", {}).items()}
        return None
    t = prop.get("type")
    if t == "boolean":
        return False
    if t in ("integer", "number"):
        return 0
    if t == "string":
        return prop["enum"][0] if prop.get("enum") else ""
    if t == "array":
        return []
    if t == "object":
        return {}
    for key in ("anyOf", "oneOf"):
        if key in prop:
            for sub in prop[key]:
                if sub.get("type") != "null":
                    return _stub_value(sub, components)
    return None


def _stub_body(schema, components):
    sch = _resolve(schema, components)
    if not sch:
        return {}
    if sch.get("examples"):
        return sch["examples"][0]
    props = sch.get("properties", {})
    return {name: _stub_value(prop, components)
            for name, prop in props.items()}


def _operation_body(operation, components):
    rb = operation.get("requestBody")
    if not rb:
        return None
    content = rb.get("content", {})
    if "application/json" in content:
        schema = content["application/json"].get("schema")
        body = _stub_body(schema, components)
        return {
            "mode": "raw",
            "raw": json.dumps(body, indent=2, ensure_ascii=False),
            "options": {"raw": {"language": "json"}},
        }
    if "multipart/form-data" in content:
        schema = content["multipart/form-data"].get("schema")
        sch = _resolve(schema, components) or {}
        formdata = []
        for name, prop in (sch.get("properties", {})).items():
            if name == "file" or prop.get("format") == "binary":
                formdata.append({"key": name, "type": "file", "src": None})
            else:
                formdata.append({
                    "key": name, "type": "text",
                    "value": str(prop.get("default", "")),
                })
        return {"mode": "formdata", "formdata": formdata}
    return None


def _postman_url(path, operation):
    raw_path = path
    variables = []
    for match in re.finditer(r"\{(\w+)\}", path):
        name = match.group(1)
        raw_path = raw_path.replace("{" + name + "}", ":" + name)
        variables.append({"key": name, "value": ""})
    url = {
        "raw": "{{baseUrl}}" + raw_path,
        "host": ["{{baseUrl}}"],
        "path": [
            part.strip("{}")
            if part.startswith("{") else part
            for part in raw_path.split("/") if part
        ],
    }
    if variables:
        url["variable"] = variables
    query = [p for p in operation.get("parameters", [])
             if p.get("in") == "query"]
    if query:
        url["query"] = [
            {"key": p["name"], "value": "",
             "description": p.get("description", "")}
            for p in query
        ]
    return url


def _operation_item(path, method, operation, components):
    name = operation.get("summary") or f"{method.upper()} {path}"
    req = {"method": method.upper(), "url": _postman_url(path, operation)}
    body = _operation_body(operation, components)
    if body:
        req["body"] = body
        if body.get("mode") == "raw":
            req["header"] = [
                {"key": "Content-Type", "value": "application/json"}]
    desc = operation.get("description")
    if desc:
        req["description"] = desc
    item = {"name": name, "request": req, "response": []}
    if path == "/api/v1/auth/login" and method.lower() == "post":
        item["event"] = [{
            "listen": "test",
            "script": {"type": "text/javascript", "exec": _LOGIN_SCRIPT},
        }]
    return item


def build_postman_collection(openapi_schema):
    info = openapi_schema.get("info", {})
    components = openapi_schema.get("components", {})
    folders = {}
    for path, path_item in openapi_schema.get("paths", {}).items():
        if path in _WS_SYNTHETIC_PATHS:
            continue
        for method, operation in path_item.items():
            if method not in ("get", "post", "patch", "put", "delete"):
                continue
            tags = operation.get("tags") or ["Lainnya"]
            tag = tags[0]
            folders.setdefault(tag, []).append(
                _operation_item(path, method, operation, components))

    items = []
    for tag in _TAG_ORDER:
        if tag in folders:
            items.append({"name": tag, "item": folders.pop(tag)})
    for tag in sorted(folders):
        items.append({"name": tag, "item": folders[tag]})

    return {
        "info": {
            "_postman_id": str(uuid.uuid4()),
            "name": info.get("title", "API"),
            "description": info.get("description", ""),
            "schema": _COLLECTION_SCHEMA,
        },
        "item": items,
        "auth": {
            "type": "bearer",
            "bearer": [{
                "key": "token",
                "value": "{{access_token}}",
                "type": "string",
            }],
        },
        "variable": [
            {"key": "baseUrl", "value": DEFAULT_BASE_URL, "type": "string"},
            {"key": "access_token", "value": "", "type": "string"},
        ],
    }


def build_dev_environment():
    return {
        "id": str(uuid.uuid4()),
        "name": "dev",
        "values": [
            {"key": "baseUrl", "value": DEFAULT_BASE_URL, "enabled": True},
            {"key": "access_token", "value": "", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
        "_postman_exported_at": datetime.now(timezone.utc).isoformat(),
        "_postman_exported_using": "TEST2 API Engine export",
    }
