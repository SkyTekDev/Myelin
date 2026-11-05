CREATE TABLE hha_rate_releases (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(catalog_sha256)=64 AND catalog_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version TEXT NOT NULL CHECK(schema_version='HHASeparatePaymentRates/1'),
    payment_year INTEGER NOT NULL CHECK(payment_year BETWEEN 2000 AND 2100),
    revision INTEGER NOT NULL CHECK(revision>0),
    effective_from TEXT NOT NULL,
    effective_through TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    rate_count INTEGER NOT NULL CHECK(rate_count>0),
    loaded_at_utc TEXT NOT NULL,
    loaded_by TEXT NOT NULL,
    sealed INTEGER NOT NULL CHECK(sealed IN (0,1)),
    UNIQUE(payment_year,revision)
);
CREATE TABLE hha_payment_rates (
    catalog_sha256 TEXT NOT NULL REFERENCES hha_rate_releases(catalog_sha256),
    hcpcs_code TEXT NOT NULL,
    unit_rate_exact TEXT NOT NULL,
    coinsurance_rate_exact TEXT NOT NULL,
    annual_part_b_deductible_exact TEXT NOT NULL,
    entry_sha256 TEXT NOT NULL CHECK(length(entry_sha256)=64 AND entry_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(catalog_sha256,hcpcs_code)
);
CREATE TRIGGER hha_rate_release_update BEFORE UPDATE ON hha_rate_releases
WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed HH rate releases are immutable'); END;
CREATE TRIGGER hha_rate_release_delete BEFORE DELETE ON hha_rate_releases
WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed HH rate releases are immutable'); END;
CREATE TRIGGER hha_rate_insert BEFORE INSERT ON hha_payment_rates
WHEN (SELECT sealed FROM hha_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed HH payment rates are immutable'); END;
CREATE TRIGGER hha_rate_update BEFORE UPDATE ON hha_payment_rates
WHEN (SELECT sealed FROM hha_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM hha_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed HH payment rates are immutable'); END;
CREATE TRIGGER hha_rate_delete BEFORE DELETE ON hha_payment_rates
WHEN (SELECT sealed FROM hha_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed HH payment rates are immutable'); END;
