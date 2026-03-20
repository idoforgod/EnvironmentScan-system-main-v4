#!/usr/bin/env python3
"""
Source Auto-Promoter — Automatic Viable Candidate Promotion
=============================================================
Automatically promotes viable exploration candidates to expansion tier
in sources.yaml when they pass viability checks.

This script implements the auto-promotion pipeline that was previously
missing: viable candidates were identified but never promoted without
manual user intervention.

Design Principle:
    "계산은 Python이, 판단은 LLM이" — The viability judgment is made by
    the LLM evaluator. The promotion mechanics (dedup check, YAML update,
    history recording) are Python-enforced and deterministic.

Pipeline Position:
    exploration-orchestrator → candidates-{date}.json (with viable list)
                                      ↓
    source_auto_promoter.py promote (THIS) → sources.yaml updated
                                      ↓
    exploration_gate.py post → records proof including promotions

Usage (CLI):
    python3 env-scanning/core/source_auto_promoter.py promote \\
        --candidates env-scanning/wf1-general/exploration/candidates/exploration-candidates-2026-03-18.json \\
        --sources env-scanning/config/sources.yaml \\
        --excluded env-scanning/wf1-general/exploration/excluded-sources.json \\
        --history-dir env-scanning/wf1-general/exploration/history \\
        --sot env-scanning/config/workflow-registry.yaml \\
        --date 2026-03-18 \\
        --output env-scanning/wf1-general/exploration/promotion-report-2026-03-18.json

Usage (importable):
    from core.source_auto_promoter import promote_viable_candidates

Exit codes:
    0 = SUCCESS (promotions applied or no viable candidates)
    1 = ERROR (critical failure)

Version: 1.0.0
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VERSION = "1.0.0"
PROMOTER_ID = "source_auto_promoter.py"

# Minimum quality score for auto-promotion (safety floor)
MIN_QUALITY_SCORE = 0.5

# Tier that promoted sources are assigned to
PROMOTION_TIER = "expansion"


# ---------------------------------------------------------------------------
# Core: Promote Viable Candidates
# ---------------------------------------------------------------------------

def promote_viable_candidates(
    candidates_path: str,
    sources_yaml_path: str,
    excluded_sources_path: str | None = None,
    history_dir: str | None = None,
    sot_path: str | None = None,
    scan_date: str = "",
    output_path: str | None = None,
) -> dict[str, Any]:
    """
    Automatically promote viable exploration candidates to expansion tier.

    Process:
        1. Load viable candidates from candidates file
        2. Load excluded sources list (WF overlap prevention)
        3. Load existing sources from sources.yaml (duplicate prevention)
        4. For each viable candidate:
           a. Check not in excluded sources
           b. Check not already in sources.yaml
           c. Check quality score >= minimum threshold
           d. Add to sources.yaml with tier: expansion
           e. Record in exploration history as approved
        5. Write promotion report

    Args:
        candidates_path: Path to exploration-candidates-{date}.json
        sources_yaml_path: Path to sources.yaml
        excluded_sources_path: Path to excluded-sources.json
        history_dir: Path to exploration history directory
        sot_path: Path to workflow-registry.yaml (for auto_promotion_scans)
        scan_date: Scan date (YYYY-MM-DD)
        output_path: Optional path to write promotion report

    Returns:
        Promotion report dictionary
    """
    if yaml is None:
        raise ImportError("PyYAML is required: pip install pyyaml")

    date = scan_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # 1. Load candidates
    cand_path = Path(candidates_path)
    if not cand_path.exists():
        return _build_report("SKIPPED", "Candidates file not found", date, [])

    with open(cand_path, "r", encoding="utf-8") as f:
        cand_data = json.load(f)

    viable_raw = cand_data.get("viable_candidates", [])

    # Handle schema where viable_candidates is an int count, not a list.
    # In this case, the actual candidate objects are in the "candidates" key.
    if isinstance(viable_raw, (int, float)):
        candidates_list = cand_data.get("candidates", [])
        if isinstance(candidates_list, list):
            viable_raw = [
                c for c in candidates_list
                if isinstance(c, dict) and c.get("scan_status", "viable") == "viable"
            ]
        else:
            viable_raw = []

    if not viable_raw:
        return _build_report("SKIPPED", "No viable candidates to promote", date, [])

    # Normalize: handle both string-only (old format) and dict formats
    skipped_legacy: list[dict] = []
    viable: list[dict] = []
    for item in viable_raw:
        if isinstance(item, str):
            # Old format: just source name — skip (no URL/metadata available)
            skipped_legacy.append({"name": item, "reason": "legacy_string_format_no_metadata"})
            continue
        elif isinstance(item, dict):
            viable.append(item)

    if not viable:
        return _build_report("SKIPPED", "No viable candidates with metadata", date, [], skipped_legacy)

    # 2. Load excluded sources
    excluded_names: set[str] = set()
    if excluded_sources_path:
        ex_path = Path(excluded_sources_path)
        if ex_path.exists():
            with open(ex_path, "r", encoding="utf-8") as f:
                ex_data = json.load(f)
            excluded_names = {
                n.lower() for n in ex_data.get("excluded_sources", [])
            }

    # 3. Load existing sources from sources.yaml
    yaml_path = Path(sources_yaml_path)
    if not yaml_path.exists():
        return _build_report("ERROR", f"sources.yaml not found: {sources_yaml_path}", date, [])

    with open(yaml_path, "r", encoding="utf-8") as f:
        sources_data = yaml.safe_load(f)

    existing_names = {
        s.get("name", "").lower()
        for s in sources_data.get("sources", [])
    }

    # 4. Load SOT for auto_promotion_scans parameter
    auto_promotion_scans = 1  # Default: immediate promotion
    if sot_path:
        try:
            with open(sot_path, "r", encoding="utf-8") as f:
                sot = yaml.safe_load(f)
            auto_promotion_scans = (
                sot.get("workflows", {})
                .get("wf1-general", {})
                .get("parameters", {})
                .get("source_exploration", {})
                .get("auto_promotion_scans", 1)
            )
        except Exception:
            pass

    # 5. Load viability tracking for multi-scan promotion
    viability_tracker = _load_viability_tracker(history_dir) if history_dir else {}

    # 6. Process each viable candidate
    promotions: list[dict] = []
    skipped_reasons: list[dict] = list(skipped_legacy)

    for candidate in viable:
        name = candidate.get("name", candidate.get("source_name", ""))
        url = candidate.get("url", candidate.get("rss_feed", candidate.get("rss_url", "")))
        source_type = candidate.get("type", candidate.get("source_type", "blog"))
        quality_score = candidate.get("quality_score", candidate.get("score", 0.0))
        steeps_focus = candidate.get("steeps_focus", candidate.get("target_steeps", []))
        discovery_method = candidate.get("discovery_method", "unknown")

        if not name:
            skipped_reasons.append({"name": "(empty)", "reason": "no_name"})
            continue

        # Check: not excluded (WF overlap)
        if name.lower() in excluded_names:
            skipped_reasons.append({"name": name, "reason": "excluded_wf_overlap"})
            continue

        # Check: not already in sources.yaml
        if name.lower() in existing_names:
            skipped_reasons.append({"name": name, "reason": "already_in_sources"})
            continue

        # Check: quality score meets minimum
        if quality_score < MIN_QUALITY_SCORE:
            skipped_reasons.append({
                "name": name,
                "reason": f"quality_score_low ({quality_score:.3f} < {MIN_QUALITY_SCORE})",
            })
            continue

        # Check: multi-scan viability tracking
        if auto_promotion_scans > 1:
            tracker_key = name.lower()
            tracker_entry = viability_tracker.get(tracker_key, {
                "consecutive_viable": 0,
                "first_seen": date,
                "last_seen": "",
            })
            # Only increment if not already counted for this date
            if tracker_entry.get("last_seen") != date:
                tracker_entry["consecutive_viable"] += 1
                tracker_entry["last_seen"] = date
            viability_tracker[tracker_key] = tracker_entry

            if tracker_entry["consecutive_viable"] < auto_promotion_scans:
                skipped_reasons.append({
                    "name": name,
                    "reason": (
                        f"viability_tracking ({tracker_entry['consecutive_viable']}"
                        f"/{auto_promotion_scans} scans)"
                    ),
                })
                continue

        # All checks passed — promote!
        promotion_record = _promote_source(
            yaml_path=yaml_path,
            sources_data=sources_data,
            name=name,
            url=url,
            source_type=source_type,
            steeps_focus=steeps_focus,
            discovery_method=discovery_method,
            quality_score=quality_score,
            date=date,
        )

        if promotion_record["status"] == "promoted":
            promotions.append(promotion_record)
            # Update existing names to prevent duplicates within same batch
            existing_names.add(name.lower())
            # Record in history
            if history_dir:
                _record_approval(history_dir, promotion_record)
        else:
            skipped_reasons.append({
                "name": name,
                "reason": f"promotion_failed: {promotion_record.get('error', 'unknown')}",
            })

    # 7. Save viability tracker (even if no promotions, for tracking)
    if history_dir and auto_promotion_scans > 1:
        _save_viability_tracker(history_dir, viability_tracker)

    # 8. Build report
    status = "SUCCESS" if promotions else "NO_PROMOTIONS"
    message = (
        f"Promoted {len(promotions)} source(s) to expansion tier"
        if promotions else
        f"No promotions (viable: {len(viable)}, skipped: {len(skipped_reasons)})"
    )

    report = _build_report(status, message, date, promotions, skipped_reasons)

    if output_path:
        _write_json(output_path, report)

    return report


# ---------------------------------------------------------------------------
# Source Promotion (atomic YAML update)
# ---------------------------------------------------------------------------

def _promote_source(
    yaml_path: Path,
    sources_data: dict,
    name: str,
    url: str,
    source_type: str,
    steeps_focus: list[str],
    discovery_method: str,
    quality_score: float,
    date: str,
) -> dict[str, Any]:
    """Add a source to sources.yaml with tier: expansion (atomic write)."""
    backup_path = yaml_path.with_suffix(".yaml.auto-promote-bak")
    shutil.copy2(yaml_path, backup_path)

    try:
        new_source: dict[str, Any] = {
            "name": name,
            "tier": PROMOTION_TIER,
            "type": source_type,
            "enabled": True,
            "rss_feed": url,
            "timeout": 15,
            "critical": False,
            "max_results": 15,
            "description": f"Auto-promoted from exploration (score: {quality_score:.3f})",
            "reliability": "medium",
            "cost": "free",
            "promoted_from": "exploration",
            "promoted_date": date,
            "discovery_method": discovery_method,
            "auto_promoted": True,
            "health_status": None,
            "last_health_check": None,
            "resolved_url": None,
            "fetch_strategy": None,
            "consecutive_failures": 0,
        }
        if steeps_focus:
            new_source["steeps_focus"] = steeps_focus

        sources_data["sources"].append(new_source)

        # Atomic write: tmp → verify → replace
        tmp_path = yaml_path.with_suffix(".yaml.promote-tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(sources_data, f, default_flow_style=False,
                      allow_unicode=True, sort_keys=False)

        # Verify written file
        with open(tmp_path, "r", encoding="utf-8") as f:
            verify_data = yaml.safe_load(f)
        found = any(
            s.get("name") == name
            for s in verify_data.get("sources", [])
        )
        if not found:
            raise ValueError(f"Verification failed: {name} not found after write")

        os.replace(tmp_path, yaml_path)
        backup_path.unlink(missing_ok=True)

        return {
            "status": "promoted",
            "name": name,
            "url": url,
            "type": source_type,
            "tier": PROMOTION_TIER,
            "steeps_focus": steeps_focus,
            "discovery_method": discovery_method,
            "quality_score": quality_score,
            "promoted_at": datetime.now(timezone.utc).isoformat(),
            "promoted_date": date,
        }

    except Exception as e:
        # Restore from backup
        if backup_path.exists():
            shutil.copy2(backup_path, yaml_path)
        tmp = yaml_path.with_suffix(".yaml.promote-tmp")
        if tmp.exists():
            tmp.unlink()
        return {
            "status": "failed",
            "name": name,
            "error": str(e)[:200],
        }


# ---------------------------------------------------------------------------
# History Recording
# ---------------------------------------------------------------------------

def _record_approval(history_dir: str, promotion: dict) -> None:
    """Record auto-promotion in exploration history."""
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from source_explorer import ExplorationHistory

        history = ExplorationHistory(history_dir)
        data = history.load()

        # Check for duplicate approval
        existing = {
            e.get("name", "").lower()
            for e in data.get("approved", [])
        }
        if promotion["name"].lower() not in existing:
            data.setdefault("approved", []).append({
                "name": promotion["name"],
                "decision": "approved",
                "decided_at": promotion["promoted_at"],
                "url": promotion["url"],
                "type": promotion["type"],
                "auto_promoted": True,
                "quality_score": promotion["quality_score"],
                "discovery_method": promotion["discovery_method"],
            })
            history.save(data)
    except Exception:
        pass  # Non-critical — promotion itself already succeeded


# ---------------------------------------------------------------------------
# Viability Tracker (for multi-scan promotion mode)
# ---------------------------------------------------------------------------

def _load_viability_tracker(history_dir: str) -> dict:
    """Load per-candidate viability tracking data."""
    tracker_path = Path(history_dir) / "viability-tracker.json"
    if tracker_path.exists():
        try:
            with open(tracker_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_viability_tracker(history_dir: str, tracker: dict) -> None:
    """Save viability tracking data."""
    tracker_path = Path(history_dir) / "viability-tracker.json"
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    with open(tracker_path, "w", encoding="utf-8") as f:
        json.dump(tracker, f, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Report Builder
# ---------------------------------------------------------------------------

def _build_report(
    status: str,
    message: str,
    date: str,
    promotions: list[dict],
    skipped: list[dict] | None = None,
) -> dict[str, Any]:
    """Build promotion report dictionary."""
    return {
        "promoter_id": PROMOTER_ID,
        "promoter_version": VERSION,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "date": date,
        "status": status,
        "message": message,
        "promotions": promotions,
        "promotions_count": len(promotions),
        "skipped": skipped or [],
        "skipped_count": len(skipped) if skipped else 0,
    }


def _write_json(path: str, data: Any) -> None:
    """Write JSON with proper encoding."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Source Auto-Promoter — automatic viable candidate promotion"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- promote ---
    prom_parser = subparsers.add_parser(
        "promote", help="Promote viable candidates to expansion tier"
    )
    prom_parser.add_argument(
        "--candidates", required=True,
        help="Path to exploration-candidates-{date}.json"
    )
    prom_parser.add_argument(
        "--sources", required=True,
        help="Path to sources.yaml"
    )
    prom_parser.add_argument(
        "--excluded", default=None,
        help="Path to excluded-sources.json"
    )
    prom_parser.add_argument(
        "--history-dir", default=None,
        help="Path to exploration history directory"
    )
    prom_parser.add_argument(
        "--sot", default=None,
        help="Path to workflow-registry.yaml"
    )
    prom_parser.add_argument(
        "--date", default="",
        help="Scan date (YYYY-MM-DD)"
    )
    prom_parser.add_argument(
        "--output", default=None,
        help="Output path for promotion report JSON"
    )
    prom_parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Print result as JSON to stdout"
    )

    args = parser.parse_args()

    try:
        if args.command == "promote":
            result = promote_viable_candidates(
                candidates_path=args.candidates,
                sources_yaml_path=args.sources,
                excluded_sources_path=args.excluded,
                history_dir=args.history_dir,
                sot_path=args.sot,
                scan_date=args.date,
                output_path=args.output,
            )

            if args.json_output:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                status = result["status"]
                icon = {
                    "SUCCESS": "PROMOTED",
                    "NO_PROMOTIONS": "PASS",
                    "SKIPPED": "SKIP",
                    "ERROR": "FAIL",
                }.get(status, "?")
                print("=" * 60)
                print(f"  [{icon}] Source Auto-Promoter")
                print(f"  {result['message']}")
                if result["promotions"]:
                    print("  Promoted:")
                    for p in result["promotions"]:
                        steeps = ", ".join(p.get("steeps_focus", []))
                        print(f"    + {p['name']} [{steeps}] (score: {p['quality_score']:.3f})")
                if result["skipped"]:
                    print("  Skipped:")
                    for s in result["skipped"]:
                        print(f"    - {s['name']}: {s['reason']}")
                print("=" * 60)

            sys.exit(0 if status in ("SUCCESS", "NO_PROMOTIONS", "SKIPPED") else 1)

    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
