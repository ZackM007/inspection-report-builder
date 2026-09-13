"""Fernwater brand palette. Single source of truth for CSS and matplotlib."""

BRAND_PRIMARY = "#1F4E46"
BRAND_LIGHT = "#6FA69B"
BRAND_ACCENT = "#C2703D"
SLATE = "#232222"
GRAY = "#99999A"
GRAY_SOFT = "#E8E8EA"
WHITE = "#FFFFFF"

# Category color coding. Anchored on the Fernwater
# palette: blues for the most common items, gold reserved for safety-critical,
# mid-blue/blue-gray for the rest, neutral gray for cosmetic categories.
CATEGORY_COLORS = {
    "Plumbing":          BRAND_PRIMARY,
    "Water Heater":      "#3E7A6E",
    "Smoke / CO Alarms": BRAND_ACCENT,
    "Electrical":        BRAND_ACCENT,
    "Front Door":        BRAND_LIGHT,
    "Windows":           BRAND_LIGHT,
    "Kitchen":           "#3E7A6E",
    "Bathroom":          "#3E7A6E",
    "HVAC":              "#3E7A6E",
    "Appliances":        "#7C8FA3",
    "Paint & Walls":     GRAY,
    "Flooring":          GRAY,
    "Other":             GRAY,
}


def color_for_category(category: str) -> str:
    return CATEGORY_COLORS.get(category, GRAY)


# Theme labels for the executive summary's "Immediate attention priorities"
# rows — a deterministic category -> owner-facing risk theme, plus a severity
# rank so the most serious themes lead the list.
CATEGORY_THEME = {
    "Smoke / CO Alarms": "Life-safety check",
    "Electrical":        "Safety priority",
    "Plumbing":          "Water-risk priority",
    "Water Heater":      "Water-risk priority",
    "Bathroom":          "Water-risk priority",
    "Front Door":        "Security priority",
    "Windows":           "Security priority",
    "HVAC":              "Habitability follow-up",
    "Housekeeping":      "Condition follow-up",
    "Kitchen":           "Condition follow-up",
    "Appliances":        "Condition follow-up",
    "Flooring":          "Condition follow-up",
    "Paint & Walls":     "Condition follow-up",
    "Other":             "Condition follow-up",
}

THEME_RANK = {
    "Life-safety check": 0,
    "Safety priority": 1,
    "Water-risk priority": 2,
    "Security priority": 3,
    "Habitability follow-up": 4,
    "Condition follow-up": 5,
}


def theme_for(category: str) -> str:
    return CATEGORY_THEME.get(category, "Condition follow-up")


PRIORITY_COLORS = {
    "High": BRAND_PRIMARY,
    "Medium": BRAND_LIGHT,
    "Low": GRAY,
}

CHART_SERIES = [BRAND_PRIMARY, BRAND_LIGHT, BRAND_ACCENT, SLATE, GRAY]


def matplotlib_rc() -> dict:
    return {
        "font.family": "Ubuntu",
        "font.size": 10,
        "axes.edgecolor": SLATE,
        "axes.labelcolor": SLATE,
        "axes.titlecolor": SLATE,
        "xtick.color": SLATE,
        "ytick.color": SLATE,
        "text.color": SLATE,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.prop_cycle": __import__("cycler").cycler(color=CHART_SERIES),
    }
