# AGENTS.md — EFHC Bot Canon v2.8 (root)

## Source of truth
- Repo scaffold and naming must follow EFHC-Bot canonical tree v2.8 1:1.
- Never invent new folders/files/names.
- If not present in canonical tree — do not create.

## Economic invariants
- GEN_PER_SEC_BASE_KWH and GEN_PER_SEC_VIP_KWH only (per-sec only).
- All EFHC/kWh amounts: Decimal(30,8), rounding DOWN.
- kWh -> EFHC only (1:1). No EFHC -> kWh.
- No P2P user->user transfers.
- All monetary ops only via transactions_service.py and efhc_transfers_log with Idempotency-Key.

## Style
- No TODOs, no fake logic. If logic not requested — leave minimal skeleton.
