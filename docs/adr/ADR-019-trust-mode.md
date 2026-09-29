# ADR-019 — Emulated trust mode

Status: accepted. Date: 2026-09-07.

Context: lab velocity needs relaxed SSH/sudo/flag trust; AWS must not inherit it.
Decision: AutoAddPolicy, passwordless sudo, unauthenticated flag file allowed
only behind explicit EMU_TRUST_MODE=true with separate EmulatedWorkerTransport
vs AWSWorkerTransport sharing the upper interface. AWS requires host-key
verification, IAM/IMDS identity, authorized channel.
Consequences: transports split now; CI asserts AWS path never enables the flag.
