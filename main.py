"""cbBTC capitulation-bounce bot for agent-fun/bars-replay/v1.

Long-only, one symbol (CBBTC). Sits in cash almost all the time and only
buys sharp capitulation flushes, then rides the snap-back with a tight
take-profit, a stop-loss, a trailing stop and a time exit.

Protocol: newline-delimited JSON on stdin/stdout. One `hello` (no reply),
one `batch` per minute (exactly one `reply` line each), then `done`.
All money math is integer-only. Fully deterministic: no clock, no
randomness, no network, no files, stdlib only.
"""

import json
import sys

SYMBOL = "CBBTC"
SLIP_BPS = 200

# Strategy knobs (basis points where applicable).
# Selectivity: one-minute return std is ~8 bps, so a 5-minute move's std
# is ~18 bps, a 15-minute move's ~31 bps and a 60-minute move's ~62 bps.
# Each trigger below sits ~4.5 standard deviations out, so it fires on a
# genuine capitulation flush, not on the slow bleed (in the dev window the
# bleed reaches 130 bps over 15 minutes and 242 bps vs the 60-bar high
# without ever flushing 80 bps in 5 minutes except at the real flush).
TRIG_5M_BPS = 80        # 0.80% drop over 5 closes
TRIG_15M_BPS = 140      # 1.40% drop over 15 closes
TRIG_60M_BPS = 280      # 2.80% drop vs trailing 60-bar high
TAKE_PROFIT_BPS = 150    # +1.50% vs average fill
STOP_LOSS_BPS = 100      # -1.00% vs average fill
TRAIL_BPS = 50           # -0.50% below best close since entry
TRAIL_ARM_BPS = 40       # trailing stop only arms once up 0.40%
MAX_HOLD_BARS = 240
COOLDOWN_BARS = 30
MAX_TRANCHES = 3
GUARD_BPS = 9000         # stop new entries once equity < 90% of start


