#!/usr/bin/env python3
"""Orca (Solana Whirlpools) high-APR pool alert bot.

Data source: https://api.mainnet.orca.so/v1/whirlpool/list
CoinGecko:  top-30 coins by market cap fetched live each run
State:      state.json committed to repo with [skip ci]
"""

import html
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = Path("state.json")

ORCA_API = "https://api.mainnet.orca.so/v1/whirlpool/list"
COINGECKO_TOP100_URL = (
    "https://api.coingecko.com/api/v3/coins/markets"
    "?vs_currency=usd&order=market_cap_desc&per_page=100&page=1&sparkline=false"
)

MIN_APR_PCT          = 500
MIN_APR_PCT_BLUECHIP =  50   # both-side blue-chip pairs (e.g. cbBTC/SOL)
MIN_VOL_24H = 50_000
MIN_FEES_24H = 500
DECLINE_TVL_PCT     = 50
DECLINE_VOL_PCT     = 60
DECLINE_VOL_TVL_PCT = 40   # alert if Vol/TVL ratio drops >40% vs previous run
ALERT_COOLDOWN_H    = 4    # minimum hours between any alerts for the same pool

# ── Hard-block filter constants ──────────────────────────────────────────────
HARD_MIN_LP_SCORE  = 40
HARD_MIN_TVL       = 50_000   # safety floor in _hard_block() — applies to all sources
HARD_MAX_APR_RANGO = 1_500
ORCA_MIN_TVL       = 100_000  # fetch-level TVL floor for Orca Whirlpools

BLACKLIST_SYMBOLS = {
    "USELESS", "SHIT", "DOGE2", "PEPE2", "SCAM",
    "FAKE", "SAFE", "MOON2", "INU2",
    "GLONK",
}

_STABLE_SYMS = {"usdc", "usdt", "dai", "busd", "tusd", "frax", "usdh", "eurc"}
# Wrapped tokens whose Binance/CoinGecko ticker is the unwrapped name
_SYM_ALIAS = {"WSOL": "SOL", "WBTC": "BTC", "WETH": "ETH", "WBNB": "BNB", "WMATIC": "MATIC", "cbBTC": "BTC"}

BLUE_CHIP_TOKENS = {
    "BTC", "WBTC", "ETH", "WETH", "SOL", "WSOL", "BNB", "WBNB", "XRP", "ADA",
    "AVAX", "DOT", "MATIC", "WMATIC", "LINK", "UNI", "LTC", "BCH", "USDC", "USDT",
    "HYPE", "WHYPE", "ARB", "OP", "UBTC", "UETH", "USOL", "USDH", "TON", "NEAR", "APT",
    "CBBTC", "CBETH", "WHETH",
}

# ── Meteora DLMM ─────────────────────────────────────────────────────────────
# Official API (dlmm-api.meteora.ag) returns 404 as of 2026-07 — GeckoTerminal
# is the only working source, used directly (no more dead-endpoint attempt).
METEORA_GT_URL   = "https://api.geckoterminal.com/api/v2/networks/solana/dexes/meteora/pools"
METEORA_MIN_TVL  = 100_000
METEORA_MIN_VOL  =  50_000
METEORA_FEE_EST  = 0.0010  # 0.10% default fee estimate (GeckoTerminal doesn't expose real fee rate)

# ── Raydium CLMM ──────────────────────────────────────────────────────────────
RAYDIUM_CLMM_URL = "https://api-v3.raydium.io/pools/info/list"

# ── Merkl reward campaigns ─────────────────────────────────────────────────────
MERKL_BASE   = "https://api.merkl.xyz/v4/opportunities"
_MERKL_CHAIN = {"Solana": 101}
RAYDIUM_MIN_TVL  = 100_000
RAYDIUM_MIN_VOL  =  50_000


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
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc?vs_currency=usd&days=14"
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=20)
                if resp.status_code == 429:
                    print("[RSI/CoinGecko] rate limit — waiting 15s…")
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
        for quote in ("USDT", "USDC"):
            try:
                resp = requests.get(
                    f"https://api.binance.com/api/v3/klines"
                    f"?symbol={sym_upper}{quote}&interval=1d&limit=50",
                    timeout=15,
                )
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


def _blue_chip_count(sym_a: str, sym_b: str) -> int:
    """Return 0, 1, or 2: how many of the two symbols are in BLUE_CHIP_TOKENS."""
    return sum(1 for s in (str(sym_a).upper(), str(sym_b).upper()) if s in BLUE_CHIP_TOKENS)


