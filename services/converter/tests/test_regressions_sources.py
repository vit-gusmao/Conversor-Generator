"""FX-1a regressions for sources/ and ingestion/ (AU1-05..AU1-32, S6, S8, S14).

Each test names the finding it pins down. Windows-only behaviour (device
names, reparse points) is skipped elsewhere, as in the existing T4 tests.
"""

from __future__ import annotations

import io
import os
import sys
import uuid
import zipfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from PIL import Image

from api.deps import ApiError
from config import settings as runtime_settings
from ingestion import registry
from ingestion.image_input import detect_signature, image_capabilities
from ingestion.registry import probe_input, probe_input_bytes, probe_input_path
from ingestion.spreadsheet_input import SpreadsheetOptions, read_spreadsheet
from ingestion.base import IngestionError
from ingestion.spreadsheet_pipeline import SpreadsheetConversionPipeline
from engine.pipeline import PipelineOptions
from audit.events import AuditRecorder
from models.db import get_session_factory
from sources.importer import ImportService
from sources.path_policy import PathPolicyError, assert_snapshot_inside_root, resolve_source_path
from sources.service import SourceService
from sources.snapshot import capture_snapshot
from sources.types import IMPORT_STATUSES, SourceCandidate, SourceDefinitionSnapshot

WIN32 = sys.platform == "win32"
INTERNAL_TOKEN = "test-internal-token"


# ----------------------------------------------------------------- helpers
def _session(api_client) -> dict:
    response = api_client.post("/api/v1/auth/anonymous")
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    me = api_client.get("/api/v1/auth/me", headers=headers)
    return {"id": me.json()["id"], "headers": headers}


def _xlsx_bytes(content: str = "dados") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["A", "B"])
    ws.append([content, 1])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _grant(api_client, owner: dict, path: Path) -> str:
    grant = api_client.post(
        "/internal/v1/source-roots",
        json={"canonical_path": str(path), "owner_id": owner["id"]},
        headers={"x-internal-token": INTERNAL_TOKEN},
    )
    assert grant.status_code == 201, grant.text
    return grant.json()["id"]


def _make_source(api_client, tmp_path: Path) -> tuple[dict, str, Path]:
    owner = _session(api_client)
    root = tmp_path / "raiz"
    (root / "docs").mkdir(parents=True)
    root_id = _grant(api_client, owner, root)
    source = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "docs", "kind": "directory", "name": "Docs"},
        headers=owner["headers"],
    )
    assert source.status_code == 201, source.text
    return owner, source.json()["id"], root


def _definition(owner: dict, source_id: str) -> SourceDefinitionSnapshot:
    sources = SourceService()
    session = get_session_factory()()
    try:
        row = sources.repository.get_source(session, source_id=source_id, owner_id=owner["id"])
        root = sources.repository.get_root(session, root_id=row.root_id, owner_id=owner["id"])
        return sources.definition_for(row, root)
    finally:
        session.close()


def _candidate(root: Path, relative: str) -> SourceCandidate:
    stat = os.stat(root / relative)
    return SourceCandidate(relative_path=relative, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)


def _ooxml_zip(extra_entries: int = 0) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        for index in range(extra_entries):
            archive.writestr(f"extra/{index}.txt", "")
    return buffer.getvalue()


# ------------------------------------------------- AU1-07 reserved names
@pytest.mark.skipif(not WIN32, reason="Windows device names are enforced on Windows")
@pytest.mark.parametrize(
    "name",
    [
        "Compras 2024.xlsx",
        "Comissões",
        "Composição.xlsx",
        "console.txt",
        "Lptoficina.csv",
        "Auxiliar.xlsx",
        "nulo.csv",
        "CON2.txt",
    ],
)
def test_names_that_merely_start_with_a_device_stem_are_allowed(tmp_path: Path, name: str):
    root = tmp_path / "raiz"
    root.mkdir()
    assert resolve_source_path(str(root), name) == root / name


@pytest.mark.skipif(not WIN32, reason="Windows device names are enforced on Windows")
@pytest.mark.parametrize(
    "name",
    ["CON", "con.txt", "PRN.csv", "aux", "NUL.xlsx", "COM1", "com9.csv", "LPT1.txt", "lpt9", "COM¹.txt", "LPT³.csv", "NUL .txt"],
)
def test_exact_device_stems_are_still_refused(tmp_path: Path, name: str):
    root = tmp_path / "raiz"
    root.mkdir()
    with pytest.raises(PathPolicyError) as info:
        resolve_source_path(str(root), name)
    assert info.value.code == "SPECIAL_PATH_DENIED"


