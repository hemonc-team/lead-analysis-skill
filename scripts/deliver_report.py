#!/usr/bin/env python3
"""Сборка отчёта, CRM-обновления, chat31598, leads_log.csv (live/dry-run)."""
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
from zoneinfo import ZoneInfo

import requests

log = logging.getLogger("deliver_report")
MSK = ZoneInfo("Europe/Moscow")
CHAT_ID = "chat31598"
BOT_ID = 23032
DISK_FOLDER = "Аналитика КЦ"
CSV_NAME = "leads_log.csv"
PORTAL = "https://laskov-partners.bitrix24.ru"

ENUM = {
    "теги_кц": "UF_CRM_1706806189",
    "вариация": "UF_CRM_1768816592560",
    "препарат": "UF_CRM_1748956354216",
    "статус_детальный": "UF_CRM_1747754997445",
    "задача": "UF_CRM_1747750972275",
}
TAG_ONKO = "1846"
VAR_OCH = "2584"
VAR_DIST = "2586"
VAR_HT = "2590"
VAR_SALE = "2588"
STAT_CONTACT = "1922"
STAT_SMETA = "1924"
STAT_NO_SVC = "2630"
STAT_PAID = "1928"
PREP_NA = "2070"
PREP_CIS = "2090"
PREP_HAL = None  # not in catalog


def webhook() -> str:
    url = os.environ.get("BITRIX24_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError("BITRIX24_WEBHOOK_URL не задан")
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
        1: "января", 2: "февраля", 3: "марта", 4: "апреля",
        5: "мая", 6: "июня", 7: "июля", 8: "августа",
        9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
    }
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{dt.day} {months[dt.month]}"


