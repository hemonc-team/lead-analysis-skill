"""Имена ответственных в отчёте: id→ФИО даже если judgements без operator_id."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from deliver_report import (  # noqa: E402
    build_report,
    enrich_operator_ids,
    load_operator_by_id,
    operator_display_name,
)


class TestAssigneeNames(unittest.TestCase):
    def test_operator_display_name_from_catalog(self):
        users = {"42": "Иванова Мария"}
        self.assertEqual(operator_display_name(42, users), "Иванова Мария")
        self.assertEqual(operator_display_name("42", users), "Иванова Мария")
        self.assertEqual(operator_display_name(None, users), "оператор")
        self.assertEqual(operator_display_name("99", users), "оператор 99")

    def test_enrich_from_triage_and_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "auto_results.jsonl").write_text("", encoding="utf-8")
            review = run_dir / "review"
            review.mkdir()
            (review / "review_01.json").write_text(
                json.dumps({
                    "leads": [
                        {"id": 1001, "operator_id": 55},
                        {"id": 1002, "operator_id": "66"},
                    ]
                }),
                encoding="utf-8",
            )
            triage = {"operator_by_id": {"1003": "77"}}
            ops = load_operator_by_id(run_dir, triage)
            self.assertEqual(ops["1001"], "55")
            self.assertEqual(ops["1002"], "66")
            self.assertEqual(ops["1003"], "77")

            judgements = [
                {"id": 1001, "in_action_list": True},
                {"id": 1002, "operator_id": None, "in_action_list": True},
            ]
            enrich_operator_ids(judgements, ops)
            self.assertEqual(judgements[0]["operator_id"], "55")
            self.assertEqual(judgements[1]["operator_id"], "66")

    def test_build_report_shows_assignee_name_without_judgement_operator_id(self):
        users = {"55": "Петрова Анна Сергеевна"}
        triage = {
            "report_date": "2026-09-22",
            "counts": {"unique": 1, "A": 0, "B": 1, "C": 0},
            "_catalogs_path": "",
        }
        judgements = [
            {
                "id": 1001,
                "emoji": "⚠️",
                "classification": "action",
                "in_action_list": True,
                "patient_wanted": "записаться",
                "what_happened": "не перезвонили",
                "why_important": "потеря",
                "action": "связаться",
            }
        ]
        enrich_operator_ids(judgements, {"1001": "55"})
        text = build_report(triage, [], judgements, {}, users=users)
        self.assertIn("Петрова Анна Сергеевна", text)
        self.assertNotIn("— оператор\n", text)
        self.assertIn("#1001[/URL] — Петрова Анна Сергеевна", text)


if __name__ == "__main__":
    unittest.main()
