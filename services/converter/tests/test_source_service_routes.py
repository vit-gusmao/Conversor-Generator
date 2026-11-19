"""T4 — source service + routes: grants, ownership isolation, validation."""

from __future__ import annotations

import uuid
from pathlib import Path

INTERNAL_HEADERS = {"x-internal-token": "test-internal-token"}


def _owner(api_client) -> dict:
    response = api_client.post("/api/v1/auth/anonymous")
    assert response.status_code in (200, 201)
    token = response.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _owner_id(api_client, owner_headers: dict) -> str:
    me = api_client.get("/api/v1/auth/me", headers=owner_headers).json()
    return me["id"]


def _grant_root(api_client, owner_headers: dict, root: Path) -> str:
    response = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": _owner_id(api_client, owner_headers), "label": "Testes"},
        headers=INTERNAL_HEADERS,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _make_source(api_client, owner_headers: dict, root_id: str, relative: str = "docs") -> dict:
    (root_id_holder := None)
    response = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": relative, "kind": "directory", "name": "Docs"},
        headers=owner_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_grant_and_create_source_flow(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "plan.xlsx").write_bytes(b"dados")
    headers = _owner(api_client)

    root_id = _grant_root(api_client, headers, root)
    source = _make_source(api_client, headers, root_id, "docs")
    assert source["kind"] == "directory"
    assert source["runtime_available"] is True

    listed = api_client.get("/api/v1/source-roots", headers=headers).json()["items"]
    assert any(item["id"] == root_id for item in listed)

    validation = api_client.post(f"/api/v1/sources/{source['id']}/validate", headers=headers).json()
    assert validation["ok"] is True
    assert validation["estimated_files"] == 1
    assert validation["sample"][0]["relative_path"].endswith("plan.xlsx")


def test_owner_cannot_use_foreign_root(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    root.mkdir()
    owner_a = _owner(api_client)
    owner_b = _owner(api_client)

    root_a = _grant_root(api_client, owner_a, root)
    response = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_a, "relative_path": "docs", "kind": "directory", "name": "X"},
        headers=owner_b,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "ROOT_NOT_FOUND"


def test_foreign_owner_gets_404_on_source(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    owner_a = _owner(api_client)
    owner_b = _owner(api_client)
    root_id = _grant_root(api_client, owner_a, root)
    source = _make_source(api_client, owner_a, root_id)

    got = api_client.get(f"/api/v1/sources/{source['id']}", headers=owner_b)
    assert got.status_code == 404

    validated = api_client.post(f"/api/v1/sources/{source['id']}/validate", headers=owner_b)
    assert validated.status_code == 404


def test_optimistic_version_conflict(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    headers = _owner(api_client)
    root_id = _grant_root(api_client, headers, root)
    source = _make_source(api_client, headers, root_id)

    renamed = api_client.patch(
        f"/api/v1/sources/{source['id']}",
        json={"version": source["version"], "name": "Docs v2"},
        headers=headers,
    )
    assert renamed.status_code == 200
    assert renamed.json()["version"] == source["version"] + 1

    stale = api_client.patch(
        f"/api/v1/sources/{source['id']}",
        json={"version": source["version"], "name": "Sobrescritor"},
        headers=headers,
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_traversal_source_is_rejected(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    root.mkdir()
    (tmp_path / "fora").mkdir()
    headers = _owner(api_client)
    root_id = _grant_root(api_client, headers, root)
    response = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "../fora", "kind": "directory", "name": "X"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] in {"TRAVERSAL_DENIED", "INVALID_SOURCE", "OUTSIDE_ROOT"}


def test_missing_source_path_is_unavailable(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    root.mkdir()
    headers = _owner(api_client)
    root_id = _grant_root(api_client, headers, root)
    response = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "fantasma", "kind": "directory", "name": "X"},
        headers=headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "SOURCE_UNAVAILABLE"


def test_validate_unavailable_vs_empty(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    (root / "vazio").mkdir(parents=True)
    headers = _owner(api_client)
    root_id = _grant_root(api_client, headers, root)

    empty_source = _make_source(api_client, headers, root_id, "vazio")
    empty = api_client.post(f"/api/v1/sources/{empty_source['id']}/validate", headers=headers).json()
    assert empty["ok"] is True and empty["available"] is True and empty["estimated_files"] == 0

    missing = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "vazio", "kind": "directory", "name": "V"},
        headers=headers,
    )
    # reuse a valid source then remove the directory underneath it
    import shutil

    shutil.rmtree(root / "vazio")
    missing_validation = api_client.post(
        f"/api/v1/sources/{empty_source['id']}/validate", headers=headers
    ).json()
    assert missing_validation["ok"] is False
    assert missing_validation["available"] is False
    assert missing_validation["code"] == "SOURCE_NOT_FOUND"


def test_ungranted_public_creation_is_impossible(api_client):
    # No public route grants roots at all.
    response = api_client.post(
        "/api/v1/source-roots",
        json={"canonical_path": "C:/Windows"},
    )
    assert response.status_code in (404, 405, 401, 403)


def test_internal_grant_requires_token(api_client, tmp_path: Path):
    root = tmp_path / "raiz"
    root.mkdir()
    response = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(root), "owner_id": "someone"},
        headers={"x-internal-token": "errado"},
    )
    assert response.status_code in (401, 403)
