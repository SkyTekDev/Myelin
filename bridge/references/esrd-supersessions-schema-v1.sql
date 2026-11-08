CREATE TABLE esrd_release_supersessions (
    replacement_catalog_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    superseded_catalog_sha256 TEXT NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    approved_at_utc TEXT NOT NULL,
    correction_reason TEXT NOT NULL CHECK(length(trim(correction_reason)) BETWEEN 1 AND 500),
    correction_source_url TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    CHECK(replacement_catalog_sha256<>superseded_catalog_sha256)
);
CREATE TRIGGER esrd_supersession_update BEFORE UPDATE ON esrd_release_supersessions
BEGIN SELECT RAISE(ABORT,'ESRD supersession records are immutable'); END;
CREATE TRIGGER esrd_supersession_delete BEFORE DELETE ON esrd_release_supersessions
BEGIN SELECT RAISE(ABORT,'ESRD supersession records are immutable'); END;
