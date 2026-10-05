"""Current acceptance entrypoint; previous suite is preserved in the backup."""
from regression_original import main
if __name__ == "__main__":
    raise SystemExit(main())
