from dataclasses import dataclass

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
        unit = "kcal" if name == "calories" else "g"
        scale = max(maximum * 1.15, eaten * 1.05, 1)
        if eaten > maximum:
            status, tone = f"{eaten - maximum} {unit} over range", "amber"
        elif eaten >= minimum:
            status, tone = "Within your range", "mint"
        else:
            remaining = f"{minimum - eaten}–{maximum - eaten}"
            prefix = "On track, " if name == "calories" and eaten else ""
            status = f"{prefix}{remaining} {unit} left"
            tone = "mint" if name == "calories" else "amber"
        nutrients.append(
            NutrientSummary(
                name=name.title(),
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
