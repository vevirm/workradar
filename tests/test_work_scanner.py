"""Regression tests for the Work Radar scanner.

These run in CI before every scan. They cover the parts where a silent
regression would quietly corrupt the corpus: the admission gate, the matrix
classifier's handling of negation, the rotation cursor, and the merge.
"""

from __future__ import annotations

import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import scan_work_radar as scanner  # noqa: E402


class TextUtilities(unittest.TestCase):
    def test_clean_text_strips_markup_and_whitespace(self):
        self.assertEqual(scanner.clean_text("<p>Hybrid   work</p>"), "Hybrid work")

    def test_distinct_matches_respects_word_boundaries(self):
        self.assertEqual(scanner.distinct_matches("worker", ["work"]), [])
        self.assertEqual(scanner.distinct_matches("the work is done", ["work"]), ["work"])

    def test_stable_identity_prefers_doi(self):
        a = scanner.stable_identity("A title", "https://doi.org/10.1234/abc")
        b = scanner.stable_identity("Different title", "10.1234/abc")
        self.assertEqual(a, b)

    def test_parse_date_handles_common_shapes(self):
        self.assertEqual(scanner.parse_date("2025-03-04"), dt.date(2025, 3, 4))
        self.assertEqual(scanner.parse_date("2025-03"), dt.date(2025, 3, 1))
        self.assertIsNone(scanner.parse_date(""))


class RotationCursor(unittest.TestCase):
    def test_batch_does_not_wrap_inside_one_run(self):
        items = list(range(10))
        batch, nxt, wrapped = scanner.rotating_batch(items, 8, 5)
        self.assertEqual(batch, [8, 9])
        self.assertEqual(nxt, 0)
        self.assertTrue(wrapped)

    def test_batch_advances_normally(self):
        batch, nxt, wrapped = scanner.rotating_batch(list(range(10)), 2, 3)
        self.assertEqual(batch, [2, 3, 4])
        self.assertEqual(nxt, 5)
        self.assertFalse(wrapped)

    def test_cursor_does_not_advance_when_nothing_ran(self):
        state = {}
        scanner.commit_cursor(state, "k", 4, 9, executed_count=0)
        self.assertEqual(state["k"], 4)
        scanner.commit_cursor(state, "k", 4, 9, executed_count=3)
        self.assertEqual(state["k"], 9)


class MatrixClassifier(unittest.TestCase):
    def test_places_positive_remote_finding(self):
        result = scanner.classify_matrix(
            "Hybrid working and firm performance",
            "We study hybrid work across 200 firms. Hybrid working improved productivity "
            "and reduced turnover among engineering staff over three years.",
        )
        self.assertEqual(result["matrix_cell"], "more_remote-higher_effectiveness")

    def test_reduced_turnover_reads_as_higher_effectiveness(self):
        score, evidence = scanner.effectiveness_reading(
            "Remote work reduced turnover across the sample."
        )
        self.assertGreater(score, 0)
        self.assertTrue(evidence)

    def test_reduced_productivity_reads_as_lower_effectiveness(self):
        score, _ = scanner.effectiveness_reading(
            "Remote work reduced productivity across the sample."
        )
        self.assertLess(score, 0)

    def test_relational_phrasing_is_read(self):
        score, _ = scanner.effectiveness_reading(
            "Telework availability was positively related to organizational performance."
        )
        self.assertGreater(score, 0)

    def test_standalone_cost_marker_is_negative(self):
        score, _ = scanner.effectiveness_reading(
            "We document the hidden costs of hybrid working for team collaboration."
        )
        self.assertLess(score, 0)

    def test_null_marker_suppresses_a_pair(self):
        score, _ = scanner.effectiveness_reading(
            "Core job performance was unaffected by hybrid working."
        )
        self.assertEqual(score, 0)

    def test_places_return_to_office_penalty(self):
        result = scanner.classify_matrix(
            "Office mandates and output",
            "Firms imposed a return to office mandate with minimum days in the office. "
            "Increased office attendance lowered productivity and raised turnover.",
        )
        self.assertEqual(result["matrix_x"], "less_remote")
        self.assertEqual(result["matrix_y"], "lower_effectiveness")

    def test_onsite_terms_outweigh_passing_office_mentions(self):
        remote_paper, _ = scanner.arrangement_reading(
            ["We study working from home and telework among staff who rarely attend the office."]
        )
        self.assertGreater(remote_paper, 0)

    def test_negation_is_clause_local(self):
        score, _ = scanner.effectiveness_reading(
            "Hybrid work did not reduce productivity in the treated offices."
        )
        self.assertGreaterEqual(score, 0)

    def test_unplaced_when_outcome_is_unlinked(self):
        result = scanner.classify_matrix(
            "Remote work adoption across regions",
            "Working from home has increased sharply. Regional labour markets differ in "
            "their industrial composition and in commuting patterns across the sample.",
        )
        self.assertEqual(result["matrix_cell"], "")
        self.assertTrue(result["matrix_unplaced_reason"])

    def test_unplaced_when_no_direction_on_outcome(self):
        result = scanner.classify_matrix(
            "Hybrid work and productivity",
            "Hybrid work is associated with productivity in our sample of firms.",
        )
        self.assertEqual(result["matrix_cell"], "")

    def test_evidence_sentences_require_cooccurrence(self):
        text = (
            "Remote work is widespread. Productivity rose over the decade. "
            "Remote work raised productivity in call centres."
        )
        found = scanner.evidence_sentences(text)
        self.assertEqual(len(found), 1)
        self.assertIn("call centres", found[0])

    def test_hedged_claims_carry_half_weight(self):
        firm, _ = scanner.effectiveness_reading("Hybrid work improved productivity.")
        hedged, _ = scanner.effectiveness_reading(
            "Hybrid work may have improved productivity, though results are preliminary."
        )
        self.assertGreater(firm, hedged)

    def test_evidence_strength_detects_experiments(self):
        label, hits = scanner.evidence_strength("We ran a randomised controlled trial of hybrid schedules.")
        self.assertEqual(label, "experimental")
        self.assertTrue(hits)


