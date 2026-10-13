from dataclasses import dataclass

from django.utils.translation import get_language, gettext

from .models import NUTRIENTS, DailyTarget


@dataclass(frozen=True)
class NutrientSummary:
    name: str
    unit: str
    eaten: int
    minimum: int
    maximum: int
    fill: float
    range_start: float
    range_width: float
    status: str
    tone: str


def summarize(totals: dict[str, int], target: DailyTarget) -> list[NutrientSummary]:
    nutrients = []
    for name in NUTRIENTS:
        eaten = totals[name]
        minimum = int(getattr(target, f"{name}_min"))
        maximum = int(getattr(target, f"{name}_max"))
        unit = gettext("kcal") if name == "calories" else gettext("g")
        scale = max(maximum * 1.15, eaten * 1.05, 1)
        if eaten > maximum:
            status, tone = (
                gettext("%(amount)s %(unit)s over range")
                % {"amount": eaten - maximum, "unit": unit},
                "amber",
            )
        elif eaten >= minimum:
            status, tone = gettext("Within your range"), "mint"
        else:
            remaining = f"{minimum - eaten}–{maximum - eaten}"
            if (get_language() or "en").startswith("fa"):
                remaining = f"\u2066{remaining}\u2069"
            status = (
                gettext("On track, %(amount)s %(unit)s left")
                % {"amount": remaining, "unit": unit}
                if name == "calories" and eaten
                else gettext("%(amount)s %(unit)s left")
                % {"amount": remaining, "unit": unit}
            )
            tone = "mint" if name == "calories" else "amber"
        nutrients.append(
            NutrientSummary(
                name={
                    "calories": gettext("Calories"),
                    "protein": gettext("Protein"),
                    "carbs": gettext("Carbs"),
                    "fat": gettext("Fat"),
                }[name],
                unit=unit,
                eaten=eaten,
                minimum=minimum,
                maximum=maximum,
                fill=round(eaten / scale * 100, 2),
                range_start=round(minimum / scale * 100, 2),
                range_width=round((maximum - minimum) / scale * 100, 2),
                status=status,
                tone=tone,
            )
        )
    return nutrients
