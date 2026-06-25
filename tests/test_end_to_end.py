"""End-to-end smoke test: drive the real CLI (skill.run.main) over the committed
synthetic example dataset and confirm it ingests, runs the battery, and writes a
workbook with the expected findings — a repeatable proof the tool is functional."""
import sys

from openpyxl import load_workbook

from core.entities import REPO_ROOT
from skill.run import main


def test_cli_runs_on_example_dataset(tmp_path, monkeypatch):
    out = tmp_path / "example.xlsx"
    monkeypatch.setattr(sys, "argv", [
        "skill.run", "--data-dir", str(REPO_ROOT / "examples" / "sample-data"),
        "--entity", "hines-homes", "--tier3", "off", "--store", "none",
        "--output", str(out)])

    main()   # the actual weekly-run entry point, offline (no AI, no Supabase)

    assert out.exists()
    wb = load_workbook(out)
    assert wb.sheetnames == ["Summary", "CRITICAL", "HIGH", "MEDIUM", "INFO",
                             "Methodology", "Run Info"]
    # The sample exports raise these four findings (see examples/README.md).
    found = set()
    for sheet in ("CRITICAL", "HIGH", "MEDIUM", "INFO"):
        found |= {row[0].value for row in wb[sheet].iter_rows(min_row=2) if row[0].value}
    assert {"T1-04", "T1-10", "T1-11", "T1-30"} <= found
    # Methodology proves Tier 1, 2, and 4 rules are all registered (honest coverage).
    methodology = {row[0].value for row in wb["Methodology"].iter_rows(min_row=2)}
    assert {"T1-30", "T2-02", "T2-10", "T4-02"} <= methodology
