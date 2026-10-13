"""Tests for public, content-free statistical reproduction."""

import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "scripts" / "reproduce.py"


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class PublicReproductionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(ENTRYPOINT.exists(), "public reproduction entrypoint has not been implemented")
        sys.path.insert(0, str(ENTRYPOINT.parent))
        spec = importlib.util.spec_from_file_location("public_reproduce", ENTRYPOINT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.output = Path(self.temporary.name) / "derived"
        self.cohort = [
            {"trace_id": "trace_000001", "case": "case_000002", "label": "unsafe", "selected_step": 1, "call_count": 3},
            {"trace_id": "trace_000002", "case": "case_000001", "label": "safe", "selected_step": 2, "call_count": 3},
            {"trace_id": "trace_000003", "case": "case_000002", "label": "unsafe", "selected_step": 3, "call_count": 3},
            {"trace_id": "trace_000004", "case": "case_000001", "label": "safe", "selected_step": 1, "call_count": 2},
        ]
        write_jsonl(self.repo / "data/cohort.jsonl", self.cohort)
        baselines = []
        for row in self.cohort:
            baseline = {key: row[key] for key in ("trace_id", "case", "label", "selected_step")}
            baseline.update({name: row["label"] == "unsafe" for name in self.module.core.BASELINES})
            baselines.append(baseline)
        write_jsonl(self.repo / "data/selected_step_baselines.jsonl", baselines)
        self.predictions = {}
        for model in self.module.DEFAULT_MODELS:
            rows = []
            for trace in self.cohort:
                for mode in ("whole", "step"):
                    rows.append({
                        "trace_id": trace["trace_id"], "sample_id": trace["trace_id"] + "::" + mode,
                        "case": trace["case"], "mode": mode, "label": trace["label"],
                        "selected_step": trace["selected_step"], "requested_model": model,
                        "returned_model": model, "decision": trace["label"], "status": "predicted",
                        "source_attempt_index": len(rows) + 1,
                    })
            self.predictions[model] = rows
            self.save_predictions(model)

    def save_predictions(self, model):
        write_jsonl(self.repo / "predictions/2026-10-13" / (model + ".jsonl"), self.predictions[model])

    def run_reproduction(self, **options):
        options.setdefault("verify", False)
        return self.module.reproduce(
            self.repo, self.output, expected_count=4, expected_positives=2,
            nboot=30, seed=20261013, **options,
        )

    def test_anonymized_cohort_keeps_frozen_steps_without_recomputing_crc32(self):
        result = self.module.load_cohort(self.repo / "data/cohort.jsonl", 4, 2)
        self.assertEqual(list(result), [row["trace_id"] for row in self.cohort])
        self.assertEqual(result["trace_000002"]["selected_step"], 2)

    def test_duplicate_trace_is_rejected(self):
        write_jsonl(self.repo / "data/cohort.jsonl", self.cohort + [self.cohort[0]])
        with self.assertRaisesRegex(ValueError, "[Dd]uplicate"):
            self.module.load_cohort(self.repo / "data/cohort.jsonl", 5, 3)

    def test_invalid_labels_clusters_and_ordinals_are_rejected(self):
        for field, bad in (("label", "unknown"), ("case", ""), ("selected_step", 0),
                           ("selected_step", 4), ("selected_step", True), ("call_count", False)):
            with self.subTest(field=field, bad=bad):
                rows = copy.deepcopy(self.cohort)
                rows[0][field] = bad
                write_jsonl(self.repo / "data/cohort.jsonl", rows)
                with self.assertRaises(ValueError):
                    self.module.load_cohort(self.repo / "data/cohort.jsonl", 4, 2)

    def test_expected_total_and_positive_count_are_checked(self):
        for expected, positives in ((5, 2), (4, 3)):
            with self.subTest(expected=expected, positives=positives):
                with self.assertRaises(ValueError):
                    self.module.load_cohort(self.repo / "data/cohort.jsonl", expected, positives)

    def test_unknown_and_parse_errors_reduce_coverage_without_becoming_safe(self):
        model = self.module.DEFAULT_MODELS[1]
        self.predictions[model][2].update(status="unknown", decision="unknown")
        self.predictions[model][5].update(status="parse_error", decision=None)
        self.save_predictions(model)
        result = self.run_reproduction()
        whole = result["api_models"][model]["whole"]
        step = result["api_models"][model]["step"]
        self.assertEqual((whole["evaluated"], whole["unknown_samples"], whole["tn"]), (3, 1, 1))
        self.assertEqual((step["evaluated"], step["failure_samples"], step["fn"]), (3, 1, 0))
        self.assertFalse(result["complete"])

    def test_last_valid_prediction_survives_later_error(self):
        model = self.module.DEFAULT_MODELS[0]
        retry = dict(self.predictions[model][0], status="request_error", decision=None, source_attempt_index=9)
        self.predictions[model].append(retry)
        self.save_predictions(model)
        result = self.run_reproduction()
        whole = result["api_models"][model]["whole"]
        self.assertEqual((whole["tp"], whole["evaluated"], whole["duplicate_attempt_rows"]), (2, 4, 1))

    def test_prediction_metadata_mismatch_is_rejected(self):
        model = self.module.DEFAULT_MODELS[0]
        self.predictions[model][0]["selected_step"] = 2
        self.save_predictions(model)
        with self.assertRaisesRegex(ValueError, "selected_step"):
            self.run_reproduction()

    def test_attempt_file_order_cannot_be_changed_silently(self):
        model = self.module.DEFAULT_MODELS[0]
        self.predictions[model][0]["source_attempt_index"] = 10
        self.save_predictions(model)
        with self.assertRaisesRegex(ValueError, "source_attempt_index"):
            self.run_reproduction()

    def test_baseline_missing_row_is_rejected(self):
        path = self.repo / "data/selected_step_baselines.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        write_jsonl(path, rows[:-1])
        with self.assertRaisesRegex(ValueError, "baseline"):
            self.run_reproduction()

    def test_all_eight_rows_and_ten_pairs_are_written(self):
        result = self.run_reproduction()
        self.assertEqual(len(result["comparison_rows"]), 8)
        self.assertEqual(len(result["paired_differences"]), 10)
        self.assertTrue((self.output / "comparison.csv").exists())
        self.assertTrue((self.output / "paired_differences.csv").exists())
        self.assertTrue((self.output / "summary.json").exists())
        self.assertNotIn(str(self.repo), json.dumps(result))

    def test_reference_verification_checks_ci_with_tolerance(self):
        result = self.run_reproduction()
        reference = self.repo / "results/2026-10-13/summary.json"
        reference.parent.mkdir(parents=True)
        reference.write_text(json.dumps(result), encoding="utf-8")
        self.run_reproduction(reference_summary=reference, verify=True)
        self.assertTrue(json.loads((self.output / "verification.json").read_text())["passed"])
        result["comparison_rows"][0]["confidence_intervals"]["recall"]["low"] -= 0.001
        reference.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "confidence_intervals"):
            self.run_reproduction(reference_summary=reference, verify=True)

    def test_default_verification_requires_published_reference(self):
        with self.assertRaisesRegex(ValueError, "[Rr]eference"):
            self.run_reproduction(verify=True)

    def test_reference_verification_checks_attempt_usage_audit(self):
        result = self.run_reproduction()
        model = self.module.DEFAULT_MODELS[0]
        result["api_attempt_audits"][model]["usage_totals"]["prompt_tokens"] = 1
        reference = self.repo / "results/2026-10-13/summary.json"
        reference.parent.mkdir(parents=True)
        reference.write_text(json.dumps(result), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "api_attempt_audits"):
            self.run_reproduction(reference_summary=reference, verify=True)

    def test_skipped_verification_cannot_retain_an_old_pass(self):
        self.output.mkdir(parents=True)
        (self.output / "verification.json").write_text('{"passed": true}', encoding="utf-8")
        self.run_reproduction(verify=False)
        self.assertIsNone(json.loads((self.output / "verification.json").read_text())["passed"])

    def test_committed_result_directory_cannot_be_overwritten(self):
        with self.assertRaisesRegex(ValueError, "output"):
            self.module.reproduce(self.repo, self.repo / "results", expected_count=4, expected_positives=2)


if __name__ == "__main__":
    unittest.main()
