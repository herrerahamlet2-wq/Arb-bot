# arb-bot

Scans Hard Rock Bet, Kalshi, Novig and ProphetX every 15 minutes (about noon to midnight ET)
and sends Telegram alerts for:

- 💰 **Arbs**: guaranteed profit across two books
- 📈 **Value bets**: Hard Rock priced better than the exchanges' fair odds
- 🔒 **Hedges**: when one of your logged bets can lock in profit

## Telegram commands
```
/check Lazio over 3 +112                             before you bet: true odds, edge, GOOD/BAD
/bet Texas -115 100                                  log a moneyline (team, odds, $ risked)
/bet Texas over 47.5 -110 50                         game total
/bet Dak Prescott over 219.5 pass Cowboys -110 50    player prop
/bets                                                open bets + live hedge numbers
/won 2 · /lost 2 · /push 2                           grade a bet (moneylines/totals auto-grade)
/result 2 +45.50                                     what you actually made after a hedge
/stats                                               record, profit, ROI, closing-line value
/del 2                                               delete a mistaken bet
/scan                                                scan now
```

## Settings
- Bankroll and markets: `.github/workflows/live.yml`
- Books, sports, fees: top of `arb_bot.py`
- Keys live in **Settings → Secrets and variables → Actions**: `ODDS_API_KEY`, `TELEGRAM_TOKEN`
