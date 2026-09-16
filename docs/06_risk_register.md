# V2 Risk Register

## P0
- Concurrent runs can create conflicting SCD2 current versions without database transaction/locking design.
- Checkpoint corruption or ambiguity could cause missed or duplicated processing.
- Late/out-of-order events can break simple temporal append logic.

## P1
- Local filesystem must not remain authoritative live state.
- Worker restart/recovery must be tested.
- Real-time source credentials must never leak to the UI.
- Free hosted infrastructure is not an uptime guarantee.

## P2
- Polling latency/load tradeoff.
- Micro-batch sizing.
- Live UI update frequency.
- Remote artifact strategy.
- Multi-user/RBAC expansion.

## Explicit non-claims
Do not claim:
- exactly-once semantics unless proven,
- zero false positives,
- all bad data can be detected,
- AI can determine truth,
- production-grade scale beyond measured limits.
