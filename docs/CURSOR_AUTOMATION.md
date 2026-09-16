# Cursor Cloud Automation — lead-analysis

Один прогон: **fetch → triage → LLM только по review-пакетам** (~50 лидов вместо ~100, компактный JSON).

## 1. Секреты

[cursor.com/dashboard](https://cursor.com/dashboard) → **Cloud Agents** → **Secrets**:

| Имя | Тип | Значение |
|-----|-----|----------|
| `BITRIX24_WEBHOOK_URL` | Runtime Secret | входящий вебхук prod `laskov-partners`, со слэшем в конце |
| `LEAD_ANALYSIS_MODE` | Environment Variable | `dry-run` для проверки, потом `live` |

Права: `crm`, `telephony`, `call`, `user`, `im`, `imopenlines`, `imbot`, `disk`, `task`.

```bash
curl -s "${BITRIX24_WEBHOOK_URL}scope.json" | python3 -m json.tool
```

## 2. Сеть

- `Default + allowlist`
- `laskov-partners.bitrix24.ru`

## 3. Environment

| Поле | Значение |
|------|----------|
| Install Script | `python3 -m pip install -r requirements.txt` |
| Start Script | *(пусто)* |
| Secrets | `BITRIX24_WEBHOOK_URL`, `LEAD_ANALYSIS_MODE=dry-run` |
| SSH-ключ | **не нужен** — удалить, если остался |

## 4. Automation

| Поле | Значение |
|------|----------|
| Repository | `hemonc-team/lead-analysis-skill`, ветка `main` |
| Schedule | 09:50 МСК (`50 6 * * *` UTC) — включить после успешного live |
| Model | **Composer / fast** достаточно для review-пакетов; не Opus на весь прогон |

### Agent instructions

```text
Ежедневный анализ лидов Bitrix24, портал laskov-partners. Экономия токенов — приоритет.

1) Сбор и триаж (код, не LLM):
   pip install -r requirements.txt
   python3 fetch.py --report-date=yesterday --out-dir .run/latest
   python3 triage.py --in-dir .run/latest
   При ошибке — стоп, в Bitrix ничего не писать.

2) Прочитать SKILL.md (раздел «Бюджет контекста» и «Пайплайн»).
   Прочитать triage.json и auto_results.jsonl.
   batch_*.json НЕ открывать.

3) LLM только по .run/latest/review/review_NN.json — по одному файлу:
   - разобрать 5 лидов (4 вопроса + классификация + crm_updates)
   - дописать judgements.jsonl
   - не перечитывать прошлые review-файлы и не копить их в контекст
   references/field_rules.md — один раз при первом live-поле
   (F04 «Кто рекомендовал» из SOURCE_ID уже в triage.crm_auto_by_id —
    в crm_updates класть только то, что ясно из разговора)

4) После judgements — ТОЛЬКО код (не руками Bitrix write-API):
   python3 scripts/deliver_report.py
   Режим берётся из env LEAD_ANALYSIS_MODE (dry-run | live).
   Скрипт: CRM (пустые UF + F04) → report.txt → в live: chat31598, кнопки бота 23032, leads_log.csv.
   ⛔ ЗАПРЕЩЕНО: crm.lead.update, im.message.add, imbot.message.add вручную.
   ⛔ Не собирать отчёт текстом вместо deliver_report.py.

5) Язык отчёта — для менеджеров, без техтерминов (UF, REST, батч, R01…).
   Самопроверка — в лог прогона / crm_stats.json, не в chat31598.

6) Вебхук только из env BITRIX24_WEBHOOK_URL. .run/ не коммитить.

Итог в Run History: report_date, total, auto/llm, 🔴/⚠️/🎧, CRM_updated, recommend_added, openlines_scope, ошибки.
Ориентир токенов: полный день < 1M (не 7M+).
```

## 5. Тестовый прогон

1. `LEAD_ANALYSIS_MODE=dry-run`
2. **Run now**
3. В логе: `triage: auto=… llm=…`, `CRM pending`, `recommend_added≈…`, `openlines_scope=True`
4. Файлы: `triage.json` (есть `crm_auto_by_id`), `judgements.jsonl`, `report.txt`, `crm_stats.json`
5. `chat31598` пуст при dry-run
6. `live` → в отчёте блок «Кто рекомендовал: … добавил N» > 0 при наличии Telegram/VK/IG лидов → расписание

## 6. Типичные проблемы

| Симптом | Причина |
|---------|---------|
| >3M токенов | агент читал batch_*.json или копил все review в контекст |
| `openlines_scope=false` | нет `im` + `imopenlines` |
| Нет кнопок | нет `imbot` |
| Двойной отчёт | старый прогон на другом аккаунте / VM |
| Обновлений в карточках: 0 | агент писал CRM руками и пропускал шаг → нужен `scripts/deliver_report.py` |
| Кто рекомендовал пусто | нет `crm_auto_by_id` / не вызван deliver; проверить `SOURCE_ID` в SELECT |