def _lp_score(
    vol_24h: float,
    tvl: float,
    rsi: float | None,
    blue_chips: int = 0,
    apr: float = 0.0,
) -> int:
    """
    LP opportunity score 0-100.
    Scale: 🔴 0-39 Evitar | 🟡 40-59 Interesante | 🟢 60-79 Buena | 🏆 80-100 Top
    Weights: Vol/TVL 35 | APR Fees 25 | TVL 15 | Blue chip 15 | RSI bonus 10
    """
    ratio = vol_24h / tvl if tvl > 0 else 0.0
    score = 0.0

    # Vol/TVL ratio — 35 pts
    if   ratio >= 5.0: score += 35.0
    elif ratio >= 3.0: score += 30.0
    elif ratio >= 2.0: score += 25.0
    elif ratio >= 1.5: score += 21.0
    elif ratio >= 1.0: score += 17.0
    elif ratio >= 0.5: score += 10.0

    # APR Fees real — 25 pts
    # High tiers (>=500%) reflect Orca Whirlpool range; lower tiers value blue-chip CLMM pools
    if   apr >= 3000: score += 25.0
    elif apr >= 2000: score += 22.0
    elif apr >= 1000: score += 18.0
    elif apr >=  700: score += 15.0
    elif apr >=  500: score += 12.0
    elif apr >=  100: score +=  8.0
    elif apr >=   50: score +=  5.0
    elif apr >=   10: score +=  2.0

    # TVL absoluto — 15 pts
    if   tvl >= 2_000_000: score += 15.0
    elif tvl >= 1_000_000: score += 13.0
    elif tvl >=   500_000: score += 11.0
    elif tvl >=   300_000: score +=  9.0
    elif tvl >=   100_000: score +=  7.0
    elif tvl >=    50_000: score +=  4.0

    # Blue chip bonus — 15 pts
    if   blue_chips >= 2: score += 15.0
    elif blue_chips == 1: score += 10.0

    # RSI bonus — 10 pts (sobrevendido = mejor entrada)
    if rsi is not None:
        if   rsi < 30: score += 10.0
        elif rsi < 50: score +=  5.0

    # Garantías de piso
    if apr > 500 and ratio > 1.0:
        score = max(score, 45.0)
    if apr > 1000 and ratio > 3.0:
        score = max(score, 72.0)

    return int(min(100, score))


def _fmt_apr_rango(val: float | None) -> str:
    if val is None:
        return "N/A"
    return "&gt;10,000%" if val > 10_000 else f"{val:,.0f}%"


def _lp_label(score: int) -> str:
    if score >= 80:
        return "Excelente"
    if score >= 60:
        return "Bueno"
    if score >= 40:
        return "Regular"
    return "Bajo"


def _pool_url(m: dict) -> tuple[str, str]:
    dex  = m.get("dex", "Orca")
    addr = m.get("address", "")
    if "Meteora" in dex:
        return f"https://app.meteora.ag/dlmm/{addr}", "Meteora DLMM"
    if "Raydium" in dex:
        return f"https://raydium.io/clmm/pool/?poolId={addr}", "Raydium CLMM"
    return f"https://www.orca.so/pools/{addr}", "Orca"


