CREATE TABLE esrd_rate_releases (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(catalog_sha256)=64),
    schema_version TEXT NOT NULL CHECK(schema_version='ESRDReferenceRates/1'),
    payment_year INTEGER NOT NULL CHECK(payment_year BETWEEN 2000 AND 2100),
    revision INTEGER NOT NULL CHECK(revision>0),
    effective_from TEXT NOT NULL,
    effective_through TEXT NOT NULL CHECK(effective_through>=effective_from),
    reference_version TEXT NOT NULL,
    algorithm TEXT NOT NULL CHECK(algorithm='BASE_WAGE_ADULT_AGE_V1'),
    sources_json TEXT NOT NULL,
    policy_sha256 TEXT NOT NULL CHECK(length(policy_sha256)=64),
    rate_count INTEGER NOT NULL CHECK(rate_count>0),
    loaded_at_utc TEXT NOT NULL,
    loaded_by TEXT NOT NULL,
    sealed INTEGER NOT NULL CHECK(sealed IN (0,1)),
    UNIQUE(payment_year,revision)
);
CREATE TABLE esrd_base_rates (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    base_rate_exact TEXT NOT NULL,
    labor_share_exact TEXT NOT NULL,
    non_labor_share_exact TEXT NOT NULL,
    wage_floor_exact TEXT NOT NULL,
    wage_cap_multiplier_exact TEXT NOT NULL
);
CREATE TABLE esrd_age_factors (
    catalog_sha256 TEXT NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    minimum_age INTEGER NOT NULL,
    maximum_age INTEGER NOT NULL CHECK(maximum_age>=minimum_age),
    factor_exact TEXT NOT NULL,
    PRIMARY KEY(catalog_sha256,minimum_age)
);
CREATE TABLE esrd_wage_indexes (
    catalog_sha256 TEXT NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    cbsa TEXT NOT NULL,
    wage_index_exact TEXT NOT NULL,
    PRIMARY KEY(catalog_sha256,cbsa)
);
CREATE TRIGGER esrd_release_update BEFORE UPDATE ON esrd_rate_releases
WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_release_delete BEFORE DELETE ON esrd_rate_releases
WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_base_insert BEFORE INSERT ON esrd_base_rates
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_base_update BEFORE UPDATE ON esrd_base_rates
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_base_delete BEFORE DELETE ON esrd_base_rates
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_age_insert BEFORE INSERT ON esrd_age_factors
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_age_update BEFORE UPDATE ON esrd_age_factors
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_age_delete BEFORE DELETE ON esrd_age_factors
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_wage_insert BEFORE INSERT ON esrd_wage_indexes
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_wage_update BEFORE UPDATE ON esrd_wage_indexes
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_wage_delete BEFORE DELETE ON esrd_wage_indexes
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
