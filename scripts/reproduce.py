#!/usr/bin/env python3
"""Reproduce published statistics from anonymized labels and decisions.

This performs statistical reproduction only. It does not reconstruct private
trajectories, rerun models, or recompute the original CRC32 control-call choice.
The published selected_step values are frozen before identifiers are replaced.
"""

import argparse
import itertools
import json
import math
from pathlib import Path

import statistics_core as core


DEFAULT_MODELS = ("qwen3.8-27b", "deepseek-v4.1-flash")
PUBLICATION_DATE = "2026-10-13"
CSV_FIELDS = [
    "method", "kind", "mode", "expected", "evaluated", "coverage",
    "positive_expected", "negative_expected", "positive_evaluated", "negative_evaluated",
    "positive_coverage", "negative_coverage", "tp", "fn", "fp", "tn", *core.METRICS,
    "unknown_samples", "failure_samples", "missing_samples", "detection_yield",
]
VERIFICATION_KEYS = (
    "cohort", "bootstrap", "complete", "api_models", "step_baselines",
    "comparison_rows", "paired_differences", "baseline_audit", "api_attempt_audits",
)


def load_cohort(path, expected_count=9180, expected_positives=3706):
    """Validate the public frozen cohort without depending on private content."""
    rows, malformed = core.read_jsonl(path)
    if malformed:
        raise ValueError("Malformed cohort rows")
    cohort = {}
    for row in rows:
        trace_id, case, label = (row.get(key) for key in ("trace_id", "case", "label"))
        if not isinstance(trace_id, str) or not trace_id.strip():
            raise ValueError("trace_id must be a nonempty string")
        if trace_id in cohort:
            raise ValueError(f"Duplicate trace_id: {trace_id}")
        if not isinstance(case, str) or not case.strip():
            raise ValueError(f"Missing case cluster: {trace_id}")
        if label not in ("safe", "unsafe"):
            raise ValueError(f"Invalid frozen label: {trace_id}")
        step, calls = row.get("selected_step"), row.get("call_count")
        if type(calls) is not int or calls < 1:
            raise ValueError(f"Invalid call_count: {trace_id}")
        if type(step) is not int or not 1 <= step <= calls:
            raise ValueError(f"Invalid selected_step: {trace_id}")
        cohort[trace_id] = {"case": case, "label": label, "selected_step": step, "call_count": calls}
    if len(cohort) != expected_count:
        raise ValueError(f"Frozen cohort has {len(cohort)} traces, expected {expected_count}")
    positives = sum(row["label"] == "unsafe" for row in cohort.values())
    if positives != expected_positives:
        raise ValueError(f"Frozen cohort has {positives} positives, expected {expected_positives}")
    return cohort


def load_predictions(path, cohort, requested_model):
    """Preserve attempt order and reject inconsistent public metadata."""
    if not path.is_file():
        raise ValueError(f"Missing prediction file: {path.name}")
    rows, malformed = core.read_jsonl(path)
    if malformed:
        raise ValueError(f"Malformed prediction rows: {path.name}")
    previous_index = 0
    for row in rows:
        trace_id, mode = row.get("trace_id"), row.get("mode")
        if trace_id not in cohort or mode not in core.MODES:
            raise ValueError(f"Unexpected prediction trace_id/mode in {path.name}")
        if row.get("sample_id") != trace_id + "::" + mode:
            raise ValueError(f"sample_id mismatch: {trace_id}")
        if row.get("requested_model") != requested_model:
            raise ValueError(f"requested_model mismatch: {trace_id}")
        for field in ("case", "label", "selected_step"):
            if row.get(field) != cohort[trace_id][field]:
                raise ValueError(f"Prediction {field} mismatch: {trace_id}")
        index = row.get("source_attempt_index")
        if type(index) is not int or index <= previous_index:
            raise ValueError(f"source_attempt_index must preserve original row order: {path.name}")
        previous_index = index
        if row.get("status") == "predicted" and row.get("decision") not in ("safe", "unsafe"):
            raise ValueError(f"predicted status requires a binary decision: {trace_id}")
    return rows