def _hard_block(m: dict) -> tuple[bool, str]:
    """Return (True, reason) if this pool must be dropped before alerting."""
    # 1. LP Score < 30
    lp = _lp_score(m.get("vol_24h", 0), m.get("tvl", 0), m.get("rsi"), _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", "")), m.get("apr", 0.0))
    print(f"[hard_block] {m.get('name', '')} — LP Score: {lp}/100, TVL: ${m.get('tvl', 0):,.0f}")
    if lp < HARD_MIN_LP_SCORE:
        return True, f"LP Score {lp} < {HARD_MIN_LP_SCORE}"
    # 2. TVL < $50,000
    if m.get("tvl", 0) < HARD_MIN_TVL:
        return True, f"TVL ${m['tvl']:,.0f} < ${HARD_MIN_TVL:,}"
    # 4. APR > 3,000% (no range calc for Orca; base APR used as proxy)
    if m.get("apr", 0) > HARD_MAX_APR_RANGO:
        return True, f"APR {m['apr']:,.0f}% > {HARD_MAX_APR_RANGO:,}%"
    # 5. Blacklisted symbol
    for sym in (m.get("symbol_a", "").upper(), m.get("symbol_b", "").upper()):
        if sym in BLACKLIST_SYMBOLS:
            return True, f"Símbolo en lista negra: {sym}"
    return False, ""


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
        print(f"[Telegram error] {exc}")
        return False


def fetch_top100_ids() -> set[str]:
    """Return CoinGecko IDs for the top 100 coins by market cap."""
    for attempt in range(3):
        try:
            resp = requests.get(COINGECKO_TOP100_URL, timeout=15)
            resp.raise_for_status()
            return {coin["id"] for coin in resp.json()}
        except Exception as exc:
            print(f"[CoinGecko] Attempt {attempt + 1} failed: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return set()


def fetch_whirlpools() -> list[dict]:
    """Fetch all Orca whirlpools from the public API."""
    for attempt in range(3):
        try:
            resp = requests.get(ORCA_API, timeout=30)
            resp.raise_for_status()
            return resp.json().get("whirlpools", [])
        except Exception as exc:
            print(f"[Orca API] Attempt {attempt + 1} failed: {exc}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    return []


def compute_metrics(pool: dict, top100_ids: set[str]) -> dict | None:
    tvl = float(pool.get("tvl") or 0)
    vol_24h = float((pool.get("volume") or {}).get("day") or 0)
    lp_fee_rate = float(pool.get("lpFeeRate") or 0)

    if tvl <= 0 or vol_24h <= 0 or lp_fee_rate <= 0:
        return None

    fees_24h = vol_24h * lp_fee_rate
    apr = (fees_24h / tvl) * 365 * 100

    token_a = pool.get("tokenA") or {}
    token_b = pool.get("tokenB") or {}
    symbol_a = token_a.get("symbol", "?").strip()
    symbol_b = token_b.get("symbol", "?").strip()
    cg_id_a = (token_a.get("coingeckoId") or "").strip()
    cg_id_b = (token_b.get("coingeckoId") or "").strip()

    in_top100 = bool(
        (cg_id_a and cg_id_a in top100_ids)
        or (cg_id_b and cg_id_b in top100_ids)
    )

    fee_pct = lp_fee_rate * 100
    name = f"{symbol_a}/{symbol_b} ({fee_pct:.2f}%)"

    return {
        "address": pool["address"],
        "name": name,
        "symbol_a": symbol_a,
        "symbol_b": symbol_b,
        "cg_id_a": cg_id_a,
        "cg_id_b": cg_id_b,
        "fee_rate": lp_fee_rate,
        "tvl": tvl,
        "vol_24h": vol_24h,
        "fees_24h": fees_24h,
        "apr": apr,
        "in_top100": in_top100,
        "badge": "🟢" if in_top100 else "🟡",
    }


def _is_trap(m: dict) -> bool:
    tvl = m.get("tvl", 0)
    if tvl < 20_000:
        return True
    if tvl > 0 and m.get("vol_24h", 0) / tvl > 10:
        return True
    if m.get("apr", 0) > 5_000:
        return True
    return False


def passes_filters(m: dict) -> bool:
    if _is_trap(m):
        return False
    bc = _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", ""))
    if _lp_score(m["vol_24h"], m["tvl"], None, bc, m.get("apr", 0.0)) < HARD_MIN_LP_SCORE:
        return False
    min_apr = MIN_APR_PCT_BLUECHIP if bc >= 2 else MIN_APR_PCT
    return (
        min_apr <= m["apr"] <= HARD_MAX_APR_RANGO
        and m["vol_24h"] >= MIN_VOL_24H
        and m["fees_24h"] >= MIN_FEES_24H
        and m["tvl"] >= ORCA_MIN_TVL
    )


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"alerted_pools": {}, "last_run": None}


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


def _fmt_addr(addr: str) -> str:
    """Shorten a contract address: 0x1234...abcd (first 6 + last 4 chars)."""
    if not addr or len(addr) < 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"

def _dexscreener_url(address: str) -> str:
    return f"https://dexscreener.com/solana/{address}"


# ── Meteora DLMM fetchers ─────────────────────────────────────────────────────

def _fetch_meteora_geckoterminal() -> list[dict]:
    """Fetch Meteora pools via GeckoTerminal (primary and only source). Paginates up to 5 pages."""
    result = []
    seen   = set()
    for page in range(1, 6):
        try:
            r = requests.get(
                METEORA_GT_URL,
                params={"page": page, "sort": "h24_volume_usd_desc"},
                headers={"Accept": "application/json"},
                timeout=20,
            )
            if not r.ok:
                break
            pools = r.json().get("data", [])
            if not pools:
                break
            for p in pools:
                a       = p.get("attributes", {})
                addr    = a.get("address", "")
                if not addr or addr in seen:
                    continue
                tvl     = float(a.get("reserve_in_usd") or 0)
                vol_24h = float((a.get("volume_usd") or {}).get("h24") or 0)
                if tvl < METEORA_MIN_TVL or vol_24h < METEORA_MIN_VOL:
                    continue
                seen.add(addr)
                name    = a.get("name", "")   # "SOL / USDC"
                parts   = name.split("/")
                sym_a   = parts[0].strip() if parts else "?"
                sym_b   = parts[1].strip() if len(parts) > 1 else "?"
                fees_24h = vol_24h * METEORA_FEE_EST
                result.append({
                    "_source": "geckoterminal",
                    "address": addr,
                    "sym_a": sym_a,
                    "sym_b": sym_b,
                    "tvl": tvl,
                    "vol_24h": vol_24h,
                    "fees_24h": fees_24h,
                    "fee_rate": METEORA_FEE_EST,
                })
        except Exception as exc:
            print(f"[GeckoTerminal/Meteora] page {page}: {exc}")
            break
    return result


def fetch_meteora_dlmm(top100_ids: set[str]) -> list[dict]:
    """Return qualifying Meteora DLMM pools as m-dicts compatible with existing alert functions."""
    raw_pools = _fetch_meteora_geckoterminal()

    result = []
    for raw in raw_pools:
        sym_a    = raw["sym_a"]
        sym_b    = raw["sym_b"]
        tvl      = raw["tvl"]
        vol_24h  = raw["vol_24h"]
        fees_24h = raw["fees_24h"]
        apr      = (fees_24h / tvl) * 365 * 100 if tvl > 0 else 0.0

        in_top100 = (str(sym_a).upper() in BLUE_CHIP_TOKENS) or (str(sym_b).upper() in BLUE_CHIP_TOKENS)
        fee_str   = f"{raw['fee_rate'] * 100:.2f}%"
        if raw.get("_source") == "geckoterminal":
            fee_str += "~"  # indicates fee rate is estimated, not from official API
        name = f"{sym_a}/{sym_b} ({fee_str})"

        result.append({
            "address":   raw["address"],
            "name":      name,
            "symbol_a":  sym_a,
            "symbol_b":  sym_b,
            "cg_id_a":   None,
            "cg_id_b":   None,
            "fee_rate":  raw["fee_rate"],
            "tvl":       tvl,
            "vol_24h":   vol_24h,
            "fees_24h":  fees_24h,
            "apr":       apr,
            "in_top100": in_top100,
            "badge":     "🟢" if in_top100 else "🟡",
            "dex":       "Meteora DLMM",
        })

    result.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))
    return result


