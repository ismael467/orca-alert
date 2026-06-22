#!/usr/bin/env python3
"""Orca (Solana Whirlpools) high-APR pool alert bot.

Data source: https://api.mainnet.orca.so/v1/whirlpool/list
CoinGecko:  top-30 coins by market cap fetched live each run
State:      state.json committed to repo with [skip ci]
"""

import json
import os
import time
from datetime import datetime, timezone
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

MIN_APR_PCT = 500
MIN_VOL_24H = 50_000
MIN_FEES_24H = 500
MAX_TVL = 5_000_000
DECLINE_TVL_PCT = 50
DECLINE_VOL_PCT = 60

# ── Hard-block filter constants ──────────────────────────────────────────────
HARD_MIN_LP_SCORE  = 30
HARD_MIN_TVL       = 50_000
HARD_MAX_APR_RANGO = 3_000

BLACKLIST_SYMBOLS = {
    "USELESS", "SHIT", "DOGE2", "PEPE2", "SCAM",
    "FAKE", "SAFE", "MOON2", "INU2",
}

_STABLE_SYMS = {"usdc", "usdt", "dai", "busd", "tusd", "frax", "usdh", "eurc"}

BLUE_CHIP_TOKENS = {
    "BTC", "WBTC", "ETH", "WETH", "SOL", "WSOL", "BNB", "WBNB", "XRP", "ADA",
    "AVAX", "DOT", "MATIC", "WMATIC", "LINK", "UNI", "LTC", "BCH", "USDC", "USDT",
    "HYPE", "WHYPE", "ARB", "OP", "UBTC", "UETH", "USOL", "USDH", "TON", "NEAR", "APT",
    "cbBTC", "cbETH",
}


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
    return sum(1 for s in (sym_a.upper(), sym_b.upper()) if s in BLUE_CHIP_TOKENS)


def _lp_score(vol_24h: float, tvl: float, rsi: float | None, blue_chips: int = 0) -> int:
    score = 0.0
    if tvl > 0:
        score += min(40.0, (vol_24h / tvl) * 20.0)
    if rsi is not None and rsi < 50:
        score += 20.0
    if tvl >= 500_000:
        score += 10.0
    if blue_chips >= 2:
        score += 15.0
    elif blue_chips == 1:
        score += 10.0
    return int(min(100, score))


def _fmt_apr_rango(val: float | None) -> str:
    if val is None:
        return "N/D"
    return ">10,000%" if val > 10_000 else f"{val:,.0f}%"


