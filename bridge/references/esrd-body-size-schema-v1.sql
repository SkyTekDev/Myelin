CREATE TABLE esrd_body_size_parameters (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    algorithm TEXT NOT NULL CHECK(algorithm='CMS_BSA_BMI_9_DIGIT_V1'),
    source_url TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
    bsa_national_average_exact TEXT NOT NULL,
    bsa_multiplier_exact TEXT NOT NULL,
    low_bmi_threshold_exact TEXT NOT NULL,
    low_bmi_multiplier_exact TEXT NOT NULL,
    maximum_height_cm_exact TEXT NOT NULL,
    maximum_weight_kg_exact TEXT NOT NULL,
    maximum_bmi_exact TEXT NOT NULL
);
CREATE TRIGGER esrd_body_size_insert BEFORE INSERT ON esrd_body_size_parameters
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_body_size_update BEFORE UPDATE ON esrd_body_size_parameters
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_body_size_delete BEFORE DELETE ON esrd_body_size_parameters
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
