"""Apps that can move money keep those tools off by default: a trade or payment cannot be undone."""

from pathlib import Path

from delta.connectors.catalog import load_catalog

REPO = Path(__file__).resolve().parents[1]


def test_trading_order_tools_are_off_by_default():
    kite = load_catalog(REPO / "config" / "mcp_catalog.yaml").apps["kite"]
    for tool in ("place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order",
                 "delete_gtt_order"):
        assert tool in kite.tools.destructive and tool not in kite.tools.read


def test_no_app_asks_for_ads_scopes():
    for app in load_catalog(REPO / "config" / "mcp_catalog.yaml").apps.values():
        assert not any(str(s).startswith("ads:") for s in (getattr(app.auth, "scopes", None) or []))


def test_every_app_has_a_known_category():
    from delta.connectors.catalog import CATEGORIES

    apps = load_catalog(REPO / "config" / "mcp_catalog.yaml").apps
    assert all(a.category in CATEGORIES and a.category != "Custom" for a in apps.values())
    assert {a["category"] for a in [a.summary() for a in apps.values()]} <= set(CATEGORIES)
