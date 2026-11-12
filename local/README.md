# Local development

How to run Chance on one machine while you work on one service: the shared dependencies (PostgreSQL 18, NATS with
JetStream, Keycloak with the realm `chance`, Mailpit and the mock payment provider) in containers from
[`compose.yaml`](compose.yaml), and the service you change natively with `npm start` against them. Several services
can run side by side; the table below gives each its own ports. Nothing here touches Azure, GitHub or the cluster.

The tools (Node.js 24, Docker with the compose plugin, kubectl, Helm, kubeconform, Azure CLI, Terraform, optionally
kind) are installed from the runbook below or by the playbook in [`../ansible/`](../ansible/README.md); the playbook is
the same list, written as code.

## What you need

| Tool | Version | Used for |
|---|---|---|
| Node.js and npm | 24 (the `engines` of every service and the Dockerfiles' `node:24`) | running and testing a service natively |
| Docker Engine and the compose plugin | 29 and 5 | the dependencies in `compose.yaml` |
| Helm, kubeconform | 4.3.1, 0.8.0 | only to check a chart (the checks of every service's pipeline) |
| kubectl, Azure CLI, Terraform | 1.35, 2.92, 1.17.1 | only for the cluster and `envs/prod`; not needed to run a service |
| kind | 0.33.0 | only for the optional local cluster at the end |

The repositories sit side by side in one folder (`identity/`, `payment-provider/`, `tenants/`, `infrastructure/`, ...):
`compose.yaml` mounts `../../identity/realm/chance-realm.json` and builds `../../payment-provider`.

### Install the tools by hand (Debian or Ubuntu; the playbook does the same)

```bash
# Node.js 24: the tarball of nodejs.org, checked against its SHASUMS256.txt, or your version manager (nvm, fnm, mise).
# Docker Engine and the compose plugin: https://docs.docker.com/engine/install/ubuntu/ (the apt repository of Docker;
#   add yourself to the docker group only if you accept that the group is root-equivalent).
# Helm 4.3.1:        https://get.helm.sh/helm-v4.3.1-linux-amd64.tar.gz  (+ .sha256sum)
# kubeconform 0.8.0: https://github.com/yannh/kubeconform/releases/tag/v0.8.0  (CHECKSUMS)
# kubectl 1.35:      https://dl.k8s.io/release/v1.35.10/bin/linux/amd64/kubectl  (+ .sha256)
# Azure CLI:         https://learn.microsoft.com/cli/azure/install-azure-cli-linux (apt repository of Microsoft)
# Terraform 1.17.1:  https://releases.hashicorp.com/terraform/1.17.1/  (SHA256SUMS)
node --version && npm --version && docker --version && docker compose version
```

## Start and stop the dependencies

```bash
cd infrastructure/local
docker compose up -d --wait                       # all five; the first start builds the provider and Keycloak builds itself (about a minute)
docker compose up -d --wait postgres nats         # only what the service you work on needs
docker compose ps                                 # every line "healthy"
docker compose down                               # stop, keep the data
```

| Service | Address on this machine | What it is |
|---|---|---|
| `postgres` | `localhost:5432` | PostgreSQL 18.6, user `postgres` (the superuser) and one database and login role per service: `tenants`, `payments`, `audit`, `markets`, `trading`, `market_data`, `keycloak` (the role has the database's name and its password is that name; the init script is [`postgres/init/10-create-databases.sql`](postgres/init/10-create-databases.sql)); `market-maker` has none |
| `nats` | `nats://localhost:4222`, monitoring `http://localhost:8222` | NATS 2.15.2 with JetStream on a volume, the version and flags of the `broker` repository |
| `keycloak` | `http://localhost:8081` (realm `chance`, admin console `/admin`), health `http://localhost:9001/health/ready` | Keycloak 26.8.2 in development mode with the realm of `identity`, imported on the first start |
| `mailpit` | `http://localhost:8025` | The mail catcher: the web interface shows every mail Keycloak sends |
| `payment-provider` | `http://localhost:8082` | The mock provider built from `../../payment-provider`; its API key and webhook secret are the local values below |

All ports are published on `127.0.0.1` only. The logins of the realm (`admin`, `platform-admin`) and the client secrets
are in `compose.yaml`, under `keycloak`, marked as local development values. Do not start this file together with the
compose file of a service (or `identity/`, `broker/`): both publish 5432, 4222, 8081 or 8082.

Quick checks, one line each:

```bash
curl -s http://localhost:8222/healthz                                          # {"status":"ok"}
curl -s http://localhost:8081/realms/chance/.well-known/openid-configuration | head -c 120
docker compose exec postgres psql -U postgres -c '\l'                          # the seven databases
```

## Run one service natively

The same steps for every service, in its own repository folder and its own terminal (the `export`s live in that
terminal only, so settings of one service never reach another):

```bash
cd ../../tenants                      # the service you work on
npm ci                                # once, and when package-lock.json changes
npm run build                         # once, and after a change of the code
# the settings of the service (next section), then
npm start
```

Each service runs its own migrations at start, against its own database. Tokens are always verified, also with
`APP_ENV=local`: the defaults of `AUTH_JWKS_URL`, `AUTH_ISSUER` and `AUTH_AUDIENCE` point to the Keycloak above
(`http://localhost:8081/realms/chance`, audience `chance`), so no `AUTH_` setting is needed. A service started alone
uses the default `PORT=8080` and `METRICS_PORT=9464`; when several run, use the table.

### Ports when several services run on one host

Every service defaults to `PORT=8080` and `METRICS_PORT=9464` (and trading to an internal API and a gRPC port), so a
second one on the same machine fails with "address in use". Use these, which avoid the ports of the dependencies above
and of the compose files of the services (8080 to 8092):

| Service | `PORT` | `METRICS_PORT` | Other ports | Database |
|---|---|---|---|---|
| tenants | 8101 | 9101 | | `tenants` |
| payments | 8102 | 9102 | | `payments` |
| audit | 8103 | 9103 | | `audit` |
| markets | 8104 | 9104 | | `markets` |
| trading | 8105 | 9105 | `INTERNAL_PORT` 8115, `GRPC_PORT` 8125 | `trading` |
| market-data | 8106 | 9106 | | `market_data` |
| market-maker | 8107 | 9107 | | none |

Fixed by the dependencies: 5432, 4222, 8222, 8081, 9001, 8025, 8082. The web app's dev server is on 5173 (fixed by the
realm). `METRICS_PORT` is read by tenants, payments, audit and payment-provider; markets, trading, market-data and
market-maker were to follow the same convention (agreed 2026-11-11) but their sources did not read it when this was
written, so it has no effect there and only `PORT` counts. The provider's webhooks go to payments on 8102 by default
(`PAYMENTS_WEBHOOK_URL` changes it).

### The settings of each service

Start with the block of the service you run. The blocks need only the dependencies each one names.

**tenants** (needs `postgres`, `nats`):

```bash
export APP_ENV=local PORT=8101 METRICS_PORT=9101
export DATABASE_URL=postgres://tenants:tenants@localhost:5432/tenants          # hooks:allow local development only
export NATS_URL=nats://localhost:4222
# With the real Keycloak (without KEYCLOAK_URL the service uses a fake identity provider):
export KEYCLOAK_URL=http://localhost:8081 KEYCLOAK_REALM=chance KEYCLOAK_CLIENT_ID=tenants
export KEYCLOAK_CLIENT_SECRET=tenants-local-secret                             # hooks:allow local development only
# With markets running (share links; without MARKETS_BASE_URL every share join answers 503):
export MARKETS_BASE_URL=http://localhost:8104 MARKETS_ADMISSION_CLIENT_ID=tenants-markets
export MARKETS_ADMISSION_CLIENT_SECRET=tenants-markets-local-secret            # hooks:allow local development only
```

**payments** (needs `postgres`, `nats`, `payment-provider`):

```bash
export APP_ENV=local PORT=8102 METRICS_PORT=9102
export DATABASE_URL=postgres://payments:payments@localhost:5432/payments       # hooks:allow local development only
export NATS_URL=nats://localhost:4222
export PROVIDER_BASE_URL=http://localhost:8082
export PROVIDER_API_KEY=provider-local-api-key                                 # hooks:allow local development only
export WEBHOOK_SECRET=provider-local-webhook-secret                            # hooks:allow local development only
export DEPOSIT_SUCCESS_URL=http://localhost:5173/deposits/success DEPOSIT_CANCEL_URL=http://localhost:5173/deposits/cancelled
# Without these three, the debit goes to an in-memory trading. With trading running (gRPC, trading's GRPC_PORT):
export TRADING_GRPC_URL=localhost:8125
export KEYCLOAK_TOKEN_URL=http://localhost:8081/realms/chance/protocol/openid-connect/token   # hooks:allow
export KEYCLOAK_CLIENT_SECRET=payments-local-secret                            # hooks:allow local development only
# To try payments alone, create tenant schemas without the tenants service: export LOCAL_TENANT_IDS=<uuid>,<uuid>
```

**audit** (needs `postgres`, `nats`):

```bash
export APP_ENV=local PORT=8103 METRICS_PORT=9103
export DATABASE_URL=postgres://audit:audit@localhost:5432/audit                # hooks:allow local development only
export NATS_URL=nats://localhost:4222
```

**markets** (needs `postgres`, `nats`):

```bash
export APP_ENV=local PORT=8104 METRICS_PORT=9104
export DATABASE_URL=postgres://markets:markets@localhost:5432/markets          # hooks:allow local development only
export NATS_URL=nats://localhost:4222
# Without TRADING_URL a fake funding client answers (FAKE_FUNDING_MODE=funded, insufficient or unknown).
# With trading running, markets calls its internal port with a service token of its Keycloak client:
export TRADING_URL=http://localhost:8115 KEYCLOAK_URL=http://localhost:8081 KEYCLOAK_REALM=chance
export MARKETS_CLIENT_SECRET=markets-local-secret                              # hooks:allow local development only
```

**trading** (needs `postgres`, `nats`):

```bash
export APP_ENV=local PORT=8105 METRICS_PORT=9105 INTERNAL_PORT=8115 GRPC_PORT=8125
export DATABASE_URL=postgres://trading:trading@localhost:5432/trading          # hooks:allow local development only
export NATS_URL=nats://localhost:4222
# Test credits for a tenant and user, granted once at start: export SEED_CREDITS=<tenantId>/<userId>=5000
```

**market-data** (needs `postgres`, `nats`):

```bash
export APP_ENV=local PORT=8106 METRICS_PORT=9106
export DATABASE_URL=postgres://market_data:market_data@localhost:5432/market_data   # hooks:allow local development only
export NATS_URL=nats://localhost:4222
```

**market-maker** (needs `nats`; no database):

```bash
export APP_ENV=local PORT=8107 METRICS_PORT=9107
export NATS_URL=nats://localhost:4222
# Without TRADING_URL the bot quotes an in-memory trading with one demo market. With trading running:
export TRADING_URL=http://localhost:8115 KEYCLOAK_URL=http://localhost:8081 KEYCLOAK_REALM=chance
export MARKET_MAKER_CLIENT_SECRET=market-maker-local-secret                    # hooks:allow local development only
```

### Several services

Start `tenants` first, then the others, each in its own terminal with its own block. The tenant schemas of markets,
trading, market-data, payments and audit are created when the `TenantCreated` event of the tenants service arrives over
NATS (a database that has no tenant yet answers `503 tenant_not_ready`), so create a tenant as the realm's
`platform-admin`. curl cannot fetch that token: no client of the realm allows a password login (direct access grants are
off, and `web` is a public client with the authorization code flow and PKCE). The token comes from the web app instead:
it forwards `/v1` and `/checkout` to `GATEWAY_URL` (default `http://localhost:8000`). There is no gateway on a laptop
(the `gateway` repository's `local/compose.yaml` starts echo backends for its own tests only), so point it at the
tenants service: run `GATEWAY_URL=http://localhost:8101 npm run dev` in `../../web`, open `http://localhost:5173`, log in
as `platform-admin` (the password is in `compose.yaml`, under `keycloak`; the first login asks only for a new password:
the realm imports the user with the address `PLATFORM_ADMIN_EMAIL` of `compose.yaml`, already verified, and a name) and create the tenant on the page
`/platform/tenants`; the page shows the first admin's invite code once. To call the API by hand, copy the bearer token
of one of those requests from the browser's network panel. To try a different single service, point `GATEWAY_URL` at it
instead.

To run all of them and check the main flows end to end without a browser, use [`e2e/`](e2e/README.md)
(`bash e2e/run.sh all`; it starts and removes this stack itself, so stop yours first).

## Reset the data

```bash
docker compose down -v                  # everything: databases, streams, the imported realm, the mails
docker compose up -d --wait             # and start again

# One database only (say tenants): drop it and let the init script create it again. The service migrates it at start.
docker compose exec postgres psql -U postgres -c 'DROP DATABASE tenants WITH (FORCE)'
docker compose exec postgres psql -U postgres -f /docker-entrypoint-initdb.d/10-create-databases.sql

# The streams only:
docker compose rm -sf nats && docker volume rm chance-local_nats-data && docker compose up -d --wait nats
```

Reset a service's database and the streams together: a database that is empty while NATS still holds the consumers'
positions (or the other way round) makes a service skip or repeat events. The realm file is imported only when the realm
does not exist, so a change of `identity/realm/chance-realm.json` reaches Keycloak only after `down -v` (or the same
change in the admin console).

## Mails

Keycloak sends its mails (the e-mail verification of the sign-up, the reset link of "Forgot Password?" and the
confirmation link when a Google login is linked to an existing account) to Mailpit over SMTP inside the compose network,
so nothing leaves the machine. Read them at **http://localhost:8025**. They are gone with the container (`docker compose
down`), not with a restart.

## Logins

The login page of the realm (`http://localhost:8081/realms/chance/account`, or the web app's login) offers three ways in:

- **Local accounts work as in the cluster.** *Register* asks for the password on the form; the verification mail is in
  Mailpit. `platform-admin` logs in with the password of `compose.yaml` (the first login asks for a new one).
- **"Forgot Password?"** sits under the password field (with organizations in the realm, on the second page, after the
  user name). It mails a reset link to the account's address, so to Mailpit here; the link asks for a new password.
- **The "Google" button does not work locally** until a real Google OAuth client exists. `GOOGLE_CLIENT_ID` and
  `GOOGLE_CLIENT_SECRET` in `compose.yaml` are placeholders: Keycloak sends the browser to Google, and Google refuses the
  unknown client with an error page. Nothing else depends on it. To try Google login here, create a client as in
  [`identity/README.md`](../../identity/README.md#google-login) with the redirect URI
  `http://localhost:8081/realms/chance/broker/google/endpoint` and a Google account of yours as a test user, put its two
  values into a compose file outside the repository (`services: keycloak: environment: GOOGLE_CLIENT_ID: ...,
  GOOGLE_CLIENT_SECRET: ...`), and start from zero with it, as the values are read only when the realm is imported:
  `docker compose down -v`, then `docker compose -f compose.yaml -f <that file> up -d --wait`. The secret never goes into
  `compose.yaml`.

## What `APP_ENV=local` replaces

| Service | Missing setting | What stands in |
|---|---|---|
| all but market-maker | `DATABASE_URL`, `NATS_URL` | An in-memory store and no broker: events are only logged, so the service works alone (trading and market-data also take one event by `POST` without NATS: `/v1/internal/local/events` and `/local/events`); market-maker has no database and runs without NATS |
| tenants | `KEYCLOAK_URL` | A fake identity provider (no user is created in Keycloak) |
| tenants | `MARKETS_BASE_URL` | Every share join answers `503` |
| payments | `TRADING_GRPC_URL` (or `TRADING_BASE_URL` with `TRADING_DEBIT_TRANSPORT=rest`) or `KEYCLOAK_TOKEN_URL` | An in-memory trading answers the debit |
| markets | `TRADING_URL` | A fake funding client, `FAKE_FUNDING_MODE` is `funded`, `insufficient` or `unknown` |
| market-maker | `TRADING_URL`, `KEYCLOAK_URL` | An in-memory trading with one open demo market |
| trading | | `INTERNAL_PORT` defaults to 8091 (a Keycloak may listen on 8081) and `SHUTDOWN_DELAY_MS` to 0 |

Never faked: **tokens**. Every service verifies the signature, issuer and audience of each request against
`AUTH_JWKS_URL`, so Keycloak (or a key set of your own) must run for any call that carries a token. `APP_ENV=local`
exists for this only: the charts refuse it (stated in the READMEs of trading and market-data).

## Option: a local kind cluster with the dev Helm values

Not needed to run or change a service, and not required unless the lab instructor asks for it (plan Q-17). It runs a
service's chart with its `values-dev.yaml` (`APP_ENV=dev`, debug logs) the way the cluster would. **These commands are
written, not run**: read each one first. They need `kind` (the Ansible playbook installs it with `-e install_kind=true`).

```bash
kind create cluster --name chance-dev                      # a throwaway cluster in a Docker container; context kind-chance-dev
docker build -t chance-tenants:local ../../tenants          # the image of the service
kind load docker-image chance-tenants:local --name chance-dev

# The static checks of the chart (they need no cluster):
helm lint ../../tenants/helm -f ../../tenants/helm/values-dev.yaml
helm template tenants ../../tenants/helm -f ../../tenants/helm/values-dev.yaml | kubeconform -strict -kubernetes-version 1.35.0 -summary

# The release itself, with the image above and the Secret the chart reads (its keys are in the service's README):
kubectl --context kind-chance-dev create secret generic tenants-env --from-literal=DATABASE_URL='<a PostgreSQL the cluster can reach>' ...
helm upgrade --install tenants ../../tenants/helm -f ../../tenants/helm/values-dev.yaml \
  --kube-context kind-chance-dev --set-string image.repository=chance-tenants --set-string image.tag=local

kind delete cluster --name chance-dev                      # when done
```

Limit: the dependencies of `compose.yaml` are published on `127.0.0.1`, which a kind node cannot reach, so the release
needs its PostgreSQL, NATS and Keycloak inside the cluster (the `broker` and `identity` repositories have charts; a
database chart does not exist here yet). Until that is decided, the cluster option stops at the static checks above.
