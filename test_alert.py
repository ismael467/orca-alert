#!/usr/bin/env python3
"""Envía alertas de prueba a Telegram para verificar el formato visual."""

import os
import sys

for var in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
    if not os.environ.get(var):
        print(f"ERROR: falta la variable {var}")
        sys.exit(1)

from orca_alert import build_new_alert, build_decline_alert, send_telegram

POOL_NEW = {
    "address":  "GBEFCNQCAJmCkfkHqBP9N5N2T4vKJGSFJkWEnJNfax1M",
    "symbol_a": "SOL",
    "symbol_b": "USDC",
    "fee_rate": 0.003,
    "tvl":      480_000,
    "vol_24h":  720_000,
    "fees_24h": 2_160,
    "apr":      1_642.0,
    "rsi":      38.0,
    "in_top100": True,
    "dex":      "Orca",
}

POOL_DECLINE = {**POOL_NEW,
    "tvl":     220_000,
    "vol_24h":  55_000,
    "fees_24h":   165,
    "apr":      274.0,
    "rsi":      68.0,
}

def send(label: str, msg: str) -> None:
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(msg)
    print("---")
    ok = send_telegram(msg)
    print("✅ Enviado" if ok else "❌ Error al enviar")

send("build_new_alert",     build_new_alert(POOL_NEW))
send("build_decline_alert", build_decline_alert(POOL_DECLINE, "Vol -85%", 480_000, 720_000))
