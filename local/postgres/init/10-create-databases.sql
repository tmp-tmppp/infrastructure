-- One database and one login role per service (DM-01) on the shared development server. market-maker has no database;
-- Keycloak keeps its own.
--
-- The image runs this file once, when the data volume is empty. It creates only what is missing, so it can be run
-- again, for example after adding a service name to the list below, without deleting the data:
--   docker compose exec postgres psql -U postgres -f /docker-entrypoint-initdb.d/10-create-databases.sql
--
-- The role and its database have the same name (market-data uses market_data), and the role's password is that name
-- too: local development values only, as in the compose files of the services. A role can connect to its own database
-- only (the public CONNECT right is revoked), as in the cluster, where each service has its own login.

CREATE TEMPORARY TABLE service_names AS
SELECT unnest(ARRAY['tenants', 'payments', 'audit', 'markets', 'trading', 'market_data', 'keycloak']) AS name;

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', s.name, s.name) -- hooks:allow local development only
FROM service_names s
WHERE NOT EXISTS (SELECT FROM pg_roles r WHERE r.rolname = s.name)
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', s.name, s.name)
FROM service_names s
WHERE NOT EXISTS (SELECT FROM pg_database d WHERE d.datname = s.name)
\gexec

SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', s.name)
FROM service_names s
\gexec
