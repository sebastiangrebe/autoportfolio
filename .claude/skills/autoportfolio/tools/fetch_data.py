#!/usr/bin/env python3
"""
fetch_data.py — Fetch live market data for a list of tickers.

Usage:
    python fetch_data.py NVDA AAPL VUSA.L
    python fetch_data.py --search "high momentum AI stocks"
    python fetch_data.py --search "safe dividend ETFs Europe" --limit 10

Output:
    JSON object keyed by ticker with price, 50-DMA, 200-DMA, RSI-14,
    dividend yield, and 5-day momentum. Tickers that fail are included
    with an "error" field.
"""

import json
import sys

try:
    import yfinance as yf
except ImportError:
    print(json.dumps({"error": "yfinance not installed. Run: pip install yfinance"}))
    sys.exit(1)


def compute_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    last_gain = float(gain.iloc[-1])
    last_loss = float(loss.iloc[-1])
    if last_loss == 0:
        return 100.0
    rs = last_gain / last_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 1)


_FX_CACHE = {}


def _fx_to_usd(currency: str) -> float:
    """Return the rate that multiplies a `currency` price into USD. Cached per process."""
    if not currency or currency.upper() == "USD":
        return 1.0
    cur = currency.upper()
    if cur in _FX_CACHE:
        return _FX_CACHE[cur]
    try:
        pair = yf.Ticker(f"{cur}USD=X")
        hist = pair.history(period="5d", timeout=10)
        if not hist.empty:
            rate = float(hist["Close"].dropna().iloc[-1])
            _FX_CACHE[cur] = rate
            return rate
        info = pair.info or {}
        rate = info.get("regularMarketPrice")
        if rate and rate > 0:
            _FX_CACHE[cur] = float(rate)
            return _FX_CACHE[cur]
    except Exception:
        pass
    return 1.0


def _normalize_dividend_yield(raw):
    """Yahoo sometimes returns dividendYield as a decimal (0.084) and sometimes as a percent (8.4).
    Detect by magnitude: values >= 1 are treated as percent already; < 1 multiplied by 100.
    Clamp sane maximum at 30% — above is almost always a data error."""
    if raw is None:
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 0:
        return None
    pct = val if val >= 1 else val * 100
    if pct > 30:
        return None  # suspicious — drop rather than mislead
    return round(pct, 2)


def _minimal_from_info(ticker, stk, info):
    """Fallback: build a minimal result from ticker.info when history is unavailable."""
    price_fallback = (
        info.get("regularMarketPrice")
        or info.get("currentPrice")
        or info.get("previousClose")
    )
    try:
        if price_fallback is None:
            price_fallback = stk.fast_info.get("lastPrice")
    except Exception:
        pass
    if price_fallback is None:
        return {"error": f"Insufficient data for {ticker}"}

    raw_currency = info.get("currency") or "USD"
    scale_to_base = 1.0
    if raw_currency == "GBp":
        base_currency = "GBP"
        scale_to_base = 0.01
    elif raw_currency == "ZAc":
        base_currency = "ZAR"
        scale_to_base = 0.01
    else:
        base_currency = raw_currency.upper()

    fx_rate = _fx_to_usd(base_currency)
    price_usd = round(float(price_fallback) * scale_to_base * fx_rate, 2)
    div_yield = _normalize_dividend_yield(
        info.get("dividendYield") or info.get("trailingAnnualDividendYield")
    )
    return {
        "name": info.get("shortName") or info.get("longName"),
        "currency": base_currency,
        "price": price_usd,
        "price_native": round(float(price_fallback), 2),
        "ma_50": None, "ma_50_native": None,
        "ma_200": None, "ma_200_native": None,
        "rsi_14": None,
        "dividend_yield_pct": div_yield,
        "momentum_5d_pct": None,
        "sector": info.get("sector"),
        "market_cap": info.get("marketCap"),
        "fx_rate_to_usd": fx_rate if base_currency != "USD" else 1.0,
    }


