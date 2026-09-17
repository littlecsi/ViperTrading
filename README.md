# AutoTrading
Bitocin Automated Trading

## Viper

Viper is an automated crypto trading bot for Binance USDⓈ-M Futures, running a zone-scaling strategy
that sizes positions based on price proximity to operator-defined support/resistance zones.

- **Zone-scaling position sizing** — scales into and out of positions automatically as price moves
  through an ordered ladder of support/resistance zones, with no separate scale-out logic needed.
- **Live settings reload** — rereads its config every tick, so trend, leverage, and the zone ladder can
  be changed without restarting the bot.
- **Minimal-footprint market data** — one REST call per exchange endpoint per tick, staying well under
  Binance's rate limits even at fast poll intervals.
- **Clock drift correction** — measures and corrects for local/server clock offset at startup to avoid
  signed-request timestamp errors.
- **Automatic risk halt** — flattens the position and pauses when price breaks out of the configured
  zone ladder.
- **Telegram notifications** — pushes alerts for executed orders, halts, and refused/reloaded settings
  changes, without ever affecting trading logic.
- **JSONL audit trail** — records a tick-by-tick decision journal and a full order journal for
  after-the-fact analysis and future RL/PPO training.
- **Live and testnet modes** — switches between separate live and testnet Binance credentials via a
  config flag.
