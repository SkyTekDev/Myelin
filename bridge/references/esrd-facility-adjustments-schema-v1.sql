CREATE TABLE esrd_facility_adjustments (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    algorithm TEXT NOT NULL CHECK(algorithm='CMS_FACILITY_LEVEL_V1'),
    rural_multiplier_exact TEXT NOT NULL,
    rural_transition_multiplier_exact TEXT NOT NULL,
    low_volume_tier1_multiplier_exact TEXT NOT NULL,
    low_volume_tier2_multiplier_exact TEXT NOT NULL,
    non_contiguous_non_labor_factors_json TEXT NOT NULL,
    quality_reduction_factors_json TEXT NOT NULL
);
CREATE TRIGGER esrd_facility_adjustments_insert BEFORE INSERT ON esrd_facility_adjustments
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_facility_adjustments_update BEFORE UPDATE ON esrd_facility_adjustments
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_facility_adjustments_delete BEFORE DELETE ON esrd_facility_adjustments
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
