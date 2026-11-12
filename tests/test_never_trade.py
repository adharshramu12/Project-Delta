"""Delta never trades: not with approval, not in Autopilot, not through a connected broker app.

Looking at an account stays free. These tests hold the rule at every place a trade could be made: a click, a form
submit or Enter in either browser, a click in a desktop app, and a connected app's order tools."""

from pathlib import Path

import pytest

from delta.config import Boundaries
from delta.connectors.catalog import AppSpec, load_catalog
from delta.connectors.store import Connection
from delta.connectors.tools import resolve_risk, tool_enabled
from delta.safety.policy import PolicyEngine, Verdict
from delta.safety.trading import is_trade_tool, is_trading_place, never_trade
from delta.tools.base import Risk
from delta.tools.browser import (
    BrowserClick,
    BrowserFill,
    BrowserManager,
    BrowserPress,
    PressArgs,
    RefClickArgs,
    RefFillArgs,
)
from delta.tools.desktop import click_risk

KITE = "https://kite.zerodha.com/holdings"
TRADES = ["Buy", "Sell", "SELL RELIANCE", "Place order", "Modify order", "Cancel order", "Square off", "Exit",
          "Exit position", "Invest now", "Start SIP", "Buy / Sell", "Place GTT", "Swap", "Trade"]
LOOKS = ["Holdings", "Positions", "Orders", "Order history", "Trade book", "Watchlist", "Funds", "Portfolio",
         "Search", "Sign in", "View details", "Option chain", "Charts", "P&L"]


@pytest.mark.parametrize("name", TRADES)
def test_a_trade_on_a_trading_page_is_never_allowed(name):
    assert never_trade(name, KITE) and click_risk(name, KITE) == Risk.SYSTEM


@pytest.mark.parametrize("name", LOOKS)
def test_looking_at_the_account_is_an_ordinary_click(name):
    assert not never_trade(name, KITE) and click_risk(name, KITE) != Risk.SYSTEM


@pytest.mark.parametrize("where", ["https://groww.in/stocks", "Upstox Pro - Google Chrome", "TradingView - Chart",
                                   "https://www.binance.com/en/trade/BTC_USDT", "Angel One - Trading",
                                   "https://app.dhan.co/", "My Broker | Demat account", "Coinbase - Buy crypto"])
def test_brokers_exchanges_and_trading_apps_are_recognised_by_address_or_title(where):
    assert is_trading_place(where) and click_risk("Sell", where) == Risk.SYSTEM


def test_shopping_and_other_sites_are_not_trading():
    for name, where in [("Buy now", "https://www.amazon.in/dp/1"), ("Place order", "https://www.flipkart.com/checkout"),
                        ("Sell", "https://www.olx.in/post"), ("Order", "https://www.swiggy.com/"),
                        ("Buy", ""), ("Sell", "")]:
        assert not never_trade(name, where), (name, where)
        assert click_risk(name, where) == Risk.EXTERNAL  # still asks first, as any purchase does


@pytest.mark.parametrize("autonomy", ["ask-risky", "trusted", "autopilot"])
def test_no_permission_level_allows_it_and_none_asks(autonomy, tmp_path):
    """Not ASK: there is no prompt to approve. The policy denies it outright, like passwords and payments."""
    mgr = BrowserManager(tmp_path)
    mgr.current_url = KITE
    pe = PolicyEngine(Boundaries(autonomy=autonomy))
    assert pe.evaluate(BrowserClick(mgr), RefClickArgs(text="Sell")).verdict == Verdict.DENY
    assert pe.evaluate(BrowserClick(mgr), RefClickArgs(text="Holdings")).verdict != Verdict.DENY
    # an order form: Enter and "fill and submit" are the same act as the button
    assert pe.evaluate(BrowserFill(mgr), RefFillArgs(field="Quantity", value="10", submit=True)).verdict == Verdict.DENY
    assert pe.evaluate(BrowserFill(mgr), RefFillArgs(field="Quantity", value="10")).verdict != Verdict.DENY
    mgr.last_fill_was_search = False
    assert pe.evaluate(BrowserPress(mgr), PressArgs(key="Enter")).verdict == Verdict.DENY
    mgr.last_fill_was_search = True  # searching for a share by name is looking
    assert pe.evaluate(BrowserPress(mgr), PressArgs(key="Enter")).verdict != Verdict.DENY
    assert pe.evaluate(BrowserFill(mgr), RefFillArgs(field="Search", value="INFY", submit=True)).verdict != Verdict.DENY
    mgr.current_url = "https://www.amazon.in/cart"  # elsewhere it is a purchase: asked, not refused
    assert pe.evaluate(BrowserClick(mgr), RefClickArgs(text="Buy now")).verdict != Verdict.DENY


def test_a_brokers_order_tools_cannot_be_used_or_switched_on():
    kite = load_catalog(Path(__file__).parents[1] / "config" / "mcp_catalog.yaml").apps["kite"]
    conn = Connection("kite")
    for name in ("place_order", "modify_order", "cancel_order", "place_gtt_order", "modify_gtt_order",
                 "delete_gtt_order"):
        assert is_trade_tool(name)
        assert resolve_risk(kite, conn, {"name": name}) == Risk.SYSTEM
        # the user's own switch and risk choice change nothing
        conn.choices[name] = {"enabled": True, "risk": "read"}
        risk = resolve_risk(kite, conn, {"name": name})
        assert risk == Risk.SYSTEM and not tool_enabled(conn, {"name": name}, risk)
    for name in ("get_holdings", "get_positions", "get_orders", "get_order_history", "get_trades", "get_quotes",
                 "get_ltp", "search_instruments", "login"):
        assert not is_trade_tool(name)
        risk = resolve_risk(kite, conn, {"name": name})
        assert risk == Risk.READ and tool_enabled(conn, {"name": name}, risk)


def test_an_order_tool_of_a_shop_or_a_task_app_is_not_a_trade():
    shop = AppSpec(id="shopify", name="Shopify", url="https://shop.example/mcp", category="Productivity")
    conn = Connection("shopify")
    assert resolve_risk(shop, conn, {"name": "create_order"}) != Risk.SYSTEM
    custom = AppSpec(id="mybroker", name="My Broker API", url="http://127.0.0.1:9000/mcp")
    assert resolve_risk(custom, Connection("mybroker"), {"name": "place_order"}) == Risk.SYSTEM
