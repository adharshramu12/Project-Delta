"""Delta never trades. Not with approval, not in Autopilot, not from a routine.

Looking is fine: holdings, positions, prices, order history, a watchlist. Placing, changing or cancelling an order,
buying or selling a security or a coin, and exiting a position are refused before anything is clicked, typed or
sent. A wrong trade cannot be undone and costs real money, and "the AI misread the page" is not an acceptable
reason for one. This is a hard floor like passwords and payments (Risk.SYSTEM in delta.safety.policy): no setting,
no permission level and no approval turns it off.

Two places enforce it:
- clicks, form submits and Enter in a browser or a desktop app, when the page or window is a trading place
  (`never_trade`);
- tools of connected apps (MCP), by the tool's name (`is_trade_tool`): Zerodha Kite's place_order and its
  siblings can no longer be switched on.

What this cannot see: a trade made through code Delta is asked to write and run (a broker's API from Python).
Running code already needs the user's approval, and docs/SECURITY says so.
"""

from __future__ import annotations

import re

# Where trading happens: brokers, exchanges and trading apps, by address or window title.
TRADING_PLACE = re.compile(
    r"zerodha|\bkite\b|groww|upstox|angel\s?one|angelbroking|\bdhan\b|5paisa|icicidirect|hdfcsec|hdfc\s?sky|"
    r"kotak\s?securities|kotakneo|sharekhan|motilal\s?oswal|\bfyers\b|paytm\s?money|indmoney|smallcase|"
    r"tradingview|binance|coinbase|wazirx|coindcx|coinswitch|kraken|kucoin|bybit|robinhood|e\*?trade|webull|"
    r"interactive\s?brokers|\bibkr\b|thinkorswim|td\s?ameritrade|schwab|fidelity|alpaca|metatrader|\bmt[45]\b|"
    r"\bdemat\b|stock\s?broker|\bbroker(?:age)?\b|\btrading\b|\btrade\b|\bstocks?\b|\bshares?\b|\bequit(?:y|ies)\b|"
    r"\bfutures\b|\boptions\b|\bf&o\b|\bforex\b|\bcrypto(?:currency)?\b|\bexchange\b|mutual\s?funds?|\bnse\b|\bbse\b",
    re.I)
# What a trade is called on a button, a link or a form.
TRADE_ACTION = re.compile(
    r"\b(?:buy|sell|short|place\s+(?:an?\s+|the\s+)?order|order|modify\s+order|cancel\s+order|square\s?off|"
    r"exit(?:\s+(?:position|all|trade))?|close\s+position|convert\s+position|trade|invest|redeem|sip|gtt|"
    r"bracket\s+order|cover\s+order|swap|stake|unstake)\b", re.I)
# Names on a trading page that only show things, although they contain a word above.
_ONLY_LOOKS = re.compile(
    r"\b(?:orders?\s+(?:history|book|list)|order\s*book|trade\s*(?:book|history|log)|orders|order\s+history|"
    r"history|positions|holdings|watchlist|portfolio|funds|reports?|statement|p\s?&\s?l|charts?|"
    r"view|details|options\s+chain|option\s+chain)\b", re.I)
# Tool names of connected apps that place, change or cancel a trade.
TRADE_TOOL = re.compile(
    r"(?:place|modify|cancel|delete|create|submit|exit|convert|close|replace)[_-]?(?:[a-z]+[_-])?"
    r"(?:order|gtt|position|trade|sip|basket)s?\b|(?:^|[_-])(?:buy|sell|trade|liquidate)(?:[_-]|$)", re.I)

REFUSAL = ("Delta never buys, sells or changes an order, in any mode. It can show your holdings, positions and "
           "prices; place the trade yourself.")


def is_trading_place(where: str) -> bool:
    """`where` is a page address, a page title or a window title."""
    return bool(where and TRADING_PLACE.search(where))


def never_trade(name: str, where: str) -> bool:
    """True when clicking/submitting something called `name` at `where` would be a trade."""
    if not name or not is_trading_place(where):
        return False
    if TRADE_ACTION.search(name):
        return not _ONLY_LOOKS.fullmatch(name.strip())
    return False


def is_trade_tool(tool: str) -> bool:
    return bool(TRADE_TOOL.search(tool or ""))