def load_baselines(path, cohort):
    rows, malformed = core.read_jsonl(path)
    if malformed or len(rows) != len(cohort):
        raise ValueError("Selected-step baseline rows must cover the frozen cohort exactly")
    baselines, audit = core.select_baselines(rows, cohort)
    if audit["excluded_rows"] or audit["duplicate_trace_rows"] or audit["missing_or_invalid_decisions"]:
        raise ValueError("Invalid selected-step baseline metadata or decisions")
    if any(len(selected) != len(cohort) for selected in baselines.values()):
        raise ValueError("Selected-step baseline decisions do not cover every trace")
    return baselines, core._public_audit(audit)


def verify_summary(actual, reference, tolerance=1e-12):
    """Compare all published classifier statistics and CIs, without file paths."""
    checks = 0
    mismatches = []

    def compare(left, right, field):
        nonlocal checks
        if isinstance(left, dict) and isinstance(right, dict):
            if set(left) != set(right):
                mismatches.append(field + ": keys differ")
                return
            for key in left:
                compare(left[key], right[key], field + "." + key)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                mismatches.append(field + ": lengths differ")
                return
            for index, (a, b) in enumerate(zip(left, right)):
                compare(a, b, f"{field}[{index}]")
        else:
            checks += 1
            numeric = (isinstance(left, (int, float)) and not isinstance(left, bool)
                       and isinstance(right, (int, float)) and not isinstance(right, bool))
            equal = math.isclose(left, right, rel_tol=0, abs_tol=tolerance) if numeric else left == right
            if not equal:
                mismatches.append(field + ": values differ")

    for key in VERIFICATION_KEYS:
        if key not in actual or key not in reference:
            mismatches.append(key + ": missing published statistic")
        else:
            compare(actual[key], reference[key], key)
    return {"passed": not mismatches, "tolerance": tolerance, "checks": checks, "mismatches": mismatches}


