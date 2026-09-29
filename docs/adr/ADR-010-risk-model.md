# ADR-010 — Phase-6 risk model

Status: accepted. Date: 2026-09-07.

Context: learned risk models would become a critical dependency prematurely.
Decision: risk = historical interruption frequency + provisioning failure
rate + restore failure rate, fed by Migration History. Thin data →
LOW confidence + conservative default. No Gaussian/Bayes/ML in V2
(statistical CI in V2.1, learned model in V3).
Consequences: Risk estimator stays simple; history writer must record the
three actuals distinctly from estimates.
