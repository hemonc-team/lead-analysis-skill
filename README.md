# lead-analysis-skill

Claude Code / Cursor skill: ежедневный анализ лидов Bitrix24 (клиника доктора Ласкова).

## Что внутри

| Файл | Зачем |
|------|--------|
| `SKILL.md` | Основной скилл (группы A/B/C, шаги 4A–8, отчёт в chat31598) |
| `references/field_rules.md` | Правила полей CRM, включая **ТегиКЦ** |
| `references/doctor_names.md` | Справочник врачей (R05) |
| `references/feedback_form_url.md` | Архив: Google-форма выведена; канал — бот в chat31598 |

## Архитектура

1. **Фаза 1** (DWH, `dwh-etl`): `lead_analysis_fetch.py` → JSON батчи `latest/`
2. **Фаза 2** (этот скилл): суждение по батчам → CRM + отчёт + CSV

Фаза 2 **не** на DWH в РФ (Claude/OAuth). Рантаймы: Cursor / Automations, GCP VM (v2).

## Установка

```bash
# Claude Code
cp -R . ~/.claude/skills/lead-analysis/

# или клон в skills
git clone https://github.com/hemonc-team/lead-analysis-skill.git ~/.claude/skills/lead-analysis
```

Секреты: `BITRIX24_WEBHOOK_URL` в env (см. vault Laskov-Clinic-Secrets). Не коммитить.

## Связанные репозитории

- [dwh-etl](https://github.com/hemonc-team/dwh-etl) — fetch + `gpt_transcripts`
- [claude-b24](https://github.com/hemonc-team/claude-b24) — зеркало истории, lead-feedback бот
- [b24](https://github.com/hemonc-team/b24) — плейбуки и расследования

## Org

[hemonc-team](https://github.com/hemonc-team) · портал prod: `laskov-partners`
