# Setup Engine

Implemented setup families:

- Opening Range Break and Retest
- VWAP Trend Pullback
- PDH/PDL Break and Retest
- Overnight High/Low Break and Retest
- Failed Breakout Reversal
- Liquidity Sweep and Reclaim

Each setup returns:

- required checklist items
- status: `valid`, `waiting`, or `invalid`
- confidence score
- timeframe
- Aussagekraft
- measured value
- wait condition
- invalidation condition
- entry zone, trigger entry, structural stop, target 1, target 2 when inferable

The setup engine uses closed bars and calculated levels. Open bars may be displayed by the UI, but they are not used as confirmed BOS/retest evidence.
