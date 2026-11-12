# Local end-to-end run

One command starts the dependencies of `../compose.yaml` (PostgreSQL, NATS, Keycloak with the realm of `identity`,
Mailpit, the mock payment provider), runs the seven services natively from their built `dist/`, drives the main flows
of Chance through them the way a browser and the web app would, prints every check as PASS or FAIL with its evidence,
and removes everything again.

```bash
cd infrastructure/local/e2e
bash run.sh all            # the whole run from zero; exit 0 only when every check passes
```

## What it checks

Steps `a` to `k` of `flows.py`, 142 checks (the run of 2026-11-12 23:19: 142 PASS, 0 FAIL; `a` to `j` 86, `k` 56).
The `kc_action=UPDATE_PROFILE` check of T4 needs a signed-in session, and the attacker's first login never gets one (it
ends on Keycloak's "Confirmation email sent" page), so a second attacker account signs in plainly first (`T4 (setup)`)
and opens the form from that session. Tokens come from Keycloak's own pages (login, sign-up, password change,
password reset) driven over HTTP: the authorization code flow with PKCE of the public client `web`. The services are
called directly on their native ports (no gateway). Google itself is never called: the local Google client is a
placeholder, so step `i` checks what Keycloak offers and where it sends the browser, not a Google login. `flows.py`
enforces it: its HTTP client raises on any request, and on any redirect it would follow, to a host other than
`localhost` or `127.0.0.1` (step `i` reads Keycloak's redirect to Google without following it).

| Step | What |
|---|---|
| a | The platform admin's first login asks only for a new password (the realm imports its verified address and name); its token holds `platform-admin` and the address. It creates a tenant; the Keycloak organization with alias = tenant id and the groups `tenant-admin` and `trader` appear |
| b | The realm binds the registration flow `chance registration` without the option `always_set_password_on_register_form` (Admin REST API). A user signs up: the form asks for no password and ends on the verify-email page; the account has no password and the required actions `VERIFY_EMAIL` and `UPDATE_PASSWORD`; the mail reaches Mailpit; its link asks for the password and then finishes the login; Keycloak holds a password credential and no required action; a new browser logs in with that password; the token has `email_verified` true |
| c | A second user signs up the same way, accepts the first admin's invite (role `tenant-admin`), the membership reaches Keycloak, a deposit through the provider's payment page credits the admin, the admin creates and opens a market with a sharing policy and a `profiles` share link for the first user, who looks it up and joins as `trader` |
| d | The trader deposits; `DepositSucceeded` is on the `PAYMENTS` stream; trading credits the trader |
| e | Trading quotes a YES buy (the market maker's offers), the trader buys, market-data reaches the trade's sequence |
| f | A withdrawal ends `paid_out`; the debit went over gRPC (trading's ledger, no REST debit in its metrics); `WithdrawalSucceeded` is on the stream |
| g | The tenant's audit log holds the events of the run and its hash chain verifies |
| h | Every service, and the provider inside its container, serves `/metrics` with requests counted |
| i | The Admin REST API shows the provider `google` enabled, with `trustEmail` true, the client id of `../compose.yaml`, the scopes `openid email profile` and the first-login flow `chance first broker login`, and masks its secret; that flow confirms a link to an existing account (`idp-confirm-link`, then that account's password) and has no `idp-email-verification`, `idp-review-profile` or `idp-auto-link`; the login page links to `/realms/chance/broker/google/login`, and that link sends the browser to Google's authorization endpoint with the client id, the redirect URI `http://localhost:8081/realms/chance/broker/google/endpoint` and the three scopes (the redirect is not followed) |
| j | The trader of step `b` resets the password: "Forgot Password?" under the password field, the reset form, the reset mail in Mailpit with its link, a new password on Keycloak's form; Keycloak holds a password and no required action; the old password is refused and the new one logs in. Run alone (`run.sh flows j`), the step signs a new user up first |
| k | Account takeover attempts (`identity/README.md`, "Account takeover protections"), attacker and victim each in their own browser, against a stand-in for Google: the realm `e2e-upstream` and the provider `e2e-upstream` in `chance`, set up like `google` (trustEmail, `IMPORT`, `chance first broker login`), which `run.sh upstream` creates through kcadm in this local Keycloak only (part of `run.sh all`). The legitimate link with the account's password works; T1 (a stranger's sign-up, then the owner's provider login: password asked, no link mail, *Forgot Password?* sets the only password and links), T2/T3 (the owner follows the stranger's verification mail: the owner sets the password, the mail warns, the stranger's waiting session gets no code), T4 (no review page, no address field on the profile forms, a posted address changes nothing; 'Review profile' leads back to the same page), T5 (the account console refuses a new address; `kc_action=UPDATE_EMAIL` mails the new address no link, and a link would change nothing), password fields added to a sign-up POST store no password, T9 (a user name with "@" is refused; the victim's provider login creates the victim's own account `e2e-upstream.<sub>` although a stranger holds the victim's provider user name) and T7 (the platform admin's address: its password, no link mail; *Forgot Password?* there mails the admin's address only; the check names whether it ran before or after the admin's first login) must all fail for the attacker. The waiting sign-up sessions must reach the verify-email page first and end on a restart or expired page; expected-no-mail checks poll Mailpit for 15 s. The stand-in provider gets google's user name mapper (`e2e-upstream.${CLAIM.sub}`) |

The results are in `results.json`, the output in `logs/flows.out`, each service's log in `logs/<service>.log`,
the phases with their times in `logs/progress.log`.

## What it needs

- Linux with Docker Engine and the compose plugin (the run uses `setsid`, `/proc` and, for the CPU caps,
  `systemd-run --user`), Node.js 24, Python 3 (standard library only), `curl` and `flock`.
- The built `dist/` of each service, side by side with `infrastructure/`: `tenants`, `trading`, `payments`, `audit`,
  `markets`, `market-data`, `market-maker` (`npm ci && npm run build` in each; `run.sh services` names the ones
  missing). The mock provider is built by compose from `../../../payment-provider` on the first start.
- The free host ports of `../compose.yaml` (5432, 4222, 8222, 8081, 9001, 8025, 8082) and of the services
  (8101 to 8107, metrics 9101 to 9107, trading's 8115 and 8125). Nothing else from `../compose.yaml` may be running.
- Docker pulls the public images it lacks (the dependencies and `natsio/nats-box` for the stream checks); no login.

Local credentials come only from `../compose.yaml` (its values marked as local development values); the run writes
none of them anywhere.

## How long

About 4 minutes on a laptop when the images are there (the run of 2026-11-12: compose up 139 s, as Keycloak in
development mode builds itself at start; the seven services 38 s; the flows 27 s; the clean-up 10 s). The first run
also pulls the images and builds the provider.

## Settings (all optional, from the environment)

| Variable | Effect |
|---|---|
| `DOCKER_CONFIG`, `DOCKER_HOST` | Docker's own settings, passed through unchanged. When the credential helper of your Docker config asks for a passphrase, point `DOCKER_CONFIG` at a folder whose `config.json` is `{}` (the images are public) |
| `HEAVY_LOCK=<file>` | Compose up and the service starts wait for `flock` on this file, so they never run at the same time as another heavy job that takes the same lock |
| `E2E_CPU_QUOTA` | CPU cap of each service's `systemd-run --user --scope` (default `25%`); empty starts the services without a scope. The dependencies are capped in `compose.override.yaml` |
| `PAYMENT_PROVIDER_IMAGE` | An image of the mock provider that exists already, used instead of building `chance-payment-provider:local` |
| `THERMAL_WATCH=1` | A thermal gate before each heavy phase and a watcher (`thermal-watch.sh`, user unit `rso-e2e-thermal`) that stops the whole run on a thermal event. **It fits one machine only**: it counts the kernel log lines with `0x6d`, the thermal notification of one ASUS laptop's embedded controller (driver `asus_wmi`), which powers that laptop off under sustained load. Leave it off elsewhere |
| `E2E_KEEP_UP=1` | `run.sh all` leaves everything running after the flows (then `run.sh down`). Run identity's `test:integration` only after `run.sh down`: it starts its own stack, whose Mailpit also binds 127.0.0.1:8025 |

## Run it in parts

```bash
bash run.sh up             # the dependencies, healthy
bash run.sh services       # the seven services, each waited for on /health/ready
bash run.sh upstream       # step k's stand-in provider (realm e2e-upstream, provider e2e-upstream in chance)
bash run.sh flows          # all steps (a to k); or some of them in order: bash run.sh flows a b
bash run.sh status         # pids, containers
bash run.sh down           # stop and remove everything
```

A second `flows` against the same stack works: the platform admin's password is then reset through the Admin REST
API, and the first-login check is skipped (it needs a fresh realm, so `run.sh all` always starts from zero).

## How it cleans up

`run.sh down` (also the end of `run.sh all`, whether the flows passed or not) stops every service by the pid recorded
in `pids.txt` (SIGTERM, SIGKILL after 20 seconds), runs `docker compose down -v` (containers, network and volumes:
the databases, the streams, the imported realm, the mails), stops the thermal watcher, and prints what is left over:
the containers and volumes of the compose project `chance-local` and the service pids still alive, each list empty
after a clean run. The logs and `results.json` stay (ignored by git).

`run.sh all` also runs it on Ctrl-C, SIGTERM and SIGHUP (exit 130; with `E2E_KEEP_UP=1` it keeps the stack, as at the
normal end). After an interrupted run, run `bash run.sh down`: it is safe to repeat, and it is the only cleanup after
SIGKILL or a crash of the machine.

A change of `identity/realm/chance-realm.json` takes effect on the next `run.sh all` (the realm is imported when the
stack starts from zero).
