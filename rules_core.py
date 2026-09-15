"""Детерминированные правила lead-analysis (R02, R06, поля CRM, компакт).

Используется triage.py. Агент не должен пересчитывать эти флаги — только читать
из triage.json / review-пакетов.
"""
from __future__ import annotations

import re
from typing import Any

CLOSED_STATUSES = frozenset({"CONVERTED", "JUNK", "4", "7", "20"})
MIN_CALL_DURATION = 15

# Сервисные контакты-агрегаторы (ПроДокторов и аналоги): один номер — разные пациенты.
SERVICE_CONTACT_MARKERS = (
    "продоктор",
    "номер не пац",
    "не пациент",
    "сервис про",
)

UF_NEXT = "UF_CRM_1599826765930"
UF_TRANSCRIPT = "UF_CRM_GPT_TRANSCRIPT"
UF_TEGI = "UF_CRM_1706806189"
UF_VARIACIA = "UF_CRM_1768816592560"
UF_DRUG = "UF_CRM_1748956354216"
UF_STATUS_DET = "UF_CRM_1747754997445"
UF_TASK = "UF_CRM_1747750972275"
UF_LINK_1C = "UF_CRM_1732558654060"
UF_WAITLIST = "UF_CRM_1761919557442"

CRM_WRITE_FIELDS = (UF_TEGI, UF_VARIACIA, UF_DRUG, UF_STATUS_DET, UF_TASK)

BOOKING_1C = "e1cib/data/Документ.ОказаниеУслуг"
WAITLIST_1C = "e1cib/data/РегистрСведений.ЛистОжидания"

TRANSCRIPT_MAX = 5500
CHAT_MAX = 3500
CALLS_COMPACT_MAX = 1200


def uf_empty(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, list):
        return len(val) == 0
    text = str(val).strip()
    return text in ("", "0", "None", "[]", "false", "False", "0000-00-00", "0000-00-00T00:00:00")


def enum_label(enums: dict, field: str, raw: Any) -> str:
    if uf_empty(raw):
        return ""
    key = str(raw[0] if isinstance(raw, list) and raw else raw)
    return (enums.get(field) or {}).get(key, key)


def status_name(catalogs: dict, status_id: str | int | None) -> str:
    return (catalogs.get("statuses") or {}).get(str(status_id or ""), "")


def contact_label(name: str | None, second_name: str | None, last_name: str | None) -> str:
    parts = [p.strip() for p in (name, second_name, last_name) if p and str(p).strip()]
    return " ".join(parts)


def is_service_contact(
    name: str | None = None,
    second_name: str | None = None,
    last_name: str | None = None,
    contact_meta: dict | None = None,
) -> tuple[bool, str]:
    """Контакт-агрегатор (ПроДокторов): перезвон на номер лида бессмысленен."""
    if contact_meta:
        if contact_meta.get("is_service"):
            return True, str(contact_meta.get("label") or "сервисный номер")
        name = contact_meta.get("name")
        second_name = contact_meta.get("second_name")
        last_name = contact_meta.get("last_name")
    blob = contact_label(name, second_name, last_name).lower().replace("ё", "е")
    if not blob:
        return False, ""
    for marker in SERVICE_CONTACT_MARKERS:
        if marker in blob:
            if "продоктор" in blob:
                return True, "ПроДокторов"
            return True, "сервисный номер"
    return False, ""


def phone_is_russian(phones: Any) -> bool:
    for item in phones or []:
        raw = item.get("VALUE") if isinstance(item, dict) else str(item)
        raw = (raw or "").strip()
        digits = re.sub(r"\D", "", raw)
        if raw.startswith("+7") or raw.startswith("8") or (len(digits) >= 11 and digits.startswith("7")):
            return True
    return False


def long_success_calls(calls: list[dict]) -> list[dict]:
    out = []
    for c in calls:
        if int(c.get("duration") or 0) < MIN_CALL_DURATION:
            continue
        if str(c.get("failed_code") or "") != "200":
            continue
        out.append(c)
    return out


