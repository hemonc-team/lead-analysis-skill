#!/usr/bin/env python3
"""Сбор лидов A/B/C + коммуникация (фаза 1) — standalone, только Bitrix REST.

Порт `dwh-etl/lead_analysis_fetch.py` без зависимостей от DWH (Postgres,
Telegram, config.py). Запускается где угодно, где есть HTTPS до портала:
Cursor Cloud automation, локальный Mac, VM.

Дальше — `triage.py` (правила R02/R06, компакт) и агент по review-пакетам.
Здесь только факты из REST: справочники, A/B/C, звонки, чаты, таймлайн, UF-текст.

День отчёта по умолчанию — вчера (Europe/Moscow).

Запуск:
    python3 fetch.py
    python3 fetch.py --report-date=yesterday
    python3 fetch.py --report-date=2026-09-09
    python3 fetch.py --limit 5 --out-dir .run/smoke

Env:
    BITRIX24_WEBHOOK_URL   входящий вебхук (обязателен), со слэшем в конце
    LEAD_ANALYSIS_REPORT_DATE  yesterday|today|YYYY-MM-DD (если нет CLI)
    BITRIX_RATE_LIMIT      запросов/сек, по умолчанию 2 (лимит портала)

Scope вебхука: crm, telephony, user — минимум; im + imopenlines — тексты
чатов (иначе openlines_scope=false и остаётся только факт сессии).

JSON в out-dir содержит ПДн: каталог не коммитить (см. .gitignore).
В stdout — только ID и счётчики, без ФИО/телефонов/текста разговоров.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from rules_core import is_service_contact

log = logging.getLogger("lead_analysis_fetch")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

MSK = ZoneInfo("Europe/Moscow")
CLOSED_STATUSES = ["CONVERTED", "JUNK", "4", "7", "20"]
BATCH_SIZE = 10
UF_NEXT = "UF_CRM_1599826765930"
UF_TRANSCRIPT = "UF_CRM_GPT_TRANSCRIPT"
MIN_CALL_DURATION = 15  # как gpt_transcripts; короче — не UF-gap
ENUM_FIELDS = [
    "UF_CRM_1706806189",  # ТегиКЦ
    "UF_CRM_1768816592560",  # Вариация приёма
    "UF_CRM_1748956354216",  # Препарат
    "UF_CRM_1747754997445",  # Статус детальный
    "UF_CRM_1747750972275",  # Задача
]
SELECT_FIELDS = [
    "ID",
    "TITLE",
    "STATUS_ID",
    "ASSIGNED_BY_ID",
    "CONTACT_ID",
    "IS_RETURN_CUSTOMER",
    "PHONE",
    "LAST_ACTIVITY_TIME",
    "DATE_CREATE",
    "DATE_MODIFY",
    "DATE_CLOSED",
    UF_TRANSCRIPT,
    "UF_CRM_1706806189",
    "UF_CRM_1768816592560",
    "UF_CRM_1748956354216",
    "UF_CRM_1747754997445",
    "UF_CRM_1747750972275",
    UF_NEXT,
    "UF_CRM_1732558654060",
    "UF_CRM_1761919557442",
]

# При scope im + imopenlines читаем тексты чатов; без scope — только таймлайн.
_openlines_scope = True


# ── Bitrix24 REST client ────────────────────────────────────────────────────

class BitrixError(RuntimeError):
    pass


_last_call = [0.0]


def _rate_limit() -> float:
    try:
        return float(os.environ.get("BITRIX_RATE_LIMIT", "2"))
    except ValueError:
        return 2.0


def _webhook() -> str:
    url = (os.environ.get("BITRIX24_WEBHOOK_URL") or "").strip()
    if not url:
        raise BitrixError("BITRIX24_WEBHOOK_URL не задан в окружении")
    return url.rstrip("/")


def _throttle() -> None:
    """Не чаще BITRIX_RATE_LIMIT запросов/сек (лимит портала — 2/сек)."""
    min_interval = 1.0 / max(_rate_limit(), 0.1)
    elapsed = time.monotonic() - _last_call[0]
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _last_call[0] = time.monotonic()


def bitrix_call(method: str, params: dict | None = None, max_retries: int = 6) -> dict:
    """Вызов REST-метода с троттлингом и ретраями (backoff)."""
    url = _webhook() + "/" + method + ".json"
    params = params or {}
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        _throttle()
        try:
            resp = requests.post(url, json=params, timeout=120)
        except requests.RequestException as exc:
            if attempt == max_retries:
                raise BitrixError(f"сеть: {exc}") from exc
            log.warning("Сетевая ошибка (%s), повтор через %.0fс", exc, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        if resp.status_code == 200:
            data = resp.json()
            if "error" in data:
                err = data.get("error")
                desc = data.get("error_description", "")
                if err in ("QUERY_LIMIT_EXCEEDED", "OPERATION_TIME_LIMIT") and attempt < max_retries:
                    log.warning("Bitrix лимит (%s), повтор через %.0fс", err, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue
                raise BitrixError(f"{err}: {desc}")
            return data

        if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
            log.warning("HTTP %s, повтор через %.0fс", resp.status_code, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        # В текст ошибки не подставляем URL: он содержит секрет вебхука.
        raise BitrixError(f"HTTP {resp.status_code} на {method}: {resp.text[:300]}")

    raise BitrixError("исчерпаны попытки запроса")


# ── Сбор данных ─────────────────────────────────────────────────────────────

def _paginate(method: str, params: dict) -> list:
    start = 0
    rows: list = []
    while True:
        payload = dict(params)
        if start:
            payload["start"] = start
        data = bitrix_call(method, payload)
        batch = data.get("result") or []
        if isinstance(batch, dict):
            batch = batch.get("items") or batch.get("chats") or []
        rows.extend(batch)
        nxt = data.get("next")
        if nxt is None:
            break
        start = int(nxt)
    return rows


def list_leads(extra_filter: dict) -> list[dict]:
    last_id = 0
    out: list[dict] = []
    while True:
        flt = {">ID": last_id, **extra_filter}
        data = bitrix_call("crm.lead.list", {
            "order": {"ID": "ASC"},
            "filter": flt,
            "select": SELECT_FIELDS,
            "start": -1,
        })
        rows = data.get("result") or []
        if not rows:
            break
        out.extend(rows)
        last_id = max(int(r["ID"]) for r in rows)
    return out


def load_catalogs() -> dict:
    statuses = {}
    data = bitrix_call("crm.status.list", {"filter": {"ENTITY_ID": "STATUS"}})
    for row in data.get("result") or []:
        statuses[str(row.get("STATUS_ID"))] = row.get("NAME") or ""

    enums: dict[str, dict[str, str]] = {k: {} for k in ENUM_FIELDS}
    uf_rows = _paginate("crm.lead.userfield.list", {})
    for uf in uf_rows:
        fid = uf.get("FIELD_NAME")
        if fid not in enums:
            continue
        for item in uf.get("LIST") or []:
            enums[fid][str(item.get("ID"))] = item.get("VALUE") or ""

    users: dict[str, str] = {}
    start = 0
    while True:
        params: dict = {"FILTER": {"ACTIVE": True}}
        if start:
            params["start"] = start
        data = bitrix_call("user.get", params)
        for u in data.get("result") or []:
            uid = str(u.get("ID"))
            name = " ".join(
                p for p in (u.get("LAST_NAME"), u.get("NAME"), u.get("SECOND_NAME")) if p
            ).strip()
            users[uid] = name or uid
        nxt = data.get("next")
        if nxt is None:
            break
        start = int(nxt)

    return {"statuses": statuses, "enums": enums, "users": users}


def fetch_calls(entity_type: str, entity_id: int) -> list[dict]:
    rows = _paginate("voximplant.statistic.get", {
        "FILTER": {"CRM_ENTITY_TYPE": entity_type, "CRM_ENTITY_ID": entity_id},
        "ORDER": {"CALL_START_DATE": "ASC"},
    })
    slim = []
    for c in rows:
        slim.append({
            "activity_id": c.get("CRM_ACTIVITY_ID"),
            "call_id": c.get("CALL_ID"),
            "date": c.get("CALL_START_DATE"),
            "type": c.get("CALL_TYPE"),
            "duration": int(c.get("CALL_DURATION") or 0),
            "failed_code": c.get("CALL_FAILED_CODE"),
            "transcript_id": c.get("TRANSCRIPT_ID"),
            "transcript_pending": c.get("TRANSCRIPT_PENDING"),
            "entity_type": c.get("CRM_ENTITY_TYPE"),
            "entity_id": c.get("CRM_ENTITY_ID"),
        })
    return slim


def fetch_chats(lead_id: int) -> list[dict]:
    global _openlines_scope
    if not _openlines_scope:
        return []
    try:
        data = bitrix_call("imopenlines.crm.chat.get", {
            "CRM_ENTITY_TYPE": "lead",
            "CRM_ENTITY": lead_id,
            "ACTIVE_ONLY": "N",
        })
    except BitrixError as exc:
        msg = str(exc)
        if "insufficient_scope" in msg:
            _openlines_scope = False
            log.warning(
                "вебхук без scope Open Lines/IM — тексты чатов не читаем, "
                "остаётся crm.activity.list (3-EXIST). Нужны права imopenlines и im. %s",
                exc,
            )
            return []
        log.warning("imopenlines lead=%s: %s", lead_id, exc)
        return []
    raw = data.get("result") or []
    if isinstance(raw, dict):
        if raw.get("CHAT_ID") or raw.get("chatId"):
            raw = [raw]
        else:
            inner = raw.get("chats") or raw.get("CHAT") or raw.get("items") or []
            raw = inner if isinstance(inner, list) else ([inner] if inner else [])
    chats = []
    for ch in raw or []:
        chat_id = ch.get("CHAT_ID") or ch.get("chatId")
        if not chat_id:
            continue
        messages = []
        try:
            msg_data = bitrix_call("im.dialog.messages.get", {
                "DIALOG_ID": f"chat{chat_id}",
                "LIMIT": 200,
            })
            msg_list = (msg_data.get("result") or {}).get("messages") or []
            if isinstance(msg_data.get("result"), list):
                msg_list = msg_data.get("result") or []
            for m in msg_list:
                text = (m.get("text") or m.get("TEXT") or "").strip()
                author = m.get("author_id") or m.get("AUTHOR_ID") or 0
                if not text or int(author or 0) == 0:
                    continue
                if "bx-messenger-content-item-ol" in text:
                    continue
                messages.append({
                    "date": m.get("date") or m.get("DATE"),
                    "author_id": int(author),
                    "text": text,
                })
        except BitrixError as exc:
            if "insufficient_scope" in str(exc):
                _openlines_scope = False
                log.warning("im.dialog без scope IM — дальше только таймлайн. %s", exc)
                break
            log.warning("im.dialog lead=%s chat=%s: %s", lead_id, chat_id, exc)
        chats.append({
            "chat_id": int(chat_id),
            "connector": ch.get("CONNECTOR") or ch.get("connector") or "",
            "messages": messages,
        })
    return chats


def fetch_timeline(lead_id: int) -> dict:
    calls = _paginate("crm.activity.list", {
        "filter": {"OWNER_TYPE_ID": 1, "OWNER_ID": lead_id, "TYPE_ID": 2},
        "select": ["ID", "SUBJECT", "START_TIME", "END_TIME", "DESCRIPTION", "SETTINGS"],
        "order": {"START_TIME": "ASC"},
    })
    chats = _paginate("crm.activity.list", {
        "filter": {
            "OWNER_TYPE_ID": 1,
            "OWNER_ID": lead_id,
            "PROVIDER_ID": "IMOPENLINES_SESSION",
        },
        "select": ["ID", "SUBJECT", "START_TIME", "ASSOCIATED_ENTITY_ID"],
        "order": {"START_TIME": "ASC"},
    })
    slim_calls = []
    for a in calls:
        settings = a.get("SETTINGS") or {}
        if isinstance(settings, str):
            settings = {}
        slim_calls.append({
            "id": a.get("ID"),
            "subject": a.get("SUBJECT") or "",
            "start": a.get("START_TIME"),
            "end": a.get("END_TIME"),
            "missed": bool(settings.get("MISSED_CALL")),
        })
    slim_chats = [{
        "id": a.get("ID"),
        "subject": a.get("SUBJECT") or "",
        "start": a.get("START_TIME"),
        "associated_entity_id": a.get("ASSOCIATED_ENTITY_ID"),
    } for a in chats]
    return {"calls": slim_calls, "chats": slim_chats}


def long_success_calls(calls: list[dict]) -> list[dict]:
    """Состоявшиеся звонки ≥ MIN_CALL_DURATION секунд."""
    out = []
    for c in calls:
        if int(c.get("duration") or 0) < MIN_CALL_DURATION:
            continue
        if str(c.get("failed_code") or "") != "200":
            continue
        out.append(c)
    return out


def uf_gap_for_lead(calls: list[dict], uf_text: str) -> dict:
    """Явный UF-gap (#24604): длинный звонок есть, текста в поле нет."""
    long_calls = long_success_calls(calls)
    has_uf = bool((uf_text or "").strip())
    gap = bool(long_calls) and not has_uf
    latest = None
    if long_calls:
        dated = sorted(
            (c for c in long_calls if c.get("date")),
            key=lambda c: c["date"],
        )
        if dated:
            latest = dated[-1].get("date")
    return {
        "uf_gap": gap,
        "long_calls": len(long_calls),
        "has_uf": has_uf,
        "latest_long_call_at": latest,
        "report_mark": (
            f"Разговор {latest or 'дата?'} не расшифрован — по содержанию не судить"
            if gap
            else None
        ),
    }


_contact_meta_cache: dict[int, dict] = {}


def fetch_contact_meta(contact_id: int) -> dict:
    """Имя контакта + флаг сервисного агрегатора (ПроДокторов и т.п.)."""
    cid = int(contact_id)
    if cid in _contact_meta_cache:
        return _contact_meta_cache[cid]
    data = bitrix_call(
        "crm.contact.get",
        {
            "id": cid,
            "select": ["ID", "NAME", "SECOND_NAME", "LAST_NAME", UF_TRANSCRIPT],
        },
    )
    row = data.get("result") or {}
    name = str(row.get("NAME") or "").strip()
    second_name = str(row.get("SECOND_NAME") or "").strip()
    last_name = str(row.get("LAST_NAME") or "").strip()
    svc, label = is_service_contact(name=name, second_name=second_name, last_name=last_name)
    meta = {
        "id": cid,
        "name": name,
        "second_name": second_name,
        "last_name": last_name,
        "is_service": svc,
        "label": label if svc else None,
        "transcript": str(row.get(UF_TRANSCRIPT) or "").strip(),
    }
    _contact_meta_cache[cid] = meta
    return meta


def fetch_contact_transcript(contact_id: int) -> str:
    """UF транскрипта с контакта. Звонки часто висят на CONTACT, скилл читает лид."""
    return fetch_contact_meta(int(contact_id)).get("transcript") or ""


def resolve_transcript(lead: dict, contact_id) -> tuple[str, str]:
    """Текст для анализа: UF лида, иначе UF связанного контакта.

    Returns:
        (text, source) где source = lead | contact | empty
    """
    uf_lead = str(lead.get(UF_TRANSCRIPT) or "").strip()
    if uf_lead:
        return uf_lead, "lead"
    if not contact_id:
        return "", "empty"
    try:
        uf_contact = fetch_contact_transcript(int(contact_id))
    except BitrixError as exc:
        log.warning("contact UF fail CONTACT#%s: %s", contact_id, exc)
        return "", "empty"
    if uf_contact:
        return uf_contact, "contact"
    return "", "empty"


def collect_lead(lead: dict, groups: list[str]) -> dict:
    lid = int(lead["ID"])
    contact_id = lead.get("CONTACT_ID")
    contact_meta = None
    if contact_id:
        try:
            contact_meta = fetch_contact_meta(int(contact_id))
        except BitrixError as exc:
            log.warning("contact meta fail CONTACT#%s: %s", contact_id, exc)
    calls = fetch_calls("LEAD", lid)
    if contact_id:
        extra = fetch_calls("CONTACT", int(contact_id))
        seen = {(c.get("activity_id"), c.get("call_id")) for c in calls}
        for c in extra:
            key = (c.get("activity_id"), c.get("call_id"))
            if key not in seen:
                calls.append(c)
                seen.add(key)
    chats = fetch_chats(lid)
    timeline = fetch_timeline(lid)
    uf_text, transcript_source = resolve_transcript(lead, contact_id)
    gap = uf_gap_for_lead(calls, uf_text)
    vox_empty = len(calls) == 0 or all(not c.get("transcript_id") for c in calls)
    ol_empty = not chats
    exist_required = vox_empty or ol_empty
    return {
        "id": lid,
        "groups": groups,
        "status_id": lead.get("STATUS_ID"),
        "assigned_by_id": lead.get("ASSIGNED_BY_ID"),
        "contact_id": contact_id,
        "contact_meta": contact_meta,
        "is_return_customer": lead.get("IS_RETURN_CUSTOMER"),
        "phone": lead.get("PHONE") or [],
        "title": lead.get("TITLE") or "",
        "fields": {k: lead.get(k) for k in SELECT_FIELDS},
        "transcript": uf_text or None,
        "transcript_len": len(uf_text),
        "transcript_source": transcript_source,
        "uf_gap": gap["uf_gap"],
        "uf_gap_detail": gap,
        "calls": calls,
        "chats": chats,
        "timeline": timeline,
        "exist_check": {
            "required": exist_required,
            "voximplant_calls": len(calls),
            "openline_chats": len(chats),
            "timeline_calls": len(timeline["calls"]),
            "timeline_chats": len(timeline["chats"]),
            "has_communication": bool(
                calls or chats or timeline["calls"] or timeline["chats"] or uf_text
            ),
        },
    }


def _uf_empty(val) -> bool:
    """Пустое datetime-UF. REST `=UF: \"\"` для таких полей игнорируется (урок 31.07)."""
    if val is None:
        return True
    text = str(val).strip()
    return text in ("", "0", "None", "0000-00-00", "0000-00-00T00:00:00")


def resolve_report_date(now: datetime, raw: str | None) -> date:
    """День отчёта (календарный, МСК). Утренний прогон → вчера."""
    value = (raw or os.environ.get("LEAD_ANALYSIS_REPORT_DATE") or "yesterday").strip().lower()
    today = now.astimezone(MSK).date()
    if value in ("yesterday", "y"):
        return today - timedelta(days=1)
    if value in ("today", "t"):
        return today
    return date.fromisoformat(value)


def day_bounds_msk(report_day: date) -> tuple[str, str]:
    """Полуинтервал [день 00:00, следующий день 00:00) в наивной строке REST (МСК)."""
    start = datetime.combine(report_day, dtime.min)
    end = start + timedelta(days=1)
    return (
        start.strftime("%Y-%m-%dT%H:%M:%S"),
        end.strftime("%Y-%m-%dT%H:%M:%S"),
    )


def fetch_groups(report_day: date) -> tuple[dict, dict[str, int]]:
    """Группы A/B/C за календарный день отчёта (МСК), не «сейчас»."""
    start_s, end_s = day_bounds_msk(report_day)

    # A: закрытые в день отчёта (DATE_CLOSED, не DATE_MODIFY — gpt_transcripts)
    a = list_leads({
        ">=DATE_CLOSED": start_s,
        "<DATE_CLOSED": end_s,
        "STATUS_ID": CLOSED_STATUSES,
    })
    # B: дата следующего контакта попадала на день отчёта (должны были связаться)
    b = list_leads({
        f">={UF_NEXT}": start_s,
        f"<{UF_NEXT}": end_s,
        "!STATUS_ID": CLOSED_STATUSES,
    })
    # C: нет даты следующего контакта, активность в день отчёта.
    # Не фильтровать пустую дату в REST — Bitrix отдаёт все (урок 31.07).
    c_raw = list_leads({
        ">=LAST_ACTIVITY_TIME": start_s,
        "<LAST_ACTIVITY_TIME": end_s,
        "!STATUS_ID": CLOSED_STATUSES,
    })
    c = [row for row in c_raw if _uf_empty(row.get(UF_NEXT))]
    counts = {
        "A": len(a),
        "B": len(b),
        "C": len(c),
        "C_raw_activity": len(c_raw),
        "report_date": report_day.isoformat(),
    }
    by_id: dict[str, dict] = {}
    group_map: dict[int, list[str]] = {}
    for label, rows in (("A", a), ("B", b), ("C", c)):
        for lead in rows:
            lid = int(lead["ID"])
            by_id[str(lid)] = lead
            group_map.setdefault(lid, []).append(label)
    counts["unique"] = len(by_id)
    return (
        {"leads": list(by_id.values()), "groups": group_map, "counts": counts},
        counts,
    )


def write_batches(payload: dict, out_dir: Path, batch_size: int) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "catalogs.json").write_text(
        json.dumps(payload["catalogs"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / "manifest.json").write_text(
        json.dumps({
            "run_at": payload["run_at"],
            "report_date": payload.get("report_date"),
            "counts": payload["counts"],
            "lead_ids": [x["id"] for x in payload["leads"]],
            "batch_size": batch_size,
            "openlines_scope": payload.get("openlines_scope"),
            "uf_gap": payload.get("uf_gap_stats"),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths = []
    leads = payload["leads"]
    for i in range(0, len(leads), batch_size):
        chunk = leads[i:i + batch_size]
        n = i // batch_size + 1
        path = out_dir / f"batch_{n:02d}.json"
        path.write_text(
            json.dumps({
                "batch": n,
                "run_at": payload["run_at"],
                "leads": chunk,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def run_fetch(
    out_dir: Path,
    limit: int | None,
    report_date_raw: str | None = None,
) -> str:
    started = datetime.now(MSK)
    t0 = started.timestamp()
    _contact_meta_cache.clear()

    report_day = resolve_report_date(started, report_date_raw)
    log.info(
        "день отчёта (МСК)=%s (канон утреннего прогона = вчера)",
        report_day.isoformat(),
    )
    catalogs = load_catalogs()
    log.info(
        "справочники: статусов=%s сотрудников=%s",
        len(catalogs["statuses"]),
        len(catalogs["users"]),
    )
    packed, counts = fetch_groups(report_day)
    log.info(
        "STOP-GATE #0 report_date=%s A=%s B=%s C=%s unique=%s",
        report_day.isoformat(),
        counts["A"], counts["B"], counts["C"], counts["unique"],
    )
    leads = packed["leads"]
    group_map = packed["groups"]
    if limit is not None:
        leads = leads[:limit]
        log.info("limit=%s — собрана коммуникация только для первых %s", limit, len(leads))

    collected = []
    for i, lead in enumerate(leads, 1):
        lid = int(lead["ID"])
        rec = collect_lead(lead, group_map.get(lid, []))
        collected.append(rec)
        exist = rec["exist_check"]
        svc = (rec.get("contact_meta") or {}).get("is_service")
        log.info(
            "лид %s [%s/%s] groups=%s calls=%s chats=%s tl_calls=%s tl_chats=%s "
            "uf=%s src=%s gap=%s comm=%s service=%s",
            lid,
            i,
            len(leads),
            ",".join(rec["groups"]),
            exist["voximplant_calls"],
            exist["openline_chats"],
            exist["timeline_calls"],
            exist["timeline_chats"],
            rec["transcript_len"],
            rec.get("transcript_source") or "empty",
            int(bool(rec.get("uf_gap"))),
            int(exist["has_communication"]),
            int(bool(svc)),
        )

    with_long = sum(
        1 for r in collected if (r.get("uf_gap_detail") or {}).get("long_calls", 0) > 0
    )
    with_gap = sum(1 for r in collected if r.get("uf_gap"))
    gap_pct = round(100.0 * with_gap / with_long, 1) if with_long else 0.0
    uf_gap_stats = {
        "leads_with_long_call": with_long,
        "leads_uf_gap": with_gap,
        "uf_gap_pct": gap_pct,
        "min_duration_sec": MIN_CALL_DURATION,
    }
    log.info("UF-gap: long_call=%s gap=%s (%.1f%%)", with_long, with_gap, gap_pct)

    payload = {
        "run_at": started.isoformat(),
        "report_date": report_day.isoformat(),
        "counts": counts,
        "catalogs": catalogs,
        "leads": collected,
        "openlines_scope": _openlines_scope,
        "uf_gap_stats": uf_gap_stats,
    }
    batches = write_batches(payload, out_dir, BATCH_SIZE)
    duration = datetime.now(MSK).timestamp() - t0
    if not _openlines_scope:
        log.warning(
            "openlines_scope=false — тексты чатов недоступны. "
            "Нужны права im + imopenlines у BITRIX24_WEBHOOK_URL."
        )
    return (
        f"report_date={report_day.isoformat()} "
        f"A={counts['A']} B={counts['B']} C={counts['C']} unique={counts['unique']} "
        f"fetched={len(collected)} batches={len(batches)} "
        f"uf_gap={with_gap}/{with_long} ({gap_pct}%) "
        f"openlines_scope={_openlines_scope} duration={duration:.0f}s out={out_dir}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Fetch leads A/B/C + communication for lead-analysis (phase 1)"
    )
    p.add_argument("--out-dir", default=".run/latest", help="каталог JSON (по умолчанию .run/latest)")
    p.add_argument("--limit", type=int, default=None, help="собрать коммуникацию только для N лидов")
    p.add_argument(
        "--report-date",
        default=None,
        help="день отчёта МСК: yesterday (default) | today | YYYY-MM-DD; "
        "или env LEAD_ANALYSIS_REPORT_DATE",
    )
    args = p.parse_args(argv)

    if not (os.environ.get("BITRIX24_WEBHOOK_URL") or "").strip():
        print("BITRIX24_WEBHOOK_URL не задан в окружении", file=sys.stderr)
        return 2

    try:
        summary = run_fetch(Path(args.out_dir), args.limit, args.report_date)
    except Exception as exc:  # noqa: BLE001 — итог в stdout, трассировка в лог
        log.exception("fetch failed")
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
