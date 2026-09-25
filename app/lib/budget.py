"""Cost maths and the project-wide budget guard (specs.md §7.2; E-29, E-43)."""

from decimal import ROUND_HALF_UP, Decimal

from app.config import BATCH_DISCOUNT, CACHE_WRITE_MULTIPLIER, GROUNDING_RUN_CAP_USD, MODEL_PRICES


class BudgetExceeded(Exception):
    """Raised before any spend when a job's cap would push total spend over the budget."""

    def __init__(self, spent: Decimal, cap: Decimal, total: Decimal):
        self.spent, self.cap, self.total = spent, cap, total
        remaining = max(Decimal("0"), total - spent)
        super().__init__(
            f"Claude budget would be exceeded: ${spent:.2f} of ${total:.2f} spent, "
            f"${remaining:.2f} left, and this job can cost up to ${cap:.2f}. An admin must raise "
            f"CLAUDE_BUDGET_TOTAL_USD before starting it."
        )

    @property
    def remaining(self) -> Decimal:
        return max(Decimal("0"), self.total - self.spent)


def _price(model: str) -> tuple[Decimal, Decimal, Decimal]:
    for known, price in MODEL_PRICES.items():
        if model == known or model.startswith(known):
            return price
    # Unknown model: charge at the most expensive known rate so we never under-count.
    return max(MODEL_PRICES.values(), key=lambda p: p[1])


def cost_from_usage(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    batch: bool = False,
) -> Decimal:
    price_in, price_out, price_cache_read = _price(model)
    million = Decimal(1_000_000)
    cost = (
        Decimal(input_tokens) * price_in
        + Decimal(cache_write_tokens) * price_in * CACHE_WRITE_MULTIPLIER
        + Decimal(cache_read_tokens) * price_cache_read
        + Decimal(output_tokens) * price_out
    ) / million
    if batch:
        cost *= BATCH_DISCOUNT
    return cost.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def assert_can_spend(spent: Decimal, cap: Decimal | float, total: Decimal | float) -> None:
    cap_d, total_d = Decimal(str(cap)), Decimal(str(total))
    if spent + cap_d > total_d:
        raise BudgetExceeded(spent, cap_d, total_d)


def assert_run_fits(spent: Decimal, run_cap: Decimal | float, total: Decimal | float) -> None:
    """Before a run (or its next phase) starts: the run's Claude cap plus its fact-check reserve must fit in
    what's left of the project budget (rule 5). One rule, used by the web route and both runner phases."""
    assert_can_spend(spent, Decimal(str(run_cap)) + GROUNDING_RUN_CAP_USD, total)