def count_outgoing_touches(lead: dict, catalogs: dict) -> dict[str, int]:
    """R02: исходящие касания (звонки type=2 + исходящие сообщения в мессенджерах)."""
    users = {str(k) for k in (catalogs.get("users") or {})}
    assigned = str(lead.get("assigned_by_id") or lead.get("fields", {}).get("ASSIGNED_BY_ID") or "")

    out_calls = sum(1 for c in lead.get("calls") or [] if str(c.get("type")) == "2")

    tg_max_msgs = 0
    other_msgs = 0
    for ch in lead.get("chats") or []:
        conn = (ch.get("connector") or "").lower()
        is_tg_max = any(x in conn for x in ("telegram", "max", "tg", "viber"))
        for m in ch.get("messages") or []:
            author = str(m.get("author_id") or "")
            # исходящее от сотрудника: author в справочнике users или совпадает с ответственным
            if author in users or (assigned and author == assigned):
                if is_tg_max:
                    tg_max_msgs += 1
                else:
                    other_msgs += 1

    total = out_calls + tg_max_msgs + other_msgs
    return {
        "out_calls": out_calls,
        "tg_max_msgs": tg_max_msgs,
        "other_msgs": other_msgs,
        "total": total,
    }


def check_r02(lead: dict, catalogs: dict) -> dict[str, Any]:
    """R02 только для открытых лидов с рос. номером и активным запросом (группы B/C)."""
    groups = lead.get("groups") or []
    status_id = str(lead.get("status_id") or lead.get("fields", {}).get("STATUS_ID") or "")
    if status_id in CLOSED_STATUSES:
        return {"applies": False, "ok": True, "detail": "закрыт — R02 не применяется"}

    svc, svc_label = is_service_contact(contact_meta=lead.get("contact_meta"))
    if svc:
        return {
            "applies": False,
            "ok": True,
            "detail": f"сервисный номер ({svc_label}) — R02 не применяется",
        }

    active_request = "B" in groups or "C" in groups
    if not active_request:
        return {"applies": False, "ok": True, "detail": "группа A без открытого follow-up — R02 не применяется"}

    if not phone_is_russian(lead.get("phone")):
        return {"applies": False, "ok": True, "detail": "не рос. номер — R02 не применяется"}

    t = count_outgoing_touches(lead, catalogs)
    ok = t["total"] >= 3 and t["out_calls"] >= 1 and t["tg_max_msgs"] >= 1
    return {
        "applies": True,
        "ok": ok,
        "touches": t,
        "detail": (
            f"касаний {t['total']}/3 (звонки {t['out_calls']}, TG/Max {t['tg_max_msgs']})"
            if not ok
            else "достаточно касаний"
        ),
        "flag_c": not ok,
    }


def check_r06(lead: dict, catalogs: dict) -> dict[str, Any]:
    fields = lead.get("fields") or {}
    status_id = str(lead.get("status_id") or fields.get("STATUS_ID") or "")
    st = status_name(catalogs, status_id).lower().replace("ё", "е")
    flags: list[str] = []
    notes: list[str] = []

    link = str(fields.get(UF_LINK_1C) or "")
    wait = str(fields.get(UF_WAITLIST) or "")

    if "запись" in st and "прием" in st:
        if BOOKING_1C not in link:
            flags.append("r06_booking")
            notes.append('стадия «Запись на приём», ссылки на запись в 1С нет')

    if "лист ожидания" in st:
        if WAITLIST_1C not in wait:
            flags.append("r06_waitlist")
            notes.append('стадия «Лист ожидания», ссылки в 1С нет')

    return {"flags": flags, "notes": notes, "flag_e": bool(flags)}


