from pathlib import Path

from myelin.pricers.ipsf import IPSFDatabase


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "myelin.db"


def main():
    print(f"Loading IPSF into: {DB_PATH}")

    with IPSFDatabase(
        db_path=str(DB_PATH),
        db_backend="sqlite",
    ) as database:
        count = database.populate(
            download=True,
            truncate=True,
        )

        print(f"Loaded {count:,} IPSF records.")


if __name__ == "__main__":
    main()