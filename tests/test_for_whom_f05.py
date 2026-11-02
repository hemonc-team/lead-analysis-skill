"""Тесты F05 — «Для кого ищете помощь» (UF_CRM_1793611825).

Поле пишется только из разговора (LLM crm_updates), только в пустое, только 2656/2658.
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import deliver_report as dr  # noqa: E402
import fetch  # noqa: E402
from rules_core import (  # noqa: E402
    CRM_WRITE_FIELDS,
    FOR_WHOM_RELATIVE,
    FOR_WHOM_SELF,
    UF_FOR_WHOM,
    UF_RECOMMEND,
    build_auto_crm_updates,
    fields_snapshot,
    for_whom_enum,
    missing_crm_fields,
)


class TestForWhomEnum(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(UF_FOR_WHOM, "UF_CRM_1793611825")
        self.assertEqual(for_whom_enum("2656"), FOR_WHOM_SELF)
        self.assertEqual(for_whom_enum(2658), FOR_WHOM_RELATIVE)

    def test_labels(self):
        self.assertEqual(for_whom_enum("Для себя"), "2656")
        self.assertEqual(for_whom_enum(" для близкого "), "2658")

    def test_not_selected_and_garbage_rejected(self):
        for val in (None, "", False, "Не выбрано", "для мамы", "1234", ["2656", "2658"]):
            self.assertIsNone(for_whom_enum(val), val)


class TestForWhomPipeline(unittest.TestCase):
    def test_registered_in_fetch_and_write_fields(self):
        self.assertIn(UF_FOR_WHOM, fetch.ENUM_FIELDS)
        self.assertIn(UF_FOR_WHOM, fetch.SELECT_FIELDS)
        self.assertIn(UF_FOR_WHOM, CRM_WRITE_FIELDS)
        self.assertIn(UF_FOR_WHOM, dr.NO_OVERWRITE)

    def test_not_derived_from_source_id(self):
        for sid in ("18", "13", "14", "CALL", "WEBFORM", "REPEAT_SALE"):
            lead = {"fields": {"SOURCE_ID": sid}}
            self.assertNotIn(UF_FOR_WHOM, build_auto_crm_updates(lead))

    def test_not_in_missing_fields(self):
        lead = {"fields": {}, "status_id": "NEW"}
        self.assertNotIn("для_кого", missing_crm_fields(lead))

    def test_snapshot_label(self):
        catalogs = {"enums": {UF_FOR_WHOM: {"2656": "Для себя", "2658": "Для близкого"}}}
        snap = fields_snapshot({"fields": {UF_FOR_WHOM: "2658"}}, catalogs)
        self.assertEqual(snap["для_кого"], "Для близкого")
        self.assertEqual(fields_snapshot({"fields": {}}, catalogs)["для_кого"], "")


class TestForWhomDeliver(unittest.TestCase):
    def test_writes_only_empty(self):
        proposed = {UF_FOR_WHOM: "2658"}
        self.assertEqual(dr.filter_writable({UF_FOR_WHOM: False}, proposed), {UF_FOR_WHOM: "2658"})
        self.assertEqual(dr.filter_writable({UF_FOR_WHOM: ""}, proposed), {UF_FOR_WHOM: "2658"})
        # форма сайта уже заполнила — не перезаписываем
        self.assertEqual(dr.filter_writable({UF_FOR_WHOM: "2656"}, proposed), {})

    def test_label_normalized_and_invalid_dropped(self):
        self.assertEqual(
            dr.filter_writable({}, {UF_FOR_WHOM: "Для себя"}), {UF_FOR_WHOM: "2656"}
        )
        self.assertEqual(dr.filter_writable({}, {UF_FOR_WHOM: "Не выбрано"}), {})

    def test_collect_pending_drops_invalid_only(self):
        judgements = [
            {"id": 1, "crm_updates": {UF_FOR_WHOM: "Для близкого"}},
            {"id": 2, "crm_updates": {UF_FOR_WHOM: "сомнение"}},
            {"id": 3, "crm_updates": {UF_FOR_WHOM: "мусор", UF_RECOMMEND: "474"}},
        ]
        pending = dr.collect_pending_updates([], judgements, {})
        self.assertEqual(pending, {"1": {UF_FOR_WHOM: "2658"}, "3": {UF_RECOMMEND: "474"}})

    def test_apply_stats(self):
        leads = {
            "1": {UF_FOR_WHOM: False},  # пусто → добавим
            "2": {UF_FOR_WHOM: "2656"},  # уже заполнено формой → не трогаем
        }

        def fake_bx(method, params):
            self.assertEqual(method, "crm.lead.get.json")
            return {"result": leads[str(params["id"])]}

        pending = {"1": {UF_FOR_WHOM: "2658"}, "2": {UF_FOR_WHOM: "2658"}}
        with mock.patch.object(dr, "bx", side_effect=fake_bx):
            stats = dr.apply_crm_updates(pending, dry_run=True)
        self.assertEqual(stats["for_whom_before"], 1)
        self.assertEqual(stats["for_whom_added"], 1)
        self.assertEqual(stats["for_whom_after"], 2)
        self.assertEqual(stats["would_update"], [{"id": "1", "fields": {UF_FOR_WHOM: "2658"}}])

    def test_report_line(self):
        triage = {"counts": {"unique": 10, "A": 3, "B": 4, "C": 3}, "report_date": "2026-11-01"}
        stats = {"for_whom_before": 2, "for_whom_after": 5, "for_whom_added": 3}
        report = dr.build_report(triage, [], [], stats, users={})
        self.assertIn("• Для кого ищут помощь: было 2 из 10, стало 5 — добавил 3", report)

    def test_csv_label(self):
        self.assertEqual(dr.for_whom_csv_label({UF_FOR_WHOM: "2656"}, {}), "Для себя")
        self.assertEqual(dr.for_whom_csv_label({}, {"для_кого": "Для близкого"}), "Для близкого")
        self.assertEqual(dr.for_whom_csv_label({}, {}), "")

    def test_csv_header_migration(self):
        header = "дата,лид_id,согласие_с_оценкой,для_кого_AI"
        old = "дата,лид_id,согласие_с_оценкой\r\n2026-10-01,1,\n"
        self.assertEqual(
            dr.migrate_csv_header(old, header),
            "дата,лид_id,согласие_с_оценкой,для_кого_AI\n2026-10-01,1,\n",
        )
        current = header + "\n2026-11-01,2,,Для себя\n"
        self.assertEqual(dr.migrate_csv_header(current, header), current)


if __name__ == "__main__":
    unittest.main()
