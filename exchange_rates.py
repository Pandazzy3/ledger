"""Live exchange rate fetching with caching + historical data.

Live rates:     open.er-api.com (no key)
Historical data: frankfurter.app (ECB data, no key)
"""
import time
import datetime as dt
import requests

_CACHE = {}
_HISTORY_CACHE = {}
_CACHE_TTL_SECONDS = 3600
_HISTORY_TTL_SECONDS = 6 * 3600

API_BASE = "https://open.er-api.com/v6/latest"
HISTORY_BASE = "https://api.frankfurter.app"

# Frankfurter only supports these currencies
FRANKFURTER_SUPPORTED = {
    "AUD", "BGN", "BRL", "CAD", "CHF", "CNY", "CZK", "DKK",
    "EUR", "GBP", "HKD", "HUF", "IDR", "ILS", "INR", "ISK",
    "JPY", "KRW", "MXN", "MYR", "NOK", "NZD", "PHP", "PLN",
    "RON", "SEK", "SGD", "THB", "TRY", "USD", "ZAR",
}


# ---------------- LIVE RATES ----------------

def fetch_rates(base_currency="USD"):
    if not base_currency:
        base_currency = "USD"
    base_currency = base_currency.upper()

    cached = _CACHE.get(base_currency)
    if cached and (time.time() - cached["fetched_at"]) < _CACHE_TTL_SECONDS:
        return cached["rates"]

    try:
        response = requests.get(f"{API_BASE}/{base_currency}", timeout=5)
        response.raise_for_status()
        data = response.json()
        if data.get("result") != "success":
            return cached["rates"] if cached else None
        rates = data.get("rates") or {}
        _CACHE[base_currency] = {"rates": rates, "fetched_at": time.time()}
        return rates
    except (requests.RequestException, ValueError):
        return cached["rates"] if cached else None


# ---------------- HISTORICAL ----------------

def fetch_history(base_currency, target_currency, days=30):
    """Fetch daily historical rates. Returns [(date_str, rate), ...] or []."""
    base_currency = (base_currency or "USD").upper()
    target_currency = (target_currency or "USD").upper()

    if base_currency == target_currency:
        return []

    # Frankfurter can't do these currencies
    if (base_currency not in FRANKFURTER_SUPPORTED and base_currency != "EUR"
            or target_currency not in FRANKFURTER_SUPPORTED and target_currency != "EUR"):
        return []

    key = f"{base_currency}_{target_currency}_{days}"
    cached = _HISTORY_CACHE.get(key)
    if cached and (time.time() - cached["fetched_at"]) < _HISTORY_TTL_SECONDS:
        return cached["series"]

    try:
        end = dt.date.today()
        start = end - dt.timedelta(days=days)

        # Frankfurter always returns with EUR as the reference.
        # For any pair X→Y: rate(X→Y) = rate(EUR→Y) / rate(EUR→X)
        params = {"from": start.isoformat(), "to": end.isoformat()}
        currencies_needed = set()
        if base_currency != "EUR":
            currencies_needed.add(base_currency)
        if target_currency != "EUR":
            currencies_needed.add(target_currency)
        if currencies_needed:
            params["to_symbols" if False else "to"] = ",".join(currencies_needed)

        # Frankfurter's "to" param conflicts with the "to" date — use symbol param
        # Their API uses ?from=DATE&to=DATE for dates, and ?symbols= for currencies.
        # When no symbols given, it returns just EUR as base.
        r = requests.get(f"{HISTORY_BASE}/{start.isoformat()}..{end.isoformat()}",
                         params={"to": ",".join(currencies_needed) if currencies_needed else "USD"},
                         timeout=8)
        r.raise_for_status()
        data = r.json()
        rates_by_date = data.get("rates", {})

        series = []
        for date_str in sorted(rates_by_date.keys()):
            day_rates = rates_by_date[date_str]
            # Frankfurter returns EUR-based rates for the requested symbols
            eur_to_base = day_rates.get(base_currency, 1.0) if base_currency != "EUR" else 1.0
            eur_to_target = day_rates.get(target_currency, 1.0) if target_currency != "EUR" else 1.0
            if eur_to_base and eur_to_base > 0:
                rate = eur_to_target / eur_to_base
                series.append((date_str, rate))

        _HISTORY_CACHE[key] = {"series": series, "fetched_at": time.time()}
        return series
    except (requests.RequestException, ValueError, KeyError):
        return []


# ---------------- CONVERSION ----------------

def convert(amount, from_currency, to_currency):
    if not from_currency or not to_currency:
        return None, None
    from_currency = from_currency.upper()
    to_currency = to_currency.upper()
    if from_currency == to_currency:
        return float(amount), 1.0
    rates = fetch_rates(from_currency)
    if not rates or to_currency not in rates:
        return None, None
    rate = float(rates[to_currency])
    return round(float(amount) * rate, 2), rate


# ---------------- RATE BOARD ----------------

def build_rate_board(base_currency, currency_list):
    """Build a board of rates + 24h change. Fast — no per-currency history calls."""
    from translations import SUPPORTED_CURRENCIES

    base_currency = (base_currency or "USD").upper()
    live = fetch_rates(base_currency) or {}

    board = []
    for code in currency_list:
        if code == base_currency:
            continue
        rate = live.get(code)
        if rate is None:
            continue
        name, symbol = SUPPORTED_CURRENCIES.get(code, (code, code))
        board.append({
            "code": code,
            "name": name,
            "symbol": symbol,
            "rate": rate,
            "yesterday": None,
            "change": None,
            "change_pct": None,
        })

    board.sort(key=lambda x: x["code"])
    return board