def fetch_ticker(ticker):
    stk = yf.Ticker(ticker)
    hist = stk.history(period="1y", timeout=15)
    info = stk.info or {}

    if hist.empty or len(hist) < 20:
        return _minimal_from_info(ticker, stk, info)

    close = hist["Close"].dropna()
    if len(close) < 20:
        return _minimal_from_info(ticker, stk, info)
    price_native = round(float(close.iloc[-1]), 2)
    ma_50_native = round(float(close.rolling(50).mean().iloc[-1]), 2) if len(close) >= 50 else None
    ma_200_native = round(float(close.rolling(200).mean().iloc[-1]), 2) if len(close) >= 200 else None
    rsi = compute_rsi(close) if len(close) >= 20 else None

    raw_currency = info.get("currency") or "USD"

    # Yahoo distinguishes pounds ("GBP") from pence ("GBp", lowercase p) by case.
    # Normalize: GBp → pence, scale 0.01 to get GBP base for FX lookup.
    scale_to_base = 1.0
    if raw_currency == "GBp":
        base_currency = "GBP"
        scale_to_base = 0.01
    elif raw_currency == "ZAc":  # South African cents — same pattern
        base_currency = "ZAR"
        scale_to_base = 0.01
    else:
        base_currency = raw_currency.upper()
    currency = base_currency  # reported currency is the unit-converted base

    # Raw Yahoo value in `currency` code
    fx_rate = _fx_to_usd(base_currency)
    price_usd = round(price_native * scale_to_base * fx_rate, 2)
    ma_50_usd = round(ma_50_native * scale_to_base * fx_rate, 2) if ma_50_native else None
    ma_200_usd = round(ma_200_native * scale_to_base * fx_rate, 2) if ma_200_native else None

    div_yield = _normalize_dividend_yield(
        info.get("dividendYield") or info.get("trailingAnnualDividendYield")
    )

    pct_5d = round((price_native / float(close.iloc[-6]) - 1) * 100, 2) if len(close) >= 6 else None

    return {
        "name": info.get("shortName") or info.get("longName"),
        "currency": currency,
        "price": price_usd,
        "price_native": price_native,
        "ma_50": ma_50_usd,
        "ma_50_native": ma_50_native,
        "ma_200": ma_200_usd,
        "ma_200_native": ma_200_native,
        "rsi_14": rsi,
        "dividend_yield_pct": div_yield,
        "momentum_5d_pct": pct_5d,
        "sector": info.get("sector"),
        "market_cap": info.get("marketCap"),
        "fx_rate_to_usd": fx_rate if currency != "USD" else 1.0,
    }


def search_tickers(query, limit=10):
    """Use yfinance search to find tickers matching a query."""
    errors = []

    try:
        results = yf.Search(query, max_results=limit)
        found = []
        for quote in (results.quotes if hasattr(results, 'quotes') else []):
            symbol = quote.get("symbol", "")
            name = quote.get("shortname") or quote.get("longname", "")
            exchange = quote.get("exchange", "")
            qtype = quote.get("quoteType", "")
            if symbol:
                found.append({
                    "ticker": symbol,
                    "name": name,
                    "exchange": exchange,
                    "type": qtype,
                })
        if found:
            return found
        errors.append("yf.Search returned no results")
    except Exception as exc:
        errors.append(f"yf.Search failed: {exc}")

    query_upper = query.strip().upper()
    if " " not in query_upper and len(query_upper) <= 12:
        try:
            stk = yf.Ticker(query_upper)
            info = stk.info or {}
            name = info.get("shortName") or info.get("longName")
            if name:
                return [{
                    "ticker": query_upper,
                    "name": name,
                    "exchange": info.get("exchange", ""),
                    "type": info.get("quoteType", ""),
                }]
            errors.append(f"Direct lookup for '{query_upper}' returned no info")
        except Exception as exc:
            errors.append(f"Direct lookup failed: {exc}")

    return [{"error": "; ".join(errors), "query": query}]


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: python fetch_data.py TICKER1 ... | --search 'query'"}))
        sys.exit(1)

    # Handle --search mode
    if sys.argv[1] == "--search":
        query = " ".join(sys.argv[2:])
        limit = 10
        # Check for --limit flag
        for i, arg in enumerate(sys.argv):
            if arg == "--limit" and i + 1 < len(sys.argv):
                try:
                    limit = int(sys.argv[i + 1])
                except ValueError:
                    pass
                query = query.replace(f"--limit {sys.argv[i + 1]}", "").strip()
        results = search_tickers(query, limit)
        print(json.dumps({"search_query": query, "results": results}, indent=2))
        return

    # Standard ticker fetch mode
    tickers = [t.strip().upper() for t in sys.argv[1:] if t.strip() and not t.startswith("--")]
    results = {}

    for ticker in tickers:
        try:
            results[ticker] = fetch_ticker(ticker)
        except Exception as exc:
            results[ticker] = {"error": str(exc)}

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