def check_f03_risk(lead: dict, catalogs: dict) -> bool:
    fields = lead.get("fields") or {}
    enums = catalogs.get("enums") or {}
    det = enum_label(enums, UF_STATUS_DET, fields.get(UF_STATUS_DET))
    return det.strip().lower() == "отказ пациента"


def missing_crm_fields(lead: dict) -> list[str]:
    fields = lead.get("fields") or {}
    missing = []
    if uf_empty(fields.get(UF_VARIACIA)):
        missing.append("вариация_приема")
    if uf_empty(fields.get(UF_STATUS_DET)):
        missing.append("статус_детальный")
    if uf_empty(fields.get(UF_TASK)) and str(lead.get("status_id") or "") not in CLOSED_STATUSES:
        missing.append("задача")
    return missing


def has_booking_1c(lead: dict) -> bool:
    return BOOKING_1C in str((lead.get("fields") or {}).get(UF_LINK_1C) or "")


def is_informational_status(lead: dict, catalogs: dict) -> bool:
    st = status_name(catalogs, lead.get("status_id")).lower()
    return "информацион" in st or st.strip() == "информационное сообщение"


def truncate_text(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.72)
    tail = limit - head - 20
    return f"{text[:head]}\n…[обрезано]…\n{text[-tail:]}"


def compact_calls(calls: list[dict]) -> str:
    parts = []
    for c in calls:
        kind = "вх" if str(c.get("type")) == "1" else "исх"
        date = (c.get("date") or "?")[:16]
        dur = int(c.get("duration") or 0)
        fc = c.get("failed_code")
        parts.append(f"{date} {kind} {dur}с fc={fc}")
    s = "; ".join(parts)
    return truncate_text(s, CALLS_COMPACT_MAX)


def compact_chats(chats: list[dict]) -> str:
    lines: list[str] = []
    for ch in chats:
        conn = ch.get("connector") or "чат"
        for m in ch.get("messages") or []:
            date = (m.get("date") or "")[:16]
            text = re.sub(r"\s+", " ", (m.get("text") or "")).strip()
            if text:
                lines.append(f"{date} [{conn}] {text[:400]}")
    return truncate_text("\n".join(lines), CHAT_MAX)


def compact_timeline_note(lead: dict) -> str:
    tl = lead.get("timeline") or {}
    tc = len(tl.get("calls") or [])
    tch = len(tl.get("chats") or [])
    if not tc and not tch:
        return ""
    missed = sum(1 for c in tl.get("calls") or [] if c.get("missed"))
    return f"таймлайн: звонков {tc} (пропущ. {missed}), чат-сессий {tch}"


def fields_snapshot(lead: dict, catalogs: dict) -> dict[str, str]:
    fields = lead.get("fields") or {}
    enums = catalogs.get("enums") or {}
    return {
        "стадия": status_name(catalogs, lead.get("status_id")),
        "теги_кц": enum_label(enums, UF_TEGI, fields.get(UF_TEGI)),
        "вариация": enum_label(enums, UF_VARIACIA, fields.get(UF_VARIACIA)),
        "препарат": enum_label(enums, UF_DRUG, fields.get(UF_DRUG)),
        "статус_детальный": enum_label(enums, UF_STATUS_DET, fields.get(UF_STATUS_DET)),
        "задача": str(fields.get(UF_TASK) or "").strip(),
        "след_контакт": str(fields.get(UF_NEXT) or "").strip()[:16],
        "запись_1с": "да" if has_booking_1c(lead) else "нет",
    }


