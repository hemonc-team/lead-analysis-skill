# Cursor Cloud Automation — lead-analysis

Ежедневный прогон анализа лидов в облаке Cursor. Ни SSH, ни DWH, ни VPN не нужны:
агент сам собирает данные из Bitrix REST и сам их разбирает.

## 1. Секреты

[cursor.com/dashboard](https://cursor.com/dashboard) → **Cloud Agents** → **Secrets**:

| Имя | Тип | Значение |
|-----|-----|----------|
| `BITRIX24_WEBHOOK_URL` | Runtime Secret | входящий вебхук prod `laskov-partners`, со слэшем в конце |
| `LEAD_ANALYSIS_MODE` | Environment Variable | `dry-run` для проверки, потом `live` |

Права вебхука: `crm`, `telephony`, `call`, `user`, `im`, `imopenlines`, `imbot`, `disk`, `task`.

Проверка прав до боевого прогона:

```bash
curl -s "${BITRIX24_WEBHOOK_URL}scope.json" | python3 -m json.tool
```

В списке должны быть `imopenlines` (тексты переписки) и `imbot` (кнопки обратной связи).

## 2. Сеть

Cloud Agents → **Security** → Network access:

- `Default + allowlist`
- в allowlist: `laskov-partners.bitrix24.ru`

Для старта допустимо `Allow all network access`, потом сузить.

## 3. Automation

| Поле | Значение |
|------|----------|
| Name | `Lead-analysis daily` |
| Repository | `hemonc-team/lead-analysis-skill`, ветка `main` |
| Trigger | Schedule, ежедневно **09:50 МСК** (cron UTC `50 6 * * *`) |
| Setup | `pip install -r requirements.txt` |

Расписание — после `gpt_transcripts` на DWH (09:00 МСК) и после `lead-feedback-apply` (09:26).

### Agent instructions

```text
Ежедневный анализ лидов Bitrix24, портал laskov-partners (prod). Один прогон.

1) Собрать данные:
   pip install -r requirements.txt
   python3 fetch.py --report-date=yesterday --out-dir .run/latest
   Прочитать .run/latest/manifest.json: report_date, counts (A/B/C/unique),
   openlines_scope, uf_gap. Если fetch упал — остановиться, написать причину
   в лог прогона, в Bitrix ничего не писать.

2) Прочитать SKILL.md, references/field_rules.md, references/doctor_names.md.

3) Разобрать ВСЕ файлы .run/latest/batch_*.json подряд, по 10 лидов:
   шаги 4A → 4F → 5 из SKILL. Ни один батч не пропускать.
   Chrome, launchCopilot, повторные crm.lead.list / voximplant не использовать.

4) Режим из LEAD_ANALYSIS_MODE (по умолчанию live):
   dry-run — только файлы отчёта, Bitrix не трогать;
   live — crm.lead.update по field_rules, один отчёт в chat31598 по шаблону
   из Шага 6, кнопки бота 23032 под карточками (если есть право imbot),
   дописать leads_log.csv на Bitrix Disk.

5) Отчёт в chat31598 — человеческим языком, по шаблону Шага 6.
   Без техтерминов: никаких UF, UF-gap, REST, scope, батчей, кодов правил,
   имён полей UF_CRM_*. Перед отправкой пройти самопроверку из SKILL
   и сохранить её в лог прогона, а не в чат.

6) Вебхук только из переменной окружения. Не искать .env, не печатать секрет.
   Каталог .run/ не коммитить: там персональные данные пациентов.

В конце — краткий итог в Run History: дата отчёта, сколько проверено,
сколько к действию, сколько без расшифровки, что записано в карточки, ошибки.
```

## 4. Тестовый прогон

1. `LEAD_ANALYSIS_MODE=dry-run`.
2. **Run now**.
3. В логе прогона проверить: `openlines_scope=True`, счётчики сходятся с `manifest.counts`,
   отчёт составлен и в нём нет запрещённых терминов.
4. В `chat31598` при `dry-run` не должно появиться ничего.
5. Переключить `LEAD_ANALYSIS_MODE=live`, снова **Run now**, проверить чат и карточки.
6. Включить расписание.

## 5. Что может пойти не так

| Симптом | Причина |
|---------|---------|
| `openlines_scope=false` в логе | у вебхука нет `im` + `imopenlines` — переписка в анализ не попадёт |
| Кнопок «Не согласен» нет | нет права `imbot` (`insufficient_scope`) |
| Сетевые ошибки на портал | домен `laskov-partners.bitrix24.ru` не в allowlist |
| Прогон долгий | лимит портала 2 запроса/сек; ~4 сек на лид, 100 лидов ≈ 7 минут |
| Двойной отчёт в чате | параллельно жив старый прогон (другой аккаунт / routine) — выключить |
