"""chatbot0 public-page content that is likely to change: URLs and the plans.

Pricing is kept here, in one place, so it can be updated without touching the
templates. The owner has not yet confirmed these prices are current (SMA-8).
"""

from __future__ import annotations

from dataclasses import dataclass

SITE_URL = "https://chatbot0.smalltech.in"
# Sign-up still lives in the existing app, now on zer0.smalltech.in.
SIGNUP_URL = "https://zer0.smalltech.in/signup"
# The chat widget (embed.js + the /embed iframe) is served by the Zer0 app; chatbot0 uses Zer0's prompts for now.
WIDGET_URL = "https://zer0.smalltech.in"
ZER0_URL = "https://zer0.smalltech.in"
CONTACT_EMAIL = "contact@smalltech.in"
GITHUB_URL = "https://github.com/smallTechOrg"
LINKEDIN_URL = "https://www.linkedin.com/company/madhyamakist"


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    monthly: int  # USD per month, billed monthly
    yearly: int | None  # USD per month, billed yearly; None for the free plan
    features: tuple[tuple[str, bool], ...]  # (feature, included)
    cta: str
    featured: bool = False

    @property
    def yearly_saving_pct(self) -> int | None:
        """Whole-percent saving of yearly over monthly billing, computed from the prices."""
        if not self.yearly or not self.monthly:
            return None
        return round((self.monthly - self.yearly) * 100 / self.monthly)


# Free / Basic / Pro, ported from the original zero.smalltech.in pricing.
PLANS: tuple[Plan, ...] = (
    Plan(
        "free", "Free", 0, None,
        (("200 conversations a month", True), ("Built-in lead dashboard", True), ("Priority support", False),
         ("Free customisations", False), ("Free backend integration", False)),
        "Start free",
    ),
    Plan(
        "basic", "Basic", 4, 3,
        (("1,000 conversations a month", True), ("Built-in lead dashboard", True), ("Priority support", True),
         ("Free customisations", True), ("Free backend integration", False)),
        "Choose Basic",
    ),
    Plan(
        "pro", "Pro", 12, 10,
        (("Unlimited conversations", True), ("Built-in lead dashboard", True), ("Priority support", True),
         ("Free customisations", True), ("Free backend integration", True)),
        "Choose Pro", featured=True,
    ),
)

FREE_CONVERSATIONS = "200"
MAX_YEARLY_SAVING_PCT = max((p.yearly_saving_pct or 0) for p in PLANS)
LOWEST_PAID_MONTHLY = min(p.monthly for p in PLANS if p.monthly)
