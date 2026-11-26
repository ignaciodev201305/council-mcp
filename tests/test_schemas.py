"""Tests de los schemas de salida del consejo."""

import unittest

from pydantic import ValidationError

from council.schemas import CouncilReport, MemberReview
from tests.fakes import REPORT, REVIEW


class MemberReviewTest(unittest.TestCase):
    def test_valid_review(self):
        review = MemberReview.model_validate(REVIEW)
        self.assertEqual(review.weaknesses[0].severity, "media")

    def test_rejects_invalid_severity_or_basis(self):
        for field, value in (("severity", "critica"), ("basis", "intuicion")):
            weak = [{**REVIEW["weaknesses"][0], field: value}, REVIEW["weaknesses"][1]]
            with self.assertRaises(ValidationError):
                MemberReview.model_validate({**REVIEW, "weaknesses": weak})

    def test_requires_two_weaknesses(self):
        with self.assertRaises(ValidationError):
            MemberReview.model_validate({**REVIEW, "weaknesses": REVIEW["weaknesses"][:1]})


class CouncilReportTest(unittest.TestCase):
    def test_accepts_report_without_checks(self):
        report = CouncilReport.model_validate({**REPORT, "checks_for_coding_agent": []})
        self.assertEqual(report.checks_for_coding_agent, [])
        without_field = {k: v for k, v in REPORT.items() if k != "checks_for_coding_agent"}
        self.assertEqual(CouncilReport.model_validate(without_field).checks_for_coding_agent, [])


if __name__ == "__main__":
    unittest.main()
