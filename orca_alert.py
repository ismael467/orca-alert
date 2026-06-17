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
    badge = m["badge"]
    data_lines = (
        f"Pool:      {m['name']}\n"
        f"APR:       {m['apr']:,.0f}%\n"
        f"Vol 24h:   {fmt_money(m['vol_24h'])}\n"
        f"Fees 24h:  {fmt_money(m['fees_24h'])}\n"
        f"TVL:       {fmt_money(m['tvl'])}"
    )
    return (
        f"🚨 NUEVA OPORTUNIDAD — ORCA {badge}\n\n"
        f"<pre>{data_lines}</pre>\n\n"
        f"🔗 https://birdeye.so/pool/{m['address']}?chain=solana"
    )


def build_decline_alert(m: dict, reason: str, prev_tvl: float, prev_vol: float) -> str:
    data_lines = (
        f"Pool:      {m['name']}\n"
        f"Motivo:    {reason}\n"
        f"TVL:       {fmt_money(m['tvl'])} (ant: {fmt_money(prev_tvl)})\n"
        f"Vol 24h:   {fmt_money(m['vol_24h'])} (ant: {fmt_money(prev_vol)})\n"
        f"Fees 24h:  {fmt_money(m['fees_24h'])}"
    )
    return (
        f"⚠️ POOL DECLIVE — ORCA\n\n"
        f"<pre>{data_lines}</pre>\n\n"
        f"🔗 https://birdeye.so/pool/{m['address']}?chain=solana"
    )


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

    alerts_sent = 0
    for m in qualifying:
        pid = m["address"]

        if pid not in alerted:
            if send_telegram(build_new_alert(m)):
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
                    if send_telegram(
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
