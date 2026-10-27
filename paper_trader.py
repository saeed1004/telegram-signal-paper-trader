#!/usr/bin/env python3
"""
Telegram Signal Paper Trader (demo)
===================================
Listens to a Telegram channel or group through a bot, extracts Solana token
addresses (contract addresses) from each message, runs basic safety checks and
opens PAPER trades -- no wallet, no private key, no real money -- with
take-profit / stop-loss / max-hold exits. Everything is reported back to Telegram
and logged to trades.csv.

Data sources (all public, no API key needed):
  - DexScreener  : price, liquidity, pair age
  - Jupiter      : swap quote -> estimated price impact for the position size
  - Solana RPC   : mint account -> mint / freeze authority, token program

Usage:
  python paper_trader.py                 # run the Telegram listener
  python paper_trader.py --test <text>   # no Telegram: process one message and follow the trade
"""
import csv
import datetime as dt
import json
import os
import re
import sys
import time

import requests

# ---------------------------------------------------------------- config ----
def load_env(path=".env"):
    """Minimal .env loader (KEY=VALUE per line) so no extra package is needed."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


load_env()


def cfg(name, default):
    v = os.environ.get(name, "")
    return type(default)(v) if v != "" else default


TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHATS = {c.strip() for c in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if c.strip()}
RPC_URL = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUP_QUOTE_URL = os.environ.get("JUPITER_QUOTE_URL", "https://lite-api.jup.ag/swap/v1/quote")

SIZE_SOL = cfg("POSITION_SIZE_SOL", 0.5)
TAKE_PROFIT = cfg("TAKE_PROFIT_PCT", 50.0)
STOP_LOSS = cfg("STOP_LOSS_PCT", 25.0)
MAX_HOLD_MIN = cfg("MAX_HOLD_MIN", 30.0)
SLIPPAGE = cfg("SLIPPAGE_PCT", 1.0)          # applied on entry and on exit
MIN_LIQ = cfg("MIN_LIQUIDITY_USD", 20000.0)
MAX_IMPACT = cfg("MAX_PRICE_IMPACT_PCT", 5.0)
MAX_OPEN = cfg("MAX_OPEN_POSITIONS", 3)
CHECK_SEC = cfg("PRICE_CHECK_SEC", 15.0)
REQUIRE_NO_AUTH = cfg("REQUIRE_NO_MINT_FREEZE_AUTHORITY", 1)

SOL_MINT = "So11111111111111111111111111111111111111112"
IGNORE = {SOL_MINT,
          "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
          "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
CA_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{32,44}(?![1-9A-HJ-NP-Za-km-z])")
TRADES_CSV = "trades.csv"

HTTP = requests.Session()
HTTP.headers["User-Agent"] = "telegram-signal-paper-trader/1.0"


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------ address parsing ----
def b58_byte_len(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return len(body) + (len(s) - len(s.lstrip("1")))


def extract_addresses(text):
    """Return Solana addresses (32-byte base58) found in a message, in order, without duplicates.
    Works for plain addresses and links such as pump.fun/coin/<CA> or dexscreener.com/solana/<CA>."""
    found = []
    for m in CA_RE.findall(text or ""):
        if m in IGNORE or m in found:
            continue
        try:
            if b58_byte_len(m) == 32:
                found.append(m)
        except ValueError:
            pass
    return found


# ------------------------------------------------------------ market data ----
def get_json(url, params=None):
    try:
        r = HTTP.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"  ! request failed: {url} ({type(e).__name__})")
        return None


def market(ca):
    """Best (highest-liquidity) Solana pair for this token from DexScreener."""
    d = get_json(f"https://api.dexscreener.com/latest/dex/tokens/{ca}")
    pairs = [p for p in (d or {}).get("pairs") or []
             if p.get("chainId") == "solana" and p.get("priceUsd")
             and (p.get("baseToken") or {}).get("address") == ca]
    if not pairs:
        return None
    p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
    created = p.get("pairCreatedAt")
    return {"price": float(p["priceUsd"]),
            "liq": float((p.get("liquidity") or {}).get("usd") or 0),
            "symbol": (p.get("baseToken") or {}).get("symbol") or "?",
            "dex": p.get("dexId") or "?",
            "url": p.get("url") or "",
            "age_h": (time.time() * 1000 - created) / 3.6e6 if created else None}


def price_impact_pct(ca, sol_amount):
    """Estimated price impact (%) of buying `sol_amount` SOL of the token, from a Jupiter quote."""
    d = get_json(JUP_QUOTE_URL, {"inputMint": SOL_MINT, "outputMint": ca,
                                 "amount": int(sol_amount * 1e9), "slippageBps": int(SLIPPAGE * 100)})
    try:
        return float(d["priceImpactPct"]) * 100
    except Exception:
        return None


def mint_info(ca):
    """Mint / freeze authority and token program from the mint account (Solana RPC)."""
    try:
        r = HTTP.post(RPC_URL, timeout=10, json={"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
                                                 "params": [ca, {"encoding": "jsonParsed"}]}).json()
        v = r["result"]["value"]
        info = v["data"]["parsed"]["info"]
        return {"mint_authority": info.get("mintAuthority"),
                "freeze_authority": info.get("freezeAuthority"),
                "program": "token-2022" if v.get("owner", "").startswith("TokenzQd") else "spl-token"}
    except Exception as e:
        log(f"  ! mint check failed ({type(e).__name__})")
        return None


def safety_check(ca, mk):
    """Return (ok, lines). Fails closed: if a check cannot be done, the trade is skipped."""
    lines, ok = [], True
    if mk["liq"] < MIN_LIQ:
        ok = False
        lines.append(f"❌ liquidity ${mk['liq']:,.0f} < ${MIN_LIQ:,.0f}")
    else:
        lines.append(f"✅ liquidity ${mk['liq']:,.0f}")
    impact = price_impact_pct(ca, SIZE_SOL)
    if impact is None:
        ok = False
        lines.append("❌ no Jupiter route / quote")
    elif impact > MAX_IMPACT:
        ok = False
        lines.append(f"❌ price impact {impact:.2f}% > {MAX_IMPACT}%")
    else:
        lines.append(f"✅ price impact {impact:.2f}% for {SIZE_SOL} SOL")
    mi = mint_info(ca)
    if mi is None:
        ok = False
        lines.append("❌ mint account check failed")
    else:
        bad = [k.replace("_", " ") for k in ("mint_authority", "freeze_authority") if mi[k]]
        if bad and REQUIRE_NO_AUTH:
            ok = False
            lines.append("❌ " + " + ".join(bad) + " still active")
        else:
            lines.append(f"✅ mint & freeze authority revoked ({mi['program']})")
    return ok, lines


# ----------------------------------------------------------------- telegram ----
def tg(method, **params):
    if not TOKEN:
        return None
    try:
        r = HTTP.post(f"https://api.telegram.org/bot{TOKEN}/{method}", data=params, timeout=40)
        d = r.json()
        return d.get("result") if d.get("ok") else None
    except Exception as e:
        log(f"  ! telegram {method} failed ({type(e).__name__})")
        return None


def say(chat_id, text):
    print(text + "\n", flush=True)
    if chat_id is not None:
        tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview="true")


# ------------------------------------------------------------ paper trading ----
open_pos = {}      # ca -> position dict
seen = set()       # addresses already handled (one trade per call)
closed = []        # closed positions (this session)


def handle_message(text, chat_id, msg_time=None):
    for ca in extract_addresses(text):
        if ca in seen:
            continue
        seen.add(ca)
        t0 = time.time()
        log(f"signal: {ca}")
        mk = market(ca)
        if not mk:
            say(chat_id, f"⚪ {ca[:6]}…{ca[-4:]}: no Solana DEX pair found, skipped")
            continue
        ok, lines = safety_check(ca, mk)
        lag = f" | signal→decision {t0 - msg_time + (time.time() - t0):.1f}s" if msg_time else ""
        head = f"📡 ${mk['symbol']} ({mk['dex']}, pair age {mk['age_h']:.1f}h)" if mk["age_h"] is not None \
            else f"📡 ${mk['symbol']} ({mk['dex']})"
        if not ok:
            say(chat_id, head + "\n" + "\n".join(lines) + "\n⛔ SKIPPED (paper)" + lag)
            continue
        if len(open_pos) >= MAX_OPEN:
            say(chat_id, head + f"\n⛔ max open positions ({MAX_OPEN}) reached, skipped")
            continue
        entry = mk["price"] * (1 + SLIPPAGE / 100)
        open_pos[ca] = {"ca": ca, "symbol": mk["symbol"], "entry": entry, "opened": time.time(),
                        "chat": chat_id, "url": mk["url"], "peak": entry}
        say(chat_id, head + "\n" + "\n".join(lines) +
            f"\n🟢 PAPER BUY {SIZE_SOL} SOL @ ${entry:.8g}\nTP +{TAKE_PROFIT:.0f}% | SL -{STOP_LOSS:.0f}% | "
            f"max hold {MAX_HOLD_MIN:.0f} min{lag}\n{mk['url']}")


def check_positions():
    for ca, p in list(open_pos.items()):
        mk = market(ca)
        if not mk:
            continue
        exit_px = mk["price"] * (1 - SLIPPAGE / 100)
        ret = (exit_px / p["entry"] - 1) * 100
        p["peak"] = max(p["peak"], mk["price"])
        held = (time.time() - p["opened"]) / 60
        reason = ("TAKE PROFIT" if ret >= TAKE_PROFIT else
                  "STOP LOSS" if ret <= -STOP_LOSS else
                  "MAX HOLD" if held >= MAX_HOLD_MIN else None)
        log(f"  {p['symbol']:<10} {ret:+6.1f}%  held {held:4.1f} min")
        if reason:
            close(p, exit_px, ret, reason)


def close(p, exit_px, ret, reason):
    open_pos.pop(p["ca"], None)
    pnl_sol = SIZE_SOL * ret / 100
    p.update(exit=exit_px, ret=ret, pnl_sol=pnl_sol, reason=reason, closed_at=time.time())
    closed.append(p)
    icon = "✅" if ret > 0 else "🔴"
    say(p["chat"], f"{icon} PAPER SELL ${p['symbol']} — {reason}\nresult {ret:+.1f}% = {pnl_sol:+.4f} SOL "
                   f"(held {(p['closed_at'] - p['opened']) / 60:.1f} min)")
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["opened_utc", "closed_utc", "symbol", "address", "entry_usd", "exit_usd",
                        "return_pct", "pnl_sol", "reason"])
        fmt = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        w.writerow([fmt(p["opened"]), fmt(p["closed_at"]), p["symbol"], p["ca"], f"{p['entry']:.10g}",
                    f"{exit_px:.10g}", f"{ret:.2f}", f"{pnl_sol:.5f}", reason])


def status_text():
    tot = sum(c["pnl_sol"] for c in closed)
    wins = sum(c["ret"] > 0 for c in closed)
    lines = [f"📊 open {len(open_pos)} | closed {len(closed)} (wins {wins}) | paper PnL {tot:+.4f} SOL"]
    lines += [f"  • ${p['symbol']} open {(time.time() - p['opened']) / 60:.0f} min" for p in open_pos.values()]
    return "\n".join(lines)


# --------------------------------------------------------------------- main ----
def run_test(text):
    log("TEST MODE (no Telegram) — paper trading only")
    handle_message(text, None)
    while open_pos:
        time.sleep(CHECK_SEC)
        check_positions()
    print(status_text())


def run_bot():
    if not TOKEN:
        sys.exit("TELEGRAM_BOT_TOKEN is missing — copy .env.example to .env and fill it in.")
    me = tg("getMe")
    if not me:
        sys.exit("Telegram token rejected — check TELEGRAM_BOT_TOKEN.")
    log(f"bot @{me['username']} listening — PAPER MODE, no wallet, no real trades")
    offset, last_check = None, 0.0
    while True:
        updates = tg("getUpdates", timeout=10, offset=offset,
                     allowed_updates=json.dumps(["message", "channel_post"])) or []
        for u in updates:
            offset = u["update_id"] + 1
            m = u.get("channel_post") or u.get("message") or {}
            chat_id = (m.get("chat") or {}).get("id")
            if ALLOWED_CHATS and str(chat_id) not in ALLOWED_CHATS:
                continue
            text = m.get("text") or m.get("caption") or ""
            if text.strip().startswith("/status"):
                say(chat_id, status_text())
            elif text:
                handle_message(text, chat_id, m.get("date"))
        if open_pos and time.time() - last_check >= CHECK_SEC:
            last_check = time.time()
            check_positions()


if __name__ == "__main__":
    try:
        if "--test" in sys.argv:
            run_test(" ".join(sys.argv[sys.argv.index("--test") + 1:]))
        else:
            run_bot()
    except KeyboardInterrupt:
        print("\n" + status_text())
