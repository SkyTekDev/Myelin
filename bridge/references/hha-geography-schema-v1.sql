CREATE TABLE hha_geography_releases (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL CHECK(length(catalog_sha256)=64 AND catalog_sha256 NOT GLOB '*[^0-9a-f]*'),
    schema_version TEXT NOT NULL CHECK(schema_version='HHAGeographyCatalog/1'),
    payment_year INTEGER NOT NULL CHECK(payment_year BETWEEN 2000 AND 2100),
    revision INTEGER NOT NULL CHECK(revision>0),
    effective_from TEXT NOT NULL,
    effective_through TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    county_count INTEGER NOT NULL CHECK(county_count>0),
    verified_count INTEGER NOT NULL CHECK(verified_count BETWEEN 0 AND county_count),
    loaded_at_utc TEXT NOT NULL,
    loaded_by TEXT NOT NULL,
    sealed INTEGER NOT NULL CHECK(sealed IN (0,1)),
    UNIQUE(payment_year,revision)
);
CREATE TABLE hha_geography_counties (
    catalog_sha256 TEXT NOT NULL REFERENCES hha_geography_releases(catalog_sha256),
    county_fips TEXT NOT NULL CHECK(length(county_fips)=5 AND county_fips NOT GLOB '*[^0-9]*'),
    county_name TEXT NOT NULL,
    state_fips TEXT NOT NULL CHECK(state_fips=substr(county_fips,1,2)),
    state_abbreviation TEXT NOT NULL,
    mapping_status TEXT NOT NULL CHECK(mapping_status IN ('Verified','SourceConflict')),
    base_wage_index_code TEXT NOT NULL,
    cms_wage_index_code TEXT NOT NULL,
    wage_index_exact TEXT NOT NULL,
    county_source_row INTEGER NOT NULL CHECK(county_source_row>0),
    census_source_row INTEGER CHECK(census_source_row>0),
    hh_urban_source_row INTEGER CHECK(hh_urban_source_row>0),
    transition_source_row INTEGER CHECK(transition_source_row>0),
    wage_source_row INTEGER NOT NULL CHECK(wage_source_row>0),
    entry_sha256 TEXT NOT NULL CHECK(length(entry_sha256)=64 AND entry_sha256 NOT GLOB '*[^0-9a-f]*'),
    PRIMARY KEY(catalog_sha256,county_fips)
);
CREATE TRIGGER hha_geography_release_update BEFORE UPDATE ON hha_geography_releases
WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed HH geography releases are immutable'); END;
CREATE TRIGGER hha_geography_release_delete BEFORE DELETE ON hha_geography_releases
WHEN OLD.sealed=1 BEGIN SELECT RAISE(ABORT,'Sealed HH geography releases are immutable'); END;
CREATE TRIGGER hha_geography_county_insert BEFORE INSERT ON hha_geography_counties
WHEN (SELECT sealed FROM hha_geography_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed HH geography counties are immutable'); END;
CREATE TRIGGER hha_geography_county_update BEFORE UPDATE ON hha_geography_counties
WHEN (SELECT sealed FROM hha_geography_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM hha_geography_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed HH geography counties are immutable'); END;
CREATE TRIGGER hha_geography_county_delete BEFORE DELETE ON hha_geography_counties
WHEN (SELECT sealed FROM hha_geography_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed HH geography counties are immutable'); END;
