"""T3 wiring fix #2: declared suffix for CSV path probes + poll sleeps."""
import io

# ---------- registry.probe_input_path signature ----------
p = 'ingestion/registry.py'
with io.open(p, encoding='utf-8') as f:
    c = f.read()

old = '''def probe_input_path(path) -> InputProbe:
    """Path-based probe for stored files (worker dispatch, upload completion).

    Uses the real file so ZIP central-directory inspection works — the first
    1 KiB of an XLSX does not carry the OOXML markers.
    """
    path = Path(path)
    with open(path, "rb") as handle:
        head = handle.read(1024)'''
new = '''def probe_input_path(path, filename: str | None = None) -> InputProbe:
    """Path-based probe for stored files (worker dispatch, upload completion).

    Uses the real file so ZIP central-directory inspection works — the first
    1 KiB of an XLSX does not carry the OOXML markers. CSV has no magic bytes,
    so the declared filename decides the CSV attempt; staging names like
    ``.part``/``.source`` never carry the real extension.
    """
    path = Path(path)
    declared_name = filename or path.name
    with open(path, "rb") as handle:
        head = handle.read(1024)'''
assert old in c, 'probe_input_path head not found'
c = c.replace(old, new)

old_suffix = '''    if path.suffix.lower() == ".csv":
        from ingestion.spreadsheet_input import IngestionError, probe_spreadsheet'''
new_suffix = '''    declared_suffix = declared_name.rsplit(".", 1)[-1].lower() if "." in declared_name else ""
    if declared_suffix == "csv":
        from ingestion.spreadsheet_input import IngestionError, probe_spreadsheet'''
assert old_suffix in c, 'csv suffix check not found'
c = c.replace(old_suffix, new_suffix)

with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
    f.write(c)
print('registry ok')

# ---------- worker: pass filename ----------
p = 'worker/tasks.py'
with io.open(p, encoding='utf-8') as f:
    c = f.read()
old = '        probe = probe_input_path(pdf_path)'
assert old in c, 'worker probe call not found'
c = c.replace(old, '        probe = probe_input_path(pdf_path, filename=filename)')
with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
    f.write(c)
print('worker ok')

# ---------- upload_service: pass item filename ----------
p = 'api/upload_service.py'
with io.open(p, encoding='utf-8') as f:
    c = f.read()
old = '            probe = probe_input_path(storage.local_path(staged_key))'
assert old in c, 'upload probe call not found'
c = c.replace(old, '            probe = probe_input_path(storage.local_path(staged_key), filename=item.filename)')
with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
    f.write(c)
print('upload ok')

# ---------- api tests: poll sleeps ----------
p = 'tests/test_spreadsheet_api.py'
with io.open(p, encoding='utf-8') as f:
    c = f.read()
c = c.replace('''    status = None
    for _ in range(50):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        status = detail.get("status")
        if status in {"completed", "completed_with_warnings", "failed"}:
            break
    assert status in {"completed", "completed_with_warnings"}, detail''',
'''    import time

    status = None
    detail = {}
    for _ in range(100):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        status = detail.get("status")
        if status in {"completed", "completed_with_warnings", "failed"}:
            break
        time.sleep(0.1)
    assert status in {"completed", "completed_with_warnings"}, detail''')
c = c.replace('''    detail = None
    for _ in range(50):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        if detail.get("status") in {"completed", "completed_with_warnings", "failed"}:
            break
    assert detail.get("status") in {"completed", "completed_with_warnings"}, detail''',
'''    import time

    detail = {}
    for _ in range(100):
        detail = api_client.get(f"/api/v1/conversions/{conversion_id}", headers=headers).json()
        if detail.get("status") in {"completed", "completed_with_warnings", "failed"}:
            break
        time.sleep(0.1)
    assert detail.get("status") in {"completed", "completed_with_warnings"}, detail''')
with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
    f.write(c)
print('tests ok')
