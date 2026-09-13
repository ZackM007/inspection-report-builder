"""Generate brand-styled charts for the summary section."""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from ..models import KPIs
from ..palette import (
    BRAND_PRIMARY,
    BRAND_LIGHT,
    GRAY,
    SLATE,
    color_for_category,
    matplotlib_rc,
)


def _styled():
    plt.rcParams.update(matplotlib_rc())


def common_issues_bar(data: list[tuple[str, int]], out_path: Path) -> Path:
    _styled()
    if not data:
        data = [("No issues flagged", 0)]
    labels = [d[0] for d in data]
    values = [d[1] for d in data]
    colors = [color_for_category(cat) for cat in labels]
    fig, ax = plt.subplots(figsize=(7.5, 3.0))
    ax.bar(labels, values, color=colors, edgecolor="none", width=0.65)
    ax.set_ylabel("Count", fontsize=10)
    ax.set_ylim(0, max(values + [1]) * 1.25)
    for i, v in enumerate(values):
        ax.text(i, v + max(values + [1]) * 0.04, str(v), ha="center", color=SLATE, fontsize=10)
    plt.xticks(rotation=20, ha="right", fontsize=10)
    plt.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path


def action_split_donut(kpis: KPIs, out_path: Path) -> Path:
    """Donut showing Action Required vs No Action vs Not Inspected."""
    _styled()
    sizes: list[int] = []
    labels: list[str] = []
    colors: list[str] = []
    for n, lbl, col in (
        (kpis.action_required, "Action Required", BRAND_PRIMARY),
        (kpis.no_action, "No Action Required", BRAND_LIGHT),
        (kpis.not_inspected, "Not Inspected", GRAY),
    ):
        if n > 0:
            sizes.append(n)
            labels.append(f"{lbl} ({n})")
            colors.append(col)
    if not sizes:
        sizes, labels, colors = [1], ["No data"], [GRAY]

    fig, ax = plt.subplots(figsize=(4.0, 3.0))
    wedges, _ = ax.pie(
        sizes,
        colors=colors,
        startangle=90,
        wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 2},
    )
    ax.legend(wedges, labels, loc="center", frameon=False, fontsize=10)
    plt.tight_layout()
    fig.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out_path
