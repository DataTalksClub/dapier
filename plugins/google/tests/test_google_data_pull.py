"""Synthetic contract tests for the read-only Google Sheets CSV puller."""
import csv
import importlib.util
import io
import os
import subprocess
from argparse import Namespace
from pathlib import Path
from urllib.parse import unquote

import pytest


_GPULL_PATH = (Path(__file__).resolve().parents[3]
               / ".agents/skills/google-data-pull/gpull.py")
_SPEC = importlib.util.spec_from_file_location("google_data_pull", _GPULL_PATH)
gpull = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gpull)

SHEETS_API = "https://sheets.googleapis.com/v4/spreadsheets/synthetic-sheet"
RENDER_OPTIONS = {
    "valueRenderOption": "UNFORMATTED_VALUE",
    "dateTimeRenderOption": "FORMATTED_STRING",
}
DATAOPS_PR307_SHA = "5de43419c7e1ce890f4e11e083c68a9cf76e9ec6"
DATAOPS_CLI_ENV = "DAPIER_DATAOPS_CLI_DIR"
BOOKKEEPING_HEADERS = [
    "Date sent", "Date paid", "Provider", "What", "Price, $", "Price, EUR",
    "Statement", "Count", "Comment", "Entry Type", "Type", "Period", "Category",
]


def _export_workbook(monkeypatch, output_dir, workbook):
    calls = []

    def fake_request(url, params=None, **_kwargs):
        calls.append((url, params))
        if url == SHEETS_API:
            return {"sheets": [
                {"properties": {"title": title}} for title in workbook
            ]}
        assert params == RENDER_OPTIONS
        a1_range = unquote(url.split("/values/", 1)[1])
        assert a1_range.startswith("'") and a1_range.endswith("'")
        title = a1_range[1:-1].replace("''", "'")
        return workbook[title]

    monkeypatch.setattr(gpull, "request", fake_request)
    monkeypatch.setattr(gpull.sys, "stderr", io.StringIO())
    gpull.cmd_sheet_values(Namespace(
        id="synthetic-sheet", range=None, out_dir=str(output_dir),
    ))
    return calls


def _synthetic_bookkeeping_workbook():
    return {
        "2026": {"values": [
            BOOKKEEPING_HEADERS,
            # Sheets displays this stored fraction as -15,204 in the synthetic case.
            ["2 Sep 2026", "2 Sep 2026", "Example Precision Vendor",
             "Annual license", "", -15203.78, None, 1, "", "Expense", "", "", ""],
        ]},
        "2025 archive": {"values": [
            BOOKKEEPING_HEADERS,
            ["3 Sep 2025", "3 Sep 2025", "Example Archive Vendor",
             "Monthly tool", "", -18.25, None, 1, "", "Expense", "", "", ""],
        ]},
    }


def test_all_tabs_export_raw_numbers_formatted_dates_and_blank_cells(
        tmp_path, monkeypatch):
    calls = _export_workbook(
        monkeypatch, tmp_path, _synthetic_bookkeeping_workbook(),
    )

    files = sorted(tmp_path.glob("*.csv"))
    assert [path.name for path in files] == ["2025_archive.csv", "2026.csv"]
    rows_2026 = list(csv.reader(io.StringIO(
        (tmp_path / "2026.csv").read_text(encoding="utf-8"))))
    assert rows_2026[1] == [
        "2 Sep 2026", "2 Sep 2026", "Example Precision Vendor", "Annual license",
        "", "-15203.78", "", "1", "", "Expense", "", "", "",
    ]
    assert "-15,204" not in (tmp_path / "2026.csv").read_text(encoding="utf-8")
    assert list(csv.reader(io.StringIO(
        (tmp_path / "2025_archive.csv").read_text(encoding="utf-8"))))[1][5] == "-18.25"
    value_calls = [params for url, params in calls if "/values/" in url]
    assert value_calls == [RENDER_OPTIONS, RENDER_OPTIONS]


def test_sheet_csv_text_serializes_boolean_cells_as_lowercase():
    assert gpull._sheet_cell_text(True) == "true"
    assert gpull._sheet_cell_text(False) == "false"


def test_sanitized_tab_filename_collisions_fail_before_any_csv_is_written(
        tmp_path, monkeypatch):
    calls = []
    workbook = {
        "Tax+Archive": {"values": [["Provider"], ["Example A"]]},
        "Tax_Archive": {"values": [["Provider"], ["Example B"]]},
    }

    def fake_request(url, params=None, **_kwargs):
        calls.append((url, params))
        if url == SHEETS_API:
            return {"sheets": [
                {"properties": {"title": title}} for title in workbook
            ]}
        raise AssertionError("cell values must not be fetched after a filename collision")

    monkeypatch.setattr(gpull, "request", fake_request)
    with pytest.raises(gpull.ApiError, match="collide after CSV filename sanitization") as exc:
        gpull.cmd_sheet_values(Namespace(
            id="synthetic-sheet", range=None, out_dir=str(tmp_path),
        ))

    assert "'Tax+Archive'" in str(exc.value)
    assert "'Tax_Archive'" in str(exc.value)
    assert "no CSV files were written" in str(exc.value)
    assert not list(tmp_path.iterdir())
    assert all("/values/" not in url for url, _params in calls)


