# lead-analysis-skill

Ежедневный анализ лидов Bitrix24 (клиника доктора Ласкова). Один прогон: собрать факты
из Bitrix REST → разобрать по правилам → заполнить CRM → отчёт в `chat31598` человеческим
языком.

## Что внутри

| Файл | Зачем |
|------|--------|
| `fetch.py` | Сбор фактов (фаза 1): лиды A/B/C за день отчёта, звонки, переписка, таймлайн, текст разговора |
| `SKILL.md` | Правила суждения (фаза 2) и формат отчёта |
| `references/field_rules.md` | Правила полей CRM, включая **ТегиКЦ** |
| `references/doctor_names.md` | Справочник врачей (R05) |
| `references/feedback_form_url.md` | Архив: Google-форма выведена; канал — бот в chat31598 |
| `docs/CURSOR_AUTOMATION.md` | Настройка и тестовый прогон в Cursor Cloud |

## Архитектура

```
fetch.py --report-date=yesterday   →  .run/latest/{manifest,catalogs,batch_XX}.json
агент по SKILL.md                  →  поля CRM + отчёт в chat31598 + leads_log.csv
```

Всё в одном прогоне, на любой машине с доступом к порталу: Cursor Cloud, локальный Mac, VM.
DWH в цепочке анализа не участвует.

Единственная внешняя зависимость — `gpt_transcripts.py` на DWH (09:00 и 18:00 МСК): он кладёт
текст разговора в `UF_CRM_GPT_TRANSCRIPT`, скилл поле только читает.

## Запуск

```bash
pip install -r requirements.txt
export BITRIX24_WEBHOOK_URL='https://laskov-partners.bitrix24.ru/rest/<id>/<code>/'

python3 fetch.py --report-date=yesterday          # весь день
python3 fetch.py --report-date=yesterday --limit 5 --out-dir .run/smoke   # проба
```

Дальше агент читает `SKILL.md` и разбирает батчи из `.run/latest`.

## Права вебхука

`crm`, `telephony` (+`call`), `user`, `im`, `imopenlines`, `imbot`, `disk`, `task`.

Без `im` + `imopenlines` в батчах не будет текстов переписки (`openlines_scope=false`).
Без `imbot` не отправятся кнопки «❌ Не согласен».

Секрет — только в env / Cursor Secrets / vault Laskov-Clinic-Secrets. Не коммитить.

## Данные пациентов

`.run/` и `logs/` содержат ПДн: в git не попадают (`.gitignore`), наружу не пересылаются,
в чат не печатаются. В логе прогона — только ID и счётчики.

## Связанные репозитории

- [dwh-etl](https://github.com/hemonc-team/dwh-etl) — `gpt_transcripts` (текст разговоров в UF)
- [claude-b24](https://github.com/hemonc-team/claude-b24) — зеркало истории, lead-feedback бот
- [b24](https://github.com/hemonc-team/b24) — плейбуки и расследования

## Org

[hemonc-team](https://github.com/hemonc-team) · портал prod: `laskov-partners`
