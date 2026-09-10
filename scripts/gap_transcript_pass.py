#!/usr/bin/env python3
"""Дорасшифровка UF-gap лидов (#22352/#24604) — только проблемные, с бюджетом.

Портировано и переработано из b24-репо (scripts/gpt_transcript_browser_pass.py +
gpt_transcript_export_gaps.py), которые запускались вручную с локальной машины.
Здесь то же самое, но:

  1. Очередь строится ТОЛЬКО из лидов, которые fetch.py уже пометил uf_gap=True
     на сегодняшний отчёт — не сканируем всю базу, дорасшифровываем только то,
     что и так уйдёт в отчёт как ⚠️ без расшифровки.
  2. Жёсткий дневной бюджет launchCopilot (--max-launch) + circuit-breaker:
     после нескольких подряд AI_ENGINE_LIMIT_EXCEEDED останавливаемся сразу,
     не долбим движок дальше. Это и стало причиной обоих инцидентов с
     лимитами (01.08 и 09.09) — см. docs/investigations/2026-09-07-copilot-auto-assessment-full-dossier.md
     в репо b24: слишком частые launchCopilot из cron сожгли общий лимит
     BitrixGPT на портале.

Механизм: то же, что делал раньше личный скилл М. Ласкова — открыть карточку
в реальном браузере (Playwright) и вызвать внутренние AJAX-методы Bitrix
(`crm.timeline.ai.getCopilotTranscript` / `...launchCopilot`), которых нет
в публичном REST API. REST может только читать уже готовую расшифровку.

Требует авторизованный профиль браузера (Playwright storage_state), см.
docs/CURSOR_AUTOMATION.md → «Дорасшифровка (gap pass)» — там же открытый
вопрос про то, как этот профиль переживает ephemeral-контейнер Cursor
Cloud Automation между прогонами (сессия НЕ гарантированно сохраняется
между отдельными scheduled-запусками — это надо проверить перед боевым
включением, см. докс).

Запуск:
    python3 scripts/gap_transcript_pass.py --run-dir .run/latest --max-launch 50 \\
        --storage-state /path/to/bitrix_state.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("gap_transcript_pass")
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

UF_TRANSCRIPT = "UF_CRM_GPT_TRANSCRIPT"
MIN_CALL_DURATION = 15
OWNER_TYPE = {"LEAD": 1, "CONTACT": 3}
PORTAL_LEAD_LIST = "https://laskov-partners.bitrix24.ru/crm/lead/list/"
LIMIT_ERROR_CODES = {"AI_ENGINE_LIMIT_EXCEEDED"}
LIMIT_BREAKER_STREAK = 3  # после стольки подряд отказов по лимиту — стоп


class GapPassError(RuntimeError):
    """Ожидаемая, не-паническая остановка (нет сессии/нет sessid/и т.п.)."""


PROCESS_ONE_JS = """
async (args) => {
  const {webhook, item, doLaunch, dryRun} = args;
  const ownerType = item.owner_type_id;
  const ek = item.entity_type + '#' + item.entity_id;
  const sessid = BX.bitrix_sessid();

  // getCopilotTranscript — только чтение, ничего не меняет в б24. Безопасно
  // звать и в dry-run: это и есть проверка "нашёл бы скрипт что-то или нет".
  const getCop = () => fetch('/bitrix/services/main/ajax.php?action=crm.timeline.ai.getCopilotTranscript', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: `activityId=${item.activity_id}&ownerTypeId=${ownerType}&ownerId=${item.entity_id}&sessid=${sessid}`,
  }).then(r => r.json());

  // launchCopilot тратит бюджет/лимит движка — в dry-run НИКОГДА не зовём.
  const launchCop = () => fetch('/bitrix/services/main/ajax.php?action=crm.timeline.ai.launchCopilot', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: `activityId=${item.activity_id}&ownerTypeId=${ownerType}&ownerId=${item.entity_id}&sessid=${sessid}`,
  }).then(r => r.json());

  // Пишет в CRM — в dry-run НИКОГДА не зовём.
  const saveField = (text) => fetch(webhook + (item.entity_type === 'LEAD' ? 'crm.lead.update' : 'crm.contact.update') + '.json', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({id: item.entity_id, fields: {UF_CRM_GPT_TRANSCRIPT: text}}),
  }).then(r => r.json());

  try {
    const cop = await getCop();
    const t = cop?.data?.aiJobResult?.transcription;
    if (t && String(t).trim()) {
      const text = String(t).trim();
      if (dryRun) {
        return {status: 'would_save_existing', entity: ek, len: text.length};
      }
      const res = await saveField(text);
      return {status: res?.result ? 'saved_existing' : 'save_failed', entity: ek, len: text.length};
    }
    if (!doLaunch) {
      return {status: 'miss_budget', entity: ek};
    }
    if (dryRun) {
      return {status: 'would_launch', entity: ek};
    }
    const lr = await launchCop();
    const err = lr?.errors?.[0]?.code;
    if (err) {
      return {status: err === 'AI_ENGINE_LIMIT_EXCEEDED' ? 'limit' : 'launch_failed', entity: ek, err};
    }
    return {status: 'launched', entity: ek, act: item.activity_id};
  } catch (e) {
    return {status: 'error', entity: ek, err: String(e.message || e)};
  }
}
"""


def build_queue(run_dir: Path) -> list[dict]:
    """Только звонки лидов с uf_gap=True: длинный успешный звонок (>=15с),
    UF пусто И REST-транскрипта тоже нет (transcript_id пуст). Всё, что не
    попало под uf_gap на сегодняшнем прогоне fetch.py, в очередь не идёт —
    это и есть «не всё подряд, а только проблемные».

    ВАЖНО: fetch.py собирает звонки лида ещё и с привязанного контакта
    (crm.lead.CONTACT_ID) — у каждого звонка свой фактический владелец
    (call['entity_type']/['entity_id']), который может быть CONTACT, а не
    сам лид. getCopilotTranscript/launchCopilot должны звать именно этого
    владельца — иначе попадём не в ту карточку и получим пустой ответ,
    который легко перепутать с «Bitrix не расшифровал».
    """
    queue: list[dict] = []
    skipped_unknown_entity = 0
    for path in sorted(run_dir.glob("batch_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        for lead in data.get("leads", []):
            if not lead.get("uf_gap"):
                continue
            for call in lead.get("calls", []):
                if int(call.get("duration") or 0) < MIN_CALL_DURATION:
                    continue
                if str(call.get("failed_code") or "") != "200":
                    continue
                if call.get("transcript_id"):
                    continue
                activity_id = call.get("activity_id")
                call_entity_type = call.get("entity_type")
                call_entity_id = call.get("entity_id")
                if not activity_id or not call_entity_id:
                    continue
                if call_entity_type not in OWNER_TYPE:
                    skipped_unknown_entity += 1
                    continue
                queue.append({
                    "activity_id": int(activity_id),
                    "entity_type": call_entity_type,
                    "entity_id": int(call_entity_id),
                    "owner_type_id": OWNER_TYPE[call_entity_type],
                    "duration": int(call.get("duration") or 0),
                    "call_date": call.get("date") or "",
                })
    if skipped_unknown_entity:
        log.info(
            "build_queue: %s звонков пропущено — владелец не LEAD/CONTACT "
            "(COMPANY/DEAL, launchCopilot для них не пробуем)",
            skipped_unknown_entity,
        )
    # дедуп на случай, если один и тот же звонок пришёл и с лида, и с контакта
    seen = set()
    deduped = []
    for item in queue:
        key = (item["entity_type"], item["entity_id"], item["activity_id"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    deduped.sort(key=lambda x: x["call_date"])  # старые звонки первыми
    return deduped


def load_webhook() -> str:
    import os

    url = os.environ.get("BITRIX24_WEBHOOK_URL", "").strip()
    if not url:
        raise GapPassError("BITRIX24_WEBHOOK_URL не задан")
    return url if url.endswith("/") else url + "/"


async def wait_for_crm_session(page, timeout_ms: int = 60_000) -> None:
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        try:
            ok = await page.evaluate(
                """() => {
                  const hasCrm = location.href.includes('/crm/');
                  const hasBx = typeof BX !== 'undefined' && BX.bitrix_sessid && BX.bitrix_sessid().length > 10;
                  return hasCrm && hasBx;
                }"""
            )
            if ok:
                return
        except Exception:
            pass
        await asyncio.sleep(1)
    raise GapPassError(
        "Нет валидной CRM-сессии за отведённое время — профиль браузера не "
        "авторизован. НЕ пытаемся логиниться сами: нужен новый storage_state "
        "(см. docs/CURSOR_AUTOMATION.md → «Дорасшифровка (gap pass)»)."
    )


async def run_pass_async(
    run_dir: Path,
    storage_state: Path,
    max_launch: int,
    retry_wait: int,
    delay: float,
    headless: bool,
    dry_run: bool,
) -> dict:
    from playwright.async_api import async_playwright

    webhook = load_webhook()
    queue = build_queue(run_dir)

    stats = {
        "dry_run": dry_run,
        "queue_size": len(queue),
        "saved_existing": 0,
        "would_save_existing": 0,
        "launched": 0,
        "would_launch": 0,
        "saved_after_launch": 0,
        "miss_budget": 0,
        "errors": 0,
        "limit_hit": False,
        "stopped_early": False,
    }
    if not queue:
        return stats

    if not storage_state.exists():
        raise GapPassError(
            f"storage_state не найден: {storage_state} — сначала одноразовый "
            "ручной логин и сохранение профиля (см. docs/CURSOR_AUTOMATION.md)"
        )

    launched: list[dict] = []
    limit_streak = 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(storage_state))
        page = await context.new_page()
        await page.goto(PORTAL_LEAD_LIST, wait_until="domcontentloaded", timeout=60_000)
        await wait_for_crm_session(page)
        log.info(
            "CRM session OK, queue=%s max_launch=%s dry_run=%s",
            len(queue), max_launch, dry_run,
        )

        for item in queue:
            do_launch = stats["launched"] < max_launch
            res = await page.evaluate(
                PROCESS_ONE_JS,
                {"webhook": webhook, "item": item, "doLaunch": do_launch, "dryRun": dry_run},
            )
            st = res.get("status")

            if st == "saved_existing":
                stats["saved_existing"] += 1
            elif st == "would_save_existing":
                stats["would_save_existing"] += 1
                log.info("[dry-run] would save LEAD#%s len=%s", item["entity_id"], res.get("len"))
            elif st == "miss_budget":
                stats["miss_budget"] += 1
            elif st == "launched":
                stats["launched"] += 1
                launched.append(item)
                limit_streak = 0
            elif st == "would_launch":
                stats["would_launch"] += 1
                log.info("[dry-run] would launchCopilot LEAD#%s act=%s", item["entity_id"], item["activity_id"])
            elif st == "limit":
                limit_streak += 1
                log.warning("AI_ENGINE_LIMIT_EXCEEDED LEAD#%s (streak=%s)", item["entity_id"], limit_streak)
                if limit_streak >= LIMIT_BREAKER_STREAK:
                    stats["limit_hit"] = True
                    stats["stopped_early"] = True
                    log.error(
                        "CIRCUIT BREAKER: %s подряд AI_ENGINE_LIMIT_EXCEEDED — стоп, "
                        "не сжигаем лимит дальше", limit_streak,
                    )
                    break
            else:
                stats["errors"] += 1
                log.warning("%s LEAD#%s %s", st, item["entity_id"], res.get("err", ""))
                limit_streak = 0

            await asyncio.sleep(delay)

        if dry_run:
            assert not launched, "dry-run никогда не должен ничего запускать"  # защита от регрессии
        if launched and retry_wait > 0 and not stats["stopped_early"] and not dry_run:
            log.info("waiting %ss before re-check of %s launched…", retry_wait, len(launched))
            await asyncio.sleep(retry_wait)
            for item in launched:
                res = await page.evaluate(PROCESS_ONE_JS, {"webhook": webhook, "item": item, "doLaunch": False})
                if res.get("status") == "saved_existing":
                    stats["saved_after_launch"] += 1
                await asyncio.sleep(delay)

        await browser.close()

    return stats


def main() -> int:
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", default=".run/latest", help="каталог с batch_*.json от fetch.py")
    parser.add_argument("--storage-state", required=True, help="путь к Playwright storage_state (авторизованный профиль)")
    parser.add_argument("--max-launch", type=int, default=50, help="дневной бюджет launchCopilot")
    parser.add_argument("--retry-wait", type=int, default=120, help="сек. ожидания перед перепроверкой запущенных")
    parser.add_argument("--delay", type=float, default=0.5, help="пауза между вызовами, сек")
    parser.add_argument("--headed", action="store_true", help="показать окно браузера (для отладки/первого прогона)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="ничего не пишет в CRM и не зовёт launchCopilot (не тратит бюджет/лимит); "
             "только getCopilotTranscript (read-only) и отчёт что БЫ произошло",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir)
    if not run_dir.exists():
        log.error("run-dir не найден: %s (сначала fetch.py)", run_dir)
        return 2

    try:
        stats = asyncio.run(
            run_pass_async(
                run_dir,
                Path(args.storage_state),
                args.max_launch,
                args.retry_wait,
                args.delay,
                headless=not args.headed,
                dry_run=args.dry_run,
            )
        )
    except GapPassError as exc:
        log.error("%s", exc)
        print(json.dumps({"status": "stopped", "reason": str(exc)}, ensure_ascii=False))
        return 3

    summary = {
        "status": "limit_hit" if stats["limit_hit"] else "ok",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **stats,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if stats["limit_hit"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