def test_range_stdout_shape_remains_compatible(monkeypatch):
    stdout = io.StringIO()
    monkeypatch.setattr(gpull.sys, "stdout", stdout)
    monkeypatch.setattr(gpull.sys, "stderr", io.StringIO())
    monkeypatch.setattr(
        gpull, "request",
        lambda _url, params=None, **_kwargs: (
            {"values": [["Date", "Amount"], ["2 Sep 2026", 15203.78]]}
            if params == RENDER_OPTIONS else None
        ),
    )

    gpull.cmd_sheet_values(Namespace(
        id="synthetic-sheet", range="Sheet1!A1:B2", out_dir=None,
    ))

    assert stdout.getvalue() == (
        "# Sheet1!A1:B2\n"
        "Date,Amount\r\n"
        "2 Sep 2026,15203.78\r\n"
    )


@pytest.mark.skipif(not os.environ.get(DATAOPS_CLI_ENV),
                    reason="set DAPIER_DATAOPS_CLI_DIR via the pinned cross-project harness")
def test_dataops_pr307_dry_run_uses_actual_synthetic_export(tmp_path, monkeypatch):
    """Feed mocked gpull exports through DataOps PR 307's real CLI dry-run."""
    dataops_cli = Path(os.environ[DATAOPS_CLI_ENV]).resolve()
    actual_sha = subprocess.check_output(
        ["git", "-C", str(dataops_cli), "rev-parse", "HEAD"], text=True,
    ).strip()
    assert actual_sha == DATAOPS_PR307_SHA, (
        f"expected DataOps PR 307 at {DATAOPS_PR307_SHA}, got {actual_sha}"
    )

    csv_dir = tmp_path / "csv"
    csv_dir.mkdir()
    _export_workbook(monkeypatch, csv_dir, _synthetic_bookkeeping_workbook())
    node_test = tmp_path / "dataops-dry-run-contract.mjs"
    node_test.write_text(r'''
import assert from 'node:assert/strict';
import os from 'node:os';
import { syncBuiltinESMExports } from 'node:module';
import { pathToFileURL } from 'node:url';
os.tmpdir = () => process.env.DAPIER_TEST_TMP;
syncBuiltinESMExports();
const cli = process.env.DAPIER_DATAOPS_CLI_DIR;
const helpers = await import(pathToFileURL(`${cli}/test/helpers.mjs`));
helpers.isolateConfig();
const sheet = await import(pathToFileURL(`${cli}/src/bookkeepingSheet.mjs`));
const { run } = await import(pathToFileURL(`${cli}/src/cli.mjs`));
const rows = sheet.readSheetDir(process.env.DAPIER_SYNTHETIC_CSV_DIR);
assert.equal(rows.length, 2, 'both output tabs reach the filename-based importer');
const precise = rows.find((row) => row.record?.counterparty === 'Example Precision Vendor');
const archive = rows.find((row) => row.record?.counterparty === 'Example Archive Vendor');
assert.equal(precise.record.transactionDate, '2026-09-02');
assert.equal(precise.record.paidDate, '2026-09-02');
assert.equal(precise.record.amount, '15203.78');
assert.equal(precise.record.entryType, 'expense');
assert.equal(precise.record.category, undefined, 'blank classification cells stay absent');
assert.equal(archive.record.transactionDate, '2025-09-03');
assert.equal(archive.record.amount, '18.25');
const existing = rows.map((row, index) => ({
  id: `synthetic-ledger-${index}`,
  sourceType: 'spreadsheet',
  sourceKey: row.sourceKey,
  ...row.record,
  category: 'DataOps classification',
  subtype: 'DataOps subtype',
  period: 'Monthly',
}));
const calls = helpers.fakePortal({
  'GET /api/bookkeeping/transactions': { items: existing },
});
const io = helpers.captureIo();
assert.equal(await run([
  'bookkeeping', 'sync', process.env.DAPIER_SYNTHETIC_CSV_DIR, '--json',
], io), 0);
const result = io.json();
assert.equal(result.mode, 'dry-run');
assert.deepEqual(result.counts, {
  sheetRows: 2, create: 0, update: 0, locked: 0, unchanged: 2, orphans: 0, skipped: 0,
});
assert.deepEqual(calls.map((call) => call.method), ['GET']);
console.log('DataOps PR 307 dry-run preserved both synthetic tabs; no writes');
''', encoding="utf-8")

    environment = dict(os.environ)
    environment.update({
        "DAPIER_DATAOPS_CLI_DIR": str(dataops_cli),
        "DAPIER_SYNTHETIC_CSV_DIR": str(csv_dir),
        "DAPIER_TEST_TMP": str(tmp_path),
    })
    completed = subprocess.run(
        ["node", str(node_test)], cwd=dataops_cli, env=environment,
        check=True, capture_output=True, text=True,
    )
    assert "DataOps PR 307 dry-run preserved both synthetic tabs; no writes" in completed.stdout