# ── Raydium CLMM fetcher ──────────────────────────────────────────────────────

def fetch_raydium_clmm(top100_ids: set[str]) -> list[dict]:
    """Fetch qualifying Raydium CLMM pools from the official Raydium v3 API."""
    result = []
    seen   = set()
    for page in range(1, 6):
        try:
            r = requests.get(
                RAYDIUM_CLMM_URL,
                params={
                    "poolType":      "concentrated",
                    "poolSortField": "volume24h",
                    "sortType":      "desc",
                    "pageSize":      100,
                    "page":          page,
                },
                timeout=20,
            )
            if not r.ok:
                print(f"[Raydium CLMM] HTTP {r.status_code} on page {page}")
                break
            body = r.json()
            if not body.get("success"):
                break
            pools = body.get("data", {}).get("data", [])
            if not pools:
                break

            any_above_vol = False
            for p in pools:
                pid = p.get("id", "")
                if not pid or pid in seen:
                    continue
                tvl     = float(p.get("tvl") or 0)
                day     = p.get("day", {})
                vol_24h = float(day.get("volume") or 0)
                if vol_24h >= RAYDIUM_MIN_VOL:
                    any_above_vol = True
                if tvl < RAYDIUM_MIN_TVL or vol_24h < RAYDIUM_MIN_VOL:
                    continue
                seen.add(pid)
                fees_24h = float(day.get("volumeFee") or 0)
                fee_rate = float(p.get("feeRate") or 0)
                apr      = float(day.get("feeApr") or 0)
                if apr == 0 and tvl > 0:
                    apr = (fees_24h / tvl) * 365 * 100
                sym_a    = ((p.get("mintA") or {}).get("symbol") or "?").strip()
                sym_b    = ((p.get("mintB") or {}).get("symbol") or "?").strip()
                in_top100 = (str(sym_a).upper() in BLUE_CHIP_TOKENS) or (str(sym_b).upper() in BLUE_CHIP_TOKENS)
                if fee_rate <= 0.0001 and not in_top100:
                    continue  # 0.01% pools with no blue-chip token are routing noise, not LP opportunities
                fee_str  = f"{fee_rate * 100:.2f}%"
                result.append({
                    "address":   pid,
                    "name":      f"{sym_a}/{sym_b} ({fee_str})",
                    "symbol_a":  sym_a,
                    "symbol_b":  sym_b,
                    "cg_id_a":   None,
                    "cg_id_b":   None,
                    "fee_rate":  fee_rate,
                    "tvl":       tvl,
                    "vol_24h":   vol_24h,
                    "fees_24h":  fees_24h,
                    "apr":       apr,
                    "in_top100": in_top100,
                    "badge":     "🟢" if in_top100 else "🟡",
                    "dex":       "Raydium CLMM",
                })

            if not any_above_vol:
                break  # all pools on this page are below min vol, no point paginating
            if not body.get("data", {}).get("hasNextPage"):
                break
        except Exception as exc:
            print(f"[Raydium CLMM] page {page}: {exc}")
            break

    result.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))
    return result


