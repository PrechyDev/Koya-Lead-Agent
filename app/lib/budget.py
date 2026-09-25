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
            f"${remaining:.2f} left, and this job can cost up to ${cap:.2f}. A developer must add to "
            f"the budget on the Spend page before starting it."
        )

    @property
    def remaining(self) -> Decimal:
        return max(Decimal("0"), self.total - self.spent)


MAX_BUDGET_USD = Decimal("1000")  # sanity ceiling for a typo; the DB check says the same (0009)


def runs_left(spent: Decimal, total: Decimal, run_cap: Decimal | float) -> int:
    """How many more runs of this size the budget allows: exactly what assert_run_fits would let start."""
    per_run = Decimal(str(run_cap)) + GROUNDING_RUN_CAP_USD
    return max(0, int((total - spent) // per_run)) if per_run > 0 else 0


def monthly_statement(start: Decimal, spent: dict[str, Decimal], added: dict[str, Decimal],
                      this_month: str) -> list[dict]:
    """A prepaid balance, month by month (UTC), newest first (D-95): each month opens with what the last one
    left unspent, then top-ups come in and runs spend. Nothing expires, so the last closing balance is exactly
    budget − spent, the number the guard uses."""
    months = sorted(set(spent) | set(added) | {this_month})
    first_y, first_m = map(int, months[0].split("-"))
    last_y, last_m = map(int, this_month.split("-"))
    rows, opening, (y, m) = [], start, (first_y, first_m)
    while (y, m) <= (last_y, last_m):
        key = f"{y:04d}-{m:02d}"
        a, s = added.get(key, Decimal(0)), spent.get(key, Decimal(0))
        rows.append({"month": key, "opening": opening, "added": a, "spent": s, "closing": opening + a - s})
        opening = opening + a - s
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return rows[::-1]


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
