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

## 5. Дорасшифровка (gap pass) — UF-gap лиды через браузер

Контекст: REST (`crm.activity.call.getTranscript` / крон `gpt_transcripts` на DWH)
только читает то, что Bitrix CoPilot уже сам расшифровал в свой AI-store. Если
там пусто — REST это не починит, это read-only. Раньше пробел закрывал личный
скилл М. Ласкова: заходил в карточку лида в браузере и жал «Оценить звонок» —
это два внутренних AJAX-метода Bitrix (`crm.timeline.ai.getCopilotTranscript` /
`...launchCopilot`), которых нет в публичном REST. `scripts/gap_transcript_pass.py`
делает то же самое через Playwright, но:

- берёт в очередь **только** лидов, которых `fetch.py` уже пометил `uf_gap=true`
  на сегодняшний отчёт (не сканирует всю базу);
- ограничен дневным бюджетом `--max-launch` (по умолчанию 50 — из расчёта
  «не больше, чем расходовали до сожжения лимита в июле-августе»);
- останавливается сам (circuit breaker) после 3 подряд `AI_ENGINE_LIMIT_EXCEEDED`
  и не пытается долбить движок дальше — это ровно то, что сожгло лимиты
  01.08 и повторно 09.09 (см. `docs/investigations/2026-09-07-copilot-auto-assessment-full-dossier.md`
  и `2026-09-10-copilot-post-limit-reset-check.json` в репо `b24`).

### 5.1. Открытый вопрос: авторизация без участия человека

Скрипту нужен уже залогиненный в Bitrix24 браузерный профиль (Playwright
`storage_state` — куки + localStorage), полученный один раз вручную:

```bash
python3 -c "
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=False)
    ctx = b.new_context()
    page = ctx.new_page()
    page.goto('https://laskov-partners.bitrix24.ru/crm/lead/list/')
    input('Залогинься в открывшемся окне, потом жми Enter здесь...')
    ctx.storage_state(path='bitrix_state.json')
    b.close()
"
```

Дальше `bitrix_state.json` нужно доставить в Cursor Cloud Automation. Здесь
есть неопределённость, которую **нужно проверить перед боевым включением**:

- Cursor Cloud **Automations** (в отличие от интерактивных Cloud Agent-сессий)
  по документации Cursor запускаются в **свежем эфемерном контейнере на
  каждый триггер** — то есть браузерный профиль, залогиненный в прошлом
  прогоне, по умолчанию **не переживает** следующий scheduled-запуск. Общая
  фраза Cursor про «browser state persists between agent sessions в рамках
  workspace» относится к интерактивным сессиям, не гарантированно — к
  автоматизациям по расписанию.
- Практический обход: `bitrix_state.json` не хранить в контейнере, а класть
  как **Runtime Secret** (`BITRIX24_STORAGE_STATE`, содержимое файла целиком)
  и в начале прогона записывать его во временный файл перед запуском
  `gap_transcript_pass.py --storage-state`. Это решает «эфемерность
  контейнера», но не решает то, что сама Bitrix-сессия (кука) у Bitrix24
  может протухнуть или быть отозвана порталом (смена IP на каждый прогон
  Cursor Cloud может триггерить проверку безопасности / повторный логин).
- Скрипт **намеренно не пытается логиниться сам** при невалидной сессии —
  просто останавливается с понятной ошибкой в Run History. Это нужно
  мониторить: если видим `status: stopped` несколько дней подряд — значит,
  сессия протухла, нужен ручной повторный захват `storage_state` (то же
  самое, разово, ~1 раз в 2-4 недели по опыту — фактическую частоту
  замера ещё нет, см. TODO ниже).
- **Ещё не проверено живьём**: держит ли Bitrix24 сессию столько, сколько
  нужно между прогонами (раз в сутки), и не блокирует ли повторные заходы с
  другого (облачного) IP каждый раз. Это надо протестировать отдельным
  прогоном перед включением по расписанию.

### 5.2. Интеграция в дневной прогон

Добавить шагом 1.5 (после `fetch.py`, перед разбором батчей агентом), только
если `manifest.json.uf_gap.leads_uf_gap > 0`:

```bash
python3 scripts/gap_transcript_pass.py --run-dir .run/latest --max-launch 50 \
    --storage-state /tmp/bitrix_state.json
```

Прогон **необязательный**: если он упал (`status: stopped` / `session_error`) —
агент продолжает обычный разбор с тем, что есть, лиды без расшифровки уходят
в отчёт с `⚠️`, как и раньше. Дорасшифровка — это попытка уменьшить `uf_gap`,
а не блокирующий шаг пайплайна.

Если `status: limit_hit` — это сигнал, что лимит BitrixGPT снова где-то
исчерпан (не обязательно нашей дорасшифровкой) — стоит написать в чат алертов
(#24766), а не тихо повторять прогон.

## 6. Что может пойти не так

| Симптом | Причина |
|---------|---------|
| `openlines_scope=false` в логе | у вебхука нет `im` + `imopenlines` — переписка в анализ не попадёт |
| Кнопок «Не согласен» нет | нет права `imbot` (`insufficient_scope`) |
| Сетевые ошибки на портал | домен `laskov-partners.bitrix24.ru` не в allowlist |
| Прогон долгий | лимит портала 2 запроса/сек; ~4 сек на лид, 100 лидов ≈ 7 минут |
| Двойной отчёт в чате | параллельно жив старый прогон (другой аккаунт / routine) — выключить |
