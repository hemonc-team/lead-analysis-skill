"""Тесты R08 — сервисные номера агрегаторов (ПроДокторов)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rules_core import check_r02, is_service_contact, structural_checks


def test_prodoctorov_contact_detected():
    svc, label = is_service_contact(
        name="ПроДокторов",
        second_name="НЕ ПЕРЕИМЕНОВЫВАТЬ, НОМЕР НЕ ПАЦ",
        last_name="Сервис",
    )
    assert svc is True
    assert label == "ПроДокторов"


def test_regular_contact_not_service():
    svc, _ = is_service_contact(name="Иванова", last_name="Лариса")
    assert svc is False


def test_r02_skipped_for_service_contact():
    lead = {
        "groups": ["C"],
        "status_id": "18",
        "phone": [{"VALUE": "+74951048339"}],
        "contact_meta": {
            "is_service": True,
            "label": "ПроДокторов",
            "name": "ПроДокторов",
            "second_name": "НЕ ПЕРЕИМЕНОВЫВАТЬ, НОМЕР НЕ ПАЦ",
            "last_name": "Сервис",
        },
        "calls": [],
        "chats": [],
        "fields": {},
    }
    r02 = check_r02(lead, {"users": {}})
    assert r02["applies"] is False
    assert "ПроДокторов" in r02["detail"]


def test_structural_r08_human():
    lead = {
        "contact_meta": {
            "is_service": True,
            "label": "ПроДокторов",
            "name": "ПроДокторов",
            "second_name": "НЕ ПЕРЕИМЕНОВЫВАТЬ, НОМЕР НЕ ПАЦ",
            "last_name": "Сервис",
        },
        "groups": ["C"],
        "status_id": "18",
        "phone": [{"VALUE": "+74951048339"}],
        "calls": [],
        "chats": [],
        "fields": {},
        "uf_gap": False,
    }
    st = structural_checks(lead, {"users": {}, "enums": {}, "statuses": {}})
    assert st["service_contact"]["is_service"] is True
    assert st["r08_human"]
    assert "перезвон" in st["r08_human"]
