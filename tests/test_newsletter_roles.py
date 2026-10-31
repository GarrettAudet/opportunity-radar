import unittest

from opportunity_radar.filtering import role_focus_allows
from tests.test_newsletter_focus_novelty import job


class NewsletterRoleBoundaryTests(unittest.TestCase):
    def test_required_direct_pm_experience_cannot_be_offset_by_welcoming_consultants(self):
        for description in (
            "Required: 3 years of product management experience. We welcome candidates from consulting backgrounds.",
            "Requirements:\n3+ years of PM experience\nWe welcome consulting backgrounds.",
            "We require experience in product management. We welcome consulting backgrounds.",
            "Product management experience is required. Consulting backgrounds are welcome.",
            "Preferred qualifications:\nProduct knowledge\nRequired: 3 years of PM experience. We welcome consulting backgrounds.",
            "3 years of product management experience required, consulting experience preferred. We welcome candidates from consulting backgrounds.",
            "Product management experience required, experience in strategy or consulting preferred.",
            "Product management experience required with strategy or consulting experience preferred. We welcome consulting backgrounds.",
            "3 years of product management experience required and consulting experience preferred. We welcome consulting backgrounds.",
        ):
            with self.subTest(description=description):
                self.assertFalse(role_focus_allows(job("Product Manager", description)))

    def test_true_consulting_alternative_and_optional_pm_experience_can_qualify(self):
        for description in (
            "Requirements: 3 years of product management or consulting experience.",
            "3 years of consulting or product management experience required.",
            "Product management experience preferred. We welcome consulting backgrounds.",
            "Preferred qualifications:\n3 years of product management experience\nWe welcome consulting backgrounds.",
            "Product management experience is not required. We welcome consulting backgrounds.",
            "3 years of product management, strategy, or management consulting experience required.",
            "Experience in product management, business operations or consulting required.",
        ):
            with self.subTest(description=description):
                self.assertTrue(role_focus_allows(job("Product Manager", description)))

    def test_gtm_keyword_does_not_qualify_an_unrelated_profession(self):
        for title in (
            "Legal Counsel, GTM", "GTM Engineering Manager", "GTM Recruitment Lead",
            "Investment Analyst, GTM", "GTM Compliance Manager",
        ):
            with self.subTest(title=title):
                self.assertFalse(role_focus_allows(job(title)))


if __name__ == "__main__":
    unittest.main()