def _fetch_merkl_bulk(cache: dict, chain_label: str) -> dict[str, dict]:
    """Fetch active Merkl campaigns for a chain. Returns {pool_addr: {merkl_apr, days_left}}."""
    chain_id = _MERKL_CHAIN.get(chain_label)
    if not chain_id:
        return {}
    key = f"_merkl_{chain_id}"
    if key in cache:
        return cache[key]
    result: dict[str, dict] = {}
    try:
        resp = requests.get(
            f"{MERKL_BASE}?chainId={chain_id}&status=LIVE",
            timeout=20,
        )
        if resp.status_code == 200:
            now_ts = datetime.now(timezone.utc).timestamp()
            for opp in resp.json():
                addr = (opp.get("identifier") or "").lower()
                apr  = float(opp.get("totalApr") or 0)
                end  = int(opp.get("latestCampaignEnd") or 0)
                if addr and apr > 0:
                    days_left = max(0, int((end - now_ts) / 86400)) if end else None
                    result[addr] = {"merkl_apr": apr, "days_left": days_left}
        else:
            print(f"[Merkl] HTTP {resp.status_code} chainId={chain_id}")
    except Exception as exc:
        print(f"[Merkl] {exc}")
    cache[key] = result
    print(f"[Merkl] chainId={chain_id}: {len(result)} active pools")
    return result


def _get_merkl(address: str, chain_label: str, cache: dict) -> dict | None:
    return _fetch_merkl_bulk(cache, chain_label).get(address.lower())


def _merkl_lines(merkl: dict | None, apr: float, W: int) -> list[str]:
    if not merkl:
        return []
    m_apr     = float(merkl.get("merkl_apr", 0))
    days_left = merkl.get("days_left")
    days_str  = f"{days_left}d" if days_left is not None else "N/A"
    return [
        f"{'APR Total:':<{W}}{apr + m_apr:,.0f}% (incl. {m_apr:.0f}% Merkl)",
        f"{'Merkl:':<{W}}{days_str} remaining ✅",
    ]


def build_new_alert(m: dict) -> str:
    rsi      = m.get("rsi")
    rsi_line = f"📉 RSI: {rsi:.0f}\n" if rsi is not None else ""
    lp       = _lp_score(m.get("vol_24h", 0), m.get("tvl", 0), rsi,
                         _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", "")),
                         m.get("apr", 0.0))
    tvl      = m.get("tvl", 0)
    vol_24h  = m.get("vol_24h", 0)
    fees_24h = m.get("fees_24h", 0)
    apr      = m.get("apr", 0)
    fees_1k  = (1_000 / tvl) * fees_24h if tvl > 0 else 0
    fees_7d  = fees_1k * 7
    vol_tvl  = vol_24h / tvl if tvl > 0 else 0
    fire     = " 🔥" if vol_tvl > 1 else ""
    dex      = m.get("dex", "Orca")
    addr     = m.get("address", "")
    sym_a    = m.get("symbol_a", "?")
    sym_b    = m.get("symbol_b", "?")
    fee_rate = m.get("fee_rate") or 0
    fee_str  = f"  {fee_rate * 100:.4g}%" if fee_rate else ""
    purl, pulab = _pool_url(m)
    return (
        f"🚨 NEW OPPORTUNITY — {dex} | SOL\n"
        f"{sym_a} / {sym_b}{fee_str}\n"
        f"<b>💰 Fees/day in range ($1K): ${fees_1k:.2f}</b>\n"
        f"<b>📅 Fees/7 days in range ($1K): ${fees_7d:.2f}</b>\n"
        f"🎯 Suggested range: N/A\n"
        f"⭐ LP Score: {lp}/100 ({_lp_label(lp)})\n"
        f"\n"
        f"💧 TVL: {fmt_money(tvl)}\n"
        f"📈 Vol 24h: {fmt_money(vol_24h)}\n"
        f"🔄 Vol/TVL: {vol_tvl:.2f}x{fire}\n"
        f"⚡ APR Fees: {apr:,.0f}%\n"
        f"{rsi_line}"
        f"\n"
        f"📋 Contract: {_fmt_addr(addr)}\n"
        f'🔗 <a href="{purl}">Open on {pulab}</a>\n'
        f'🔗 <a href="{_dexscreener_url(addr)}">View on DexScreener</a>\n'
        f"⏱ Detected now\n"
        f"━━━━━━━━━━━━━━━"
    )


