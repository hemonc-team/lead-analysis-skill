#!/usr/bin/env python3
"""Сборка отчёта, CRM-обновления, chat31598, leads_log.csv (live/dry-run).

Единственная точка записи в Bitrix после triage + judgements.
Агент НЕ вызывает crm.lead.update вручную — только этот скрипт.
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rules_core import (  # noqa: E402
    UF_RECOMMEND,
    UF_RECOMMEND_COMMENT,
    UF_TEGI,
    merge_crm_updates,
    uf_empty,
)

log = logging.getLogger("deliver_report")
CHAT_ID = "chat31598"
BOT_ID = 23032
DISK_FOLDER = "Аналитика КЦ"
CSV_NAME = "leads_log.csv"
PORTAL = "https://laskov-partners.bitrix24.ru"

# Поля, которые не перезаписываем, если в карточке уже есть значение.
NO_OVERWRITE = {
    UF_TEGI,
    "UF_CRM_1768816592560",
    "UF_CRM_1748956354216",
    "UF_CRM_1747754997445",
    "UF_CRM_1747750972275",
    UF_RECOMMEND,
    UF_RECOMMEND_COMMENT,
}


def webhook() -> str:
    url = (
        os.environ.get("BITRIX24_WEBHOOK_URL")
        or os.environ.get("BITRIX_WEBHOOK_URL")
        or ""
    ).strip()
    if not url:
        raise RuntimeError("BITRIX24_WEBHOOK_URL (или BITRIX_WEBHOOK_URL) не задан")
    return url if url.endswith("/") else url + "/"


def bx(method: str, params: dict) -> dict:
    r = requests.post(webhook() + method, json=params, timeout=120)
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method}: {data.get('error_description', data['error'])}")
    return data


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def month_name_ru(d: str) -> str:
    months = {
        1: "января",
        2: "февраля",
        3: "марта",
        4: "апреля",
        5: "мая",
        6: "июня",
        7: "июля",
        8: "августа",
        9: "сентября",
        10: "октября",
        11: "ноября",
        12: "декабря",
    }
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day} {months[dt.month]}"


def build_report(triage: dict, auto: list[dict], judgements: list[dict], crm_stats: dict) -> str:
    c = triage["counts"]
    rd = triage["report_date"]
    date_h = month_name_ru(rd)

    emoji_cnt = {"🔴": 0, "⚠️": 0, "✅": 0, "ℹ️": 0, "🎧": 0}
    for row in auto + judgements:
        e = row.get("emoji") or {
            "ok": "✅",
            "info": "ℹ️",
            "gap_only": "🎧",
        }.get(row.get("classification"), "✅")
        if e in emoji_cnt:
            emoji_cnt[e] += 1

    gap_total = emoji_cnt["🎧"]
    gap_ids = [
        str(r["id"])
        for r in auto + judgements
        if (r.get("emoji") == "🎧" or r.get("classification") == "gap_only")
    ]
    actions = [r for r in judgements if r.get("in_action_list")]

    users = {}
    try:
        cat_path = Path(triage.get("_catalogs_path") or "")
        if cat_path.is_file():
            users = json.loads(cat_path.read_text(encoding="utf-8")).get("users") or {}
    except Exception:
        pass

    def op_name(oid: str) -> str:
        return users.get(str(oid), f"оператор {oid}" if oid else "оператор")

    lines = [
        f"📊 Проверка обращений за {date_h}",
        (
            f"Посмотрели {c['unique']} карточку: {c['A']} закрыли вчера, "
            f"{c['B']} с назначенной на вчера связью,"
        ),
        f"{c['C']} с вчерашней активностью без следующего шага.",
        "",
        f"🔴 Похоже, потеряли пациента: {emoji_cnt['🔴']}",
        f"⚠️ Нужно доделать: {emoji_cnt['⚠️']}",
        f"✅ Всё в порядке: {emoji_cnt['✅']}",
        f"ℹ️ Просто консультация, запись не планировалась: {emoji_cnt['ℹ️']}",
        (
            f"🎧 Разговор не расшифровался: {gap_total} — "
            "по ним смотрели только переписку и карточку"
        ),
        "",
        "🤖 Что заполнил робот",
        (
            f"• Обновлений в карточках: {crm_stats.get('updated', 0)}"
            + (
                f" (ошибок: {len(crm_stats.get('errors') or [])})"
                if crm_stats.get("errors")
                else ""
            )
        ),
        (
            f"• Тема обращения: было {crm_stats.get('tags_before', 0)} из {c['unique']}, "
            f"стало {crm_stats.get('tags_after', 0)} — добавил {crm_stats.get('tags_added', 0)}"
        ),
        (
            f"• Кто рекомендовал: было {crm_stats.get('recommend_before', 0)} из {c['unique']}, "
            f"стало {crm_stats.get('recommend_after', 0)} — "
            f"добавил {crm_stats.get('recommend_added', 0)}"
        ),
    ]

    if actions:
        lines += ["", "━━━━━━━━━━━━━━━━━━━━━━━", "ЧТО СДЕЛАТЬ СЕГОДНЯ", ""]
        for r in actions:
            lid = r["id"]
            op = op_name(r.get("operator_id", ""))
            lines += [
                f"⚠️ [URL={PORTAL}/crm/lead/details/{lid}/]#{lid}[/URL] — {op}",
                f"Что хотел пациент: {r.get('patient_wanted', '')}",
                f"Что произошло: {r.get('what_happened', '')}",
                f"Почему это важно: {r.get('why_important', '')}",
                f"Что сделать: {r.get('action', '')}",
                "",
            ]
    else:
        lines += ["", "Обращений, требующих действий, нет"]

    if gap_total:
        pct = round(100 * gap_total / max(c["unique"], 1), 1)
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            f"🎧 Разговор не расшифровался: {gap_total} из {c['unique']} ({pct}%)",
            f"Номера: {', '.join('#' + i for i in gap_ids)}",
            "Что это значит: звонок состоялся, запись не сохранилась. Действий не требуется.",
        ]

    return "\n".join(lines)


def collect_pending_updates(
    auto: list[dict],
    judgements: list[dict],
    crm_auto_by_id: dict[str, dict],
) -> dict[str, dict[str, str]]:
    """id → fields для crm.lead.update (auto F04 + LLM crm_updates)."""
    by_id: dict[str, dict] = {}

    for row in auto:
        sid = str(row["id"])
        by_id[sid] = merge_crm_updates(
            crm_auto_by_id.get(sid),
            row.get("crm_updates_auto"),
            row.get("crm_updates"),
        )

    for row in judgements:
        sid = str(row["id"])
        by_id[sid] = merge_crm_updates(
            by_id.get(sid),
            crm_auto_by_id.get(sid),
            row.get("crm_updates_auto"),
            row.get("crm_updates"),
        )

    # auto F04 для review-лидов, которых нет в judgements (прогон оборвался)
    for sid, auto_u in crm_auto_by_id.items():
        if sid not in by_id:
            by_id[sid] = dict(auto_u)
        else:
            by_id[sid] = merge_crm_updates(auto_u, by_id[sid])

    return {sid: fields for sid, fields in by_id.items() if fields}


def filter_writable(lead: dict, proposed: dict[str, str]) -> dict[str, str]:
    """Не перезаписывать уже заполненные UF."""
    out: dict[str, str] = {}
    for key, val in proposed.items():
        if key in NO_OVERWRITE and not uf_empty(lead.get(key)):
            continue
        if uf_empty(val):
            continue
        out[key] = val
    return out


def apply_crm_updates(pending: dict[str, dict[str, str]], *, dry_run: bool) -> dict:
    stats = {
        "updated": 0,
        "skipped_empty": 0,
        "tags_before": 0,
        "tags_after": 0,
        "tags_added": 0,
        "recommend_before": 0,
        "recommend_after": 0,
        "recommend_added": 0,
        "errors": [],
        "would_update": [],
    }

    for sid, proposed in pending.items():
        try:
            lead = bx("crm.lead.get.json", {"id": sid})["result"]
            had_tag = not uf_empty(lead.get(UF_TEGI))
            had_rec = not uf_empty(lead.get(UF_RECOMMEND))
            if had_tag:
                stats["tags_before"] += 1
            if had_rec:
                stats["recommend_before"] += 1

            fields = filter_writable(lead, proposed)
            if not fields:
                stats["skipped_empty"] += 1
                if had_tag:
                    stats["tags_after"] += 1
                if had_rec:
                    stats["recommend_after"] += 1
                continue

            if dry_run:
                stats["updated"] += 1
                stats["would_update"].append({"id": sid, "fields": fields})
            else:
                bx("crm.lead.update.json", {"id": sid, "fields": fields})
                stats["updated"] += 1

            if UF_TEGI in fields and not had_tag:
                stats["tags_added"] += 1
            if UF_RECOMMEND in fields and not had_rec:
                stats["recommend_added"] += 1
            if had_tag or UF_TEGI in fields:
                stats["tags_after"] += 1
            if had_rec or UF_RECOMMEND in fields:
                stats["recommend_after"] += 1
        except Exception as e:  # noqa: BLE001
            stats["errors"].append(f"#{sid}: {e}")
            log.warning("CRM update #%s: %s", sid, e)

    return stats


def baseline_field_counts(all_rows: list[dict]) -> dict[str, int]:
    """Сколько полей уже заполнено до записи (по снимку triage/judgements)."""
    tags = 0
    rec = 0
    for r in all_rows:
        fields = r.get("fields") or {}
        if str(fields.get("теги_кц") or "").strip():
            tags += 1
        if str(fields.get("кто_рекомендовал") or "").strip():
            rec += 1
        # auto rows may not have label snapshot for recommend if old triage
        crm = r.get("crm_updates") or {}
        if UF_RECOMMEND in crm and not str(fields.get("кто_рекомендовал") or "").strip():
            pass
    return {"tags_before": tags, "recommend_before": rec}


def send_chat(message: str) -> None:
    bx("im.message.add.json", {"DIALOG_ID": CHAT_ID, "MESSAGE": message})


def send_bot_buttons(actions: list[dict], users: dict) -> None:
    for r in actions:
        lid = r["id"]
        op = users.get(str(r.get("operator_id", "")), "")
        short = (r.get("action") or r.get("patient_wanted") or "")[:120]
        msg = f"⚠️ #{lid} — {op}. {short}"
        bx(
            "imbot.message.add.json",
            {
                "BOT_ID": BOT_ID,
                "CLIENT_ID": "ai_review_bot",
                "DIALOG_ID": CHAT_ID,
                "MESSAGE": msg,
                "KEYBOARD": json.dumps(
                    [
                        {
                            "TEXT": "❌ Не согласен",
                            "COMMAND": "disagree",
                            "COMMAND_PARAMS": str(lid),
                            "BG_COLOR": "#E55D53",
                            "TEXT_COLOR": "#FFFFFF",
                            "DISPLAY": "LINE",
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        )


def append_csv(all_rows: list[dict], report_date: str) -> int:
    """Дописать строки в leads_log.csv на Disk."""
    storages = bx("disk.storage.getlist.json", {})["result"]
    root_id = None
    for st in storages:
        if st.get("ENTITY_TYPE") == "common":
            root_id = st["ROOT_OBJECT_ID"]
            break
    if not root_id:
        raise RuntimeError("Общий диск не найден")
    folders = bx("disk.folder.getchildren.json", {"id": root_id})["result"]
    folder_id = None
    for f in folders:
        if f.get("NAME") == DISK_FOLDER:
            folder_id = f["ID"]
            break
    if not folder_id:
        raise RuntimeError(f"Папка Disk «{DISK_FOLDER}» не найдена")

    children = bx("disk.folder.getchildren.json", {"id": folder_id})["result"]
    file_id = None
    existing = ""
    for ch in children:
        if ch.get("NAME") == CSV_NAME:
            file_id = ch["ID"]
            dl = bx("disk.file.get.json", {"id": file_id})["result"].get("DOWNLOAD_URL")
            if dl:
                with tempfile.NamedTemporaryFile(delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    for attempt in range(3):
                        proc = subprocess.run(
                            ["curl", "-sS", "-L", "--max-time", "180", "-o", tmp_path, dl],
                            check=False,
                        )
                        if proc.returncode == 0 and Path(tmp_path).stat().st_size > 1000:
                            break
                        log.warning(
                            "disk download attempt %s failed (rc=%s)",
                            attempt + 1,
                            proc.returncode,
                        )
                    else:
                        raise RuntimeError("не удалось скачать leads_log.csv с Disk")
                    existing = Path(tmp_path).read_text(encoding="utf-8")
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
            break

    header = (
        "дата,лид_id,ссылка,оператор,направление,классификация,флаг_C,флаг_E,"
        "тег_кц_AI,препарат_AI,статус_детальный_AI,задача_AI,кто_рекомендовал_AI,"
        "согласие_с_оценкой"
    )
    out = io.StringIO()
    if existing.strip():
        out.write(existing.rstrip("\n") + "\n")
    elif not file_id:
        out.write(header + "\n")

    n = 0
    for r in all_rows:
        st = r.get("structural") or {}
        fields = r.get("fields") or {}
        cls = r.get("classification", "")
        emoji = r.get("emoji") or {
            "ok": "✅",
            "info": "ℹ️",
            "gap_only": "🎧",
        }.get(cls, "✅")
        crm = r.get("crm_updates") or {}
        out.write(
            ",".join(
                [
                    report_date,
                    str(r["id"]),
                    f"{PORTAL}/crm/lead/details/{r['id']}/",
                    str(r.get("operator_id", "")),
                    r.get("direction", ""),
                    emoji,
                    "1" if st.get("flag_c") else "0",
                    "1" if st.get("flag_e") else "0",
                    str(crm.get("UF_CRM_1706806189", fields.get("теги_кц", ""))),
                    str(crm.get("UF_CRM_1748956354216", fields.get("препарат", ""))),
                    str(crm.get("UF_CRM_1747754997445", fields.get("статус_детальный", ""))),
                    str(crm.get("UF_CRM_1747750972275", fields.get("задача", "")))[:200],
                    str(crm.get(UF_RECOMMEND, fields.get("кто_рекомендовал", ""))),
                    "",
                ]
            )
            + "\n"
        )
        n += 1

    content = out.getvalue().encode("utf-8")
    b64 = base64.b64encode(content).decode()
    if file_id:
        bx("disk.file.uploadversion.json", {"id": file_id, "fileContent": [CSV_NAME, b64]})
    else:
        bx(
            "disk.folder.uploadfile.json",
            {
                "id": folder_id,
                "data": {"NAME": CSV_NAME},
                "fileContent": [CSV_NAME, b64],
            },
        )
    return n


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    run_dir = Path(os.environ.get("RUN_DIR", ".run/latest"))
    mode = os.environ.get("LEAD_ANALYSIS_MODE", "dry-run").lower()
    dry = mode != "live"

    triage_path = run_dir / "triage.json"
    if not triage_path.exists():
        log.error("нет %s — сначала fetch + triage + judgements", triage_path)
        return 2

    triage = json.loads(triage_path.read_text(encoding="utf-8"))
    triage["_catalogs_path"] = str(run_dir / "catalogs.json")
    catalogs = {}
    if (run_dir / "catalogs.json").exists():
        catalogs = json.loads((run_dir / "catalogs.json").read_text(encoding="utf-8"))

    auto = load_jsonl(run_dir / "auto_results.jsonl")
    judgements = load_jsonl(run_dir / "judgements.jsonl")
    crm_auto_by_id = triage.get("crm_auto_by_id") or {}

    struct = triage.get("structural_by_id") or {}
    all_rows: list[dict] = []
    seen: set[str] = set()
    for row in auto + judgements:
        sid = str(row["id"])
        merged = dict(row)
        if sid in struct:
            merged["structural"] = struct[sid]
        # финальные updates для CSV
        merged["crm_updates"] = merge_crm_updates(
            crm_auto_by_id.get(sid),
            row.get("crm_updates_auto"),
            row.get("crm_updates"),
        )
        all_rows.append(merged)
        seen.add(sid)

    pending = collect_pending_updates(auto, judgements, crm_auto_by_id)
    baseline = baseline_field_counts(all_rows)
    log.info(
        "CRM pending leads=%s (mode=%s, auto_f04=%s, judgements=%s)",
        len(pending),
        mode,
        len(crm_auto_by_id),
        len(judgements),
    )

    if dry:
        try:
            crm_stats = apply_crm_updates(pending, dry_run=True)
        except Exception as e:  # noqa: BLE001
            log.warning("dry-run CRM probe failed (нет вебхука?): %s", e)
            crm_stats = {
                "updated": len(pending),
                "tags_before": baseline["tags_before"],
                "tags_after": baseline["tags_before"],
                "tags_added": 0,
                "recommend_before": baseline["recommend_before"],
                "recommend_after": baseline["recommend_before"]
                + sum(1 for f in pending.values() if UF_RECOMMEND in f),
                "recommend_added": sum(1 for f in pending.values() if UF_RECOMMEND in f),
                "errors": [str(e)],
                "would_update": [
                    {"id": sid, "fields": fields} for sid, fields in pending.items()
                ],
            }
    else:
        crm_stats = apply_crm_updates(pending, dry_run=False)

    # Базовые «было» из снимка всех лидов прогона (не только pending).
    crm_stats["tags_before"] = baseline["tags_before"]
    crm_stats["recommend_before"] = baseline["recommend_before"]
    crm_stats["tags_after"] = baseline["tags_before"] + int(crm_stats.get("tags_added") or 0)
    crm_stats["recommend_after"] = baseline["recommend_before"] + int(
        crm_stats.get("recommend_added") or 0
    )

    report = build_report(triage, auto, judgements, crm_stats)
    (run_dir / "report.txt").write_text(report, encoding="utf-8")
    (run_dir / "crm_stats.json").write_text(
        json.dumps(crm_stats, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    actions = [r for r in judgements if r.get("in_action_list")]

    if not dry:
        send_chat(report)
        if actions:
            send_bot_buttons(actions, catalogs.get("users") or {})
        try:
            csv_n = append_csv(all_rows, triage["report_date"])
        except Exception as e:  # noqa: BLE001
            log.warning("CSV Disk skip: %s", e)
            csv_n = 0
        log.info(
            "live: CRM updated=%s recommend_added=%s CSV=%s chat=ok buttons=%s",
            crm_stats["updated"],
            crm_stats.get("recommend_added"),
            csv_n,
            len(actions),
        )
    else:
        log.info(
            "dry-run: report → %s would_update=%s recommend_added≈%s",
            run_dir / "report.txt",
            crm_stats.get("updated"),
            crm_stats.get("recommend_added"),
        )
        for item in (crm_stats.get("would_update") or [])[:20]:
            log.info("  would #%s %s", item["id"], item["fields"])

    c = triage["counts"]
    log.info(
        "САМОПРОВЕРКА: date=%s total=%s auto=%s llm=%s openlines=%s mode=%s "
        "CRM_updated=%s recommend_added=%s err=%s",
        triage["report_date"],
        c["unique"],
        triage.get("auto_leads"),
        triage.get("llm_leads"),
        triage.get("openlines_scope"),
        mode,
        crm_stats.get("updated"),
        crm_stats.get("recommend_added"),
        crm_stats.get("errors"),
    )
    return 0 if not crm_stats.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