def _hard_block(m: dict) -> tuple[bool, str]:
    """Return (True, reason) if this pool must be dropped before alerting."""
    # 1. LP Score < 30
    lp = _lp_score(m.get("vol_24h", 0), m.get("tvl", 0), m.get("rsi"), _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", "")))
    print(f"[hard_block] {m.get('name', '')} — LP Score: {lp}/100, TVL: ${m.get('tvl', 0):,.0f}")
    if lp < HARD_MIN_LP_SCORE:
        return True, f"LP Score {lp} < {HARD_MIN_LP_SCORE}"
    # 2. RSI N/D
    if m.get("rsi") is None:
        return True, "RSI N/D (token no encontrado en CoinGecko/Binance)"
    # 3. TVL < $50,000
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


def passes_filters(m: dict) -> bool:
    return (
        m["apr"] >= MIN_APR_PCT
        and m["vol_24h"] >= MIN_VOL_24H
        and m["fees_24h"] >= MIN_FEES_24H
        and m["tvl"] >= HARD_MIN_TVL
        and m["tvl"] <= MAX_TVL
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


def build_new_alert(m: dict) -> str:
    rsi       = m.get("rsi")
    rsi_str   = f"{rsi:.0f}" if rsi is not None else "N/D"
    rsi_emoji = "" if rsi is None else ("🟢" if rsi < 30 else ("🟡" if rsi < 50 else ("🟠" if rsi < 70 else "🔴")))
    lp        = _lp_score(m.get("vol_24h", 0), m.get("tvl", 0), rsi, _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", "")))
    lp_emoji  = "🟢" if lp >= 70 else ("🟡" if lp >= 50 else "🔴")
    tvl       = m["tvl"]
    vol_24h   = m["vol_24h"]
    fees_24h  = m["fees_24h"]
    apr       = m["apr"]
    vol_tvl   = vol_24h / tvl if tvl > 0 else 0
    fire      = " 🔥" if vol_tvl > 1 else ""
    fees_1k   = (1_000 / tvl) * fees_24h if tvl > 0 else 0
    badge     = "🟢" if m.get("in_top100") else "🟡"
    purl      = f"https://birdeye.so/pool/{m['address']}?chain=solana"
    sep       = "━" * 19
    W         = 19
    body_lines = [
        f"{'Pool:':<{W}}{m['symbol_a']} / {m['symbol_b']}",
        f"{'TVL:':<{W}}{fmt_money(tvl)}",
        f"{'Vol 24h:':<{W}}{fmt_money(vol_24h)}",
        f"{'Vol/TVL:':<{W}}{vol_tvl:.2f}x{fire}",
        sep,
        f"{'Fees/día ($1K):':<{W}}${fees_1k:.2f}",
        f"{'APR Fees:':<{W}}{apr:,.0f}%",
        sep,
        f"{'RSI:':<{W}}{rsi_str}{' ' + rsi_emoji if rsi_emoji else ''}",
        f"{'LP Score:':<{W}}{lp}/100 {lp_emoji}",
    ]
    header = f"🚨 NUEVA OPORTUNIDAD — Orca | SOL {badge}"
    body   = "\n".join(body_lines)
    return f'{header}\n<pre>{body}</pre>\n🔗 <a href="{purl}">Birdeye</a>'


def build_decline_alert(m: dict, reason: str, prev_tvl: float, prev_vol: float) -> str:
    rsi       = m.get("rsi")
    rsi_str   = f"{rsi:.0f}" if rsi is not None else "N/D"
    lp        = _lp_score(m.get("vol_24h", 0), m.get("tvl", 0), rsi, _blue_chip_count(m.get("symbol_a", ""), m.get("symbol_b", "")))
    lp_emoji  = "🟢" if lp >= 70 else ("🟡" if lp >= 50 else "🔴")
    fees_1k   = (1_000 / m["tvl"]) * m["fees_24h"] if m["tvl"] > 0 else 0
    purl      = f"https://birdeye.so/pool/{m['address']}?chain=solana"
    sep       = "━" * 19
    W         = 19
    body_lines = [
        f"{'Pool:':<{W}}{m['symbol_a']} / {m['symbol_b']}",
        f"{'Motivo:':<{W}}{reason}",
        f"{'TVL:':<{W}}{fmt_money(m['tvl'])} (ant: {fmt_money(prev_tvl)})",
        f"{'Vol 24h:':<{W}}{fmt_money(m['vol_24h'])} (ant: {fmt_money(prev_vol)})",
        sep,
        f"{'Fees/día ($1K):':<{W}}${fees_1k:.2f}",
        f"{'RSI:':<{W}}{rsi_str}",
        f"{'LP Score:':<{W}}{lp}/100 {lp_emoji}",
    ]
    header = f"⚠️ POOL DECLIVE — Orca | SOL"
    body   = "\n".join(body_lines)
    return f'{header}\n<pre>{body}</pre>\n🔗 <a href="{purl}">Birdeye</a>'


def main() -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
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

                decline_reason = None
                if tvl_chg <= -(DECLINE_TVL_PCT / 100):
                    decline_reason = f"TVL cayó {tvl_chg * 100:.1f}% (>{DECLINE_TVL_PCT}%)"
                elif vol_chg <= -(DECLINE_VOL_PCT / 100):
                    decline_reason = f"Vol cayó {vol_chg * 100:.1f}% (>{DECLINE_VOL_PCT}%)"

                if decline_reason:
                    _blocked, _reason = _hard_block(m)
                    if _blocked:
                        print(f"[SKIP-DECLINE] {m['name']} — {_reason}")
                    elif send_telegram(
                        build_decline_alert(m, decline_reason, prev_tvl, prev_vol)
                    ):
                        alerts_sent += 1
                    prev["alerted_decline"] = True

            prev["last_tvl"] = m["tvl"]
            prev["last_vol_24h"] = m["vol_24h"]

    state["last_run"] = now_iso
    state["qualifying_count"] = len(qualifying)
    save_state(state)

    print(
        f"[{now_iso}] Done. Alerts sent: {alerts_sent}. "
        f"Tracked pools: {len(alerted)}."
    )


if __name__ == "__main__":
    main()