def build_decline_alert(m: dict, reason: str, prev_tvl: float, prev_vol: float) -> str:
    rsi      = m.get("rsi")
    rsi_line = f"📉 RSI: {rsi:.0f}\n" if rsi is not None else ""
    lp       = _lp_score(m.get("vol_24h", 0), m.get("tvl", 0), rsi,
                         _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", "")),
                         m.get("apr", 0.0))
    tvl      = m.get("tvl", 0)
    vol_24h  = m.get("vol_24h", 0)
    fees_24h = m.get("fees_24h", 0)
    apr      = m.get("apr", 0)
    fees_1k  = (1_000 / tvl) * fees_24h if tvl > 0 else 0
    fees_7d  = fees_1k * 7
    vol_tvl  = vol_24h / tvl if tvl > 0 else 0
    dex      = m.get("dex", "Orca")
    addr     = m.get("address", "")
    sym_a    = m.get("symbol_a", "?")
    sym_b    = m.get("symbol_b", "?")
    fee_rate = m.get("fee_rate") or 0
    fee_str  = f"  {fee_rate * 100:.4g}%" if fee_rate else ""
    purl, pulab = _pool_url(m)
    return (
        f"⚠️ POOL DECLINE — {dex} | SOL\n"
        f"{sym_a} / {sym_b}{fee_str}\n"
        f"<b>💰 Fees/day in range ($1K): ${fees_1k:.2f}</b>\n"
        f"<b>📅 Fees/7 days in range ($1K): ${fees_7d:.2f}</b>\n"
        f"⭐ LP Score: {lp}/100 ({_lp_label(lp)})\n"
        f"\n"
        f"💧 TVL: {fmt_money(tvl)} (prev: {fmt_money(prev_tvl)})\n"
        f"📈 Vol 24h: {fmt_money(vol_24h)} (prev: {fmt_money(prev_vol)})\n"
        f"🔄 Vol/TVL: {vol_tvl:.2f}x\n"
        f"⚡ APR Fees: {apr:,.0f}%\n"
        f"{rsi_line}"
        f"⚠️ Reason: {html.escape(reason)}\n"
        f"\n"
        f"📋 Contract: {_fmt_addr(addr)}\n"
        f'🔗 <a href="{purl}">Open on {pulab}</a>\n'
        f'🔗 <a href="{_dexscreener_url(addr)}">View on DexScreener</a>\n'
        f"━━━━━━━━━━━━━━━"
    )


