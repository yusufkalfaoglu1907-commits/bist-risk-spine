"""Risk / scenario tooling — the non-alpha use of the exposure tensor and linkage graph.

After the three-pillar cross-sectional alpha search was concluded NO-GO (ADR-0006), the durable
deliverable is an honest research substrate **and a risk spine**. This package is that spine made
into a first-class tool: define a channel-shock scenario, re-price the current exposure tensor
against it (reusing the §240 ``events.channel_stress`` engine), and report worst/best-exposed
names + portfolio stress P&L — with full coverage honesty and no fabricated numbers (§4).

This is **re-pricing, not prediction**: there is no Sharpe, no promotion gate, no ``signal_registry``
write. A scenario answers "if these channels move by this much, what happens to the book," not "will
they." The two honest scenario sources are (a) a small **stylized** library of named hypotheticals
(clearly tagged, never mistaken for fitted truth) and (b) an **empirical** vector derived from real
realized factor returns over a historical window (unit-correct by construction).
"""