def build_report(triage: dict, auto: list[dict], judgements: list[dict], crm_stats: dict) -> str:
    c = triage["counts"]
    rd = triage["report_date"]
    date_h = month_name_ru(rd)

    emoji_cnt = {"🔴": 0, "⚠️": 0, "✅": 0, "ℹ️": 0, "🎧": 0}
    for row in auto + judgements:
        e = row.get("emoji") or {"ok": "✅", "info": "ℹ️", "gap_only": "🎧"}.get(row.get("classification"), "✅")
        if e in emoji_cnt:
            emoji_cnt[e] += 1

    gap_total = emoji_cnt["🎧"]
    gap_ids = [str(r["id"]) for r in auto + judgements if (r.get("emoji") == "🎧" or r.get("classification") == "gap_only")]
    actions = [r for r in judgements if r.get("in_action_list")]

    users = {}
    try:
        users = json.loads(Path(triage.get("_catalogs_path", "")).read_text())["users"] if triage.get("_catalogs_path") else {}
    except Exception:
        pass

    def op_name(oid: str) -> str:
        return users.get(str(oid), f"оператор {oid}")

    lines = [
        f"📊 Проверка обращений за {date_h}",
        f"Посмотрели {c['unique']} карточку: {c['A']} закрыли вчера, {c['B']} с назначенной на вчера связью,",
        f"{c['C']} с вчерашней активностью без следующего шага.",
        "",
        f"🔴 Похоже, потеряли пациента: {emoji_cnt['🔴']}",
        f"⚠️ Нужно доделать: {emoji_cnt['⚠️']}",
        f"✅ Всё в порядке: {emoji_cnt['✅']}",
        f"ℹ️ Просто консультация, запись не планировалась: {emoji_cnt['ℹ️']}",
        f"🎧 Разговор не расшифровался: {gap_total} — по ним смотрели только переписку и карточку",
        "",
        "🤖 Что заполнил робот",
        f"• Тема обращения: было {crm_stats.get('tags_before', 0)} из {c['unique']}, стало {crm_stats.get('tags_after', 0)} — добавил {crm_stats.get('tags_added', 0)}",
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
        pct = round(100 * gap_total / c["unique"], 1)
        lines += [
            "━━━━━━━━━━━━━━━━━━━━━━━",
            f"🎧 Разговор не расшифровался: {gap_total} из {c['unique']} ({pct}%)",
            f"Номера: {', '.join('#' + i for i in gap_ids)}",
            "Что это значит: звонок состоялся, запись не сохранилась. Действий не требуется.",
        ]

    return "\n".join(lines)


def apply_crm_updates(all_rows: list[dict], catalogs: dict) -> dict:
    stats = {"updated": 0, "tags_before": 0, "tags_after": 0, "tags_added": 0, "errors": []}
    enum_maps = catalogs.get("enums", {})

    for row in all_rows:
        lid = row["id"]
        updates = row.get("crm_updates") or {}
        if not updates:
            if row.get("fields", {}).get("теги_кц"):
                stats["tags_before"] += 1
            continue

        fields = {"id": lid}
        for k, v in updates.items():
            fields[k] = v

        try:
            lead = bx("crm.lead.get.json", {"id": lid})["result"]
            had_tag = bool(lead.get("UF_CRM_1706806189"))
            if had_tag:
                stats["tags_before"] += 1
            bx("crm.lead.update.json", {"id": lid, "fields": fields})
            stats["updated"] += 1
            if "UF_CRM_1706806189" in fields and not had_tag:
                stats["tags_added"] += 1
            if lead.get("UF_CRM_1706806189") or "UF_CRM_1706806189" in fields:
                stats["tags_after"] += 1
        except Exception as e:
            stats["errors"].append(f"#{lid}: {e}")
            log.warning("CRM update #%s: %s", lid, e)

    return stats


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
                        )
                        if proc.returncode == 0 and Path(tmp_path).stat().st_size > 1000:
                            break
                        log.warning("disk download attempt %s failed (rc=%s)", attempt + 1, proc.returncode)
                    else:
                        raise RuntimeError("не удалось скачать leads_log.csv с Disk")
                    existing = Path(tmp_path).read_text(encoding="utf-8")
                finally:
                    Path(tmp_path).unlink(missing_ok=True)
            break

    header = "дата,лид_id,ссылка,оператор,направление,классификация,флаг_C,флаг_E,тег_кц_AI,препарат_AI,статус_детальный_AI,задача_AI,согласие_с_оценкой"
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
        emoji = r.get("emoji") or {"ok": "✅", "info": "ℹ️", "gap_only": "🎧"}.get(cls, "✅")
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
                    str(crm.get("UF_CRM_1747750972275", fields.get("задача", ""))[:200]),
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

    triage = json.loads((run_dir / "triage.json").read_text(encoding="utf-8"))
    triage["_catalogs_path"] = str(run_dir / "catalogs.json")
    catalogs = json.loads((run_dir / "catalogs.json").read_text(encoding="utf-8"))
    auto = load_jsonl(run_dir / "auto_results.jsonl")
    judgements = load_jsonl(run_dir / "judgements.jsonl")

    # merge structural/fields/operator for CSV
    struct = triage.get("structural_by_id", {})
    all_rows = []
    for row in auto + judgements:
        sid = str(row["id"])
        merged = dict(row)
        if sid in struct:
            merged["structural"] = struct[sid]
        all_rows.append(merged)

    crm_stats = {"updated": 0, "tags_before": 0, "tags_after": 0, "tags_added": 0, "errors": []}
    if mode == "live":
        crm_stats = apply_crm_updates(all_rows, catalogs)
    else:
        for row in all_rows:
            if row.get("fields", {}).get("теги_кц"):
                crm_stats["tags_before"] += 1

    report = build_report(triage, auto, judgements, crm_stats)
    (run_dir / "report.txt").write_text(report, encoding="utf-8")

    actions = [r for r in judgements if r.get("in_action_list")]

    if mode == "live":
        send_chat(report)
        if actions:
            send_bot_buttons(actions, catalogs.get("users", {}))
        csv_n = append_csv(all_rows, triage["report_date"])
        log.info("live: CRM updated=%s, CSV rows=%s, chat=ok, buttons=%s", crm_stats["updated"], csv_n, len(actions))
    else:
        log.info("dry-run: report → %s", run_dir / "report.txt")

    # самопроверка в лог
    c = triage["counts"]
    log.info(
        "САМОПРОВЕРКА: date=%s total=%s auto=%s llm=%s openlines=%s mode=%s CRM_err=%s",
        triage["report_date"],
        c["unique"],
        triage["auto_leads"],
        triage["llm_leads"],
        triage.get("openlines_scope"),
        mode,
        crm_stats.get("errors"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
