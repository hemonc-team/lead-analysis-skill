---
name: lead-analysis
description: 'Ежедневный анализ лидов Bitrix24 (laskov-partners). fetch.py → triage.py → LLM только по review-пакетам. Отчёт в chat31598 человеческим языком. День отчёта = вчера МСК.'
---

# Анализ лидов Б24

## Контур

| Переменная | Назначение |
|---|---|
| `BITRIX24_WEBHOOK_URL` | входящий REST prod, слэш в конце |
| `LEAD_ANALYSIS_MODE` | `dry-run` / `live` |

Scope: `crm`, `telephony`, `call`, `user`, `im`, `imopenlines`, `imbot`, `disk`, `task`.

Отчёт → `chat31598`. CSV → Disk «Аналитика КЦ» / `leads_log.csv`.
Кнопки «❌ Не согласен» → бот 23032 (`CLIENT_ID=ai_review_bot`).

Кодировка REST: **Python + requests + UTF-8**. PowerShell запрещён.

---

## ⛔ БЮДЖЕТ КОНТЕКСТА (главное правило)

**Цель:** один прогон ≈ сотни тысяч токенов, не миллионы.

```
ЗАПРЕЩЕНО:
  • читать batch_*.json (сырые батчи — только для fetch/triage)
  • держать в голове/контексте прошлые review-файлы
  • перечитывать SKILL.md целиком после старта
  • повторять crm.lead.list / voximplant / imopenlines
  • Chrome / launchCopilot / getCopilotTranscript

ОБЯЗАТЕЛЬНО:
  • после каждого review/review_NN.json → judgements.jsonl (append)
  • в работе только: triage.json + ОДИН текущий review-файл
  • финальный отчёт собирать из triage + auto_results + judgements
```

---

## Пайплайн (строго по порядку)

### 0. Сбор

```bash
pip install -r requirements.txt
python3 fetch.py --report-date=yesterday --out-dir .run/latest
python3 triage.py --in-dir .run/latest
```

При падении fetch/triage — стоп, в Bitrix ничего не писать.

### 1. Сводка (один раз)

Прочитать `.run/latest/triage.json`:
- `report_date`, `counts`, `openlines_scope`, `uf_gap_stats`
- `tiers` (сколько auto / llm)
- `llm_leads`, `review_batches`

Прочитать `.run/latest/auto_results.jsonl` — готовые классификации без LLM.

**Не открывать** `batch_*.json`.

### 2. LLM-разбор (только review-пакеты)

Для каждого `.run/latest/review/review_NN.json` **по порядку**:

1. Открыть **только этот** файл (5 лидов, уже компакт + structural-флаги).
2. На каждый лид — суждение (см. ниже).
3. Дописать строку в `.run/latest/judgements.jsonl`.
4. Закрыть файл. **Не возвращаться** к нему.

Формат строки `judgements.jsonl`:

```json
{
  "id": 92460,
  "classification": "loss|action|ok|info|gap_only",
  "emoji": "🔴|⚠️|✅|ℹ️|🎧",
  "patient_wanted": "…",
  "what_happened": "…",
  "why_important": "…",
  "action": "…",
  "direction": "онкология|гематология|…",
  "crm_updates": {"UF_CRM_…": "enum_id или текст"},
  "crm_skip_reason": "теги уже заполнены / сомнение",
  "in_action_list": true
}
```

`in_action_list=true` только для 🔴 и ⚠️ (карточка в «Что сделать сегодня»).

### 3. Сборка отчёта

Объединить:
- `auto_results.jsonl` → счётчики ✅/ℹ️/🎧
- `judgements.jsonl` → 🔴/⚠️/остальные

Шаблон и язык — `references/report_format.md` (прочитать **один раз** перед отправкой).

### 4. Live / dry-run

| Режим | Поведение |
|---|---|
| `dry-run` | файлы отчёта + лог; Bitrix не трогать |
| `live` | `crm.lead.update`, отчёт в chat31598, кнопки бота, CSV на Disk |

Правила полей CRM — `references/field_rules.md`.
ФИО врачей — `references/doctor_names.md`.

---

## Суждение по лиду (review-пакет)

Structural-флаги **уже посчитаны** в `lead.structural` — не пересчитывать, использовать в тексте.

### 3-EXIST (обязательно)

⛔ Не писать «коммуникации не было», пока не проверены:
- `calls` / `chats`
- `exist_check.only_timeline` и `timeline_note`

Инцидент 21.07: #89342, #87944, #87800 — звонок был, в отчёте «пусто».

### R01 — расшифровка

- Текст только в `transcript` (поле UF, наполняет `gpt_transcripts.py` на DWH).
- `structural.uf_gap=true` → не судить о **содержании** речи.
  - Если нет других поводов → `gap_only`, только номер в блоке 🎧.
  - Если есть R02/R06/брошенный запрос → карточка, в «Что произошло» указать «записи разговора нет».
- `launchCopilot` / Chrome — **запрещено** (#24604).

### 4 вопроса (для 🔴/⚠️ — в отчёт)

1. Что хотел пациент?
2. Что произошло? (звонки, переписка, договорённости)
3. Почему это важно? (перевести structural: `r02_human`, `r06_human`, f03)
4. Что сделать? (одно действие)

### Классификация

| emoji | Когда |
|---|---|
| 🔴 loss | хотел записаться/купить, брошен без follow-up |
| ⚠️ action | R02/R06/F03, неверная стадия, дата контакта, процесс |
| 🎧 gap_only | только uf_gap, иных поводов нет |
| ℹ️ info | информационный запрос, запись не планировалась |
| ✅ ok | всё корректно |

⛔ Не помечать «дубль» без чтения всей переписки текущего лида (инцидент #64274).

### CRM (live)

Писать только если пусто и **очевидно** из разговора (`field_rules.md`):
ТегиКЦ, Вариация, Препарат, Статус детальный, Задача.

Препарат не в справочнике → не писать «Не применимо», зафиксировать в лог прогона.

### CSV (все лиды)

```
дата,лид_id,ссылка,оператор,направление,классификация,флаг_C,флаг_E,тег_кц_AI,препарат_AI,статус_детальный_AI,задача_AI,согласие_с_оценкой
```

---

## Справочник правил (кратко)

Полный текст — `references/field_rules.md`. Коды **не** попадают в chat31598.

| Код | Суть |
|---|---|
| R01 | длинный звонок без текста → 🎧, речь не судить |
| R02 | рос. номер + B/C → ≥3 исходящих касания (≥1 TG/Max + ≥1 звонок) |
| R03 | ссылка 1С с записью → записан |
| R06 | стадия «Запись»/«Лист ожидания» без ссылки 1С |
| F03 | «Отказ пациента» только при явном отказе |
| F01 | препарат упомянут → не «Не применимо» |

R02/R06 для auto-лидов уже в `triage.py` / `auto_results.jsonl`.

---

## Обратная связь

Текстовый `[FEEDBACK]` выведен. Несогласия — бот 23032 + `lead-feedback-apply` (09:26).
Lead-analysis на шаге 8 ничего не обрабатывает.

---

## Итог в Run History

Кратко: `report_date`, проверено, auto/llm, 🔴/⚠️, 🎧, что записано в CRM, ошибки, `openlines_scope`.
