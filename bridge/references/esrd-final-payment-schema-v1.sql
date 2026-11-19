CREATE TABLE esrd_final_payment (
    catalog_sha256 TEXT PRIMARY KEY NOT NULL REFERENCES esrd_rate_releases(catalog_sha256),
    algorithm TEXT NOT NULL CHECK(algorithm='CMS_FINAL_PAYMENT_V1'),
    network_reduction_amount_exact TEXT NOT NULL,
    post_tdapa_add_ons_json TEXT NOT NULL
);
CREATE TRIGGER esrd_final_payment_insert BEFORE INSERT ON esrd_final_payment
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_final_payment_update BEFORE UPDATE ON esrd_final_payment
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
OR (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=NEW.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
CREATE TRIGGER esrd_final_payment_delete BEFORE DELETE ON esrd_final_payment
WHEN (SELECT sealed FROM esrd_rate_releases WHERE catalog_sha256=OLD.catalog_sha256)=1
BEGIN SELECT RAISE(ABORT,'Sealed ESRD references are immutable'); END;
