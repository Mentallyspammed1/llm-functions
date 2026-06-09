#!/bin/bash
# fs_sql - simple SQLite wrapper
# Usage: fs_sql <database_file> <SQL_query_or_file>
# If only one argument is given, it is treated as the database file and the query
# will be read from stdin.

DB="${1:-$HOME/.default.db}"
shift

if [[ $# -eq 0 ]]; then
    # Read SQL from stdin
    sqlite3 "$DB"
else
    # Execute the provided query
    sqlite3 "$DB" "$@"
fi
