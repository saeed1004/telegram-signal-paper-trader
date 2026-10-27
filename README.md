# Telegram Signal Paper Trader (Solana)

A small, readable demo of a **signal-driven Solana trading bot** — in **paper mode**:
no wallet, no private key, no real money.

It listens to a Telegram channel or group, extracts token contract addresses from each
message (plain addresses or pump.fun / DexScreener links), runs safety checks and opens
**paper** trades with take-profit, stop-loss and max-hold exits. Every step is reported
back to Telegram and logged to `trades.csv`.

```
Telegram call ──► extract CA ──► DexScreener (price, liquidity, pair age)
                                 Jupiter quote (price impact for the size)
                                 Solana RPC   (mint / freeze authority)
                              ──► safety OK? ──► PAPER BUY ──► TP / SL / max hold ──► PAPER SELL + report
```

## Features
- Contract-address extraction with base58 validation (ignores SOL / USDC / USDT)
- One trade per address, max open positions
- Safety filters that **fail closed**: minimum liquidity, maximum price impact,
  mint & freeze authority must be revoked
- Entry/exit include slippage; results in % and SOL
- `/status` command in the chat: open positions, wins, paper PnL
- Measures **signal → decision** time for each call
- Only public data, no API keys except your own Telegram bot token

## Quick start
1. Install Python 3.10+ and run `pip install -r requirements.txt`
2. Create a new bot with **@BotFather**, copy its token
3. Create a test channel, add the bot as **admin**
4. Copy `.env.example` to `.env` and paste the token
5. Run `python paper_trader.py`, then post a token address in the channel

Test without Telegram:
```
python paper_trader.py --test "new call: https://dexscreener.com/solana/<TOKEN_ADDRESS>"
```

## Going live (not included)
The live version replaces the paper buy/sell with real swaps (Jupiter swap API or direct
AMM instructions), priority fees / Jito bundles, a dedicated RPC and a Telegram user
session (Telethon) to read channels the bot cannot join. Keys stay only on the
owner's server in `.env`.

## Disclaimer
Educational demo. Not financial advice. Memecoin trading is extremely risky.
