#!/usr/bin/env python3
"""Multi-DEX high-APR pool monitor.

Sources:
  PancakeSwap V3 BSC  — GeckoTerminal (original filters, logic unchanged)
  Orca Whirlpools     — api.mainnet.orca.so
  Uniswap V3 Arbitrum — The Graph
  Uniswap V3 Base     — The Graph

New-source filters (Orca + Uniswap only):
  TVL ≥$200K, Vol ≥$100K/day, APR 200–3000%,
  price Δ24h ∈ [-10%, +20%], vol spike ≥2× 6-day avg, top-20 tokens only.

State is namespaced by DEX in state.json and committed with [skip ci].
Optional: set GRAPH_API_KEY env var to use The Graph decentralised network.
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
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE      = Path("state_monitor.json")
GRAPH_API_KEY   = os.environ.get("GRAPH_API_KEY", "")  # enables decentralised Graph

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
TOP_N_COINS    =  20

# ─────────────────────────────────────────────────────────────────────────────
# Orca
# ─────────────────────────────────────────────────────────────────────────────

ORCA_API = "https://api.mainnet.orca.so/v1/whirlpool/list"

# ─────────────────────────────────────────────────────────────────────────────
# Uniswap V3 — The Graph
# ─────────────────────────────────────────────────────────────────────────────

_GRAPH_GATEWAY = "https://gateway.thegraph.com/api/{key}/subgraphs/id/{id}"
_GRAPH_HOSTED  = "https://api.thegraph.com/subgraphs/name/{name}"

UNISWAP_NETWORKS: dict[str, dict] = {
    "arbitrum": {
        "hosted_name":  "uniswap/uniswap-v3-arbitrum",
        "subgraph_id":  "FbCGRftH4a3yZugY7TnbYgPJVEv2LvMT6oF1fxPe9aEM",
        "app_url":      "https://app.uniswap.org/explore/pools/arbitrum/{addr}",
        "label":        "UNISWAP V3 ARBITRUM",
    },
    "base": {
        "hosted_name":  "uniswap/uniswap-v3-base",
        "subgraph_id":  "HMoE6nHMfBpX4nMFmF5mHEvqFd5BfrBJPnHtJZJFxzs5",
        "app_url":      "https://app.uniswap.org/explore/pools/base/{addr}",
        "label":        "UNISWAP V3 BASE",
    },
}

# Fetch 200 pools sorted by TVL; poolDayData gives 8 days to compute spike + price Δ
_POOL_QUERY = """
{
  pools(
    first: 200
    orderBy: totalValueLockedUSD
    orderDirection: desc
    where: { totalValueLockedUSD_gte: "200000" }
  ) {
    id
    token0 { id symbol name }
    token1 { id symbol name }
    feeTier
    totalValueLockedUSD
    poolDayData(first: 8 orderBy: date orderDirection: desc) {
      date
      volumeUSD
      feesUSD
      tvlUSD
      open
      close
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


def _symbol_to_change(top_coins: dict[str, dict]) -> dict[str, float]:
    """symbol → 24h price change (%), for Uniswap token matching."""
    out: dict[str, float] = {}
    for info in top_coins.values():
        sym = info["symbol"]
        if info["change_24h"] is not None and sym not in STABLE_SYMBOLS:
            out[sym] = info["change_24h"]
    return out


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
    if m.get("volume_spike", 0.0) < NEW_SPIKE_MIN:
        return False
    if not m.get("in_top20"):
        return False
    return True


def _build_new_alert(m: dict, label: str) -> str:
    pc    = m.get("price_change_24h")
    spike = m.get("volume_spike", 0.0)
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
    return (
        f"🚨 NUEVA OPORTUNIDAD — {label} {m['badge']}\n\n"
        f"<pre>{body}</pre>\n\n"
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
                wait = int(resp.headers.get("Retry-After", 10))
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


def run_pancakeswap(ns_state: dict, now_iso: str) -> int:
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

    # Volume spike: today vs (week minus today) / 6
    six_day_vol = max(vol_week - vol_day, 0.0)
    six_day_avg = six_day_vol / 6 if six_day_vol > 0 else 0.0
    vol_spike   = vol_day / six_day_avg if six_day_avg > 0 else 0.0

    token_a = pool.get("tokenA") or {}
    token_b = pool.get("tokenB") or {}
    sym_a   = token_a.get("symbol", "?").strip()
    sym_b   = token_b.get("symbol", "?").strip()
    cg_a    = (token_a.get("coingeckoId") or "").strip()
    cg_b    = (token_b.get("coingeckoId") or "").strip()

    in_a     = cg_a in top_coins
    in_b     = cg_b in top_coins
    in_top20 = in_a or in_b

    # Price Δ: use the non-stable top-20 token's CoinGecko 24h change
    price_change_24h: float | None = None
    for cg_id, is_in in ((cg_a, in_a), (cg_b, in_b)):
        if is_in and top_coins[cg_id]["symbol"] not in STABLE_SYMBOLS:
            price_change_24h = top_coins[cg_id].get("change_24h")
            break

    fee_pct = fee_rate * 100
    name    = f"{sym_a}/{sym_b} ({fee_pct:.2f}%)"

    return {
        "address":          pool["address"],
        "name":             name,
        "tvl":              tvl,
        "vol_24h":          vol_day,
        "fees_24h":         fees_24h,
        "apr":              apr,
        "volume_spike":     vol_spike,
        "price_change_24h": price_change_24h,
        "in_top20":         in_top20,
        "badge":            "🟢" if in_top20 else "🟡",
        "pool_url":         f"https://birdeye.so/pool/{pool['address']}?chain=solana",
    }


def run_orca(ns_state: dict, now_iso: str, top_coins: dict[str, dict]) -> int:
    print("[Orca] Fetching whirlpools…")
    pools = _fetch_orca_pools()
    print(f"[Orca] Fetched {len(pools)} pools")

    qualifying: list[dict] = []
    for pool in pools:
        m = _compute_orca_metrics(pool, top_coins)
        if m and passes_new_filters(m):
            qualifying.append(m)

    qualifying.sort(key=lambda x: (-int(x["in_top20"]), -x["apr"]))
    print(f"[Orca] {len(qualifying)} pools pass filters")
    ns_state["qualifying_count"] = len(qualifying)
    return _process_new_source(qualifying, "ORCA", ns_state, now_iso)


# ─────────────────────────────────────────────────────────────────────────────
# Uniswap V3 via The Graph (Arbitrum + Base)
# ─────────────────────────────────────────────────────────────────────────────

def _graphql_endpoint(chain: str) -> str:
    cfg = UNISWAP_NETWORKS[chain]
    if GRAPH_API_KEY:
        return _GRAPH_GATEWAY.format(key=GRAPH_API_KEY, id=cfg["subgraph_id"])
    return _GRAPH_HOSTED.format(name=cfg["hosted_name"])


def _graphql_query(url: str, query: str) -> dict | None:
    for attempt in range(3):
        try:
            resp = requests.post(url, json={"query": query}, timeout=30)
            if resp.status_code == 200:
                payload = resp.json()
                if "errors" in payload:
                    print(f"[Graph] GraphQL errors: {payload['errors'][:1]}")
                    return None
                return payload.get("data")
            print(f"[Graph] HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as exc:
            print(f"[Graph] Attempt {attempt + 1}: {exc}")
        if attempt < 2:
            time.sleep(2 ** attempt)
    return None


def _compute_uniswap_metrics(
    pool: dict,
    chain: str,
    top_coins: dict[str, dict],
    sym_change: dict[str, float],
) -> dict | None:
    tvl = float(pool.get("totalValueLockedUSD") or 0)
    if tvl <= 0:
        return None

    day_data = pool.get("poolDayData") or []
    if not day_data:
        return None

    # day_data[0] = most recent UTC day (may be partially filled).
    # Use [1] as the last *complete* day; fall back to [0] if only one entry.
    ri = 1 if len(day_data) > 1 else 0
    vol_24h  = float(day_data[ri].get("volumeUSD") or 0)
    fees_24h = float(day_data[ri].get("feesUSD")   or 0)
    tvl_snap = float(day_data[ri].get("tvlUSD")    or tvl)

    if vol_24h <= 0:
        return None

    apr = (fees_24h / tvl_snap) * 365 * 100 if tvl_snap > 0 else 0.0

    # Volume spike: last complete day vs avg of prior 6 complete days
    prior = day_data[ri + 1 : ri + 7]
    if len(prior) >= 2:
        prior_vols  = [float(d.get("volumeUSD") or 0) for d in prior]
        six_day_avg = sum(prior_vols) / len(prior_vols)
        vol_spike   = vol_24h / six_day_avg if six_day_avg > 0 else 0.0
    else:
        vol_spike = 0.0  # insufficient history → fails spike filter

    t0   = pool.get("token0") or {}
    t1   = pool.get("token1") or {}
    sym0 = t0.get("symbol", "").lower()
    sym1 = t1.get("symbol", "").lower()

    # Price Δ24h: prefer CoinGecko (USD-based); fall back to pool open/close ratio
    price_change_24h: float | None = None
    if sym0 in sym_change and sym0 not in STABLE_SYMBOLS:
        price_change_24h = sym_change[sym0]
    elif sym1 in sym_change and sym1 not in STABLE_SYMBOLS:
        price_change_24h = sym_change[sym1]
    elif len(day_data) > ri + 1:
        curr_close = float(day_data[ri].get("close")     or 0)
        prev_close = float(day_data[ri + 1].get("close") or 0)
        if curr_close > 0 and prev_close > 0:
            price_change_24h = (curr_close - prev_close) / prev_close * 100

    top_syms = {info["symbol"] for info in top_coins.values()}
    in_top20 = sym0 in top_syms or sym1 in top_syms

    fee_tier_bps = int(pool.get("feeTier") or 0)
    fee_pct_str  = f"{fee_tier_bps / 10_000:.2f}%"
    name = f"{t0.get('symbol','?')}/{t1.get('symbol','?')} ({fee_pct_str})"

    cfg      = UNISWAP_NETWORKS[chain]
    pool_url = cfg["app_url"].format(addr=pool["id"])

    return {
        "address":          pool["id"],
        "name":             name,
        "tvl":              tvl,
        "vol_24h":          vol_24h,
        "fees_24h":         fees_24h,
        "apr":              apr,
        "volume_spike":     vol_spike,
        "price_change_24h": price_change_24h,
        "in_top20":         in_top20,
        "badge":            "🟢" if in_top20 else "🟡",
        "pool_url":         pool_url,
    }


def run_uniswap(
    chain: str,
    ns_state: dict,
    now_iso: str,
    top_coins: dict[str, dict],
    sym_change: dict[str, float],
) -> int:
    label = UNISWAP_NETWORKS[chain]["label"]
    url   = _graphql_endpoint(chain)

    print(f"[{label}] Querying The Graph ({url[:50]}…)")
    data = _graphql_query(url, _POOL_QUERY)
    if not data:
        print(f"[{label}] No data — skipping. (Set GRAPH_API_KEY if hosted is deprecated.)")
        ns_state["qualifying_count"] = 0
        return 0

    raw_pools = data.get("pools") or []
    print(f"[{label}] Got {len(raw_pools)} pools")

    qualifying: list[dict] = []
    for pool in raw_pools:
        m = _compute_uniswap_metrics(pool, chain, top_coins, sym_change)
        if m and passes_new_filters(m):
            qualifying.append(m)

    qualifying.sort(key=lambda x: (-int(x["in_top20"]), -x["apr"]))
    print(f"[{label}] {len(qualifying)} pools pass filters")
    ns_state["qualifying_count"] = len(qualifying)
    return _process_new_source(qualifying, label, ns_state, now_iso)


# ─────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    state   = load_state()
    total   = 0

    # Fetch CoinGecko top-20 once; shared by Orca + Uniswap
    print(f"[CoinGecko] Fetching top-{TOP_N_COINS} coins…")
    top_coins  = fetch_top_coins(TOP_N_COINS)
    sym_change = _symbol_to_change(top_coins)
    print(f"[CoinGecko] Loaded {len(top_coins)} coins")

    total += run_pancakeswap(state.setdefault("pancakeswap", {}), now_iso)
    total += run_orca(state.setdefault("orca", {}), now_iso, top_coins)
    total += run_uniswap("arbitrum", state.setdefault("uniswap_arbitrum", {}), now_iso, top_coins, sym_change)
    total += run_uniswap("base",     state.setdefault("uniswap_base",     {}), now_iso, top_coins, sym_change)

    state["last_run"] = now_iso
    save_state(state)
    print(f"\n[Done] Total alerts sent: {total}")


if __name__ == "__main__":
    main()
