"""Live market-data feed integration: ticks -> 1-minute bars -> master (deduped) ->
S/R -> execution-aware Teams alerts.

All network/credential I/O is isolated in ``feed_proc`` (the ``--feed l1`` subprocess) and
``auth``; the rest of the package is pure/offline and unit-tested. The desktop app spawns
the feed subprocess + the alert monitor only from ``desktop.main`` (never in tests/selftest),
so importing this package never opens a connection.
"""