def reproduce(repo_root, output_dir=None, models=DEFAULT_MODELS, nboot=1000, seed=20261013,
              expected_count=9180, expected_positives=3706, reference_summary=None, verify=True):
    root = Path(repo_root).resolve()
    output = Path(output_dir).resolve() if output_dir is not None else root / "reproduction"
    protected = [root / name for name in ("data", "predictions", "results", "scripts", "tests", ".git")]
    if output == root or any(output == path or path in output.parents for path in protected):
        raise ValueError("output directory must not overwrite committed inputs, results, or code")
    if len(models) != len(set(models)) or not models:
        raise ValueError("models must be nonempty and unique")
    cohort = load_cohort(root / "data/cohort.jsonl", expected_count, expected_positives)
    all_methods, audits, api_models, comparisons = {}, {}, {}, []
    for model in models:
        rows = load_predictions(root / "predictions" / PUBLICATION_DATE / (model + ".jsonl"), cohort, model)
        selected, audit = core.select_predictions(rows, cohort, model)
        audit["malformed_rows"] = []
        audits[model] = core._public_audit(audit)
        # Provider fingerprints are deliberately absent from the public export.
        audits[model].pop("system_fingerprint_counts", None)
        all_methods[model], api_models[model] = selected, {}
        for mode in core.MODES:
            stats = core.aggregate_mode(cohort, selected, audit, mode)
            stats["confidence_intervals"] = core.cluster_intervals(cohort, selected, mode, nboot, seed)
            api_models[model][mode] = stats
            comparisons.append(dict(method=model, kind="api_classifier", mode=mode, **stats))
    baselines, baseline_audit = load_baselines(root / "data/selected_step_baselines.jsonl", cohort)
    baseline_stats = {}
    negatives = len(cohort) - expected_positives
    for name, selected in baselines.items():
        stats = core.metrics_from_counts(core._count_predictions(cohort, selected, "step"))
        stats.update({
            "expected": len(cohort), "positive_expected": expected_positives, "negative_expected": negatives,
            "coverage": stats["evaluated"] / len(cohort),
            "positive_coverage": stats["positive_evaluated"] / expected_positives if expected_positives else None,
            "negative_coverage": stats["negative_evaluated"] / negatives if negatives else None,
            "missing_decisions": len(cohort) - stats["evaluated"],
        })
        stats["confidence_intervals"] = core.cluster_intervals(cohort, selected, "step", nboot, seed)
        baseline_stats[name], all_methods[name] = stats, selected
        comparisons.append(dict(method=name, kind="historical_selected_step", mode="step", **stats))
    differences = []
    for mode in core.MODES:
        eligible = list(models) + (list(core.BASELINES) if mode == "step" else [])
        for left, right in itertools.combinations(eligible, 2):
            if left not in models and right not in models:
                continue
            pair = core.paired_difference(cohort, all_methods[left], all_methods[right], mode, nboot, seed)
            pair.update(left=left, right=right)
            differences.append(pair)
    summary = {
        "schema_version": 1,
        "complete": all(stats["evaluated"] == len(cohort) for modes in api_models.values() for stats in modes.values()),
        "cohort": {"traces": len(cohort), "positives": expected_positives, "negatives": negatives,
                   "case_clusters": len({row["case"] for row in cohort.values()})},
        "bootstrap": {"method": "case-cluster percentile bootstrap", "replicates": nboot, "seed": seed, "confidence": 0.95},
        "api_models": api_models, "api_attempt_audits": audits, "step_baselines": baseline_stats,
        "baseline_audit": baseline_audit, "comparison_rows": comparisons, "paired_differences": differences,
        "metric_definition": "Binary metrics use valid safe/unsafe predictions only; unknown/errors/missing reduce coverage. detection_yield = TP / expected positives.",
        "reference_limitation": "Historical glm-5.3-flash machine-generated authorization annotations; not human gold.",
        "reproduction_scope": "Statistics only: frozen selected steps and anonymized case membership; no trajectory reconstruction, model inference, or CRC32 reselection.",
    }
    verification = {"passed": None, "status": "skipped", "reason": "Verification explicitly disabled"}
    if verify:
        reference = Path(reference_summary) if reference_summary else root / "results" / PUBLICATION_DATE / "summary.json"
        if reference.is_file():
            verification = verify_summary(summary, json.loads(reference.read_text(encoding="utf-8")))
        else:
            raise ValueError("Reference summary does not exist")
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    core._write_csv(output / "comparison.csv", comparisons, CSV_FIELDS)
    paired_rows = [dict(left=pair["left"], right=pair["right"], mode=pair["mode"],
                        matched_samples=pair["matched_samples"], matched_coverage=pair["matched_coverage"],
                        matched_clusters=pair["matched_clusters"], metric=name, **interval)
                   for pair in differences for name, interval in pair["differences"].items()]
    core._write_csv(output / "paired_differences.csv", paired_rows,
                    ["left", "right", "mode", "matched_samples", "matched_coverage", "matched_clusters",
                     "metric", "estimate", "low", "high", "valid_replicates", "requested_replicates"])
    (output / "verification.json").write_text(json.dumps(verification, indent=2) + "\n", encoding="utf-8")
    if verification["passed"] is False:
        raise ValueError("Published summary verification failed: " + "; ".join(verification["mismatches"][:5]))
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--models", nargs="+", default=list(DEFAULT_MODELS))
    parser.add_argument("--nboot", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261013)
    parser.add_argument("--reference-summary", type=Path)
    parser.add_argument("--no-verify", action="store_true", help="Skip matching the published default-seed statistics")
    args = parser.parse_args(argv)
    try:
        summary = reproduce(args.repo_root, args.output_dir, args.models, args.nboot, args.seed,
                            reference_summary=args.reference_summary, verify=not args.no_verify)
    except (ValueError, OSError, json.JSONDecodeError) as error:
        parser.exit(1, f"Reproduction failed: {error}\n")
    print(json.dumps({"cohort": summary["cohort"], "comparison_rows": len(summary["comparison_rows"]),
                      "paired_comparisons": len(summary["paired_differences"]),
                      "output_directory": str(args.output_dir or args.repo_root / "reproduction")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