class CuratorOverrides(unittest.TestCase):
    def test_override_pins_a_cell(self):
        overrides = {"https://a.test/x": {"matrix_cell": "less_remote-higher_effectiveness", "note": "checked"}}
        item = {"link": "https://a.test/x/", "title": "t", "matrix_cell": "", "matrix_unplaced_reason": "unclear"}
        result = scanner.apply_manual_placement(item, overrides)
        self.assertEqual(result["matrix_cell"], "less_remote-higher_effectiveness")
        self.assertEqual(result["matrix_x"], "less_remote")
        self.assertEqual(result["placement_source"], "curator")
        self.assertEqual(result["matrix_unplaced_reason"], "")

    def test_empty_override_holds_an_item_out_of_the_matrix(self):
        overrides = {"https://a.test/y": {"matrix_cell": "", "note": ""}}
        item = {"link": "https://a.test/y", "matrix_cell": "more_remote-higher_effectiveness"}
        result = scanner.apply_manual_placement(item, overrides)
        self.assertEqual(result["matrix_cell"], "")
        self.assertIn("curator", result["matrix_unplaced_reason"])

    def test_unmatched_item_is_untouched(self):
        item = {"link": "https://a.test/z", "matrix_cell": "more_remote-higher_effectiveness"}
        result = scanner.apply_manual_placement(dict(item), {})
        self.assertEqual(result["matrix_cell"], item["matrix_cell"])


class AdmissionGate(unittest.TestCase):
    def base_candidate(self, **overrides):
        candidate = {
            "title": "Hybrid working and team productivity in professional services",
            "authors": "A Researcher",
            "source": "Journal of Labor Economics",
            "tier": 2,
            "kind": "scholarly",
            "type": "peer-reviewed article",
            "date": dt.date.today() - dt.timedelta(days=60),
            "link": "https://doi.org/10.1234/example",
            "abstract": (
                "We study hybrid work in a large professional services firm using "
                "administrative data on employees and their teams. Working from home "
                "increased over the sample period. We find hybrid work delivered "
                "increased productivity and reduced turnover among staff, with no "
                "measurable cost to collaboration between teams in the organisation."
            ),
            "body": "",
        }
        candidate.update(overrides)
        return candidate

    def test_admits_a_clean_candidate(self):
        item = scanner.admit(self.base_candidate())
        self.assertIsNotNone(item)
        self.assertEqual(item["source_tier"], "Tier 2")
        self.assertTrue(item["location_evidence"])

    def test_rejects_unlisted_source(self):
        self.assertIsNone(scanner.admit(self.base_candidate(tier=9)))

    def test_rejects_off_topic_item(self):
        candidate = self.base_candidate(
            title="Monetary policy transmission in small open economies",
            abstract=(
                "We estimate the response of inflation to policy rate changes using a "
                "structural vector autoregression across twelve small open economies "
                "over four decades of quarterly macroeconomic data and find persistent "
                "effects on output and prices throughout the sample period studied."
            ),
        )
        self.assertIsNone(scanner.admit(candidate))

    def test_rejects_stale_item(self):
        stale = self.base_candidate(date=dt.date.today() - dt.timedelta(days=365 * 12))
        self.assertIsNone(scanner.admit(stale))

    def test_rejects_thin_abstract(self):
        self.assertIsNone(scanner.admit(self.base_candidate(abstract="Hybrid work from home study.")))

    def test_unplaceable_item_is_still_admitted(self):
        candidate = self.base_candidate(
            abstract=(
                "We document the spread of hybrid work across a large professional "
                "services firm using administrative records on employees and teams. "
                "The share of staff working from home varies by grade, tenure and "
                "office location throughout the observation window we study here."
            )
        )
        item = scanner.admit(candidate)
        self.assertIsNotNone(item)
        self.assertEqual(item["matrix_cell"], "")
        self.assertTrue(item["matrix_unplaced_reason"])


