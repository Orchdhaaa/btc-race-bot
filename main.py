#!/usr/bin/env python3
"""One-Swing 55/80 + Probe -- BTC race bot (btc-race-24h-20261112-1439).

Trades cbBTC/USDC on a 24h replay of one Orca Whirlpool pool. Paper money
only; the harness simulates fills, fees, slippage and price impact.

Design (swarm-verified, backtest +$8.53 on the dev window, 2 filled orders,
max drawdown 0.30% -- see work/STRATEGY-2026-11-12.md):
- Main lane, max 1 trade: buy 100% of cash when the close is >= 55 bp below
  the trailing 60-bar high (first decision at the 60th close, batch seq 59).
  Exits, evaluated on every close while holding and filled at the next open:
  take-profit +80 bp vs entry cost; trailing stop -50 bp from the peak close
  since entry, armed once the mark is >= +50 bp vs entry cost; hard stop
  -100 bp vs entry cost; time stop 480 bars after the entry fill.
  No entry decision at batch seq >= 1409. Buys slip 100 bp, sells slip 300 bp.
- Probe lane: a $250 fixed buy at decision batch seq 1200, only if zero
  filled trades so far and flat. Exits: TP +60, trail -40 (armed once the
  mark is > cost), stop -60, hold 120. After the probe fires the main lane
  is disabled for the rest of the window. The probe guarantees the
  >= 1-trade guardrail in monotonic regimes where the main lane never fires.
  Worst probe outcome is about -$1.90.

All decisions are integer bps (cross-multiplied comparisons); no floats in
any branch, no randomness, no wall clock, no files, no network. Deterministic:
the same bars give the same replies, byte for byte.

Python 3.13, stdlib only. The protocol shim is vendored under vendor/
(makefun_bars.py, byte-identical copy of the kit SDK).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10000

# --- main lane -------------------------------------------------------------
HIGH_N = 60            # trailing-high lookback (bars)
DIP_MIN_BPS = 55       # buy when close is >= 55 bp below the 60-bar high
TP_BPS = 80            # take-profit vs entry cost
TRAIL_BPS = 50         # trailing stop: give-back from peak close since entry
ARM_BPS = 50           # arm the trailing stop once mark >= +50 bp vs cost
STOP_BPS = 100         # hard stop vs entry cost
MAX_HOLD_BARS = 480    # time stop (bars after entry fill)
COOL_BARS = 5          # cooldown after a sell fill (batches)
BUY_MAX_SLIP = 100     # max_slippage_bps on buys
SELL_MAX_SLIP = 300    # max_slippage_bps on sells
NO_ENTRY_SEQ = 1409    # no entry decision at batch seq >= 1409
MIN_NOTIONAL = 1000000  # 1 USDC, in raw units (fees.min_notional_quote_units)

# --- probe lane ------------------------------------------------------------
PROBE_BAR = 1200       # decision batch seq that triggers the probe
PROBE_SIZE = 250000000  # $250 fixed notional, in raw units
PROBE_TP = 60
PROBE_TRAIL = 40
PROBE_STOP = 60
PROBE_HOLD = 120


def value(qty, price):
    """Value of qty (base units) at price (price-scale units), in raw units."""
    return qty * price // 10**7


class Bot:
    """One bot, one position, one trade. State is reconciled from fills only."""

    def __init__(self):
        self.closes = []          # every CBBTC close, in batch order (ints)
        self.entry_fill_seq = -1  # batch seq whose open filled the entry
        self.peak_close = 0       # highest close since entry (trailing-stop ref)
        self.trail_armed = False
        self.filled_trades = 0    # filled own orders (flatten excluded)
        self.main_trades = 0      # filled main-lane trades (max 1)
        self.probe_fired = False  # the probe buy has filled
        self.is_probe = False     # the open position is the probe trade
        self.cooldown_until_seq = 0
        self._cid = 0             # client-id counter (unique ids, no reuse)
        self._kind = {}           # client_id -> "main" | "probe" | "sell"

    # -- indicators ---------------------------------------------------------
    def _dd60_bps(self):
        """Drawdown of the latest close vs the trailing 60-bar high, in bps."""
        if len(self.closes) < HIGH_N:
            return None
        hi = max(self.closes[-HIGH_N:])
        c = self.closes[-1]
        return (hi - c) * BPS // hi

    # -- orders -------------------------------------------------------------
    def _buy(self, notional, kind):
        self._cid += 1
        cid = "o%d" % self._cid
        self._kind[cid] = kind
        return {"side": "buy", "symbol": SYMBOL,
                "notional_quote_units": int(notional),
                "max_slippage_bps": BUY_MAX_SLIP, "client_id": cid}

    def _sell(self, qty):
        self._cid += 1
        cid = "o%d" % self._cid
        self._kind[cid] = "sell"
        return {"side": "sell", "symbol": SYMBOL, "qty": int(qty),
                "max_slippage_bps": SELL_MAX_SLIP, "client_id": cid}

    # -- per-batch ----------------------------------------------------------
    def on_batch(self, state, batch):
        # 1. Reconcile fills (authoritative -- fills, not sent orders, move
        #    bot state). The SDK already folded them into state; here we
        #    update only the strategy's own fields.
        for fill in batch.get("fills", []):
            if fill.get("status") != "filled":
                continue  # a rejected fill changes nothing
            if str(fill.get("client_id", "")).startswith("_"):
                continue  # the flatten is not our trade
            kind = self._kind.get(fill.get("client_id"))
            if fill.get("side") == "buy":
                self.entry_fill_seq = int(fill["fill_bar"])
                self.peak_close = 0
                self.trail_armed = False
                self.filled_trades += 1
                self.is_probe = (kind == "probe")
                if kind == "probe":
                    self.probe_fired = True
                else:
                    self.main_trades += 1
            elif fill.get("side") == "sell":
                self.entry_fill_seq = -1
                self.peak_close = 0
                self.trail_armed = False
                self.is_probe = False
                self.filled_trades += 1
                self.cooldown_until_seq = int(batch["seq"]) + COOL_BARS

        # 2. Update indicators from the new bar (if any). The gap batch
        #    carries no bar: no close is appended, no signal is computed.
        bar = batch["bars"][0] if batch.get("bars") else None
        if bar is not None:
            self.closes.append(int(bar["c"]))

        # 3. Guards: last batch (orders rejected after_end) and warm-up.
        if state.seq >= 1439 or state.warming_up or bar is None:
            return []

        # 4. Exits first (whole position, full qty, at most one sell).
        holding = state.positions.get(SYMBOL)
        if holding is not None and holding.qty > 0:
            order = self._decide_exit(state, int(bar["c"]))
            if order is not None:
                return [order]
            return []  # holding and no exit: never buy (max 1 position)

        # 5. Entries (only when flat).
        order = self._decide_entry(state, int(bar["c"]))
        if order is not None:
            return [order]
        return []

    # -- decisions ----------------------------------------------------------
    def _decide_exit(self, state, close):
        holding = state.positions.get(SYMBOL)
        if holding is None or holding.qty <= 0:
            return None
        qty = holding.qty
        cost = holding.cost  # cash spent on the quantity held, fees included
        mark = value(qty, close)
        # Arm the trailing stop once the mark clears the arm level.
        if self.is_probe:
            if mark > cost:
                self.trail_armed = True
        elif mark * BPS >= cost * (BPS + ARM_BPS):
            self.trail_armed = True
        # Peak close since entry (trailing-stop reference).
        if close > self.peak_close:
            self.peak_close = close
        bars_held = state.seq - self.entry_fill_seq
        if self.is_probe:
            tp, trail, stop, hold = PROBE_TP, PROBE_TRAIL, PROBE_STOP, PROBE_HOLD
        else:
            tp, trail, stop, hold = TP_BPS, TRAIL_BPS, STOP_BPS, MAX_HOLD_BARS
        if mark * BPS >= cost * (BPS + tp):
            pass  # take-profit fires
        elif mark * BPS <= cost * (BPS - stop):
            pass  # hard stop fires
        elif self.trail_armed and close * BPS <= self.peak_close * (BPS - trail):
            pass  # trailing stop fires
        elif bars_held >= hold:
            pass  # time stop fires
        else:
            return None
        return self._sell(qty)

    def _decide_entry(self, state, close):
        if state.seq >= NO_ENTRY_SEQ:
            return None
        if self.probe_fired:
            return None  # the probe IS the trade; the main lane is disabled
        # Probe lane: time-triggered at decision batch seq 1200, only if flat
        # and zero filled trades. It takes precedence over the main lane at
        # this seq; if its size check fails it returns nothing (no fall-through).
        if state.seq == PROBE_BAR and self.filled_trades == 0:
            if PROBE_SIZE >= MIN_NOTIONAL and PROBE_SIZE <= state.cash:
                return self._buy(PROBE_SIZE, "probe")
            return None
        # Main lane: max 1 trade, first decision at the 60th close (seq 59).
        if self.main_trades >= 1:
            return None
        if state.seq < self.cooldown_until_seq:
            return None
        if len(self.closes) < HIGH_N:
            return None
        dd = self._dd60_bps()
        if dd is not None and dd >= DIP_MIN_BPS:
            notional = min(state.cash, state.weight_room(SYMBOL))
            if notional >= MIN_NOTIONAL:
                return self._buy(notional, "main")
        return None


BOT = Bot()


def on_batch(state, batch):
    return BOT.on_batch(state, batch)


if __name__ == "__main__":
    makefun_bars.run(on_batch)