def test_reserved_name_is_a_per_item_failure_not_an_aborted_import(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    service = ImportService()
    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )
    definition = _definition(owner, source_id)
    options_hash = service._options_hash({})
    batch = [
        SourceCandidate(relative_path="docs/NUL.txt", size_bytes=3, mtime_ns=0),
        _candidate(root, "docs/plan.xlsx"),
    ]
    service._capture_batch(
        record["id"], owner["id"], definition, batch, {}, options_hash,
        force=False, staging_dir=tmp_path / "staging", captured=[],
    )
    items = {item["relative_path"]: item for item in service.items(record["id"], owner["id"])}
    assert items["docs/NUL.txt"]["status"] == "failed"
    assert items["docs/NUL.txt"]["error_code"] == "SPECIAL_PATH_DENIED"
    assert items["docs/plan.xlsx"]["status"] == "captured"


# ------------------------------------------------ AU1-08 resumed completion
def test_resumed_import_with_all_items_submitted_still_completes(api_client, tmp_path: Path, monkeypatch):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("retomada"))
    service = ImportService()
    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )
    import_id = record["id"]

    # First execution "crashes" before versions are recorded: items stay submitted.
    monkeypatch.setattr(service, "_record_versions", lambda *args, **kwargs: None)
    service.run(import_id, owner["id"])
    assert {item["status"] for item in service.items(import_id, owner["id"])} == {"submitted"}
    monkeypatch.undo()

    # Resume: nothing is captured anymore, but the import must still finish.
    resumed = service.run(import_id, owner["id"])
    assert resumed["status"] == "completed"
    items = service.items(import_id, owner["id"])
    assert [item["status"] for item in items] == ["completed"]
    assert len([item for item in items if item["conversion_id"]]) == 1


def test_import_only_writes_contract_statuses(api_client, tmp_path: Path, monkeypatch):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes("estados"))
    service = ImportService()
    record = service.start(
        owner_id=owner["id"],
        source_id=source_id,
        request={"options": {"schema_version": 3}},
        idempotency_key=uuid.uuid4().hex,
    )
    seen: list[str] = []
    original = service._mark_status

    def spy(import_id, owner_id, status, total=None):
        seen.append(status)
        return original(import_id, owner_id, status, total=total)

    monkeypatch.setattr(service, "_mark_status", spy)
    service.run(record["id"], owner["id"])
    assert seen, "the run must record its intermediate statuses"
    assert set(seen) <= set(IMPORT_STATUSES)
    assert "finalize" not in seen


# ------------------------------------------------ AU1-09 deterministic rows
def test_recapture_of_oversized_file_reuses_its_item(api_client, tmp_path: Path, monkeypatch):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "grande.bin").write_bytes(b"x" * 100)
    service = ImportService()
    record = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    monkeypatch.setattr(runtime_settings, "converter_max_pdf_bytes", 10)
    definition = _definition(owner, source_id)
    options_hash = service._options_hash({})
    batch = [_candidate(root, "docs/grande.bin")]
    for _ in range(2):
        service._capture_batch(
            record["id"], owner["id"], definition, batch, {}, options_hash,
            force=False, staging_dir=tmp_path / "staging", captured=[],
        )
    items = [item for item in service.items(record["id"], owner["id"]) if item["relative_path"] == "docs/grande.bin"]
    assert len(items) == 1
    assert items[0]["error_code"] == "FILE_TOO_LARGE"


