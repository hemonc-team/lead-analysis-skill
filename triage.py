#!/usr/bin/env python3
"""Фаза 1.5: детерминированный триаж + компактные пакеты для LLM.

После fetch.py:
  python3 triage.py --in-dir .run/latest

Пишет:
  triage.json          — сводка, счётчики, structural по id
  auto_results.jsonl   — лиды без LLM (ok / info / gap_only)
  review/review_NN.json — по 5 лидов, компакт, только needs_llm

Агент читает ТОЛЬКО review/*.json по одному файлу за раз.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from rules_core import (
    auto_classification,
    build_auto_crm_updates,
    compact_lead_for_review,
    decide_auto_tier,
    structural_checks,
)

log = logging.getLogger("lead_analysis_triage")
MSK = ZoneInfo("Europe/Moscow")
REVIEW_BATCH_SIZE = 5


def load_leads(in_dir: Path) -> tuple[dict, dict, list[dict]]:
    manifest = json.loads((in_dir / "manifest.json").read_text(encoding="utf-8"))
    catalogs = json.loads((in_dir / "catalogs.json").read_text(encoding="utf-8"))
    leads: list[dict] = []
    for path in sorted(in_dir.glob("batch_*.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        leads.extend(batch.get("leads") or [])
    return manifest, catalogs, leads


def run_triage(in_dir: Path, review_size: int) -> str:
    manifest, catalogs, leads = load_leads(in_dir)
    if not leads:
        raise RuntimeError(f"нет лидов в {in_dir}")

    auto_lines: list[dict] = []
    review_leads: list[dict] = []
    structural_by_id: dict[str, dict] = {}
    operator_by_id: dict[str, str] = {}
    crm_auto_by_id: dict[str, dict] = {}
    tier_counts: dict[str, int] = {}

    for lead in leads:
        lid = int(lead["id"])
        structural = structural_checks(lead, catalogs)
        structural_by_id[str(lid)] = structural
        assigned = lead.get("assigned_by_id")
        if assigned is not None and str(assigned).strip() and str(assigned) not in ("None", "0"):
            operator_by_id[str(lid)] = str(assigned)
        auto_crm = build_auto_crm_updates(lead)
        if auto_crm:
            crm_auto_by_id[str(lid)] = auto_crm
        tier = decide_auto_tier(lead, catalogs, structural)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

        if tier == "needs_llm":
            review_leads.append(compact_lead_for_review(lead, catalogs, structural))
            continue

        auto_lines.append({
            "id": lid,
            "tier": tier,
            "classification": auto_classification(tier, lead, structural),
            "groups": lead.get("groups") or [],
            "operator_id": lead.get("assigned_by_id"),
            "structural": {
                "uf_gap": structural.get("uf_gap"),
                "flag_c": structural.get("flag_c"),
                "flag_e": structural.get("flag_e"),
                "r02_human": structural.get("r02_human"),
                "r06_human": structural.get("r06_human"),
            },
            "fields": compact_lead_for_review(lead, catalogs, structural)["fields"],
            "crm_updates": auto_crm or {},
            "crm_updates_auto": auto_crm or {},
        })

    review_dir = in_dir / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    for old in review_dir.glob("review_*.json"):
        old.unlink()

    review_paths: list[str] = []
    for i in range(0, len(review_leads), review_size):
        chunk = review_leads[i : i + review_size]
        n = i // review_size + 1
        path = review_dir / f"review_{n:02d}.json"
        path.write_text(
            json.dumps({
                "batch": n,
                "report_date": manifest.get("report_date"),
                "leads": chunk,
                "instructions": (
                    "Разобрать каждый лид. Результат — одна строка в judgements.jsonl. "
                    "Не перечитывать другие review-файлы."
                ),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        review_paths.append(path.name)

    auto_path = in_dir / "auto_results.jsonl"
    with auto_path.open("w", encoding="utf-8") as fh:
        for row in auto_lines:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "run_at": datetime.now(MSK).isoformat(),
        "report_date": manifest.get("report_date"),
        "counts": manifest.get("counts"),
        "openlines_scope": manifest.get("openlines_scope"),
        "uf_gap_stats": manifest.get("uf_gap"),
        "total_leads": len(leads),
        "tiers": tier_counts,
        "llm_leads": len(review_leads),
        "auto_leads": len(auto_lines),
        "review_batches": len(review_paths),
        "review_batch_size": review_size,
        "structural_by_id": structural_by_id,
        "operator_by_id": operator_by_id,
        "crm_auto_by_id": crm_auto_by_id,
        "crm_auto_count": len(crm_auto_by_id),
        "token_note": (
            "Агент: читать review/review_NN.json по одному; после каждого файла дописать "
            "judgements.jsonl; batch_*.json и SKILL целиком повторно не открывать. "
            "CRM пишет scripts/deliver_report.py (не руками crm.lead.update)."
        ),
    }
    (in_dir / "triage.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    pct = round(100.0 * len(auto_lines) / len(leads), 1) if leads else 0
    return (
        f"triage: total={len(leads)} auto={len(auto_lines)} ({pct}%) "
        f"llm={len(review_leads)} review_batches={len(review_paths)} "
        f"tiers={tier_counts} out={in_dir}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Triage + compact review packs for lead-analysis")
    p.add_argument("--in-dir", default=".run/latest", help="каталог после fetch.py")
    p.add_argument("--review-size", type=int, default=REVIEW_BATCH_SIZE, help="лидов в review-пакете")
    args = p.parse_args(argv)

    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    in_dir = Path(args.in_dir)
    if not (in_dir / "manifest.json").exists():
        print(f"нет manifest.json в {in_dir} — сначала fetch.py", file=sys.stderr)
        return 2

    try:
        print(run_triage(in_dir, max(1, args.review_size)))
    except Exception as exc:  # noqa: BLE001
        log.exception("triage failed")
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
