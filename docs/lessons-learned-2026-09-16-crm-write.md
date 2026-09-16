# 2026-09-16: CRM write path + F04

См. полный lesson: `Laskov-Clinic/docs/lessons-learned/2026-09-16-lead-analysis-crm-write-path.md`.

Кратко: F04 и остальные UF пишет только `scripts/deliver_report.py` (после judgements). Агент не вызывает `crm.lead.update` руками.