class CorpusMerge(unittest.TestCase):
    def item(self, link, title, date=None, tier="Tier 2"):
        return {
            "link": link,
            "title": title,
            "date": (date or dt.date.today() - dt.timedelta(days=30)).isoformat(),
            "source_tier": tier,
            "matrix_cell": "",
        }

    def test_previous_items_survive_a_scan_that_finds_nothing(self):
        previous = [self.item("https://a.test/1", "One")]
        merged, new = scanner.merge_corpus(previous, [])
        self.assertEqual(len(merged), 1)
        self.assertEqual(new, 0)
        self.assertFalse(merged[0]["new_this_scan"])

    def test_new_items_are_flagged_and_counted(self):
        previous = [self.item("https://a.test/1", "One")]
        merged, new = scanner.merge_corpus(previous, [self.item("https://a.test/2", "Two")])
        self.assertEqual(len(merged), 2)
        self.assertEqual(new, 1)
        self.assertEqual(sum(1 for m in merged if m["new_this_scan"]), 1)

    def test_rediscovery_updates_without_duplicating(self):
        previous = [self.item("https://a.test/1", "One")]
        merged, new = scanner.merge_corpus(previous, [self.item("https://a.test/1/", "One revised")])
        self.assertEqual(len(merged), 1)
        self.assertEqual(new, 0)
        self.assertEqual(merged[0]["title"], "One revised")

    def test_stale_low_tier_items_age_out(self):
        old = self.item("https://a.test/9", "Old", date=dt.date.today() - dt.timedelta(days=365 * 9))
        merged, _ = scanner.merge_corpus([old], [])
        self.assertEqual(merged, [])

    def test_tier_one_items_use_the_extended_window(self):
        midpoint = dt.date.today() - relativedelta_months(
            (int(scanner.CONFIG["lookback_months"]) + int(scanner.CONFIG["extended_top_quality_lookback_months"])) // 2
        )
        tier1 = self.item("https://a.test/t1", "Tier one", date=midpoint, tier="Tier 1")
        tier2 = self.item("https://a.test/t2", "Tier two", date=midpoint, tier="Tier 2")
        merged, _ = scanner.merge_corpus([tier1, tier2], [])
        titles = [m["title"] for m in merged]
        self.assertIn("Tier one", titles)
        self.assertNotIn("Tier two", titles)


def relativedelta_months(months: int):
    from dateutil.relativedelta import relativedelta

    return relativedelta(months=months)


class MatrixCounts(unittest.TestCase):
    def test_counts_cover_every_configured_cell(self):
        counts = scanner.matrix_counts([])
        for cell in scanner.CONFIG["matrix_cells"]:
            self.assertIn(cell, counts)
        self.assertIn("unplaced", counts)

    def test_unknown_cell_counts_as_unplaced(self):
        counts = scanner.matrix_counts([{"matrix_cell": "nonsense"}, {"matrix_cell": ""}])
        self.assertEqual(counts["unplaced"], 2)


class Budget(unittest.TestCase):
    def test_stage_deadline_never_outlives_the_global_budget(self):
        import time as _time

        scanner.SCAN_DEADLINE_MONO = _time.monotonic() + 20
        deadline = scanner.new_stage_deadline(600, reserve=5)
        self.assertLessEqual(deadline - _time.monotonic(), 16)
        scanner.SCAN_DEADLINE_MONO = None

    def test_no_budget_means_no_deadline(self):
        scanner.SCAN_DEADLINE_MONO = None
        self.assertFalse(scanner.deadline_reached(0))


if __name__ == "__main__":
    unittest.main()
