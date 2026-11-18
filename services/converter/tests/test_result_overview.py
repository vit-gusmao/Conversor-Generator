"""Compact result overview and paginated sheet metadata (U1 contract)."""

from __future__ import annotations

import time

TERMINAL = {"completed", "completed_with_warnings", "failed", "cancelled"}


def _upload(client, path, device: str = "dev_overview"):
    with open(path, "rb") as handle:
        return client.post(
            "/api/v1/conversions",
            headers={"x-device-id": device},
            files={"file": (path.name, handle, "application/pdf")},
        )


def _wait(client, conversion_id: str, device: str = "dev_overview", timeout: float = 90.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = client.get(
            f"/api/v1/conversions/{conversion_id}", headers={"x-device-id": device}
        ).json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.15)
    raise AssertionError(f"timeout: {last}")


def _completed(client, path, device: str = "dev_overview") -> str:
    created = _upload(client, path, device)
    assert created.status_code == 202, created.text
    conversion_id = created.json()["conversion_id"]
    final = _wait(client, conversion_id, device)
    assert final["status"] in {"completed", "completed_with_warnings"}, final
    return conversion_id


def test_overview_is_compact_and_complete(api_client, pdf_paths):
    conversion_id = _completed(api_client, pdf_paths["mixed_report"])

    response = api_client.get(
        f"/api/v1/conversions/{conversion_id}/result-summary",
        headers={"x-device-id": "dev_overview"},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["conversion_id"] == conversion_id
    assert body["run_id"]
    assert body["status"] in {"completed", "completed_with_warnings"}
    assert set(body["counts"]) == {"pages", "tables", "sheets", "cells", "records", "fields"}
    assert body["counts"]["sheets"] >= 1
    assert body["counts"]["pages"] >= 1
    assert body["warning_counts"]["total"] >= body["warning_counts"]["error"]
    assert isinstance(body["partial"], bool)
    assert body["default_sheet"]
    assert body["audit_url"].endswith("/audit")
    # The overview must not inline per-page arrays.
    assert "sheets" not in body
    assert "source_pages" not in str(body)


def test_overview_available_while_running(api_client, pdf_paths):
    created = _upload(api_client, pdf_paths["multi_fields"], "dev_overview_running")
    conversion_id = created.json()["conversion_id"]

    response = api_client.get(
        f"/api/v1/conversions/{conversion_id}/result-summary",
        headers={"x-device-id": "dev_overview_running"},
    )
    # Either it already finished (fast fixture) or it reports zero sheets while
    # running; both are valid and never a 500.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["counts"]["sheets"] >= 0
    _wait(api_client, conversion_id, "dev_overview_running")


def test_default_sheet_prefers_the_consolidated_view(api_client, pdf_paths):
    conversion_id = _completed(api_client, pdf_paths["mixed_report"])
    overview = api_client.get(
        f"/api/v1/conversions/{conversion_id}/result-summary",
        headers={"x-device-id": "dev_overview"},
    ).json()
    sheets = api_client.get(
        f"/api/v1/conversions/{conversion_id}/sheets?limit=200",
        headers={"x-device-id": "dev_overview"},
    ).json()

    consolidated = [item for item in sheets["items"] if item["kind"] == "consolidated"]
    if consolidated and consolidated[0]["columns"] <= 30 and consolidated[0]["rows"] > 0:
        assert overview["default_sheet"] == consolidated[0]["name"]
    else:
        assert overview["default_sheet"] != ""


def test_sheets_are_paginated_and_hide_page_lists(api_client, pdf_paths):
    conversion_id = _completed(api_client, pdf_paths["mixed_report"])

    first = api_client.get(
        f"/api/v1/conversions/{conversion_id}/sheets?limit=2",
        headers={"x-device-id": "dev_overview"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["total"] >= 1
    assert len(body["items"]) <= 2

    for item in body["items"]:
        assert set(item) == {
            "id",
            "name",
            "kind",
            "rows",
            "columns",
            "source_page_count",
            "table_id",
        }
        assert isinstance(item["source_page_count"], int)

    # Walking the cursor covers every sheet exactly once.
    seen: list[str] = []
    cursor = None
    while True:
        query = f"/api/v1/conversions/{conversion_id}/sheets?limit=1"
        if cursor:
            query += f"&cursor={cursor}"
        page = api_client.get(query, headers={"x-device-id": "dev_overview"}).json()
        seen.extend(item["name"] for item in page["items"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert len(seen) == body["total"]
    assert len(set(seen)) == len(seen)


def test_sheet_pages_are_paginated(api_client, pdf_paths):
    conversion_id = _completed(api_client, pdf_paths["mixed_report"])
    sheets = api_client.get(
        f"/api/v1/conversions/{conversion_id}/sheets?limit=50",
        headers={"x-device-id": "dev_overview"},
    ).json()
    target = next((item for item in sheets["items"] if item["source_page_count"] > 1), None)
    if target is None:
        return  # single-page fixture: nothing to paginate

    first = api_client.get(
        f"/api/v1/conversions/{conversion_id}/sheets/{target['id']}/pages?limit=1",
        headers={"x-device-id": "dev_overview"},
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["sheet_id"] == target["id"]
    assert len(body["pages"]) == 1
    assert body["total"] == target["source_page_count"]

    second = api_client.get(
        f"/api/v1/conversions/{conversion_id}/sheets/{target['id']}/pages?limit=1&cursor={body['next_cursor']}",
        headers={"x-device-id": "dev_overview"},
    ).json()
    assert second["pages"][0] != body["pages"][0]


def test_overview_respects_ownership(api_client, pdf_paths):
    conversion_id = _completed(api_client, pdf_paths["mixed_report"])
    foreign = api_client.get(
        f"/api/v1/conversions/{conversion_id}/result-summary",
        headers={"x-device-id": "someone-else"},
    )
    assert foreign.status_code == 403
