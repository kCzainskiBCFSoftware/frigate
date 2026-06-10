"""Peewee migrations -- 033_add_recordings_variant.py.

Adds dual-stream recording support:
- variant column (defaults "main"; "sub" for the secondary stream)
- codec_name, width, height, bitrate media metadata
- transcoded_from_main flag (reserved for on-demand transcoding fallback)
- composite index (camera, variant, start_time DESC, end_time DESC)

Existing rows are backfilled with variant="main" via the column default.
"""

import peewee as pw

SQL = pw.SQL


def migrate(migrator, database, fake=False, **kwargs):
    migrator.sql(
        'ALTER TABLE recordings ADD COLUMN variant VARCHAR(20) NOT NULL DEFAULT "main"'
    )
    migrator.sql("ALTER TABLE recordings ADD COLUMN codec_name VARCHAR(50) NULL")
    migrator.sql("ALTER TABLE recordings ADD COLUMN width INTEGER NULL")
    migrator.sql("ALTER TABLE recordings ADD COLUMN height INTEGER NULL")
    migrator.sql("ALTER TABLE recordings ADD COLUMN bitrate INTEGER NULL")
    migrator.sql(
        "ALTER TABLE recordings ADD COLUMN transcoded_from_main INTEGER NOT NULL DEFAULT 0"
    )
    migrator.sql(
        'CREATE INDEX "recordings_variant" ON "recordings" ("variant")'
    )
    migrator.sql(
        'CREATE INDEX "recordings_camera_variant_start_time_end_time" ON "recordings" ("camera", "variant", "start_time" DESC, "end_time" DESC)'
    )


def rollback(migrator, database, fake=False, **kwargs):
    migrator.sql('DROP INDEX IF EXISTS "recordings_camera_variant_start_time_end_time"')
    migrator.sql('DROP INDEX IF EXISTS "recordings_variant"')
    migrator.sql("ALTER TABLE recordings DROP COLUMN transcoded_from_main")
    migrator.sql("ALTER TABLE recordings DROP COLUMN bitrate")
    migrator.sql("ALTER TABLE recordings DROP COLUMN height")
    migrator.sql("ALTER TABLE recordings DROP COLUMN width")
    migrator.sql("ALTER TABLE recordings DROP COLUMN codec_name")
    migrator.sql("ALTER TABLE recordings DROP COLUMN variant")
