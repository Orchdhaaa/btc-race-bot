# BTC Race Bot

One-Swing 55/80 + Probe: a BTC trading bot for the make.fun 24h race.

Buys 100% of cash when the close is >= 55 bp below the trailing 60-bar
high; exits on take-profit +80 bp, trailing stop -50 bp (armed at +50 bp),
hard stop -100 bp, or a 480-bar time stop. A $250 probe buy at batch 1200
guarantees the >= 1-trade guardrail if the main lane never fires.

Run: `python3 -P -s main.py` (Python 3.13, stdlib only).
