# Design Fidelity Ledger

Reference concepts:

- `docs/design/flowdesk-data-planner-concept.png`
- `docs/design/flowdesk-data-planner-mobile-concept.png`
- `docs/design/flowdesk-backtest-plan-concept.png`

Implementation captures:

- `docs/screenshots/data-planner-desktop.png`
- `docs/screenshots/data-planner-mobile.png`
- `docs/screenshots/backtest-plan.png`
- `docs/screenshots/purchase-confirmation.png`
- `docs/screenshots/practice-plan.png`
- `docs/screenshots/settings-unlocked.png`
- `docs/screenshots/data-planner-1630.png`
- `docs/screenshots/data-health-active-session.png`
- `docs/screenshots/setup-rule-groups.png`
- `docs/screenshots/orderflow-deduplicated-candidates.png`

## Matched Decisions

1. The existing Flowdesk sidebar, top service strip, graphite surfaces, thin separators, cyan interaction, green validation, amber caution, and red failure semantics are retained.
2. Data Planner keeps the five-step acquisition sequence, compact request controls, exact Berlin/UTC preview, three-column mode comparison, limits, cost ledger, and Session Library in the first working screen.
3. Full L3, Economy, and Chart Context remain visually comparable while unsupported capabilities are explicit instead of silently degraded.
4. Purchase review is a focused modal with instrument, UTC scope, local replay, records, size, estimated/max cost, remaining budget, acknowledgement, and exact phrase.
5. Backtest Plan preserves the Practice/Pilot/Locked segmentation, phase progress, deterministic hash, session split, candidate scan, audit, and conservative performance block.
6. Locked state is visible at every relevant layer: selected segment, hash badge, locked split, disabled configuration controls, future-seek text, and audit entries.
7. Mobile keeps the same hierarchy in a single-column flow; the active nav item auto-centers and the document has no horizontal page overflow.
8. The repaired plan screen follows the existing compact Flowdesk concept but leads with one active plan, one current-run state, explicit assignment actions, and a separately disclosed archive.
9. Planner time controls and their Berlin/UTC preview occupy one continuous band, making the exact `15:00-16:30` request inspectable before metadata is requested.
10. Data Health preserves the registry/detail split while adding an explicit active-versus-inspected identity and a file-bound verification banner.
11. Setup evidence uses three non-overlapping groups with semantic green, cyan, and amber treatment; blockers are never labeled as passed.
12. Orderflow retains the dense terminal presentation while candidate score components are subordinate details and the default list remains bounded.

## Intentional Differences

- The concept used hypothetical multi-gigabyte numbers and several sessions. The implementation displays only real Databento metadata and the two genuinely validated local sessions.
- The concept placed a permanent estimate inspector in a right rail. The implementation uses a three-panel inspector below the comparison so the existing Flowdesk header and narrower desktop layouts remain usable.
- The concept contained assumed candidate outcomes. The implementation shows deterministic engine decisions only and labels the scan as discovery/QA with no profitability claim.
- The original concepts did not include the repair-specific archive, active-session inspection, or candidate-component states. These use native Flowdesk panels rather than introducing a separate visual language.

## Above-The-Fold Copy

Concept: `DATA PLANNER`, `SESSION / COMPARE / ESTIMATE / CONFIRM / VALIDATE`.

Implementation: `Data Planner`, `Scope / Time / Compare / Authorize / Validate`, plus `Estimate first / Explicit authorization / Atomic import`. The implementation wording is more explicit about cost safety while preserving the same five-stage meaning.

## QA Method

The accepted Backtest and Data Planner concept PNGs were inspected at original resolution with the local image viewer before capture. The implementation was exercised in the Codex in-app Browser against `http://localhost:3001`, using a `1440x1000` desktop viewport and a `390x844` mobile viewport. The mobile document measured `375px` client and scroll width, the browser console contained no warnings or errors, and every saved repair screenshot was reopened with the local image viewer before acceptance.
