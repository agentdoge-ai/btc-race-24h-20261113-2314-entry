# cbBTC Capitulation-Bounce Bot

A long-only, single-symbol (CBBTC) trading bot for the make.fun
`btc-race-24h-20261113-2314` competition — "Race a BTC trading bot live
for 24 hours". It speaks the `agent-fun/bars-replay/v1` protocol:
newline-delimited JSON on stdin/stdout, one reply per batch, no reply
to `hello`, clean exit after `done`.

## Strategy

Sit in cash almost all the time. Buy only sharp capitulation flushes,
then ride the snap-back.

- **Entries (flat, after warmup, cooldown expired):** a 5-minute
  close-to-close drop of at least 0.80%, or a 15-minute drop of at
  least 1.40%, or a 60-minute drop of at least 2.80% versus the
  trailing 60-bar high. Each threshold sits roughly 4.5 standard
  deviations out (1-minute return std is about 0.08%), so the bot
  fires on a genuine capitulation flush, not on the slow bleed — in
  the dev window the bleed reaches 1.30% over 15 minutes and 2.42%
  versus the 60-bar high without ever flushing 0.80% in 5 minutes
  except at the real flush.
- **Scale-in:** up to 3 tranches, each one third of current cash.
  Tranche 1 on the first trigger; tranches 2–3 on a fresh trigger
  while holding, or over the two bars right after the first fill
  (a rare 4.5-sigma flush is deployed in full over three bars to
  average the fill). Each tranche is its own buy order with its own
  client id, and the real fill price is tracked from the fill
  reports — never assumed.
- **Exits (whole position at once):** take-profit at +1.50% vs the
  average fill price; stop-loss at −1.00%; trailing stop at −0.50%
  below the best close since entry, armed only once the position is
  up at least +0.40% (so one-minute noise cannot stop out a
  developing bounce); time exit after 240 bars held.
  The sell quantity is always taken from the `account` positions in
  the current batch — the account object is the source of truth, not
  the bot's own bookkeeping.
- **Cooldown:** 30 bars after any exit before a new entry.
- **Drawdown guard:** if equity falls 10% or more below the $1,000
  start, no new entries for the rest of the run (exits still apply).
- **Sizing:** each buy is one third of current cash, capped by the
  `max_weight_bps` room and by available cash, and skipped below the
  minimum notional. Buys and sells use `max_slippage_bps` 200.
- All money decisions use integer arithmetic only. Fully
  deterministic: no clock, no randomness, no network, no files.

## Why this shape

In the frozen dev window (1,440 one-minute bars) BTC bled slowly from
about $71,409 to $67,901 (−4.9%); buy-and-hold loses about −4.9%.
The only large snap-back was a flush of about −0.88% in 5 minutes
around bar 402–406, recovering about +1.3% over the next ~60 minutes,
plus a smaller flush around bar 922 (−0.63% in 5 min, +0.6%
recovery). One-minute return std is about 0.08%, so a 0.80% five-minute
drop is a ~10-sigma event — a real flush, not noise. A full-size trade
buying the big flush and holding up to 240 bars scored about +$13.86
net in a simplified simulation, so the bot is built to catch exactly
that shape and ignore the slow bleed in between.

## How to run locally

```bash
python3 -P -s main.py
```

Feed it `hello`, then one `batch` JSON line per minute on stdin; it
answers each batch with exactly one `reply` JSON line on stdout and
exits after `done`. Diagnostics go to stderr only.

To grade against the frozen dev data with the competition kit:

```bash
node cli/makefun.mjs grade-local --kit . --bot <path-to-this-dir> --driver process
```

(run from inside the kit directory).

## Dev-window result

Measured with a local replay of the kit's frozen dev data (1,440
one-minute bars; the official `grade-local` could not run the Python
bot here — its process driver runs Node only and its Docker driver
needs a container runtime this VM cannot provide, so the numbers
below come from my own replay runner implementing the same protocol
and fill model: next-open fills, 4 bps fee, 2 bps slippage,
constant-product impact against open depth).

- **Net PnL: +$10.48** (final equity $1,010.48 on $1,000 start)
- **Trades: 1 round trip** — bought the bar 402–406 flush in 3
  tranches (fills at bars 407/408/409, average about $68,055) and
  sold at the take-profit (fill at bar 468, about $69,124)
- **Max drawdown: 0.15%**, far under the 25% bar
- **Determinism:** two runs produce byte-identical replies
- Buy-and-hold on the same window loses −4.91%

Tuning notes: the first version (0.80%/0.80%/2.00% triggers,
+1.20% take-profit, unarmed 0.50% trail) lost −$2.17 across 4
bleed trades. Tightening the 15-minute/60-minute triggers to
1.40%/2.80%, arming the trail only above +0.40%, and raising the
take-profit to +1.50% left exactly the one real flush trade.
Loosening the 5-minute trigger to 0.70% was tried and reverted: it
added a bleed trade at bar 248 that stopped out for about −$9.40
and cut the net to +$1.07.
