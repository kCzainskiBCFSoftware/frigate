"""Peewee migrations -- 036_reset_recording_ranges.py.

Discards the precomputed playback timeline so it is rebuilt from scratch.

The rollup that shipped in 035 deleted whole unclipped ranges but regenerated
only the part inside its own window, so it ate the table from the left on every
tick. Because the coverage row still claimed the full span, the live fallback
never ran and the endpoints under-reported -- a device with 12 h of continuous
recording answered with 3.5 h.

The damaged rows sit *between* covered_from and covered_to, which neither the
incremental rollup (which only advances the right edge) nor the backfill (which
only extends the left edge) ever revisits, so fixing the rollup does not heal
them. Nothing is lost by discarding: both tables are a cache over `recordings`,
and an uncovered window is merged live. Expect correct-but-slower answers until
the rollup covers the present and the backfill walks history (~a day per hour).
"""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql("DELETE FROM recording_range_coverage")
    migrator.sql("DELETE FROM recording_ranges")


def rollback(migrator, database, fake=False, **kwargs):
    # nothing to restore: the tables are a rebuildable cache
    pass