def main() -> None:
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    state = load_state()
    alerted: dict = state.setdefault("alerted_pools", {})

    print(f"[{now_iso}] Fetching CoinGecko top 30…")
    top100_ids = fetch_top100_ids()
    print(f"[{now_iso}] Top 100 loaded: {len(top100_ids)} coins")

    print(f"[{now_iso}] Fetching Orca whirlpools…")
    whirlpools = fetch_whirlpools()
    print(f"[{now_iso}] Fetched {len(whirlpools)} whirlpools")

    qualifying: list[dict] = []
    for pool in whirlpools:
        m = compute_metrics(pool, top100_ids)
        if m and passes_filters(m):
            qualifying.append(m)

    # Sort: 🟢 (top-30) pools first, then by APR descending
    qualifying.sort(key=lambda x: (-int(x["in_top100"]), -x["apr"]))

    print(f"[{now_iso}] {len(qualifying)} pools pass filters")

    # Fetch RSI for qualifying pools (required for hard-block checks)
    rsi_cache: dict = {}
    for m in qualifying:
        sym_a = m.get("symbol_a", "")
        sym_b = m.get("symbol_b", "")
        if sym_a.lower() not in _STABLE_SYMS:
            cg_id, sym = m.get("cg_id_a") or None, sym_a
        elif sym_b.lower() not in _STABLE_SYMS:
            cg_id, sym = m.get("cg_id_b") or None, sym_b
        else:
            cg_id, sym = None, ""
        m["rsi"] = _fetch_rsi(cg_id, sym, rsi_cache)

    merkl_cache: dict = {}
    for m in qualifying:
        m["merkl"] = _get_merkl(m["address"], "Solana", merkl_cache)

    cutoff_4h = (now_utc - timedelta(hours=ALERT_COOLDOWN_H)).isoformat()
    alerts_sent = 0
    for m in qualifying:
        pid = m["address"]

        if pid not in alerted:
            _blocked, _reason = _hard_block(m)
            if _blocked:
                print(f"[SKIP] {m['name']} — {_reason}")
            elif send_telegram(build_new_alert(m)):
                alerts_sent += 1
                alerted[pid] = {
                    "first_seen": now_iso,
                    "last_alert": now_iso,
                    "last_tvl": m["tvl"],
                    "last_vol_24h": m["vol_24h"],
                    "alerted_decline": False,
                    "name": m["name"],
                }
        else:
            prev = alerted[pid]
            prev_tvl = prev.get("last_tvl", m["tvl"])
            prev_vol = prev.get("last_vol_24h", m["vol_24h"])

            if not prev.get("alerted_decline"):
                tvl_chg = (m["tvl"] - prev_tvl) / prev_tvl if prev_tvl > 0 else 0
                vol_chg = (m["vol_24h"] - prev_vol) / prev_vol if prev_vol > 0 else 0

                prev_vol_tvl = prev_vol / prev_tvl if prev_tvl > 0 else 0
                curr_vol_tvl = m["vol_24h"] / m["tvl"] if m["tvl"] > 0 else 0
                vol_tvl_chg  = (curr_vol_tvl - prev_vol_tvl) / prev_vol_tvl if prev_vol_tvl > 0 else 0

                decline_reason = None
                if tvl_chg <= -(DECLINE_TVL_PCT / 100):
                    decline_reason = f"TVL cayó {tvl_chg * 100:.1f}% (>{DECLINE_TVL_PCT}%)"
                elif vol_chg <= -(DECLINE_VOL_PCT / 100):
                    decline_reason = f"Vol cayó {vol_chg * 100:.1f}% (>{DECLINE_VOL_PCT}%)"
                elif vol_tvl_chg <= -(DECLINE_VOL_TVL_PCT / 100):
                    decline_reason = f"Vol/TVL cayó {vol_tvl_chg * 100:.1f}% (>{DECLINE_VOL_TVL_PCT}%)"

                if decline_reason:
                    if prev.get("last_alert", "") > cutoff_4h:
                        print(f"[COOLDOWN] {m['name']} — decline within {ALERT_COOLDOWN_H}h cooldown")
                    else:
                        _blocked, _reason = _hard_block(m)
                        if _blocked:
                            print(f"[SKIP-DECLINE] {m['name']} — {_reason}")
                        elif send_telegram(build_decline_alert(m, decline_reason, prev_tvl, prev_vol)):
                            alerts_sent += 1
                            prev["last_alert"] = now_iso
                    prev["alerted_decline"] = True

            prev["last_tvl"] = m["tvl"]
            prev["last_vol_24h"] = m["vol_24h"]

    # ── Meteora DLMM ─────────────────────────────────────────────────────────
    print(f"[{now_iso}] Fetching Meteora DLMM pools…")
    meteora_alerted: dict = state.setdefault("meteora_alerted", {})
    meteora_pools = fetch_meteora_dlmm(top100_ids)
    print(f"[{now_iso}] {len(meteora_pools)} Meteora pools pass TVL/Vol filters")

    for m in meteora_pools:
        sym_a = m.get("symbol_a", "")
        sym_b = m.get("symbol_b", "")
        if sym_a.lower() not in _STABLE_SYMS:
            raw_sym = sym_a
        elif sym_b.lower() not in _STABLE_SYMS:
            raw_sym = sym_b
        else:
            raw_sym = ""
        sym = _SYM_ALIAS.get(raw_sym.upper(), raw_sym)
        m["rsi"] = _fetch_rsi(None, sym, rsi_cache)
        m["merkl"] = _get_merkl(m["address"], "Solana", merkl_cache)

    for m in meteora_pools:
        pid = m["address"]
        if pid not in meteora_alerted:
            _blocked, _reason = _hard_block(m)
            if _blocked:
                print(f"[METEORA-SKIP] {m['name']} — {_reason}")
            elif send_telegram(build_new_alert(m)):
                alerts_sent += 1
                meteora_alerted[pid] = {
                    "first_seen":      now_iso,
                    "last_alert":      now_iso,
                    "last_tvl":        m["tvl"],
                    "last_vol_24h":    m["vol_24h"],
                    "alerted_decline": False,
                    "name":            m["name"],
                }
        else:
            prev     = meteora_alerted[pid]
            prev_tvl = prev.get("last_tvl", m["tvl"])
            prev_vol = prev.get("last_vol_24h", m["vol_24h"])

            if not prev.get("alerted_decline"):
                tvl_chg      = (m["tvl"] - prev_tvl) / prev_tvl if prev_tvl > 0 else 0
                vol_chg      = (m["vol_24h"] - prev_vol) / prev_vol if prev_vol > 0 else 0
                prev_vol_tvl = prev_vol / prev_tvl if prev_tvl > 0 else 0
                curr_vol_tvl = m["vol_24h"] / m["tvl"] if m["tvl"] > 0 else 0
                vol_tvl_chg  = (curr_vol_tvl - prev_vol_tvl) / prev_vol_tvl if prev_vol_tvl > 0 else 0

                decline_reason = None
                if tvl_chg <= -(DECLINE_TVL_PCT / 100):
                    decline_reason = f"TVL cayó {tvl_chg * 100:.1f}% (>{DECLINE_TVL_PCT}%)"
                elif vol_chg <= -(DECLINE_VOL_PCT / 100):
                    decline_reason = f"Vol cayó {vol_chg * 100:.1f}% (>{DECLINE_VOL_PCT}%)"
                elif vol_tvl_chg <= -(DECLINE_VOL_TVL_PCT / 100):
                    decline_reason = f"Vol/TVL cayó {vol_tvl_chg * 100:.1f}% (>{DECLINE_VOL_TVL_PCT}%)"

                if decline_reason:
                    if prev.get("last_alert", "") > cutoff_4h:
                        print(f"[COOLDOWN] {m['name']} — meteora decline within {ALERT_COOLDOWN_H}h cooldown")
                    else:
                        _blocked, _reason = _hard_block(m)
                        if _blocked:
                            print(f"[METEORA-SKIP-DECLINE] {m['name']} — {_reason}")
                        elif send_telegram(build_decline_alert(m, decline_reason, prev_tvl, prev_vol)):
                            alerts_sent += 1
                            prev["last_alert"] = now_iso
                    prev["alerted_decline"] = True

            prev["last_tvl"]     = m["tvl"]
            prev["last_vol_24h"] = m["vol_24h"]

    # ── Raydium CLMM ─────────────────────────────────────────────────────────
    print(f"[{now_iso}] Fetching Raydium CLMM pools…")
    raydium_alerted: dict = state.setdefault("raydium_alerted", {})
    raydium_pools = fetch_raydium_clmm(top100_ids)
    print(f"[{now_iso}] {len(raydium_pools)} Raydium CLMM pools pass TVL/Vol filters")

    for m in raydium_pools:
        sym_a = m.get("symbol_a", "")
        sym_b = m.get("symbol_b", "")
        if sym_a.lower() not in _STABLE_SYMS:
            raw_sym = sym_a
        elif sym_b.lower() not in _STABLE_SYMS:
            raw_sym = sym_b
        else:
            raw_sym = ""
        sym = _SYM_ALIAS.get(raw_sym.upper(), raw_sym)
        m["rsi"] = _fetch_rsi(None, sym, rsi_cache)
        m["merkl"] = _get_merkl(m["address"], "Solana", merkl_cache)

    for m in raydium_pools:
        pid = m["address"]
        if pid not in raydium_alerted:
            _blocked, _reason = _hard_block(m)
            if _blocked:
                print(f"[RAYDIUM-SKIP] {m['name']} — {_reason}")
            elif send_telegram(build_new_alert(m)):
                alerts_sent += 1
                raydium_alerted[pid] = {
                    "first_seen":      now_iso,
                    "last_alert":      now_iso,
                    "last_tvl":        m["tvl"],
                    "last_vol_24h":    m["vol_24h"],
                    "alerted_decline": False,
                    "name":            m["name"],
                }
        else:
            prev     = raydium_alerted[pid]
            prev_tvl = prev.get("last_tvl", m["tvl"])
            prev_vol = prev.get("last_vol_24h", m["vol_24h"])

            if not prev.get("alerted_decline"):
                tvl_chg      = (m["tvl"] - prev_tvl) / prev_tvl if prev_tvl > 0 else 0
                vol_chg      = (m["vol_24h"] - prev_vol) / prev_vol if prev_vol > 0 else 0
                prev_vol_tvl = prev_vol / prev_tvl if prev_tvl > 0 else 0
                curr_vol_tvl = m["vol_24h"] / m["tvl"] if m["tvl"] > 0 else 0
                vol_tvl_chg  = (curr_vol_tvl - prev_vol_tvl) / prev_vol_tvl if prev_vol_tvl > 0 else 0

                decline_reason = None
                if tvl_chg <= -(DECLINE_TVL_PCT / 100):
                    decline_reason = f"TVL cayó {tvl_chg * 100:.1f}% (>{DECLINE_TVL_PCT}%)"
                elif vol_chg <= -(DECLINE_VOL_PCT / 100):
                    decline_reason = f"Vol cayó {vol_chg * 100:.1f}% (>{DECLINE_VOL_PCT}%)"
                elif vol_tvl_chg <= -(DECLINE_VOL_TVL_PCT / 100):
                    decline_reason = f"Vol/TVL cayó {vol_tvl_chg * 100:.1f}% (>{DECLINE_VOL_TVL_PCT}%)"

                if decline_reason:
                    if prev.get("last_alert", "") > cutoff_4h:
                        print(f"[COOLDOWN] {m['name']} — raydium decline within {ALERT_COOLDOWN_H}h cooldown")
                    else:
                        _blocked, _reason = _hard_block(m)
                        if _blocked:
                            print(f"[RAYDIUM-SKIP-DECLINE] {m['name']} — {_reason}")
                        elif send_telegram(build_decline_alert(m, decline_reason, prev_tvl, prev_vol)):
                            alerts_sent += 1
                            prev["last_alert"] = now_iso
                    prev["alerted_decline"] = True

            prev["last_tvl"]     = m["tvl"]
            prev["last_vol_24h"] = m["vol_24h"]

    state["last_run"] = now_iso
    state["qualifying_count"] = len(qualifying)
    state["meteora_qualifying_count"] = len(meteora_pools)
    state["raydium_qualifying_count"] = len(raydium_pools)
    save_state(state)

    print(
        f"[{now_iso}] Done. Alerts sent: {alerts_sent}. "
        f"Orca: {len(alerted)}. Meteora: {len(meteora_alerted)}. Raydium: {len(raydium_alerted)}."
    )


if __name__ == "__main__":
    main()