def to_int(value):
    """Coerce an integer-decimal-string (or int) to int; None -> 0."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    return int(str(value))


def log(*parts):
    print(" ".join(str(p) for p in parts), file=sys.stderr, flush=True)


class BounceBot:
    def __init__(self):
        self.price_scale = 5
        self.qty_scale = 8
        self.quote_scale = 6
        self.start_equity = 1000000000
        self.warmup_bars = 0
        self.max_weight_bps = 10000
        self.min_notional = 0
        self.closes = []
        self.highs = []
        self.fills_in = []       # filled buy tranches: {"qty": int, "price": int}
        self.pending = {}        # client_id -> "buy" | "sell"
        self.buy_n = 0
        self.sell_n = 0
        self.entry_seq = None    # batch seq when the first tranche filled
        self.best_close = None
        self.cooldown_until = -1
        self.trades_opened = 0

    # -- config ---------------------------------------------------------
    def on_hello(self, msg):
        caps = msg.get("caps") or {}
        fees = msg.get("fees") or {}
        capital = msg.get("capital") or {}
        self.warmup_bars = to_int(msg.get("warmup_bars") or 0)
        self.max_weight_bps = to_int(caps.get("max_weight_bps") or 10000)
        self.min_notional = to_int(fees.get("min_notional_quote_units") or 0)
        if capital.get("quote_units") is not None:
            self.start_equity = to_int(capital.get("quote_units"))
        for sym in msg.get("symbols") or []:
            if sym.get("symbol") == SYMBOL:
                self.price_scale = to_int(sym.get("price_scale") or 5)
                self.qty_scale = to_int(sym.get("qty_scale") or 8)
        if capital.get("quote_scale") is not None:
            self.quote_scale = to_int(capital.get("quote_scale"))
        log("hello warmup=%d max_weight=%d min_notional=%d start=%d"
            % (self.warmup_bars, self.max_weight_bps,
               self.min_notional, self.start_equity))

    # -- money ----------------------------------------------------------
    def value_quote(self, qty, price):
        """USDC raw units (1e-6) of qty at price, integer-only."""
        num = qty * price * (10 ** self.quote_scale)
        den = 10 ** (self.qty_scale + self.price_scale)
        return num // den

    def avg_fill_price(self):
        if not self.fills_in:
            return None
        total_qty = 0
        total_cost = 0
        for tranche in self.fills_in:
            total_qty += tranche["qty"]
            total_cost += tranche["qty"] * tranche["price"]
        if total_qty <= 0:
            return None
        return total_cost // total_qty

    # -- fills ----------------------------------------------------------
    def on_fills(self, fills, seq):
        for fill in fills or []:
            cid = fill.get("client_id")
            side = fill.get("side")
            status = fill.get("status")
            self.pending.pop(cid, None)
            if status != "filled":
                log("seq=%d fill rejected %s %s" % (seq, cid, side))
                continue
            if side == "buy":
                qty = to_int(fill.get("qty"))
                price = to_int(fill.get("price"))
                if qty > 0 and price > 0:
                    self.fills_in.append({"qty": qty, "price": price})
                    if self.entry_seq is None:
                        self.entry_seq = seq
                        self.trades_opened += 1
                    log("seq=%d buy filled %s qty=%d price=%d"
                        % (seq, cid, qty, price))
            elif side == "sell":
                self.fills_in = []
                self.entry_seq = None
                self.best_close = None
                self.cooldown_until = seq + COOLDOWN_BARS
                log("seq=%d sell filled %s cooldown_until=%d"
                    % (seq, cid, self.cooldown_until))

    # -- signals --------------------------------------------------------
    def drop_bps(self, old, new):
        """How far `new` fell below `old`, in bps (negative if it rose)."""
        if old <= 0:
            return 0
        return (old - new) * 10000 // old

    def triggers(self):
        """Return (any_trigger, drop5_bps) from the close history."""
        if not self.closes:
            return False, 0
        close = self.closes[-1]
        drop5 = 0
        if len(self.closes) >= 6:
            drop5 = self.drop_bps(self.closes[-6], close)
        drop15 = 0
        if len(self.closes) >= 16:
            drop15 = self.drop_bps(self.closes[-16], close)
        drop60 = 0
        if len(self.highs) >= 60:
            high60 = max(self.highs[-60:])
            drop60 = self.drop_bps(high60, close)
        fired = (drop5 >= TRIG_5M_BPS or drop15 >= TRIG_15M_BPS
                 or drop60 >= TRIG_60M_BPS)
        return fired, drop5

    # -- sizing ---------------------------------------------------------
    def buy_notional(self, cash, equity, pos_qty, close):
        notional = cash // MAX_TRANCHES
        if notional <= 0:
            return 0
        pos_value = self.value_quote(pos_qty, close) if pos_qty > 0 else 0
        max_pos = equity * self.max_weight_bps // 10000
        room = max_pos - pos_value
        if room <= 0:
            return 0
        if notional > room:
            notional = room
        if notional > cash:
            notional = cash
        if notional < self.min_notional:
            return 0
        return notional

    # -- one batch ------------------------------------------------------
    def on_batch(self, msg):
        seq = to_int(msg.get("seq"))
        account = msg.get("account") or {}
        cash = to_int(account.get("cash_quote_units"))
        equity = to_int(account.get("equity_quote_units"))
        pos_qty = 0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == SYMBOL:
                pos_qty += to_int(pos.get("qty"))

        self.on_fills(msg.get("fills") or [], seq)

        # Recompute position after fills were applied to our bookkeeping.
        # Account is the source of truth; if it says flat and nothing is
        # in flight, drop any stale tranche bookkeeping.
        pending_buys = sum(1 for v in self.pending.values() if v == "buy")
        if pos_qty == 0 and pending_buys == 0 and not self.fills_in:
            self.entry_seq = None
            self.best_close = None
        if pos_qty == 0 and pending_buys == 0 and self.fills_in:
            # Position vanished without a sell fill we saw (should not
            # happen); trust the account and reset.
            self.fills_in = []
            self.entry_seq = None
            self.best_close = None

        bar = None
        for item in msg.get("bars") or []:
            if item.get("symbol") == SYMBOL:
                bar = item
                break
        close = None
        if bar is not None:
            close = to_int(bar.get("c"))
            self.closes.append(close)
            self.highs.append(to_int(bar.get("h")))
            if pos_qty > 0:
                if self.best_close is None or close > self.best_close:
                    self.best_close = close

        orders = []
        if close is not None and pos_qty > 0:
            exit_order = self.maybe_exit(seq, pos_qty, close)
            if exit_order is not None:
                orders.append(exit_order)
        if not orders and close is not None:
            buy_order = self.maybe_enter(seq, cash, equity, pos_qty,
                                         close, pending_buys)
            if buy_order is not None:
                orders.append(buy_order)

        return {"type": "reply", "seq": seq, "orders": orders}

    def maybe_exit(self, seq, pos_qty, close):
        avg = self.avg_fill_price()
        if avg is None or avg <= 0:
            return None
        move_bps = (close - avg) * 10000 // avg
        held = 0 if self.entry_seq is None else seq - self.entry_seq
        trail_hit = False
        if self.best_close is not None and self.best_close > 0:
            # Arm the trail only once the bounce has actually shown up:
            # a 50 bps pullback from a best close that never cleared
            # +0.40% is just one-minute noise, not a failed bounce.
            armed = (self.best_close - avg) * 10000 // avg >= TRAIL_ARM_BPS
            pulled = (self.best_close - close) * 10000 // self.best_close >= TRAIL_BPS
            trail_hit = armed and pulled
        reason = None
        if move_bps >= TAKE_PROFIT_BPS:
            reason = "take-profit"
        elif move_bps <= -STOP_LOSS_BPS:
            reason = "stop-loss"
        elif trail_hit:
            reason = "trailing-stop"
        elif held >= MAX_HOLD_BARS:
            reason = "time-exit"
        if reason is None:
            return None
        self.sell_n += 1
        cid = "sell-%d" % self.sell_n
        self.pending[cid] = "sell"
        log("seq=%d exit %s move_bps=%d held=%d qty=%d"
            % (seq, reason, move_bps, held, pos_qty))
        return {"side": "sell", "client_id": cid, "symbol": SYMBOL,
                "qty": str(pos_qty), "max_slippage_bps": SLIP_BPS}

    def maybe_enter(self, seq, cash, equity, pos_qty, close, pending_buys):
        if seq < self.warmup_bars:
            return None
        if seq < self.cooldown_until:
            return None
        if equity < self.start_equity * GUARD_BPS // 10000:
            return None
        if pending_buys > 0:
            return None
        have = len(self.fills_in)
        if pos_qty == 0 and have > 0:
            return None
        if pos_qty > 0 and have >= MAX_TRANCHES:
            return None
        if pos_qty == 0 and have >= 1:
            return None
        fired, drop5 = self.triggers()
        if pos_qty == 0:
            if not fired:
                return None
        else:
            # Scale-in while holding: a fresh trigger adds the next
            # tranche, and so does the two-bar deployment window right
            # after the first fill. A 4.5-sigma flush is rare; when one
            # fires, the full size goes in over three bars to average
            # the fill instead of betting everything on one open.
            held = 0 if self.entry_seq is None else seq - self.entry_seq
            deploying = held <= 2
            if not fired and not deploying:
                return None
        if close <= 0:
            return None
        notional = self.buy_notional(cash, equity, pos_qty, close)
        if notional <= 0:
            return None
        self.buy_n += 1
        cid = "buy-%d" % self.buy_n
        self.pending[cid] = "buy"
        log("seq=%d enter tranche=%d notional=%d drop5=%d close=%d"
            % (seq, have + 1, notional, drop5, close))
        return {"side": "buy", "client_id": cid, "symbol": SYMBOL,
                "notional_quote_units": str(notional),
                "max_slippage_bps": SLIP_BPS}


def main():
    bot = BounceBot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        mtype = msg.get("type")
        if mtype == "hello":
            bot.on_hello(msg)
        elif mtype == "batch":
            reply = bot.on_batch(msg)
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif mtype == "done":
            log("done seq=%s trades_opened=%d"
                % (msg.get("seq"), bot.trades_opened))
            break


if __name__ == "__main__":
    main()
