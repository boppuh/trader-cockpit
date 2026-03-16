"""Jinja2 renderer for Trader's Cockpit.

Takes a data dict (matching market_data.json schema) and renders the
cockpit.html template to a string.
"""

import logging

from jinja2 import Environment, FileSystemLoader, select_autoescape

import config

logger = logging.getLogger("cockpit.render")


def _create_env() -> Environment:
    """Create and configure the Jinja2 environment."""
    env = Environment(
        loader=FileSystemLoader(str(config.TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )

    # Custom filters
    env.filters["fmt_number"] = _fmt_number
    env.filters["fmt_pct"] = _fmt_pct
    env.filters["fmt_volume"] = _fmt_volume
    env.filters["sign"] = _sign

    return env


def _fmt_number(value, decimals=2):
    """Format a number with commas and decimal places."""
    try:
        return f"{float(value):,.{decimals}f}"
    except (ValueError, TypeError):
        return str(value)


def _fmt_pct(value, decimals=1):
    """Format a percentage value."""
    try:
        v = float(value)
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.{decimals}f}%"
    except (ValueError, TypeError):
        return str(value)


def _fmt_volume(value):
    """Format volume as human-readable (e.g., 41.1M)."""
    try:
        v = int(value)
        if v >= 1_000_000_000:
            return f"{v / 1_000_000_000:.1f}B"
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.0f}K"
        return str(v)
    except (ValueError, TypeError):
        return str(value)


def _sign(value):
    """Return '+' prefix for positive numbers."""
    try:
        v = float(value)
        return f"+{v}" if v > 0 else str(v)
    except (ValueError, TypeError):
        return str(value)


def render(data: dict) -> str:
    """Render the cockpit template with the given data."""
    env = _create_env()
    template = env.get_template("cockpit.html")
    return template.render(**data)
