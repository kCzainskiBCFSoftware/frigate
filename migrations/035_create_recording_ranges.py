"""Peewee migrations -- 035_create_recording_ranges.py.

Adds the precomputed playback-timeline tables:
- recording_ranges: contiguous runs of recording coverage, merged from the
  ~8,640 segment rows a camera writes per day down to ~40 rows.
- recording_range_coverage: the window each (camera, variant) has actually been
  rolled up for, so the API can tell "no footage" from "not precomputed yet"
  and fall back to a live merge for anything outside it.

Both start empty. RecordingCleanup fills them on its existing 60s tick and
backfills history an hour at a time, so no data migration is needed here and
the endpoints are correct (just slower) until the backfill catches up.
"""


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql("""
        CREATE TABLE IF NOT EXISTS recording_ranges (
            id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
            camera VARCHAR(20) NOT NULL,
            variant VARCHAR(20) NOT NULL DEFAULT "main",
            start_time DATETIME NOT NULL,
            end_time DATETIME NOT NULL
        )
        """)
    migrator.sql(
        'CREATE INDEX IF NOT EXISTS "recording_ranges_camera_variant_start_time_end_time"'
        ' ON "recording_ranges" ("camera", "variant", "start_time", "end_time")'
    )
    migrator.sql("""
        CREATE TABLE IF NOT EXISTS recording_range_coverage (
            camera VARCHAR(20) NOT NULL,
            variant VARCHAR(20) NOT NULL,
            covered_from DATETIME NOT NULL,
            covered_to DATETIME NOT NULL,
            PRIMARY KEY (camera, variant)
        )
        """)


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql("DROP TABLE IF EXISTS recording_range_coverage")
    migrator.sql(
        'DROP INDEX IF EXISTS "recording_ranges_camera_variant_start_time_end_time"'
    )
    migrator.sql("DROP TABLE IF EXISTS recording_ranges")
