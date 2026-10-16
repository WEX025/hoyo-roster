"""Compatibility entrypoint for the packaged offline analyzer."""

from hoyo_roster.collection_analysis import (
    main as main,
    read_run as read_run,
    compare_runs as compare_runs,
    union_ms as union_ms,
)

if __name__ == "__main__":
    raise SystemExit(main())
