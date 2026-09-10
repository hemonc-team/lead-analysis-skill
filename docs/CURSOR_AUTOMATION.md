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
   references/report_format.md — один раз перед отчётом

4) Собрать отчёт из auto_results + judgements. Шаблон — report_format.md.
   LEAD_ANALYSIS_MODE:
   dry-run — только файлы + лог;
   live — crm.lead.update, chat31598, кнопки бота 23032, leads_log.csv.

5) Язык отчёта — для менеджеров, без техтерминов (UF, REST, батч, R01…).
   Самопроверка — в лог прогона, не в chat31598.

6) Вебхук только из env. .run/ не коммитить.

Итог в Run History: report_date, total, auto/llm, 🔴/⚠️/🎧, openlines_scope, ошибки.
Ориентир токенов: полный день < 1M (не 7M+).
```

## 5. Тестовый прогон

1. `LEAD_ANALYSIS_MODE=dry-run`
2. **Run now**
3. В логе: `triage: auto=… llm=… review_batches=…`, `openlines_scope=True`
4. Файлы: `triage.json`, `auto_results.jsonl`, `review/review_*.json`, `judgements.jsonl`
5. `chat31598` пуст при dry-run
6. `live` → проверить чат и карточки → расписание

## 6. Типичные проблемы

| Симптом | Причина |
|---------|---------|
| >3M токенов | агент читал batch_*.json или копил все review в контекст |
| `openlines_scope=false` | нет `im` + `imopenlines` |
| Нет кнопок | нет `imbot` |
| Двойной отчёт | старый прогон на другом аккаунте / VM |