def test_recapture_of_missing_file_reuses_its_item(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    service = ImportService()
    record = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    definition = _definition(owner, source_id)
    options_hash = service._options_hash({})
    batch = [SourceCandidate(relative_path="docs/ausente.xlsx", size_bytes=5, mtime_ns=0)]
    for _ in range(2):
        service._capture_batch(
            record["id"], owner["id"], definition, batch, {}, options_hash,
            force=False, staging_dir=tmp_path / "staging", captured=[],
        )
    items = [item for item in service.items(record["id"], owner["id"]) if item["relative_path"] == "docs/ausente.xlsx"]
    assert len(items) == 1
    assert items[0]["error_code"] == "SOURCE_MISSING"


def test_recapture_of_unchanged_file_reuses_its_skip_item(api_client, tmp_path: Path):
    from sources.repository import SourceRepository

    owner, source_id, root = _make_source(api_client, tmp_path)
    content = b"ja processado"
    (root / "docs" / "ja.txt").write_bytes(content)
    service = ImportService()
    record = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    import hashlib

    options_hash = service._options_hash({})
    session = get_session_factory()()
    try:
        SourceRepository().upsert_source_file_version(
            session,
            owner_id=owner["id"],
            source_id=source_id,
            relative_path="docs/ja.txt",
            sha256=hashlib.sha256(content).hexdigest(),
            options_hash=options_hash,
            import_id=record["id"],
            conversion_id=None,
        )
        session.commit()
    finally:
        session.close()
    definition = _definition(owner, source_id)
    batch = [_candidate(root, "docs/ja.txt")]
    for _ in range(2):
        service._capture_batch(
            record["id"], owner["id"], definition, batch, {}, options_hash,
            force=False, staging_dir=tmp_path / "staging", captured=[],
        )
    items = [item for item in service.items(record["id"], owner["id"]) if item["relative_path"] == "docs/ja.txt"]
    assert len(items) == 1
    assert items[0]["status"] == "skipped"


# --------------------------------------------- AU1-14 snapshot-start parity
def test_snapshot_start_normalizes_options_by_source_kind(api_client, tmp_path: Path):
    owner, source_id, _root = _make_source(api_client, tmp_path)
    service = ImportService()
    definition = _definition(owner, source_id)
    record = service.start_from_snapshot(
        owner_id=owner["id"],
        definition=definition,
        request={"options": {}},
        idempotency_key=uuid.uuid4().hex,
    )
    session = get_session_factory()()
    try:
        stored = service.repository.get_import(session, import_id=record["id"], owner_id=owner["id"])
        stored_options = stored.options_json
    finally:
        session.close()
    # Source without spreadsheet patterns is a PDF/mixed source: no v3 section.
    assert stored_options == service._normalize_for_source({}, definition)
    assert "spreadsheet" not in stored_options


def test_disabled_imports_refuse_snapshot_starts(api_client, tmp_path: Path, monkeypatch):
    owner, source_id, _root = _make_source(api_client, tmp_path)
    service = ImportService()
    definition = _definition(owner, source_id)
    monkeypatch.setattr(runtime_settings, "source_imports_enabled", False)
    with pytest.raises(ApiError) as info:
        service.start_from_snapshot(
            owner_id=owner["id"], definition=definition, request={"options": {}},
            idempotency_key=uuid.uuid4().hex,
        )
    assert info.value.detail["error"]["code"] == "FEATURE_DISABLED"


# ----------------------------------------------- S6 pending imports are busy
def test_pending_import_makes_the_source_busy(api_client, tmp_path: Path):
    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    service = ImportService()
    service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    with pytest.raises(ApiError) as info:
        service.start(
            owner_id=owner["id"], source_id=source_id,
            request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
        )
    assert info.value.detail["error"]["code"] == "SOURCE_IMPORT_BUSY"


def test_stale_pending_import_does_not_block_the_source_forever(api_client, tmp_path: Path):
    from datetime import UTC, datetime, timedelta

    from models.db import SourceImportRow

    owner, source_id, root = _make_source(api_client, tmp_path)
    (root / "docs" / "plan.xlsx").write_bytes(_xlsx_bytes())
    service = ImportService()
    first = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    session = get_session_factory()()
    try:
        row = session.get(SourceImportRow, first["id"])
        row.created_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()
    finally:
        session.close()
    second = service.start(
        owner_id=owner["id"], source_id=source_id,
        request={"options": {"schema_version": 3}}, idempotency_key=uuid.uuid4().hex,
    )
    assert second["id"] != first["id"]


# ---------------------------------------------- AU1-27 key reuse by source
def test_same_key_on_another_source_is_a_conflict(api_client, tmp_path: Path):
    owner, source_a, root = _make_source(api_client, tmp_path)
    root_id = api_client.get(f"/api/v1/sources/{source_a}", headers=owner["headers"]).json()["root_id"]
    (root / "outra").mkdir()
    source_b = api_client.post(
        "/api/v1/sources",
        json={"root_id": root_id, "relative_path": "outra", "kind": "directory", "name": "Outra"},
        headers=owner["headers"],
    ).json()["id"]
    service = ImportService()
    key = uuid.uuid4().hex
    body = {"options": {"schema_version": 3}}
    service.start(owner_id=owner["id"], source_id=source_a, request=body, idempotency_key=key)
    with pytest.raises(ApiError) as info:
        service.start(owner_id=owner["id"], source_id=source_b, request=body, idempotency_key=key)
    assert info.value.detail["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


# ------------------------------------------- AU1-32 / AU1-05 probe reasons
def test_xlsx_budget_error_returns_an_unsupported_probe(tmp_path: Path):
    data = _ooxml_zip(extra_entries=2100)  # above XLSX_MAX_ENTRIES
    probe = probe_input(data)
    assert probe.kind == "spreadsheet"
    assert probe.supported is False
    assert probe.reason_code == "SPREADSHEET_BUDGET_EXCEEDED"
    assert probe.reason


def test_probes_expose_the_precise_reason_code():
    not_ooxml = io.BytesIO()
    with zipfile.ZipFile(not_ooxml, "w") as archive:
        archive.writestr("notas.txt", "x")
    assert probe_input(not_ooxml.getvalue()).reason_code == "NOT_SPREADSHEET"

    encoding = probe_input_bytes(b"Nome;Valor\n\xff\xfe\x00bad\n", "dados.csv")
    assert encoding.supported is False
    assert encoding.reason_code == "CSV_ENCODING_REQUIRED"

    ambiguous = probe_input_bytes(b"a,b;c\n1,2;3\n", "dados.csv")
    assert ambiguous.supported is False
    assert ambiguous.reason_code == "CSV_DIALECT_REQUIRED"


def test_path_probe_exposes_the_csv_reason_code(tmp_path: Path):
    path = tmp_path / "ambiguo.csv"
    path.write_bytes(b"a,b;c\n1,2;3\n")
    probe = probe_input_path(path, "ambiguo.csv")
    assert probe.supported is False
    assert probe.reason_code == "CSV_DIALECT_REQUIRED"


# ------------------------------------------- AU1-15 magic bytes need headers
def test_csv_content_with_bm_or_pnm_prefix_is_not_a_raster():
    assert detect_signature(b"BMF;Ativo\n1;2\n") is None
    assert detect_signature(b"P2;Nome\n1;2\n") is None
    csv_probe = probe_input_bytes(b"BMF;Ativo\n1;2\n", "ativos.csv")
    assert csv_probe.kind == "spreadsheet"
    assert csv_probe.format == "CSV"
    pnm_probe = probe_input_bytes(b"P2;Nome\n1;2\n", "nomes.csv")
    assert pnm_probe.kind == "spreadsheet"
    assert pnm_probe.format == "CSV"


def test_real_bmp_and_pnm_headers_are_still_detected():
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, "BMP")
    assert detect_signature(buffer.getvalue()) == "BMP"
    assert detect_signature(b"BM") is None
    assert detect_signature(b"P5\n2 2\n255\n" + b"\x00" * 4) == "PNM"
    assert detect_signature(b"P6 2 2 255\n") == "PNM"


def test_declared_csv_wins_over_a_raster_magic_when_content_is_text():
    raster_like = b"P1\nNome;Valor\n1;2\n"  # valid PNM magic, plainly text rows
    probe = probe_input_bytes(raster_like, "dados.csv")
    assert probe.kind == "spreadsheet"
    assert probe.format == "CSV"


# ------------------------------------------- AU1-16 / AU1-30 codec groups
def test_group_formats_are_matched_per_name(monkeypatch):
    fake_groups = [
        {"group": "advanced-modern", "formats": ["AVIF", "HEIF/HEIC"], "available": True, "outputs": [], "multipage": False},
    ]
    monkeypatch.setattr(registry, "image_capabilities", lambda: fake_groups)
    assert registry._format_available("HEIF") is True
    assert registry._format_available("HEIC") is True


def test_psd_does_not_depend_on_rawpy(monkeypatch):
    import ingestion.image_input as image_input

    monkeypatch.setattr(image_input, "_module_available", lambda name: False)
    groups = image_capabilities()
    psd_group = next(group for group in groups if "PSD" in [str(f).upper() for f in group["formats"]])
    assert psd_group["available"] is True
    assert registry._format_available("PSD") is True


def test_jpeg2000_availability_follows_the_openjpeg_codec(monkeypatch):
    import ingestion.image_input as image_input

    real_check = image_input.features.check
    monkeypatch.setattr(
        image_input.features, "check", lambda name: False if name == "jpg_2000" else real_check(name)
    )
    groups = image_capabilities()
    jp2_group = next(group for group in groups if "JPEG2000" in [str(f).upper() for f in group["formats"]])
    assert jp2_group["available"] is False
    assert registry._format_available("JPEG2000") is False


# ------------------------------------------ AU1-17 empty sheet with header
def test_header_row_true_on_an_empty_sheet_does_not_crash(tmp_path: Path):
    path = tmp_path / "vazia.xlsx"
    wb = Workbook()
    wb.create_sheet("Vazia")
    wb.save(path)

    class _NullSink:
        def update(self, *args, **kwargs):
            pass

    recorder = AuditRecorder(run_id="run_header_empty")
    pipeline = SpreadsheetConversionPipeline(
        spreadsheet_path=path,
        run_id="run_header_empty",
        conversion_id="conv_header_empty",
        recorder=recorder,
        sink=_NullSink(),
        options=PipelineOptions(spreadsheet={"header_row": True}),
    )
    result = pipeline.run()
    assert result.ok is True
    assert "EMPTY_SHEET" in {issue.code for issue in recorder.issues}


# ------------------------------------------------ S8 malformed CSV fields
def test_oversized_csv_field_is_an_explicit_ingestion_error(tmp_path: Path):
    path = tmp_path / "campo.csv"
    path.write_bytes(("a;" + "x" * 200_000 + "\n").encode("utf-8"))
    with pytest.raises(IngestionError) as info:
        read_spreadsheet(path, SpreadsheetOptions())
    assert info.value.code == "CSV_PARSE_FAILED"


# ------------------------------------------------ S14 stability wait
def test_old_files_skip_the_stability_wait(tmp_path: Path, monkeypatch):
    import time as time_module

    root = tmp_path / "raiz"
    root.mkdir()
    target = root / "antigo.xlsx"
    target.write_bytes(b"conteudo antigo")
    old = target.stat().st_mtime - 3600
    os.utime(target, (old, old))
    sleeps: list[float] = []
    monkeypatch.setattr(time_module, "sleep", lambda seconds: sleeps.append(seconds))
    staging = tmp_path / "staging"
    ref = capture_snapshot(
        _candidate(root, "antigo.xlsx"), str(root), staging_dir=staging,
        stability_seconds=2.0, source_path=target,
    )
    assert ref.size_bytes == len(b"conteudo antigo")
    assert sleeps == []


def test_fresh_files_keep_the_stability_wait(tmp_path: Path, monkeypatch):
    import time as time_module

    root = tmp_path / "raiz"
    root.mkdir()
    target = root / "novo.xlsx"
    target.write_bytes(b"gravando")
    sleeps: list[float] = []
    monkeypatch.setattr(time_module, "sleep", lambda seconds: sleeps.append(seconds))
    capture_snapshot(
        _candidate(root, "novo.xlsx"), str(root), staging_dir=tmp_path / "staging",
        stability_seconds=2.0, source_path=target,
    )
    assert sleeps == [2.0]


# ------------------------------------- AU1-24 realpath roots (Windows only)
@pytest.mark.skipif(not WIN32, reason="requires Windows reparse points")
def test_capture_under_a_junctioned_root_is_not_an_escape(tmp_path: Path):
    import _winapi

    real = tmp_path / "real"
    real.mkdir()
    (real / "a.xlsx").write_bytes(b"dados")
    junction = tmp_path / "atalho-raiz"
    _winapi.CreateJunction(str(real), str(junction))
    stat = os.stat(junction / "a.xlsx")
    ref = capture_snapshot(
        SourceCandidate(relative_path="a.xlsx", size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns),
        str(junction),
        staging_dir=tmp_path / "staging",
        stability_seconds=0,
        source_path=junction / "a.xlsx",
    )
    assert ref.size_bytes == 5


@pytest.mark.skipif(not WIN32, reason="requires Windows reparse points")
def test_containment_accepts_the_real_file_under_a_junctioned_root(tmp_path: Path):
    import _winapi

    real = tmp_path / "real"
    real.mkdir()
    (real / "b.csv").write_bytes(b"a;b")
    junction = tmp_path / "raiz-link"
    _winapi.CreateJunction(str(real), str(junction))
    assert_snapshot_inside_root(real / "b.csv", str(junction))


@pytest.mark.skipif(not WIN32, reason="requires Windows reparse points")
def test_grant_stores_the_real_root_path(api_client, tmp_path: Path):
    import _winapi

    from sources.repository import SourceRepository

    owner = _session(api_client)
    real = tmp_path / "real"
    real.mkdir()
    junction = tmp_path / "raiz-link"
    _winapi.CreateJunction(str(real), str(junction))
    root_id = _grant(api_client, owner, junction)
    session = get_session_factory()()
    try:
        root = SourceRepository().get_root(session, root_id=root_id, owner_id=owner["id"])
        stored = root.canonical_path
    finally:
        session.close()
    assert os.path.normcase(stored) == os.path.normcase(os.path.realpath(str(junction)))
