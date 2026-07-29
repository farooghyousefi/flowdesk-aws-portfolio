# Blind Replay

Practice allows seek and editable settings; its outcomes are not strategy proof. Pilot blocks future seek while allowing audited changes between sessions. Locked Backtest hashes setup version, orderflow settings, risk settings, fill assumptions, instrument, session IDs, and source hashes.

Locked mode disables future seek and settings changes at both UI and API layers only after its blind replay run has started. Merely selecting or storing a Locked plan does not create a global lock. At a `trade_ready` point, direction, entry, stop, and target must be committed before continuation. The trade stores decision, risk, and feature snapshots. On close, gross movement, fees, slippage, net result, result R, MAE, MFE, and holding time are recorded. Closed Locked trades are immutable; corrections require a new audit event.

No blind mode sends an order or controls a broker.
