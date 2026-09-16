"""Тесты F04 — детерминированное «Кто рекомендовал» из SOURCE_ID."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rules_core import (
    UF_RECOMMEND,
    UF_RECOMMEND_COMMENT,
    build_auto_crm_updates,
    recommend_enum_from_source,
    suggest_recommend_update,
)


class TestRecommendF04(unittest.TestCase):
    def test_source_telegram_maps_to_social(self):
        self.assertEqual(recommend_enum_from_source("18"), "484")

    def test_source_call_not_mapped(self):
        self.assertIsNone(recommend_enum_from_source("CALL"))
        self.assertIsNone(recommend_enum_from_source("WEBFORM"))

    def test_suggest_fills_empty_recommend(self):
        lead = {"fields": {"SOURCE_ID": "18", UF_RECOMMEND: False}}
        upd = suggest_recommend_update(lead)
        self.assertEqual(upd[UF_RECOMMEND], "484")
        self.assertEqual(upd[UF_RECOMMEND_COMMENT], "Telegram")

    def test_suggest_skips_if_already_filled(self):
        lead = {"fields": {"SOURCE_ID": "18", UF_RECOMMEND: "484"}}
        self.assertEqual(suggest_recommend_update(lead), {})

    def test_whatsapp_assistant_maps_to_assistance(self):
        lead = {"fields": {"SOURCE_ID": "14"}}
        self.assertEqual(build_auto_crm_updates(lead)[UF_RECOMMEND], "476")

    def test_wz_telegram_prefix(self):
        self.assertEqual(recommend_enum_from_source("WZaf84f452-abc"), "484")


if __name__ == "__main__":
    unittest.main()