def compact_lead_for_review(lead: dict, catalogs: dict, structural: dict) -> dict:
    """Минимальный пакет для LLM — без дублирования сырого JSON."""
    fields = lead.get("fields") or {}
    transcript = lead.get("transcript") or fields.get(UF_TRANSCRIPT) or ""
    contact_meta = lead.get("contact_meta") or {}
    svc = structural.get("service_contact") or {}
    return {
        "id": lead["id"],
        "title": (lead.get("title") or "")[:120],
        "groups": lead.get("groups") or [],
        "operator_id": lead.get("assigned_by_id"),
        "is_return_customer": lead.get("is_return_customer") == "Y",
        "contact": {
            "is_service": bool(svc.get("is_service")),
            "label": svc.get("label") or None,
            "warning": svc.get("human"),
        } if svc.get("is_service") else None,
        "structural": structural,
        "fields": fields_snapshot(lead, catalogs),
        "exist_check": {
            "has_communication": (lead.get("exist_check") or {}).get("has_communication"),
            "only_timeline": (
                not lead.get("calls")
                and not lead.get("chats")
                and bool((lead.get("timeline") or {}).get("calls") or (lead.get("timeline") or {}).get("chats"))
            ),
        },
        "transcript": truncate_text(str(transcript), TRANSCRIPT_MAX) if transcript else None,
        "transcript_source": lead.get("transcript_source"),
        "calls": compact_calls(lead.get("calls") or []),
        "chats": compact_chats(lead.get("chats") or []),
        "timeline_note": compact_timeline_note(lead),
    }


def decide_auto_tier(lead: dict, catalogs: dict, structural: dict) -> str:
    """auto_ok | auto_info | auto_gap | needs_llm — консервативно, лучше лишний LLM чем пропуск потери."""
    status_id = str(lead.get("status_id") or "")
    groups = lead.get("groups") or []
    uf_gap = bool(lead.get("uf_gap"))
    has_tr = bool(lead.get("transcript"))
    st_flags = structural.get("r06", {}).get("flags") or []
    r02 = structural.get("r02", {})
    f03 = structural.get("f03_risk")

    if st_flags or f03 or (r02.get("applies") and not r02.get("ok")):
        return "needs_llm"

    if is_informational_status(lead, catalogs) and not uf_gap:
        return "auto_info"

    if uf_gap and status_id in CLOSED_STATUSES and not structural.get("missing_fields"):
        return "auto_gap"

    if "A" in groups and status_id == "CONVERTED" and has_booking_1c(lead) and not structural.get("missing_fields"):
        if has_tr or (lead.get("exist_check") or {}).get("has_communication"):
            return "auto_ok"

    if "A" in groups and status_id in CLOSED_STATUSES:
        if status_id == "JUNK" and not has_tr and not long_success_calls(lead.get("calls") or []):
            return "auto_ok"
        if status_id in ("4", "7", "20") and not uf_gap:
            return "auto_ok"

    return "needs_llm"


def auto_classification(tier: str, lead: dict, structural: dict) -> str:
    if tier == "auto_ok":
        return "ok"
    if tier == "auto_info":
        return "info"
    if tier == "auto_gap":
        return "gap_only"
    return "needs_llm"


def service_contact_check(lead: dict) -> dict[str, Any]:
    svc, label = is_service_contact(contact_meta=lead.get("contact_meta"))
    if not svc:
        return {"is_service": False}
    return {
        "is_service": True,
        "label": label,
        "human": (
            f"номер агрегатора {label}: перезвон на номер лида невозможен, "
            "с одного номера звонят разные пациенты — не использовать историю контакта"
        ),
    }


def structural_checks(lead: dict, catalogs: dict) -> dict[str, Any]:
    r02 = check_r02(lead, catalogs)
    r06 = check_r06(lead, catalogs)
    missing = missing_crm_fields(lead)
    svc = service_contact_check(lead)
    return {
        "r02": r02,
        "r06": r06,
        "service_contact": svc,
        "f03_risk": check_f03_risk(lead, catalogs),
        "uf_gap": bool(lead.get("uf_gap")),
        "missing_fields": missing,
        "flag_c": bool(r02.get("flag_c")),
        "flag_e": bool(r06.get("flag_e")),
        "r02_human": r02.get("detail") if r02.get("flag_c") else None,
        "r06_human": "; ".join(r06.get("notes") or []) or None,
        "r08_human": svc.get("human") if svc.get("is_service") else None,
    }
