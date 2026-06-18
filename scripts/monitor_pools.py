#!/usr/bin/env python3
"""Multi-DEX high-APR pool monitor.

Sources:
  PancakeSwap V3 BSC      — GeckoTerminal (original filters, logic unchanged)
  Orca Whirlpools         — api.mainnet.orca.so
  Raydium CLMM            — api-v3.raydium.io
  Uniswap V3 Ethereum     — GeckoTerminal
  Uniswap V3 Arbitrum     — GeckoTerminal
  Uniswap V3 Base         — GeckoTerminal
  ProjectX HyperEVM       — Goldsky subgraph (Uniswap V3 schema)

New-source filters (Orca + Uniswap + ProjectX):
  TVL ≥$200K, Vol ≥$100K/day, APR 200–3000%,
  price Δ24h ∈ [-10%, +20%], vol spike ≥2× 6-day avg, top-100 tokens only.

State is namespaced by DEX in state_monitor.json and committed with [skip ci].
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ─────────────────────────────────────────────────────────────────────────────
# Shared
# ─────────────────────────────────────────────────────────────────────────────

TELEGRAM_TOKEN  = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT"]
STATE_FILE      = Path("state_monitor.json")

DECLINE_TVL_PCT = 50
DECLINE_VOL_PCT = 60

# ─────────────────────────────────────────────────────────────────────────────
# PancakeSwap constants — DO NOT CHANGE (original filters)
# ─────────────────────────────────────────────────────────────────────────────

GECKO_BASE    = "https://api.geckoterminal.com/api/v2"
GECKO_HEADERS = {"Accept": "application/json;version=20230302"}

PANCAKE_MIN_APR  = 500
PANCAKE_MIN_VOL  = 50_000
PANCAKE_MIN_FEES = 500
PANCAKE_MAX_TVL  = 5_000_000

BTCB_ADDR = "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c"
WBNB_ADDR = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"

# ─────────────────────────────────────────────────────────────────────────────
# New-source filter constants (Orca + Uniswap)
# ─────────────────────────────────────────────────────────────────────────────

NEW_MIN_TVL    = 200_000
NEW_MIN_VOL    = 100_000
NEW_MIN_APR    = 200.0
NEW_MAX_APR    = 3_000.0
NEW_PRICE_MIN  = -10.0   # %
NEW_PRICE_MAX  =  20.0   # %
NEW_SPIKE_MIN  =   2.0   # × 6-day average
TOP_N_COINS    = 100

# ─────────────────────────────────────────────────────────────────────────────
# Orca
# ─────────────────────────────────────────────────────────────────────────────

ORCA_API = "https://api.mainnet.orca.so/v1/whirlpool/list"

# ─────────────────────────────────────────────────────────────────────────────
# Raydium CLMM (Solana)
# ─────────────────────────────────────────────────────────────────────────────

RAYDIUM_API = (
    "https://api-v3.raydium.io/pools/info/list"
    "?poolType=concentrated&poolSortField=volume24h&sortType=desc&pageSize=100&page={page}"
)

# ─────────────────────────────────────────────────────────────────────────────
# Uniswap V3 — GeckoTerminal
# ─────────────────────────────────────────────────────────────────────────────

UNISWAP_NETWORKS: dict[str, dict] = {
    "ethereum": {
        "gecko_network": "eth",
        "gecko_dex":     "uniswap_v3",
        "app_url":       "https://app.uniswap.org/explore/pools/ethereum/{addr}",
        "label":         "UNISWAP V3 ETHEREUM",
    },
    "arbitrum": {
        "gecko_network": "arbitrum",
        "gecko_dex":     "uniswap_v3_arbitrum",
        "app_url":       "https://app.uniswap.org/explore/pools/arbitrum/{addr}",
        "label":         "UNISWAP V3 ARBITRUM",
    },
    "base": {
        "gecko_network": "base",
        "gecko_dex":     "uniswap-v3-base",
        "app_url":       "https://app.uniswap.org/explore/pools/base/{addr}",
        "label":         "UNISWAP V3 BASE",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# ProjectX — HyperEVM (Goldsky / Uniswap V3 schema)
# ─────────────────────────────────────────────────────────────────────────────

PROJECTX_GRAPHQL = (
    "https://api.goldsky.com/api/public/project_cmbbm2iwckb1b01t39xed236t"
    "/subgraphs/uniswap-v3-hyperevm-position/prod/gn"
)
PROJECTX_APP_URL = "https://app.projectx.finance/#/pool/{addr}"

_PROJECTX_QUERY = """
{
  pools(first: 100, orderBy: totalValueLockedUSD, orderDirection: desc) {
    id
    feeTier
    totalValueLockedUSD
    token0 { symbol }
    token1 { symbol }
    poolDayData(first: 8, orderBy: date, orderDirection: desc) {
      volumeUSD
      feesUSD
      tvlUSD
    }
  }
}
"""

# Symbols we treat as stablecoins (skip for price-Δ filter)
STABLE_SYMBOLS = {
    "usdc", "usdt", "dai", "usds", "busd", "tusd", "gusd", "lusd",
    "frax", "usdp", "usd+", "cusd", "susd", "musd", "mim", "eurc",
}


# ─────────────────────────────────────────────────────────────────────────────
# Shared utilities
# ─────────────────────────────────────────────────────────────────────────────

def send_telegram(message: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"[Telegram] {exc}")
        return False


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def fmt_money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Suggested LP range helpers
# ─────────────────────────────────────────────────────────────────────────────

_RANGE_LOW_VOL = {"btc", "wbtc", "btcb", "eth", "weth"}
_RANGE_MED_VOL = {"sol", "wsol", "bnb", "wbnb", "avax", "wavax"}


def _range_fallback_pct(sym_a: str, sym_b: str) -> float:
    syms = {sym_a.lower(), sym_b.lower()}
    if syms & STABLE_SYMBOLS:
        return 10.0
    if syms & _RANGE_LOW_VOL:
        return 12.0
    if syms & _RANGE_MED_VOL:
        return 15.0
    return 20.0


def _fmt_price(p: float) -> str:
    if p >= 1_000:
        return f"${p:,.0f}"
    if p >= 1.0:
        return f"${p:.2f}"
    if p >= 0.01:
        return f"${p:.4f}"
    if p >= 0.0001:
        return f"${p:.6f}"
    return f"${p:.2e}"


def _avg_spike_days(day_volumes: list[float]) -> float | None:
    if len(day_volumes) < 3:
        return None
    sorted_vols = sorted(day_volumes)
    median_vol  = sorted_vols[len(sorted_vols) // 2]
    if median_vol <= 0:
        return None
    threshold   = 2.0 * median_vol
    runs, run   = [], 0
    for v in day_volumes:
        if v >= threshold:
            run += 1
        elif run:
            runs.append(run)
            run = 0
    if run:
        runs.append(run)
    return (sum(runs) / len(runs)) if runs else None


def _range_line(
    sym_a: str,
    sym_b: str,
    price_base: float | None,
    price_change_24h: float | None,
    day_volumes: list[float] | None = None,
    range_hist: dict | None = None,
    apr: float | None = None,
) -> str:
    spike_count   = range_hist["spike_count"]   if range_hist else 0
    avg_spike_dur = range_hist["avg_spike_dur"] if range_hist else None
    if avg_spike_dur is None and day_volumes:
        avg_spike_dur = _avg_spike_days(day_volumes)
    dur_str = f"{avg_spike_dur:.1f} días" if avg_spike_dur is not None else "N/D"

    low: float | None = None
    high: float | None = None

    if spike_count >= 3 and range_hist:
        spike_prices = range_hist["spike_prices"]
        low  = min(spike_prices) * 0.95
        high = max(spike_prices) * 1.05
        if price_change_24h is not None and price_change_24h > 5.0:
            high *= 1.05
        elif price_change_24h is not None and price_change_24h < -5.0:
            low  *= 0.95
        range_str = f"{_fmt_price(low)} — {_fmt_price(high)} (histórico)"
    else:
        base_pct = _range_fallback_pct(sym_a, sym_b)
        low_pct  = base_pct
        high_pct = base_pct
        if price_change_24h is not None and abs(price_change_24h) > 5.0:
            if price_change_24h > 0:
                high_pct += 5.0
            else:
                low_pct  += 5.0
        if price_base and price_base > 0:
            low  = price_base * (1 - low_pct  / 100)
            high = price_base * (1 + high_pct / 100)
            pct_str   = f"±{base_pct:.0f}%" if low_pct == high_pct else f"-{low_pct:.0f}%/+{high_pct:.0f}%"
            range_str = f"{_fmt_price(low)} — {_fmt_price(high)} ({pct_str})"
        else:
            pct_str   = f"±{base_pct:.0f}%" if low_pct == high_pct else f"-{low_pct:.0f}%/+{high_pct:.0f}%"
            range_str = pct_str

    apr_line = ""
    if low and high and price_base and price_base > 0 and apr is not None:
        ancho = (high - low) / price_base
        if ancho > 0:
            apr_rango = apr / ancho
            if apr_rango > 10_000:
                apr_line = "\n⚠️ APR en rango: >10,000%"
            else:
                apr_line = f"\n💰 APR en rango: {apr_rango:,.0f}%"

    return f"📐 Rango sugerido: {range_str} | ⏱ Duración media spike: {dur_str}{apr_line}"


# ─────────────────────────────────────────────────────────────────────────────
# RSI helpers
# ─────────────────────────────────────────────────────────────────────────────

def _calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains    = [max(d, 0.0) for d in deltas]
    losses   = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def _fetch_rsi(cg_id: str | None, symbol: str, cache: dict) -> float | None:
    if not symbol:
        return None
    key = cg_id or symbol.upper()
    if key in cache:
        return cache[key]

    closes: list[float] = []

    if cg_id:
        url = (
            "https://api.coingecko.com/api/v3/coins"
            f"/{cg_id}/ohlc?vs_currency=usd&days=14"
        )
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 429:
                    print(f"[RSI/CoinGecko] rate limit — waiting 15s…")
                    time.sleep(15)
                    continue
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        closes = [float(row[4]) for row in data]
                    break
            except Exception as exc:
                print(f"[RSI/CoinGecko] {exc}")
                if attempt < 2:
                    time.sleep(2)

    if not closes:
        sym_upper = symbol.upper()
        for quote in ("USDT", "USDC", "BUSD"):
            try:
                url = (
                    "https://api.binance.com/api/v3/klines"
                    f"?symbol={sym_upper}{quote}&interval=1d&limit=50"
                )
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    data = resp.json()
                    if data:
                        closes = [float(row[4]) for row in data]
                    break
            except Exception as exc:
                print(f"[RSI/Binance] {sym_upper}{quote}: {exc}")

    rsi = _calc_rsi(closes) if closes else None
    cache[key] = rsi
    return rsi


def _rsi_line(rsi: float | None) -> str:
    if rsi is None:
        return "📊 RSI: N/D"
    v = f"{rsi:.0f}"
    if rsi < 30:
        return f"📊 RSI: {v} — 🟢 Buena entrada (sobrevendido)"
    if rsi < 50:
        return f"📊 RSI: {v} — 🟡 Entrada aceptable (momentum bajista)"
    if rsi < 70:
        return f"📊 RSI: {v} — 🟠 Precaución (momentum alcista)"
    return f"📊 RSI: {v} — 🔴 Evitar (sobrecomprado)"


def _enrich_rsi(qualifying: list[dict], rsi_cache: dict) -> None:
    for m in qualifying:
        m["rsi"] = _fetch_rsi(m.get("cg_id_main"), m.get("sym_main", ""), rsi_cache)


def _fetch_range_history(cg_id: str | None, cache: dict) -> dict | None:
    if not cg_id:
        return None
    key = f"rng_{cg_id}"
    if key in cache:
        return cache[key]
    url = (
        f"https://api.coingecko.com/api/v3/coins/{cg_id}"
        "/market_chart?vs_currency=usd&days=30&interval=daily"
    )
    result = None
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=20)
            if resp.status_code == 429:
                print(f"[Range/CoinGecko] rate limit — waiting 15s…")
                time.sleep(15)
                continue
            if resp.status_code == 200:
                data = resp.json()
                prices_raw = data.get("prices", [])
                vols_raw   = data.get("total_volumes", [])
                if len(prices_raw) >= 10 and len(vols_raw) >= 10:
                    prices = [float(p[1]) for p in prices_raw]
                    vols   = [float(v[1]) for v in vols_raw]
                    spike_prices: list[float] = []
                    runs: list[int] = []
                    run = 0
                    for i in range(6, len(vols)):
                        prev = vols[i - 6:i]
                        mean_prev = sum(prev) / 6
                        if mean_prev > 0 and vols[i] >= 2 * mean_prev:
                            spike_prices.append(prices[i])
                            run += 1
                        else:
                            if run:
                                runs.append(run)
                                run = 0
                    if run:
                        runs.append(run)
                    avg_dur = sum(runs) / len(runs) if runs else None
                    result = {
                        "spike_prices":  spike_prices,
                        "spike_count":   len(spike_prices),
                        "avg_spike_dur": avg_dur,
                    }
                break
        except Exception as exc:
            print(f"[Range/CoinGecko] {exc}")
            if attempt < 2:
                time.sleep(2)
    cache[key] = result
    return result


def _enrich_range(qualifying: list[dict], rsi_cache: dict) -> None:
    for m in qualifying:
        m["range_hist"] = _fetch_range_history(m.get("cg_id_main"), rsi_cache)


# ─────────────────────────────────────────────────────────────────────────────
# CoinGecko top-N (fetched once per run, shared by all new sources)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_top_coins(n: int = TOP_N_COINS) -> dict[str, dict]:
    """Return {coingecko_id: {symbol, name, change_24h}} for top-N coins."""
    url = (
        "https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=usd&order=market_cap_desc&per_page={n}&page=1&sparkline=false"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            return {
                coin["id"]: {
                    "symbol":     coin["symbol"].lower(),
                    "name":       coin["name"],
                    "change_24h": coin.get("price_change_percentage_24h"),
                }
                for coin in resp.json()
            }
        except Exception as exc:
            print(f"[CoinGecko] Attempt {attempt + 1}: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return {}


# ─────────────────────────────────────────────────────────────────────────────
# New-source shared filter + alert builders
# ─────────────────────────────────────────────────────────────────────────────

def passes_new_filters(m: dict) -> bool:
    if m["tvl"] < NEW_MIN_TVL:
        return False
    if m["vol_24h"] < NEW_MIN_VOL:
        return False
    if not (NEW_MIN_APR <= m["apr"] <= NEW_MAX_APR):
        return False
    pc = m.get("price_change_24h")
    if pc is not None and not (NEW_PRICE_MIN <= pc <= NEW_PRICE_MAX):
        return False
    spike = m.get("volume_spike")
    if spike is not None and spike < NEW_SPIKE_MIN:
        return False
    if not m.get("in_top100"):
        return False
    return True


def _build_new_alert(m: dict, label: str) -> str:
    pc    = m.get("price_change_24h")
    spike = m.get("volume_spike") or 0.0
    pc_line = f"Precio Δ24h: {pc:+.1f}%\n" if pc is not None else ""
    body = (
        f"Pool:        {m['name']}\n"
        f"APR:         {m['apr']:,.0f}%\n"
        f"Vol 24h:     {fmt_money(m['vol_24h'])}\n"
        f"Fees 24h:    {fmt_money(m['fees_24h'])}\n"
        f"TVL:         {fmt_money(m['tvl'])}\n"
        f"Vol Spike:   {spike:.1f}×\n"
        f"{pc_line}"
    ).rstrip()
    title = m.get("alert_title") or f"🚨 NUEVA OPORTUNIDAD — {label} {m['badge']}"
    rng = _range_line(
        m.get("sym_a", "?"),
        m.get("sym_b", "?"),
        m.get("price_base"),
        pc,
        m.get("day_volumes"),
        m.get("range_hist"),
        m.get("apr"),
    )
    return (
        f"{title}\n\n"
        f"<pre>{body}</pre>\n\n"
        f"{rng}\n"
        f"{_rsi_line(m.get('rsi'))}\n\n"
        f"🔗 {m['pool_url']}"
    )


def _build_new_decline(
    m: dict, label: str, reason: str, prev_tvl: float, prev_vol: float
) -> str:
    body = (
        f"Pool:      {m['name']}\n"
        f"Motivo:    {reason}\n"
        f"TVL:       {fmt_money(m['tvl'])} (ant: {fmt_money(prev_tvl)})\n"
        f"Vol 24h:   {fmt_money(m['vol_24h'])} (ant: {fmt_money(prev_vol)})\n"
        f"Fees 24h:  {fmt_money(m['fees_24h'])}"
    )
    return (
        f"⚠️ POOL DECLIVE — {label}\n\n"
        f"<pre>{body}</pre>\n\n"
        f"🔗 {m['pool_url']}"
    )


def _process_new_source(
    qualifying: list[dict],
    label: str,
    ns_state: dict,
    now_iso: str,
) -> int:
    alerted: dict = ns_state.setdefault("alerted_pools", {})
    alerts_sent = 0

    for m in qualifying:
        pid = m["address"]

        if pid not in alerted:
            if send_telegram(_build_new_alert(m, label)):
                alerts_sent += 1
            alerted[pid] = {
                "first_seen":     now_iso,
                "last_tvl":       m["tvl"],
                "last_vol_24h":   m["vol_24h"],
                "alerted_decline": False,
                "name":           m["name"],
            }
        else:
            prev     = alerted[pid]
            prev_tvl = prev.get("last_tvl", m["tvl"])
            prev_vol = prev.get("last_vol_24h", m["vol_24h"])

            if not prev.get("alerted_decline"):
                tvl_chg = (m["tvl"] - prev_tvl) / prev_tvl if prev_tvl > 0 else 0
                vol_chg = (m["vol_24h"] - prev_vol) / prev_vol if prev_vol > 0 else 0

                reason = None
                if tvl_chg <= -(DECLINE_TVL_PCT / 100):
                    reason = f"TVL cayó {tvl_chg * 100:.1f}% (>{DECLINE_TVL_PCT}%)"
                elif vol_chg <= -(DECLINE_VOL_PCT / 100):
                    reason = f"Vol cayó {vol_chg * 100:.1f}% (>{DECLINE_VOL_PCT}%)"

                if reason:
                    if send_telegram(_build_new_decline(m, label, reason, prev_tvl, prev_vol)):
                        alerts_sent += 1
                    prev["alerted_decline"] = True

            prev["last_tvl"]    = m["tvl"]
            prev["last_vol_24h"] = m["vol_24h"]

    return alerts_sent


# ─────────────────────────────────────────────────────────────────────────────
# PancakeSwap V3 BSC — original logic, do not modify
# ─────────────────────────────────────────────────────────────────────────────

def _parse_fee_tier(pool_name: str) -> float:
    m = re.search(r"([\d.]+)%\s*$", pool_name.strip())
    return float(m.group(1)) / 100 if m else 0.0


def _fetch_pancake_pools(max_pages: int = 20) -> list[dict]:
    pools: list[dict] = []
    for page in range(1, max_pages + 1):
        url = (
            f"{GECKO_BASE}/networks/bsc/dexes/pancakeswap-v3-bsc/pools"
            f"?sort=h24_volume_usd_desc&page={page}"
        )
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=GECKO_HEADERS, timeout=20)
            except Exception as exc:
                print(f"[PancakeSwap] Network error page {page} attempt {attempt + 1}: {exc}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                wait = max(15, int(resp.headers.get("Retry-After", 15)))
                print(f"[PancakeSwap] Rate limited, waiting {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"[PancakeSwap] HTTP {resp.status_code} on page {page}")
                resp = None
            break
        if resp is None:
            break
        data = resp.json().get("data", [])
        if not data:
            break
        pools.extend(data)
        last_vol = float(data[-1]["attributes"]["volume_usd"].get("h24") or 0)
        if last_vol < PANCAKE_MIN_VOL:
            break
        time.sleep(0.4)
    return pools


def _compute_pancake_metrics(pool: dict) -> dict | None:
    attr = pool["attributes"]
    name = attr.get("name", "")
    fee_tier = _parse_fee_tier(name)
    if fee_tier == 0.0:
        return None
    vol_24h = float(attr["volume_usd"].get("h24") or 0)
    vol_h1  = float(attr["volume_usd"].get("h1")  or 0)
    tvl = float(attr.get("reserve_in_usd") or 0)
    if tvl <= 0:
        return None
    vol_for_apr = max(vol_24h, vol_h1 * 24)
    if vol_for_apr <= 0:
        return None
    fees_24h = vol_for_apr * fee_tier
    apr = (fees_24h / tvl) * 365 * 100
    rels     = pool.get("relationships", {})
    base_id  = rels.get("base_token",  {}).get("data", {}).get("id", "").lower()
    quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "").lower()
    is_btcb  = BTCB_ADDR in (base_id + quote_id)
    is_wbnb  = WBNB_ADDR in (base_id + quote_id)
    price_chg_h24 = float(attr.get("price_change_percentage", {}).get("h24") or 0)
    price_chg_h1  = float(attr.get("price_change_percentage", {}).get("h1")  or 0)
    _sm = re.match(r"^(.+?)\s*/\s*(.+?)\s+[\d.]+%", name)
    sym_a      = _sm.group(1).strip() if _sm else "?"
    sym_b      = _sm.group(2).strip() if _sm else "?"
    price_base = float(attr.get("base_token_price_usd") or 0) or None
    _a_stable  = sym_a.lower() in STABLE_SYMBOLS
    cg_id_main = None
    sym_main   = sym_a if not _a_stable else (sym_b if sym_b.lower() not in STABLE_SYMBOLS else "")
    return {
        "pool_id":       pool["id"],
        "address":       attr["address"],
        "name":          name,
        "fee_tier":      fee_tier,
        "vol_24h":       vol_24h,
        "vol_h1":        vol_h1,
        "tvl":           tvl,
        "fees_24h":      fees_24h,
        "apr":           apr,
        "is_btcb":       is_btcb,
        "is_wbnb":       is_wbnb,
        "price_chg_h24": price_chg_h24,
        "price_chg_h1":  price_chg_h1,
        "sym_a":         sym_a,
        "sym_b":         sym_b,
        "price_base":    price_base,
        "cg_id_main":    cg_id_main,
        "sym_main":      sym_main,
    }


def _passes_pancake_filters(m: dict) -> bool:
    vol_ok = m["vol_24h"] >= PANCAKE_MIN_VOL or m["vol_h1"] * 24 >= PANCAKE_MIN_VOL
    return (
        m["apr"] >= PANCAKE_MIN_APR
        and vol_ok
        and m["fees_24h"] >= PANCAKE_MIN_FEES
        and m["tvl"] <= PANCAKE_MAX_TVL
    )


def _build_pancake_alert(m: dict) -> str:
    btcb_badge = " 🔥 BTCB" if m["is_btcb"] else ""
    rng = _range_line(
        m.get("sym_a", "?"),
        m.get("sym_b", "?"),
        m.get("price_base"),
        m.get("price_chg_h24"),
        None,
        m.get("range_hist"),
        m.get("apr"),
    )
    return (
        f"🚨 <b>NUEVA OPORTUNIDAD{btcb_badge}</b>\n"
        f"<b>Pool:</b> {m['name']}\n"
        f"<b>APR:</b> {m['apr']:,.0f}%\n"
        f"<b>Volumen 24h:</b> {fmt_money(m['vol_24h'])}\n"
        f"<b>Fees 24h:</b> {fmt_money(m['fees_24h'])}\n"
        f"<b>TVL:</b> {fmt_money(m['tvl'])}\n"
        f"<b>Fee tier:</b> {m['fee_tier']*100:.2f}%\n"
        f"<b>Precio Δ1h:</b> {m['price_chg_h1']:+.2f}%  "
        f"<b>Δ24h:</b> {m['price_chg_h24']:+.2f}%\n"
        f"{rng}\n"
        f"{_rsi_line(m.get('rsi'))}\n"
        f"🔗 <a href='https://pancakeswap.finance/info/v3/pairs/{m['address']}'>PancakeSwap V3</a>"
    )


def _build_pancake_decline(m: dict, reason: str, prev_tvl: float, prev_vol: float) -> str:
    return (
        f"⚠️ <b>DECLIVE DETECTADO</b>\n"
        f"<b>Pool:</b> {m['name']}\n"
        f"<b>Motivo:</b> {reason}\n"
        f"<b>TVL:</b> {fmt_money(m['tvl'])} (ant: {fmt_money(prev_tvl)})\n"
        f"<b>Vol 24h:</b> {fmt_money(m['vol_24h'])} (ant: {fmt_money(prev_vol)})\n"
        f"<b>Fees 24h:</b> {fmt_money(m['fees_24h'])}\n"
        f"🔗 <a href='https://pancakeswap.finance/info/v3/pairs/{m['address']}'>PancakeSwap V3</a>"
    )


def run_pancakeswap(ns_state: dict, now_iso: str, rsi_cache: dict) -> int:
    print("[PancakeSwap] Fetching pools…")
    raw_pools = _fetch_pancake_pools()
    print(f"[PancakeSwap] Fetched {len(raw_pools)} pools")

    qualifying: list[dict] = []
    for pool in raw_pools:
        m = _compute_pancake_metrics(pool)
        if m and _passes_pancake_filters(m):
            qualifying.append(m)

    qualifying.sort(key=lambda x: (-int(x["is_btcb"]), -x["apr"]))
    print(f"[PancakeSwap] {len(qualifying)} pools pass filters")
    _enrich_rsi(qualifying, rsi_cache)
    _enrich_range(qualifying, rsi_cache)

    alerted: dict = ns_state.setdefault("alerted_pools", {})
    alerts_sent = 0
    for m in qualifying:
        pid = m["pool_id"]
        if pid not in alerted:
            if send_telegram(_build_pancake_alert(m)):
                alerts_sent += 1
            alerted[pid] = {
                "first_seen":      now_iso,
                "last_alert":      now_iso,
                "last_tvl":        m["tvl"],
                "last_vol_24h":    m["vol_24h"],
                "alerted_decline": False,
                "name":            m["name"],
            }
        else:
            prev     = alerted[pid]
            prev_tvl = prev.get("last_tvl", m["tvl"])
            prev_vol = prev.get("last_vol_24h", m["vol_24h"])
            if not prev.get("alerted_decline"):
                tvl_chg = (m["tvl"] - prev_tvl) / prev_tvl if prev_tvl > 0 else 0
                vol_chg = (m["vol_24h"] - prev_vol) / prev_vol if prev_vol > 0 else 0
                decline_reason = None
                if tvl_chg <= -(DECLINE_TVL_PCT / 100):
                    decline_reason = f"TVL cayó {tvl_chg * 100:.1f}% (>{DECLINE_TVL_PCT}%)"
                elif vol_chg <= -(DECLINE_VOL_PCT / 100):
                    decline_reason = f"Volumen cayó {vol_chg * 100:.1f}% (>{DECLINE_VOL_PCT}%)"
                if decline_reason:
                    if send_telegram(_build_pancake_decline(m, decline_reason, prev_tvl, prev_vol)):
                        alerts_sent += 1
                    prev["alerted_decline"] = True
            prev["last_tvl"]     = m["tvl"]
            prev["last_vol_24h"] = m["vol_24h"]

    ns_state["qualifying_count"] = len(qualifying)
    return alerts_sent


# ─────────────────────────────────────────────────────────────────────────────
# Orca Whirlpools
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_orca_pools() -> list[dict]:
    for attempt in range(3):
        try:
            resp = requests.get(ORCA_API, timeout=30)
            resp.raise_for_status()
            return resp.json().get("whirlpools", [])
        except Exception as exc:
            print(f"[Orca] Attempt {attempt + 1}: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return []


def _compute_orca_metrics(pool: dict, top_coins: dict[str, dict]) -> dict | None:
    tvl      = float(pool.get("tvl") or 0)
    vol_day  = float((pool.get("volume") or {}).get("day")  or 0)
    vol_week = float((pool.get("volume") or {}).get("week") or 0)
    fee_rate = float(pool.get("lpFeeRate") or 0)

    if tvl <= 0 or vol_day <= 0 or fee_rate <= 0:
        return None

    fees_24h = vol_day * fee_rate
    apr      = (fees_24h / tvl) * 365 * 100

    # Spike filter skipped for Orca (weekly vol not granular enough to be reliable)

    token_a = pool.get("tokenA") or {}
    token_b = pool.get("tokenB") or {}
    sym_a   = token_a.get("symbol", "?").strip()
    sym_b   = token_b.get("symbol", "?").strip()
    cg_a    = (token_a.get("coingeckoId") or "").strip()
    cg_b    = (token_b.get("coingeckoId") or "").strip()

    in_a     = cg_a in top_coins
    in_b     = cg_b in top_coins
    in_top100 = in_a or in_b

    # Price Δ: use the non-stable top-20 token's CoinGecko 24h change
    price_change_24h: float | None = None
    for cg_id, is_in in ((cg_a, in_a), (cg_b, in_b)):
        if is_in and top_coins[cg_id]["symbol"] not in STABLE_SYMBOLS:
            price_change_24h = top_coins[cg_id].get("change_24h")
            break

    price_base = float(token_a.get("usdPrice") or 0) or None
    _a_stable  = sym_a.lower() in STABLE_SYMBOLS
    _b_stable  = sym_b.lower() in STABLE_SYMBOLS
    if not _a_stable:
        cg_id_main, sym_main = cg_a or None, sym_a
    elif not _b_stable:
        cg_id_main, sym_main = cg_b or None, sym_b
    else:
        cg_id_main, sym_main = None, ""
    fee_pct    = fee_rate * 100
    name       = f"{sym_a}/{sym_b} ({fee_pct:.2f}%)"

    return {
        "address":          pool["address"],
        "name":             name,
        "tvl":              tvl,
        "vol_24h":          vol_day,
        "fees_24h":         fees_24h,
        "apr":              apr,
        "volume_spike":     None,
        "price_change_24h": price_change_24h,
        "in_top100":        in_top100,
        "badge":            "🟢" if in_top100 else "🟡",
        "alert_title":      f"🌊 ORCA — POOL ESTABLE {'🟢' if in_top100 else '🟡'}",
        "pool_url":         f"https://birdeye.so/pool/{pool['address']}?chain=solana",
        "sym_a":            sym_a,
        "sym_b":            sym_b,
        "price_base":       price_base,
        "day_volumes":      None,
        "cg_id_main":       cg_id_main,
        "sym_main":         sym_main,
    }


def run_orca(ns_state: dict, now_iso: str, top_coins: dict[str, dict], rsi_cache: dict) -> int:
    print("[Orca] Fetching whirlpools…")
    pools = _fetch_orca_pools()
    print(f"[Orca] Fetched {len(pools)} pools")

    qualifying: list[dict] = []
    for pool in pools:
        m = _compute_orca_metrics(pool, top_coins)
        if m and passes_new_filters(m):
            qualifying.append(m)

    qualifying.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))
    print(f"[Orca] {len(qualifying)} pools pass filters")
    _enrich_rsi(qualifying, rsi_cache)
    _enrich_range(qualifying, rsi_cache)
    ns_state["qualifying_count"] = len(qualifying)
    return _process_new_source(qualifying, "ORCA", ns_state, now_iso)


# ─────────────────────────────────────────────────────────────────────────────
# Uniswap V3 via GeckoTerminal (Arbitrum + Base)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_uniswap_pools(chain: str, max_pages: int = 5) -> tuple[list[dict], dict[str, str]]:
    """Fetch Uniswap V3 pools for a given chain from GeckoTerminal.

    Returns (pools, token_cg_map).
    """
    cfg     = UNISWAP_NETWORKS[chain]
    network = cfg["gecko_network"]
    dex     = cfg["gecko_dex"]
    label   = cfg["label"]

    pools: list[dict] = []
    token_cg_map: dict[str, str] = {}

    for page in range(1, max_pages + 1):
        url = (
            f"{GECKO_BASE}/networks/{network}/dexes/{dex}/pools"
            f"?sort=h24_volume_usd_desc&page={page}&include=base_token,quote_token"
        )
        resp = None
        for attempt in range(3):
            try:
                resp = requests.get(url, headers=GECKO_HEADERS, timeout=20)
            except Exception as exc:
                print(f"[{label}] Network error page {page} attempt {attempt+1}: {exc}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429:
                wait = max(15, int(resp.headers.get("Retry-After", 15)))
                print(f"[{label}] Rate limited, waiting {wait}s…")
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                print(f"[{label}] HTTP {resp.status_code} on page {page}")
                resp = None
            break

        if resp is None:
            break

        body = resp.json()
        data = body.get("data", [])
        if not data:
            break

        pools.extend(data)

        for item in body.get("included", []):
            if item.get("type") == "token":
                cg_id = item.get("attributes", {}).get("coingecko_coin_id") or ""
                if cg_id:
                    token_cg_map[item["id"]] = cg_id

        last_vol = float(data[-1]["attributes"]["volume_usd"].get("h24") or 0)
        if last_vol < NEW_MIN_VOL:
            break

        time.sleep(0.4)

    return pools, token_cg_map


def _compute_uniswap_metrics(
    pool: dict,
    chain: str,
    top_coins: dict[str, dict],
    token_cg_map: dict[str, str],
) -> dict | None:
    attr = pool["attributes"]
    name = attr.get("name", "")

    fee_tier = _parse_fee_tier(name)
    if fee_tier == 0.0:
        return None

    vol_24h = float(attr["volume_usd"].get("h24") or 0)
    vol_h1  = float(attr["volume_usd"].get("h1")  or 0)
    tvl     = float(attr.get("reserve_in_usd") or 0)

    if tvl <= 0:
        return None

    vol_for_apr = max(vol_24h, vol_h1 * 24)
    if vol_for_apr <= 0:
        return None

    fees_24h = vol_for_apr * fee_tier
    apr      = (fees_24h / tvl) * 365 * 100

    # Price Δ24h directly from GeckoTerminal
    pc_raw = attr.get("price_change_percentage", {}).get("h24")
    price_change_24h: float | None = float(pc_raw) if pc_raw is not None else None

    # Top-100 via CoinGecko IDs from included token data
    rels     = pool.get("relationships", {})
    base_id  = rels.get("base_token",  {}).get("data", {}).get("id", "")
    quote_id = rels.get("quote_token", {}).get("data", {}).get("id", "")
    base_cg  = token_cg_map.get(base_id,  "")
    quote_cg = token_cg_map.get(quote_id, "")
    in_top100 = bool(
        (base_cg  and base_cg  in top_coins) or
        (quote_cg and quote_cg in top_coins)
    )

    _sm        = re.match(r"^(.+?)\s*/\s*(.+?)\s+[\d.]+%", name)
    sym_a      = _sm.group(1).strip() if _sm else "?"
    sym_b      = _sm.group(2).strip() if _sm else "?"
    price_base = float(attr.get("base_token_price_usd") or 0) or None
    _a_stable  = sym_a.lower() in STABLE_SYMBOLS
    _b_stable  = sym_b.lower() in STABLE_SYMBOLS
    if not _a_stable:
        cg_id_main, sym_main = base_cg or None, sym_a
    elif not _b_stable:
        cg_id_main, sym_main = quote_cg or None, sym_b
    else:
        cg_id_main, sym_main = None, ""

    cfg      = UNISWAP_NETWORKS[chain]
    pool_url = cfg["app_url"].format(addr=attr["address"])

    return {
        "address":          attr["address"],
        "name":             name,
        "tvl":              tvl,
        "vol_24h":          vol_24h,
        "fees_24h":         fees_24h,
        "apr":              apr,
        "volume_spike":     None,
        "price_change_24h": price_change_24h,
        "in_top100":        in_top100,
        "badge":            "🟢" if in_top100 else "🟡",
        "pool_url":         pool_url,
        "sym_a":            sym_a,
        "sym_b":            sym_b,
        "price_base":       price_base,
        "day_volumes":      None,
        "cg_id_main":       cg_id_main,
        "sym_main":         sym_main,
    }


def run_uniswap(
    chain: str,
    ns_state: dict,
    now_iso: str,
    top_coins: dict[str, dict],
    rsi_cache: dict,
) -> int:
    label = UNISWAP_NETWORKS[chain]["label"]
    print(f"[{label}] Fetching pools from GeckoTerminal…")

    raw_pools, token_cg_map = _fetch_uniswap_pools(chain)
    print(f"[{label}] Fetched {len(raw_pools)} pools, {len(token_cg_map)} token CG IDs")

    qualifying: list[dict] = []
    for pool in raw_pools:
        m = _compute_uniswap_metrics(pool, chain, top_coins, token_cg_map)
        if m and passes_new_filters(m):
            qualifying.append(m)

    qualifying.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))
    print(f"[{label}] {len(qualifying)} pools pass filters")
    _enrich_rsi(qualifying, rsi_cache)
    _enrich_range(qualifying, rsi_cache)
    ns_state["qualifying_count"] = len(qualifying)
    return _process_new_source(qualifying, label, ns_state, now_iso)


# ─────────────────────────────────────────────────────────────────────────────
# ProjectX HyperEVM — Goldsky subgraph
# ─────────────────────────────────────────────────────────────────────────────

def _graphql_post(url: str, query: str) -> dict | None:
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"query": query}, timeout=60)
            if resp.status_code == 200:
                payload = resp.json()
                if "errors" in payload:
                    print(f"[GraphQL] errors: {payload['errors'][:1]}")
                    return None
                return payload.get("data")
            print(f"[GraphQL] HTTP {resp.status_code}")
        except Exception as exc:
            print(f"[GraphQL] attempt {attempt + 1}: {exc}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None


def _compute_projectx_metrics(
    pool: dict,
    top_coins: dict[str, dict],
) -> dict | None:
    tvl = float(pool.get("totalValueLockedUSD") or 0)
    if tvl <= 0:
        return None

    day_data = pool.get("poolDayData") or []
    if not day_data:
        return None

    # day_data[0] = most recent (may be partial); use [1] as last complete day
    ri = 1 if len(day_data) > 1 else 0
    vol_24h  = float(day_data[ri].get("volumeUSD") or 0)
    fees_24h = float(day_data[ri].get("feesUSD")   or 0)
    tvl_snap = float(day_data[ri].get("tvlUSD")    or tvl)

    if vol_24h <= 0:
        return None

    apr = (fees_24h / tvl_snap) * 365 * 100 if tvl_snap > 0 else 0.0

    # Volume spike: last complete day vs avg of prior 6 complete days
    prior = day_data[ri + 1: ri + 7]
    if len(prior) >= 2:
        prior_vols  = [float(d.get("volumeUSD") or 0) for d in prior]
        six_day_avg = sum(prior_vols) / len(prior_vols)
        vol_spike   = vol_24h / six_day_avg if six_day_avg > 0 else 0.0
    else:
        vol_spike = None  # insufficient history → spike check skipped

    t0   = pool.get("token0") or {}
    t1   = pool.get("token1") or {}
    sym0 = t0.get("symbol", "?")
    sym1 = t1.get("symbol", "?")

    top_syms  = {info["symbol"].lower() for info in top_coins.values()}
    in_top100 = sym0.lower() in top_syms or sym1.lower() in top_syms

    fee_bps     = int(pool.get("feeTier") or 0)
    fee_pct_str = f"{fee_bps / 10_000:.2f}%"
    name        = f"{sym0}/{sym1} ({fee_pct_str})"

    all_vols   = [float(d.get("volumeUSD") or 0) for d in day_data]
    _a_stable  = sym0.lower() in STABLE_SYMBOLS
    cg_id_main = None
    sym_main   = sym0 if not _a_stable else (sym1 if sym1.lower() not in STABLE_SYMBOLS else "")

    return {
        "address":          pool["id"],
        "name":             name,
        "tvl":              tvl,
        "vol_24h":          vol_24h,
        "fees_24h":         fees_24h,
        "apr":              apr,
        "volume_spike":     vol_spike,
        "price_change_24h": None,
        "in_top100":        in_top100,
        "badge":            "🟢" if in_top100 else "🟡",
        "pool_url":         PROJECTX_APP_URL.format(addr=pool["id"]),
        "sym_a":            sym0,
        "sym_b":            sym1,
        "price_base":       None,
        "day_volumes":      all_vols if len(all_vols) >= 3 else None,
        "cg_id_main":       cg_id_main,
        "sym_main":         sym_main,
    }


def run_projectx(ns_state: dict, now_iso: str, top_coins: dict[str, dict], rsi_cache: dict) -> int:
    label = "PROJECTX HYPERLIQUID"
    print(f"[{label}] Querying Goldsky subgraph…")
    data = _graphql_post(PROJECTX_GRAPHQL, _PROJECTX_QUERY)
    if not data:
        print(f"[{label}] No data — skipping.")
        ns_state["qualifying_count"] = 0
        return 0

    raw_pools = data.get("pools") or []
    print(f"[{label}] Got {len(raw_pools)} pools")

    qualifying: list[dict] = []
    for pool in raw_pools:
        m = _compute_projectx_metrics(pool, top_coins)
        if m and passes_new_filters(m):
            qualifying.append(m)

    qualifying.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))
    print(f"[{label}] {len(qualifying)} pools pass filters")
    _enrich_rsi(qualifying, rsi_cache)
    _enrich_range(qualifying, rsi_cache)
    ns_state["qualifying_count"] = len(qualifying)
    return _process_new_source(qualifying, label, ns_state, now_iso)


# ─────────────────────────────────────────────────────────────────────────────
# Raydium CLMM (Solana)
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_raydium_pools(max_pages: int = 5) -> list[dict]:
    pools: list[dict] = []
    for page in range(1, max_pages + 1):
        url = RAYDIUM_API.format(page=page)
        resp = None
        for attempt in range(3):
            try:
                r = requests.get(url, timeout=30)
                r.raise_for_status()
                resp = r
                break
            except Exception as exc:
                print(f"[Raydium] page {page} attempt {attempt+1}: {exc}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
        if resp is None:
            break
        data = resp.json().get("data", {}).get("data", [])
        if not data:
            break
        pools.extend(data)
        last_vol = float((data[-1].get("day") or {}).get("volume") or 0)
        if last_vol < NEW_MIN_VOL:
            break
    return pools


def _compute_raydium_metrics(pool: dict, top_coins: dict[str, dict]) -> dict | None:
    tvl      = float(pool.get("tvl") or 0)
    vol_day  = float((pool.get("day")  or {}).get("volume")    or 0)
    vol_week = float((pool.get("week") or {}).get("volume")    or 0)
    fees_24h = float((pool.get("day")  or {}).get("volumeFee") or 0)

    if tvl <= 0 or vol_day <= 0 or fees_24h <= 0:
        return None

    apr = (fees_24h / tvl) * 365 * 100

    six_day_vol = max(vol_week - vol_day, 0.0)
    six_day_avg = six_day_vol / 6 if six_day_vol > 0 else 0.0
    vol_spike   = vol_day / six_day_avg if six_day_avg > 0 else None

    mint_a = pool.get("mintA") or {}
    mint_b = pool.get("mintB") or {}
    sym_a  = (mint_a.get("symbol") or "?").strip()
    sym_b  = (mint_b.get("symbol") or "?").strip()

    top_syms  = {info["symbol"].lower() for info in top_coins.values()}
    in_top100 = sym_a.lower() in top_syms or sym_b.lower() in top_syms

    price_base_raw = mint_a.get("price") or pool.get("price")
    price_base     = float(price_base_raw) if price_base_raw else None
    _a_stable      = sym_a.lower() in STABLE_SYMBOLS
    cg_id_main     = None
    sym_main       = sym_a if not _a_stable else (sym_b if sym_b.lower() not in STABLE_SYMBOLS else "")

    fee_pct = float(pool.get("feeRate") or 0) * 100
    name    = f"{sym_a}/{sym_b} ({fee_pct:.2f}%)"

    pool_id = pool.get("id", "")
    return {
        "address":          pool_id,
        "name":             name,
        "tvl":              tvl,
        "vol_24h":          vol_day,
        "fees_24h":         fees_24h,
        "apr":              apr,
        "volume_spike":     vol_spike,
        "price_change_24h": None,
        "in_top100":        in_top100,
        "badge":            "🟢" if in_top100 else "🟡",
        "pool_url":         f"https://birdeye.so/pool/{pool_id}?chain=solana",
        "sym_a":            sym_a,
        "sym_b":            sym_b,
        "price_base":       price_base,
        "day_volumes":      None,
        "cg_id_main":       cg_id_main,
        "sym_main":         sym_main,
    }


def run_raydium(ns_state: dict, now_iso: str, top_coins: dict[str, dict], rsi_cache: dict) -> int:
    label = "RAYDIUM"
    print(f"[{label}] Fetching concentrated pools…")
    pools = _fetch_raydium_pools()
    print(f"[{label}] Fetched {len(pools)} pools")
    qualifying: list[dict] = []
    for pool in pools:
        m = _compute_raydium_metrics(pool, top_coins)
        if m and passes_new_filters(m):
            qualifying.append(m)
    qualifying.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))
    print(f"[{label}] {len(qualifying)} pools pass filters")
    _enrich_rsi(qualifying, rsi_cache)
    _enrich_range(qualifying, rsi_cache)
    ns_state["qualifying_count"] = len(qualifying)
    return _process_new_source(qualifying, label, ns_state, now_iso)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    state   = load_state()
    total   = 0

    # Fetch CoinGecko top-N once; shared by Orca + Uniswap
    print(f"[CoinGecko] Fetching top-{TOP_N_COINS} coins…")
    top_coins = fetch_top_coins(TOP_N_COINS)
    print(f"[CoinGecko] Loaded {len(top_coins)} coins")

    rsi_cache: dict = {}
    total += run_pancakeswap(state.setdefault("pancakeswap", {}), now_iso, rsi_cache)
    time.sleep(3)
    total += run_orca(state.setdefault("orca", {}), now_iso, top_coins, rsi_cache)
    time.sleep(3)
    total += run_uniswap("ethereum", state.setdefault("uniswap_ethereum", {}), now_iso, top_coins, rsi_cache)
    time.sleep(3)
    total += run_uniswap("arbitrum", state.setdefault("uniswap_arbitrum", {}), now_iso, top_coins, rsi_cache)
    time.sleep(3)
    total += run_uniswap("base",     state.setdefault("uniswap_base",     {}), now_iso, top_coins, rsi_cache)
    time.sleep(3)
    total += run_projectx(state.setdefault("projectx", {}), now_iso, top_coins, rsi_cache)
    time.sleep(3)
    total += run_raydium(state.setdefault("raydium", {}), now_iso, top_coins, rsi_cache)

    state["last_run"] = now_iso
    save_state(state)
    print(f"\n[Done] Total alerts sent: {total}")


if __name__ == "__main__":
    main()
