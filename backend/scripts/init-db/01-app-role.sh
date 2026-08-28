#!/usr/bin/env bash
#
# Create the role the application connects as, and the extension §10 needs.
#
# The role is the important half, and it is the one that is easy to skip.
# PostgreSQL ignores every row-level security policy for a superuser or for any
# role with BYPASSRLS -- silently, with the policies still listed in
# `pg_policies` and `FORCE ROW LEVEL SECURITY` still set on every table. A
# deployment that connects as the owner has tenant isolation that has never once
# worked, and nothing about it looks wrong.
#
# So: two roles. The owner runs migrations, because creating tables, enabling
# RLS and installing an extension all need privileges the application must not
# have. The application connects as `documind_app`, which cannot bypass a
# policy even by accident.
#
# Runs once, on an empty data directory, via the postgres image's
# docker-entrypoint-initdb.d hook.
set -euo pipefail

APP_PASSWORD="${APP_DB_PASSWORD:-documind_app_local}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE EXTENSION IF NOT EXISTS vector;

    DO \$\$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'documind_app') THEN
            CREATE ROLE documind_app
                LOGIN PASSWORD '${APP_PASSWORD}'
                NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
        END IF;
    END
    \$\$;

    GRANT USAGE ON SCHEMA public TO documind_app;

    -- Data privileges only. No CREATE, no DDL: the application never migrates
    -- itself, and a role that cannot alter a table cannot drop a policy either.
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO documind_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO documind_app;

    -- Migrations run after this script, so the grants above cover nothing yet.
    -- These make every table a future migration creates inherit them, which is
    -- what stops "we added a table and the app got permission denied" from
    -- being a deploy-day discovery.
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO documind_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public
        GRANT USAGE, SELECT ON SEQUENCES TO documind_app;
SQL

echo "documind_app created: NOSUPERUSER NOBYPASSRLS (row-level security applies to it)"
