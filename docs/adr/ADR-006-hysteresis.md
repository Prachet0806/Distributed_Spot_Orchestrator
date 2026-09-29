# ADR-006 — Hysteresis values

Status: accepted. Date: 2026-09-07.

Context: arbitrage without damping thrashes on price jitter.
Decision: margin 10%, cooldown 900s, 3 favorable observations,
20% completion-overhead as hard ceiling (not target). All versioned
policy config, not constants. Emergency bypasses hysteresis.
Consequences: `policy_engine.ArbitragePolicyConfig` defaults change
(3h→15m, 2→3 obs); economics must emit predicted overhead for the gate.
