"""Peewee migrations -- 034_drop_redundant_recordings_indexes.py.

Drops two counterproductive single-column indexes on recordings:
- recordings_variant: only two distinct values ("main"/"sub"); without fresh
  planner statistics SQLite can pick it for variant-filtered playback queries
  and scan roughly half the table via random row lookups.
- recordings_camera: redundant — every camera lookup is served by the
  composite indexes that lead with camera.

Both indexes are pure write amplification on the segment-insert hot path
(one row per camera per variant every segment_time seconds).
"""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP INDEX IF EXISTS "recordings_variant"')
    migrator.sql('DROP INDEX IF EXISTS "recordings_camera"')


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recordings_variant" ON "recordings" ("variant")'
    )
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recordings_camera" ON "recordings" ("camera")'
    )
