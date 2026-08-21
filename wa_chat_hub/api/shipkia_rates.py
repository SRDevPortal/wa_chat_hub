from __future__ import annotations

from typing import Any


def calculate_shipkia_rate(**arguments: Any) -> dict[str, Any]:
    """WA MCP wrapper for the Confluence ShipKia rate calculator."""
    from confluence_ai.services.shipkia_rates import calculate_shipkia_rate as calculate

    return calculate(dict(arguments or {}))
