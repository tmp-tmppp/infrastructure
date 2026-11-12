#!/usr/bin/env python3
"""The flows of the local end-to-end run of Chance against a real Keycloak (steps a to j; README.md).

Tokens are fetched as a browser does: the authorization-code flow with PKCE against the realm's public client `web`,
with Keycloak's own login, registration, password-update and profile forms driven over HTTP with a cookie jar. The
services are called directly (no gateway). Every check prints PASS or FAIL with its evidence; the results go to
results.json next to this file. Local credentials come only from ../compose.yaml. Standard library only.
"""
import base64
import hashlib
import html
import http.cookiejar
import json
import os
import re
import secrets
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from html.parser import HTMLParser

E2E = os.path.dirname(os.path.abspath(__file__))
COMPOSE_FILE = os.path.join(os.path.dirname(E2E), "compose.yaml")
LOGS = f"{E2E}/logs"
KC = "http://localhost:8081"
ISSUER = f"{KC}/realms/chance"
REDIRECT = "http://localhost:5173/auth/callback"
SVC = {
    "tenants": "http://localhost:8101",
    "payments": "http://localhost:8102",
    "audit": "http://localhost:8103",
    "markets": "http://localhost:8104",
    "trading": "http://localhost:8105",
    "market-data": "http://localhost:8106",
    "market-maker": "http://localhost:8107",
}
METRICS = {name: f"http://localhost:{9101 + i}" for i, name in enumerate(SVC)}
PROVIDER = "http://localhost:8082"
MAILPIT = "http://localhost:8025"
RUN = time.strftime("%H%M%S") + secrets.token_hex(2)
DOCKER_ENV = dict(os.environ)  # DOCKER_CONFIG and DOCKER_HOST, when set, come from the caller (README.md)

RESULTS = []
STATE = {"run": RUN}


def compose_value(key):
    """A local development value of compose.yaml (the file marks them as local only)."""
    with open(COMPOSE_FILE, encoding="utf-8") as fh:
        for line in fh:
            m = re.match(rf"^\s*{re.escape(key)}:\s*([^\s#]+)", line)
            if m:
                return m.group(1)
    raise KeyError(key)


def clip(value, n=600):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= n else text[:n] + "..."


def check(step, name, ok, evidence=None):
    RESULTS.append({"step": step, "check": name, "ok": bool(ok), "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'} [{step}] {name}" + (f"\n      evidence: {clip(evidence)}" if evidence is not None else ""), flush=True)
    return bool(ok)


def info(step, text):
    RESULTS.append({"step": step, "info": text})
    print(f"     [{step}] {clip(text, 900)}", flush=True)


# ------------------------------------------------------------------------------------------------------------ HTTP -----
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


PLAIN = urllib.request.build_opener(NoRedirect())
LOCAL_HOSTS = ("localhost", "127.0.0.1")


def refuse_outside(url, what):
    """The harness reaches this machine only: whatever the realm or a page redirects to, Google or any other outside host is refused."""
    parts = urllib.parse.urlparse(str(url))
    if parts.scheme not in ("http", "https") or parts.hostname not in LOCAL_HOSTS:
        raise RuntimeError(f"refusing to {what} {url}: its host is not localhost or 127.0.0.1, and the end-to-end run never reaches an outside system")


class Resp:
    def __init__(self, method, url, status, headers, text):
        self.method, self.url, self.status, self.headers, self.text = method, url, status, headers, text
        try:
            self.json = json.loads(text) if text else None
        except ValueError:
            self.json = None

    def ev(self):
        return {"request": f"{self.method} {self.url}", "status": self.status, "body": self.json if self.json is not None else clip(self.text, 300)}


def call(method, url, body=None, token=None, headers=None, timeout=20, raw=None, ctype=None):
    data = None
    h = {"accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        h["content-type"] = "application/json"
    elif raw is not None:
        data = raw
        h["content-type"] = ctype or "application/x-www-form-urlencoded"
    if token:
        h["authorization"] = f"Bearer {token}"
    h.update(headers or {})
    refuse_outside(url, "call")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with PLAIN.open(req, timeout=timeout) as r:
            return Resp(method, url, r.status, dict(r.headers), r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return Resp(method, url, e.code, dict(e.headers), e.read().decode("utf-8", "replace"))
    except Exception as e:  # network error: status 0
        return Resp(method, url, 0, {}, f"{type(e).__name__}: {e}")


def poll(fn, done, timeout=60, interval=1.0):
    deadline = time.time() + timeout
    last = None
    while True:
        last = fn()
        if done(last):
            return True, last
        if time.time() > deadline:
            return False, last
        time.sleep(interval)


def jwt_claims(token):
    part = token.split(".")[1]
    part += "=" * (-len(part) % 4)
    return json.loads(base64.urlsafe_b64decode(part))


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


# -------------------------------------------------------------------------------------------------- the browser -----
class Page(HTMLParser):
    def __init__(self, body):
        super().__init__()
        self.forms, self.links, self._form, self._text = [], [], None, []
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self._form = {"id": a.get("id"), "action": a.get("action"), "method": (a.get("method") or "get").lower(), "inputs": {}}
            self.forms.append(self._form)
        elif tag in ("input", "select", "textarea") and self._form is not None and a.get("name"):
            kind = (a.get("type") or "text").lower()
            if kind in ("checkbox", "radio") and "checked" not in a:
                return
            if kind in ("submit", "button", "image"):
                return
            self._form["inputs"].setdefault(a["name"], a.get("value") or "")
        elif tag == "a" and a.get("href"):
            self.links.append(a["href"])

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None

    def handle_data(self, data):
        if data.strip() and self.lasttag not in ("script", "style"):
            self._text.append(data.strip())

    def form(self, *ids):
        for f in self.forms:
            if f["id"] in ids:
                return f
        return None

    @property
    def text(self):
        return " ".join(self._text)


class Browser:
    """A cookie-keeping client for Keycloak's pages on http://localhost:8081.

    Keycloak sets its cookies `Secure; SameSite=None; Version=1`. A browser sends Secure cookies to http://localhost (a
    potentially trustworthy origin); Python's http.cookiejar does not (and drops Version=1 cookies without RFC 2965), so
    this class keeps the cookies of the one host itself, like a browser would for localhost. Cookies are kept per name and
    Path, as a browser does: the realm chance and the stand-in provider realm e2e-upstream (step k) set cookies of the
    same names under their own paths.
    """

    def __init__(self, label):
        self.label = label
        self.cookies = {}  # (name, path) -> value
        self.pending = (None, None)
        self.opener = PLAIN
        self.last_url = None
        self.last_body = ""

    def _store(self, headers, url):
        for raw in headers.get_all("Set-Cookie") or []:
            first, _, attrs = raw.partition(";")
            name, _, value = first.strip().partition("=")
            attrs_l = attrs.lower()
            path = next((a.split("=", 1)[1].strip() for a in attrs.split(";") if a.strip().lower().startswith("path=")), None)
            if not path:
                path = urllib.parse.urlparse(url).path.rsplit("/", 1)[0] or "/"
            expired = "max-age=0" in attrs_l.replace(" ", "") or "expires=thu, 01-jan-1970" in attrs_l or "expires=thu, 01 jan 1970" in attrs_l
            if expired or value == "":
                self.cookies.pop((name, path), None)
            else:
                self.cookies[(name, path)] = value

    def cookie_header(self, url):
        """The cookies a browser sends to this URL: those whose Path is a prefix of its path, the longest Path first."""
        path = urllib.parse.urlparse(url).path or "/"
        match = [(p, n, v) for (n, p), v in self.cookies.items() if path == p or path.startswith(p.rstrip("/") + "/") or p == "/"]
        return "; ".join(f"{n}={v}" for p, n, v in sorted(match, key=lambda x: -len(x[0])))

    def request(self, method, url, form=None):
        refuse_outside(url, "request")
        data = urllib.parse.urlencode(form).encode() if form is not None else None
        headers = {"user-agent": "Mozilla/5.0 (X11; Linux x86_64) e2e-browser", "accept": "text/html,application/xhtml+xml"}
        if data is not None:
            headers["content-type"] = "application/x-www-form-urlencoded"
        if self.cookies and urllib.parse.urlparse(url).netloc == "localhost:8081" and self.cookie_header(url):
            headers["cookie"] = self.cookie_header(url)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=30) as r:
                self._store(r.headers, url)
                return r.status, r.headers, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            self._store(e.headers, url)
            return e.code, e.headers, e.read().decode("utf-8", "replace")

    def walk(self, method, url, form=None):
        """Follows redirects until a page, or a redirect to the web app (then {'redirect': url})."""
        for _ in range(12):
            status, headers, body = self.request(method, url, form)
            if status in (301, 302, 303, 307, 308):
                loc = urllib.parse.urljoin(url, headers.get("Location"))
                refuse_outside(loc, "follow the redirect to")
                if loc.startswith(REDIRECT):
                    return {"redirect": loc}
                method, url, form = "GET", loc, None
                continue
            self.last_url, self.last_body = url, body
            return {"status": status, "body": body, "url": url}
        raise RuntimeError("too many redirects")


class AuthNeedsMail(Exception):
    pass


class LoginRejected(Exception):
    pass


def authorize_url(scope, challenge, state):
    q = {
        "client_id": "web",
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "nonce": secrets.token_urlsafe(12),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{ISSUER}/protocol/openid-connect/auth?{urllib.parse.urlencode(q)}"


def exchange(code, verifier):
    form = {"grant_type": "authorization_code", "client_id": "web", "code": code, "redirect_uri": REDIRECT, "code_verifier": verifier}
    r = call("POST", f"{ISSUER}/protocol/openid-connect/token", raw=urllib.parse.urlencode(form).encode())
    if r.status != 200:
        raise RuntimeError(f"code exchange failed: {r.ev()}")
    return r.json


def drive(browser, user, page, verifier, state, log):
    """Answers Keycloak's pages until the redirect to the app; returns the token response."""
    for _ in range(10):
        if "redirect" in page:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(page["redirect"]).query)
            if "error" in q:
                raise RuntimeError(f"authorization error: {q}")
            if q.get("state", [None])[0] != state:
                raise RuntimeError("state mismatch")
            return exchange(q["code"][0], verifier)
        p = Page(page["body"])
        f = p.form("kc-form-login")
        if f:
            # The realm's browser flow asks the username first and the password on a second page (organizations on:
            # identity-first login), so two login forms in a row are normal; an error text or a third one is a rejection.
            if re.search(r"invalid|incorrect|locked|disabled", p.text, re.I) or log[-2:] == ["login form", "login form"]:
                raise LoginRejected(p.text[:200])
            log.append("login form")
            inputs = dict(f["inputs"], username=user["username"], password=user["password"])
            page = browser.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), inputs)
            continue
        f = p.form("kc-passwd-update-form")
        if f:
            log.append("password update form (required action UPDATE_PASSWORD)")
            new = "Pw-" + secrets.token_hex(8) + "-Aa1"
            inputs = dict(f["inputs"], **{"password-new": new, "password-confirm": new})
            if user.get("logout_sessions"):
                inputs["logout-sessions"] = "on"  # "Sign out from other devices": ends the other sessions of the account
            else:
                inputs.pop("logout-sessions", None)
            page = browser.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), inputs)
            user["password"] = new
            continue
        f = p.form("kc-update-profile-form")
        if f:
            log.append(f"update profile form (fields {sorted(f['inputs'])})")
            inputs = dict(f["inputs"])
            inputs.update({"email": user["email"], "firstName": user.get("first", "E2e"), "lastName": user.get("last", "User")})
            page = browser.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), inputs)
            continue
        f = p.form("kc-register-form")
        if f:
            log.append(f"registration form (fields {sorted(f['inputs'])})")
            inputs = dict(f["inputs"])
            inputs.update({"firstName": user.get("first", "E2e"), "lastName": user.get("last", "User"), "email": user["email"], "username": user["username"]})
            if "password" in f["inputs"]:  # only a form that asks for it gets a password (the realm sets it after the mail link)
                inputs.update({"password": user["password"], "password-confirm": user["password"]})
            page = browser.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), inputs)
            continue
        if re.search(r"verify your email|email verification|verify email", p.text, re.I):
            log.append("verify-email page")
            raise AuthNeedsMail(p.text[:300])
        onward = [h for h in p.links if "/login-actions/" in h and "restart" not in h and "registration" not in h]
        if onward:
            log.append(f"info page, following its link ({p.text[:100]})")
            page = browser.walk("GET", urllib.parse.urljoin(page["url"], html.unescape(onward[0])))
            continue
        dump = f"{LOGS}/page-{user['username']}-{int(time.time())}.html"
        with open(dump, "w", encoding="utf-8") as fh:
            fh.write(page.get("body", ""))
        raise RuntimeError(f"unexpected page {page.get('status')} at {page.get('url')} (forms {[f['id'] for f in p.forms]}, saved {dump}): {p.text[:400]}")
    raise RuntimeError("too many pages")


def login(browser, user, scope="openid", log=None):
    log = [] if log is None else log
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(12)
    browser.pending = (verifier, state)
    page = browser.walk("GET", authorize_url(scope, challenge, state))
    return drive(browser, user, page, verifier, state, log)


def registration_page(browser, log):
    """Opens the login page as the web app would and follows its registration link; returns (page, verifier, state)."""
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(12)
    browser.pending = (verifier, state)
    page = browser.walk("GET", authorize_url("openid", challenge, state))
    p = Page(page.get("body", ""))
    reg = [h for h in p.links if "/login-actions/registration" in h]
    if not reg:
        raise RuntimeError(f"no registration link on the login page: {p.text[:300]}")
    log.append("registration link found on the login page")
    return browser.walk("GET", urllib.parse.urljoin(page["url"], reg[0])), verifier, state


def register(browser, user, log):
    page, verifier, state = registration_page(browser, log)
    return drive(browser, user, page, verifier, state, log)


# ------------------------------------------------------------------------------------------------------- mailpit -----
def mails_to(address):
    r = call("GET", f"{MAILPIT}/api/v1/messages?limit=100")
    out = []
    for m in (r.json or {}).get("messages", []):
        if any((t.get("Address") or "").lower() == address.lower() for t in m.get("To") or []):
            out.append(m)
    return out


def action_link(message_id):
    m = call("GET", f"{MAILPIT}/api/v1/message/{message_id}").json or {}
    blob = (m.get("Text") or "") + " " + (m.get("HTML") or "")
    links = [html.unescape(x) for x in re.findall(r"https?://[^\s\"'<>]+", blob)]
    return m, [x for x in links if "action-token" in x]


def verify_by_mail(step, browser, user, log):
    ok, mails = poll(lambda: mails_to(user["email"]), lambda ms: len(ms) > 0, timeout=60, interval=1)
    if not check(step, f"the verification mail for {user['email']} arrived in Mailpit", ok, {"mails": [(m.get("ID"), m.get("Subject")) for m in mails or []]}):
        return None
    msg, links = action_link(mails[0]["ID"])
    check(step, "the mail holds an action-token link to Keycloak", bool(links), {"subject": msg.get("Subject"), "from": msg.get("From"), "links": [x[:90] for x in links]})
    if not links:
        return None
    info(step, f"mail: id {mails[0]['ID']}, subject {msg.get('Subject')!r}, from {(msg.get('From') or {}).get('Address')}, link {links[0][:80]}...")
    page = browser.walk("GET", links[0])
    log.append("followed the mail's link")
    verifier, state = browser.pending
    # The same browser goes on with its login: Keycloak may ask for more (a password for a user who registered
    # without one) before it redirects to the app with a code for the authorization request that started it.
    return drive(browser, user, page, verifier, state, log)


# --------------------------------------------------------------------------------------------- keycloak admin -----
def admin_token():
    form = {"grant_type": "password", "client_id": "admin-cli", "username": compose_value("KC_BOOTSTRAP_ADMIN_USERNAME"), "password": compose_value("KC_BOOTSTRAP_ADMIN_PASSWORD")}
    r = call("POST", f"{KC}/realms/master/protocol/openid-connect/token", raw=urllib.parse.urlencode(form).encode())
    if r.status != 200:
        raise RuntimeError(f"admin token: {r.status} {r.text[:200]}")
    return r.json["access_token"]


def kc_orgs():
    r = call("GET", f"{KC}/admin/realms/chance/organizations?first=0&max=200&briefRepresentation=false", token=admin_token())
    return r


def kc_user(username):
    """The user of the realm (Admin REST API) and the types of its credentials; (None, []) when there is none."""
    at = admin_token()
    users = call("GET", f"{KC}/admin/realms/chance/users?username={urllib.parse.quote(username)}&exact=true", token=at).json or []
    if not users:
        return None, []
    creds = call("GET", f"{KC}/admin/realms/chance/users/{users[0]['id']}/credentials", token=at).json or []
    return users[0], [c.get("type") for c in creds if isinstance(c, dict)]


def kc_registration_flow():
    """The realm's bound registration flow, its password step's requirement and that step's options."""
    at = admin_token()
    alias = (call("GET", f"{KC}/admin/realms/chance", token=at).json or {}).get("registrationFlow")
    steps = call("GET", f"{KC}/admin/realms/chance/authentication/flows/{urllib.parse.quote(str(alias))}/executions", token=at).json or []
    step = next((s for s in steps if isinstance(s, dict) and s.get("providerId") == "registration-password-action"), {})
    config = {}
    if step.get("authenticationConfig"):
        config = (call("GET", f"{KC}/admin/realms/chance/authentication/config/{step['authenticationConfig']}", token=at).json or {}).get("config") or {}
    return {"registrationFlow": alias, "passwordStep": step.get("requirement"), "config": config}


def kc_google():
    """The realm's provider google, the realm, the provider's first-login flow and that flow's steps (Admin REST API)."""
    at = admin_token()
    idp = call("GET", f"{KC}/admin/realms/chance/identity-provider/instances/google", token=at)
    realm = call("GET", f"{KC}/admin/realms/chance", token=at).json or {}
    flow = (idp.json or {}).get("firstBrokerLoginFlowAlias") or realm.get("firstBrokerLoginFlow")
    steps = call("GET", f"{KC}/admin/realms/chance/authentication/flows/{urllib.parse.quote(str(flow))}/executions", token=at).json or []
    return idp, realm, flow, [s for s in steps if isinstance(s, dict)]


def login_page(browser):
    """Opens Keycloak's login page as the web app does; returns (page, verifier, state)."""
    verifier = b64url(secrets.token_bytes(32))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(12)
    browser.pending = (verifier, state)
    return browser.walk("GET", authorize_url("openid", challenge, state)), verifier, state


def reset_link_page(browser, user, log):
    """The login page that holds the link "Forgot Password?" and that link; (page, link or None, verifier, state).

    The link sits under the password field. With organizations in the realm the first page asks for the user name only
    (identity-first login) and the password field comes on the next page, so the user name is sent once when needed.
    """
    page, verifier, state = login_page(browser)
    for _ in range(2):
        p = Page(page.get("body", ""))
        links = [h for h in p.links if "/login-actions/reset-credentials" in h]
        if links:
            return page, urllib.parse.urljoin(page["url"], links[0]), verifier, state
        f = p.form("kc-form-login")
        if not f or "password" in f["inputs"]:
            break
        log.append("login form (user name first)")
        page = browser.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], username=user["username"]))
    return page, None, verifier, state


# --------------------------------------------------------------------------------------------- service helpers -----
def log_lines(service, pattern, limit=5):
    path = f"{LOGS}/{service}.log"
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if re.search(pattern, line):
                    out.append(line.strip()[:400])
    except FileNotFoundError:
        return [f"(no log {path})"]
    return out[-limit:]


def nats(*args):
    cmd = ["docker", "run", "--rm", "--network", "chance-local_default", "natsio/nats-box:0.20.0", "nats", "--server", "nats://nats:4222", *args]
    p = subprocess.run(cmd, capture_output=True, text=True, env=DOCKER_ENV, timeout=60)
    return (p.stdout + p.stderr).strip()


def psql(db, query):
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, "-f", f"{E2E}/compose.override.yaml", "exec", "-T", "postgres", "psql", "-U", "postgres", "-d", db, "-tA", "-F", "|", "-c", query]
    p = subprocess.run(cmd, capture_output=True, text=True, env=DOCKER_ENV, timeout=60)
    return (p.stdout + p.stderr).strip()


def org_token(browser, user, alias, step, label):
    log = []
    tok = login(browser, user, scope=f"openid chance-organization:{alias}", log=log)
    claims = jwt_claims(tok["access_token"])
    info(step, f"{label} token with scope chance-organization:{alias}: organization claim {json.dumps(claims.get('organization'))}, aud {claims.get('aud')}, flow {log}")
    return tok, claims


def groups_of(claims, alias):
    org = claims.get("organization")
    if isinstance(org, dict):
        entry = org.get(alias) or {}
        return entry.get("groups") or []
    return []


def deposit(step, token, amount, who):
    key = str(uuid.uuid4())
    r = call("POST", f"{SVC['payments']}/v1/deposits", body={"amount": amount}, token=token, headers={"idempotency-key": key})
    if not check(step, f"{who}: POST /v1/deposits {amount} answers 201 with a checkout URL", r.status == 201 and (r.json or {}).get("checkoutUrl"), r.ev()):
        return None
    dep = r.json
    info(step, f"deposit {dep['id']} pending, checkoutUrl {dep['checkoutUrl']}")
    page = call("GET", dep["checkoutUrl"], headers={"accept": "text/html"})
    check(step, "the provider's payment page answers 200", page.status == 200, {"status": page.status, "body": page.text[:200]})
    pay = call("POST", dep["checkoutUrl"].rstrip("/") + "/pay", headers={"accept": "text/html"})
    check(step, "pressing Pay on the provider's page (POST /checkout/{id}/pay) answers 303 to the success URL", pay.status in (200, 303), {"status": pay.status, "location": pay.headers.get("Location") or pay.headers.get("location")})
    ok, last = poll(lambda: call("GET", f"{SVC['payments']}/v1/deposits/{dep['id']}", token=token), lambda x: (x.json or {}).get("status") in ("succeeded", "failed"), timeout=60, interval=1)
    check(step, f"{who}: the deposit becomes succeeded (the provider's webhook reached payments)", ok and last.json.get("status") == "succeeded", last.ev())
    info(step, f"payments log, webhook: {log_lines('payments', 'webhook', 3)}")
    info(step, f"provider log, webhook delivery: {provider_log('webhook|delivered|Webhook', 3)}")
    return dep


def provider_log(pattern, limit=3):
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, "-f", f"{E2E}/compose.override.yaml", "logs", "--no-color", "--tail", "400", "payment-provider"]
    p = subprocess.run(cmd, capture_output=True, text=True, env=DOCKER_ENV, timeout=60)
    lines = [x.strip()[:300] for x in p.stdout.splitlines() if re.search(pattern, x)]
    return lines[-limit:]


def portfolio(token):
    return call("GET", f"{SVC['trading']}/v1/trading/portfolio", token=token)


def credits_of(pf):
    j = pf.json or {}
    for key in ("creditsMicro", "balanceMicro", "credits", "balance"):
        if key in j:
            return key, j[key]
    return None, None


# ===================================================================================================== the steps =====
def step_a():
    step = "a"
    padmin = {"username": compose_value("PLATFORM_ADMIN_USERNAME"), "password": compose_value("PLATFORM_ADMIN_PASSWORD"), "email": compose_value("PLATFORM_ADMIN_EMAIL")}
    b = Browser("platform-admin")
    log = []
    first_login = True
    try:
        try:
            tok = login(b, padmin, log=log)
        except LoginRejected:
            # A rerun against the same Keycloak: the first run already replaced the temporary password. Set a fresh one
            # for this run through the Admin REST API (never written anywhere) and log in again.
            first_login = False
            log.append("the import's temporary password was already changed by an earlier run: reset through the Admin API")
            at = admin_token()
            users = call("GET", f"{KC}/admin/realms/chance/users?username={padmin['username']}&exact=true", token=at).json or []
            padmin["password"] = "Pw-" + secrets.token_hex(8) + "-Aa1"
            call("PUT", f"{KC}/admin/realms/chance/users/{users[0]['id']}/reset-password", body={"type": "password", "value": padmin["password"], "temporary": False}, token=at)
            b = Browser("platform-admin")
            tok = login(b, padmin, log=log)
    except AuthNeedsMail:
        log.append("platform admin asked to verify the e-mail it just entered")
        tok = verify_by_mail(step, b, padmin, log) or login(b, padmin, log=log)
    claims = jwt_claims(tok["access_token"])
    check(step, "platform-admin logs in through the web client (code flow + PKCE) and holds the realm role platform-admin", "platform-admin" in (claims.get("realm_access") or {}).get("roles", []), {"flow": log, "realm_access": claims.get("realm_access"), "aud": claims.get("aud"), "iss": claims.get("iss")})
    if first_login:
        asked = [x for x in log if x != "login form"]
        check(step, "the platform admin's first login asks only for the new password (no profile form, no verification mail)", asked == ["password update form (required action UPDATE_PASSWORD)"], {"flow": log})
    else:
        info(step, "not a first login (this Keycloak was used by an earlier run): the first-login check is skipped; run.sh all starts from zero")
    kc, creds = kc_user(padmin["username"])
    seen = {k: (kc or {}).get(k) for k in ("email", "emailVerified", "firstName", "lastName", "requiredActions")}
    check(step, "the platform admin has the address of compose.yaml, verified, a name, and no required action left; the token says the same", kc is not None and seen["email"] == padmin["email"] and seen["emailVerified"] is True and seen["firstName"] and seen["lastName"] and not seen["requiredActions"] and claims.get("email") == padmin["email"] and claims.get("email_verified") is True, {"admin_api": seen, "credentials": creds, "token": {"email": claims.get("email"), "email_verified": claims.get("email_verified")}})
    STATE["padmin"] = (b, padmin)
    slug = f"e2e-{RUN}"
    r = call("POST", f"{SVC['tenants']}/v1/tenants", body={"name": f"E2E Org {RUN}", "slug": slug}, token=tok["access_token"])
    if not check(step, "POST /v1/tenants answers 201 with the tenant and the first admin's invite", r.status == 201, r.ev()):
        return False
    tenant = r.json["tenant"]
    STATE["tenant"] = tenant
    STATE["invite"] = r.json["adminInvite"]["code"]
    info(step, f"tenant {tenant['id']} slug {tenant['slug']}; invite role {r.json['adminInvite']['role']}, maxUses {r.json['adminInvite']['maxUses']}")

    def find():
        res = kc_orgs()
        orgs = res.json if isinstance(res.json, list) else []
        return [o for o in orgs if o.get("alias") == tenant["id"]]

    ok, orgs = poll(find, lambda x: len(x) > 0, timeout=60, interval=1)
    if check(step, "the Keycloak organization with alias = tenant id appears (Admin REST API)", ok, {"orgs": orgs}):
        org = orgs[0]
        STATE["org_id"] = org["id"]
        g = call("GET", f"{KC}/admin/realms/chance/organizations/{org['id']}/groups", token=admin_token())
        names = sorted(x.get("name") for x in (g.json or []) if isinstance(x, dict))
        check(step, "the organization holds the groups tenant-admin and trader", names == ["tenant-admin", "trader"], {"status": g.status, "groups": names, "org": {k: org.get(k) for k in ("id", "name", "alias", "enabled")}})
    info(step, f"tenants log, identity sync: {log_lines('tenants', 'rganization|identity', 3)}")
    return True


def check_registration_flow(step):
    flow = kc_registration_flow()
    check(step, "the realm binds the registration flow 'chance registration'; its password step is REQUIRED without always_set_password_on_register_form (the password is set after the mail link; Admin REST API)", flow["registrationFlow"] == "chance registration" and flow["passwordStep"] == "REQUIRED" and flow["config"].get("always_set_password_on_register_form") != "true", flow)


def new_user(step, role_label):
    user = {"username": f"{role_label}-{RUN}", "email": f"{role_label}-{RUN}@example.com", "password": None, "first": role_label.capitalize(), "last": "E2e"}
    b = Browser(role_label)
    log = []
    try:
        register(b, user, log)
        check(step, f"{role_label}: registration went straight through (no e-mail verification asked)", False, log)
    except AuthNeedsMail as e:
        check(step, f"{role_label}: self-registration on Keycloak's form ends on the verify-email page", True, {"flow": log, "page": str(e)[:160]})
    form = next((x for x in log if x.startswith("registration form")), "")
    check(step, f"{role_label}: the sign-up form asks for no password (it is chosen after the mail link, by whoever follows it)", bool(form) and "'password'" not in form and "'password-confirm'" not in form, form)
    kc, creds = kc_user(user["username"])
    check(step, f"{role_label}: before the mail link the account has no password and the required actions VERIFY_EMAIL and UPDATE_PASSWORD", kc is not None and "password" not in creds and {"VERIFY_EMAIL", "UPDATE_PASSWORD"} <= set(kc.get("requiredActions") or []),
          {"credentials": creds, "requiredActions": (kc or {}).get("requiredActions")})
    tok = verify_by_mail(step, b, user, log)
    after = log[log.index("followed the mail's link"):] if "followed the mail's link" in log else log
    check(step, f"{role_label}: following the mail's link asks for the password (UPDATE_PASSWORD) and then finishes the login (redirect with a code, exchanged with PKCE)", bool(tok) and any(x.startswith("password update form") for x in after) and bool(user["password"]), after)
    kc, creds = kc_user(user["username"])
    check(step, f"{role_label}: Keycloak holds a password credential for the new user and no required action", kc is not None and "password" in creds and not (kc.get("requiredActions") or []), {"credentials": creds, "requiredActions": (kc or {}).get("requiredActions"), "emailVerified": (kc or {}).get("emailVerified")})
    fresh = []
    try:
        login(Browser(f"{role_label}-fresh"), user, log=fresh)
        ok = not any(x.startswith("password update form") for x in fresh)
    except Exception as e:  # a refused login: the evidence says why
        fresh.append(f"{type(e).__name__}: {str(e)[:160]}")
        ok = False
    check(step, f"{role_label}: a new browser logs in with the password chosen after the mail link", ok, fresh)
    tok = login(b, user, log=log)  # a fresh token through the SSO session, as the web app gets one later
    claims = jwt_claims(tok["access_token"])
    check(step, f"{role_label}: after following the link the token carries email_verified true", claims.get("email_verified") is True, {"email": claims.get("email"), "email_verified": claims.get("email_verified"), "sub": claims.get("sub"), "flow": log[-4:]})
    return b, user, tok, claims


def step_b():
    step = "b"
    check_registration_flow(step)
    b, user, tok, claims = new_user(step, "trader")
    STATE["trader"] = {"browser": b, "user": user, "sub": claims["sub"], "tok": tok}
    return claims.get("email_verified") is True


def step_c():
    step = "c"
    tenant = STATE["tenant"]
    alias = tenant["id"]
    b, user, tok, claims = new_user(step, "tadmin")
    r = call("POST", f"{SVC['tenants']}/v1/invites/{urllib.parse.quote(STATE['invite'])}/accept", token=tok["access_token"])
    if not check(step, "the second user accepts the first admin's invite: 200, role tenant-admin", r.status == 200 and (r.json or {}).get("role") == "tenant-admin", r.ev()):
        return False
    ok, me = poll(lambda: call("GET", f"{SVC['tenants']}/v1/me", token=login(b, user)["access_token"]), lambda x: x.status == 200 and x.json.get("identityPending") is False, timeout=60, interval=2)
    check(step, "GET /v1/me: identityPending turns false (the identity-sync worker put the admin into the organization)", ok, me.ev())
    atok, aclaims = org_token(b, user, alias, step, "tenant admin")
    check(step, "the tenant admin's token names the organization with the group /tenant-admin", groups_of(aclaims, alias) == ["/tenant-admin"], {"organization": aclaims.get("organization")})
    STATE["tadmin"] = {"browser": b, "user": user, "sub": aclaims["sub"]}

    # Funding a market takes the creator's credits: the admin tops up through the real deposit flow first.
    dep = deposit(step, atok["access_token"], 5000, "tenant admin")
    ok, pf = poll(lambda: portfolio(atok["access_token"]), lambda x: x.status == 200 and (credits_of(x)[1] or 0) > 0, timeout=60, interval=1)
    check(step, "trading credited the admin's deposit (portfolio)", ok, pf.ev())

    atok, aclaims = org_token(b, user, alias, step, "tenant admin (fresh)")
    close = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3 * 3600))
    body = {"question": f"Will the e2e run {RUN} pass every step?", "description": "Local end-to-end run against a real Keycloak", "closeTime": close, "startingPrice": 0.5, "fundingAmount": 1000, "sharing": {"whoMayShare": "admins", "linkTypes": ["profiles"]}}
    r = call("POST", f"{SVC['markets']}/v1/markets", body=body, token=atok["access_token"])
    if not check(step, "the tenant admin creates a market with a sharing policy (admins, profiles): 201 draft", r.status == 201, r.ev()):
        return False
    market = r.json
    STATE["market"] = market
    check(step, "the market returns its sharing policy", market.get("sharing") == {"whoMayShare": "admins", "linkTypes": ["profiles"]}, {"sharing": market.get("sharing")})
    r = call("POST", f"{SVC['markets']}/v1/markets/{market['id']}/open", token=atok["access_token"])
    check(step, "the creator opens the market (funding through trading's internal API): 200 or 202", r.status in (200, 202), r.ev())
    ok, m = poll(lambda: call("GET", f"{SVC['markets']}/v1/markets/{market['id']}", token=atok["access_token"]), lambda x: (x.json or {}).get("status") == "open", timeout=60, interval=1)
    check(step, "the market is open", ok, m.ev())
    r = call("POST", f"{SVC['markets']}/v1/markets/{market['id']}/share-links", body={"type": "profiles", "emails": [STATE["trader"]["user"]["email"]]}, token=atok["access_token"])
    if not check(step, "the admin makes a share link of type profiles for the verified user's address: 201 with token", r.status == 201 and (r.json or {}).get("token"), {"status": r.status, "body": {k: v for k, v in (r.json or {}).items() if k != "token"} if r.json else r.text[:300]}):
        return False
    share_token = r.json["token"]
    info(step, f"share link {r.json.get('id')} type {r.json.get('type')} emails {r.json.get('emails')} urlPath /share#<token> (token not printed)")
    trader = STATE["trader"]
    ttok = login(trader["browser"], trader["user"])
    r = call("POST", f"{SVC['markets']}/v1/shared-markets/lookup", body={"token": share_token}, token=ttok["access_token"])
    check(step, "the public lookup with the user's token answers 200 with market, organization and next", r.status == 200 and (r.json or {}).get("market", {}).get("id") == market["id"], r.ev())
    r0 = call("POST", f"{SVC['markets']}/v1/shared-markets/lookup", body={"token": share_token})
    info(step, f"lookup without a token: {r0.status} {clip(r0.json if r0.json is not None else r0.text, 300)}")
    r = call("POST", f"{SVC['tenants']}/v1/share-joins", body={"token": share_token}, token=ttok["access_token"])
    if not check(step, "the user joins through POST /v1/share-joins: 200, joined, role trader", r.status == 200 and (r.json or {}).get("role") == "trader", r.ev()):
        info(step, f"tenants log: {log_lines('tenants', 'share|admission|markets', 4)}")
        info(step, f"markets log: {log_lines('markets', 'admission|share', 4)}")
        return False
    ok, me = poll(lambda: call("GET", f"{SVC['tenants']}/v1/me", token=login(trader['browser'], trader['user'])["access_token"]), lambda x: x.status == 200 and x.json.get("identityPending") is False, timeout=60, interval=2)
    check(step, "the user's membership reached Keycloak (identityPending false)", ok, me.ev())
    _, tclaims = org_token(trader["browser"], trader["user"], alias, step, "trader")
    check(step, "the user's token for the organization holds the group /trader", groups_of(tclaims, alias) == ["/trader"], {"organization": tclaims.get("organization")})
    return True


def step_d():
    step = "d"
    alias = STATE["tenant"]["id"]
    trader = STATE["trader"]
    ttok, _ = org_token(trader["browser"], trader["user"], alias, step, "trader")
    before = portfolio(ttok["access_token"])
    info(step, f"portfolio before: {before.status} {clip(before.json, 300)}")
    dep = deposit(step, ttok["access_token"], 2000, "trader")
    if not dep:
        return False
    out = nats("stream", "subjects", "PAYMENTS")
    check(step, "DepositSucceeded is on the PAYMENTS stream for the tenant", f"chance.payments.DepositSucceeded.{alias}" in out, out[-900:])
    ok, pf = poll(lambda: portfolio(ttok["access_token"]), lambda x: x.status == 200 and (credits_of(x)[1] or 0) > 0, timeout=60, interval=1)
    check(step, "trading credited the user (portfolio shows the credits)", ok, {"credits": credits_of(pf), "status": pf.status})
    STATE["trader_credits_after_deposit"] = credits_of(pf)
    return ok


def step_e():
    step = "e"
    alias = STATE["tenant"]["id"]
    market = STATE["market"]
    trader = STATE["trader"]
    ttok, _ = org_token(trader["browser"], trader["user"], alias, step, "trader")
    def quote(amount):
        return call("GET", f"{SVC['trading']}/v1/trading/quote?marketId={market['id']}&side=YES&amount={amount}", token=ttok["access_token"])

    amount = 100
    ok, q = poll(lambda: quote(amount), lambda x: x.status == 200 or (x.status == 409 and ((x.json or {}).get("maxAmount") or 0) >= 1), timeout=90, interval=2)
    if q.status == 409:
        # The bot's offers on a market funded with 1000 credits hold less than 100 credits of YES (documented: a YES buy
        # beyond the offers is refused with insufficient_liquidity and the largest amount they absorb).
        info(step, f"a quote of 100 credits: 409 insufficient_liquidity, maxAmount {q.json.get('maxAmount')}; buying a smaller amount")
        amount = min(50, q.json["maxAmount"])
        q = quote(amount)
    ok = q.status == 200
    check(step, f"trading quotes {amount} credits of YES (the market maker posted offers)", ok, q.ev())
    if not ok:
        info(step, f"market-maker log: {log_lines('market-maker', 'quot|error|warn', 5)}")
        return False
    r = call("POST", f"{SVC['trading']}/v1/trading/orders", body={"marketId": market["id"], "side": "YES", "amount": amount}, token=ttok["access_token"], headers={"idempotency-key": str(uuid.uuid4())})
    if not check(step, f"the user buys YES for {amount} credits: 201", r.status == 201, r.ev()):
        return False
    seq = r.json.get("tradeSequence")
    info(step, f"order: tradeSequence {seq}, costMicro {r.json.get('costMicro')}, sharesMicro {r.json.get('sharesMicro')}, balanceMicro {r.json.get('balanceMicro')}")
    ok, md = poll(lambda: call("GET", f"{SVC['market-data']}/v1/market-data/markets/{market['id']}", token=ttok["access_token"]), lambda x: x.status == 200 and isinstance(seq, int) and ((x.json or {}).get("lastTradeSequence") or 0) >= seq, timeout=30, interval=1)
    if not ok:
        info(step, f"market-data answer: {clip(md.ev(), 900)}")
    check(step, "market-data shows the trade (its view of the market reached the trade's sequence)", ok, md.ev())
    hist = call("GET", f"{SVC['market-data']}/v1/market-data/markets/{market['id']}/history", token=ttok["access_token"])
    info(step, f"market-data history: {hist.status} {clip(hist.json if hist.json is not None else hist.text, 500)}")
    return ok


def step_f():
    step = "f"
    alias = STATE["tenant"]["id"]
    trader = STATE["trader"]
    ttok, _ = org_token(trader["browser"], trader["user"], alias, step, "trader")
    before = credits_of(portfolio(ttok["access_token"]))
    r = call("POST", f"{SVC['payments']}/v1/withdrawals", body={"amount": 500}, token=ttok["access_token"], headers={"idempotency-key": str(uuid.uuid4())})
    if not check(step, "POST /v1/withdrawals 500 is accepted (201, 202 or 200)", r.status in (200, 201, 202), r.ev()):
        return False
    wd = r.json
    ok, last = poll(lambda: call("GET", f"{SVC['payments']}/v1/withdrawals/{wd['id']}", token=ttok["access_token"]), lambda x: (x.json or {}).get("status") in ("paid_out", "rejected", "failed", "refunded"), timeout=60, interval=1)
    check(step, "the withdrawal ends paid_out", ok and last.json.get("status") == "paid_out", last.ev())
    check(step, "payments says its debits go over gRPC", bool(log_lines("payments", '"transport":"grpc"', 2)), log_lines("payments", "transport", 2))
    after = credits_of(portfolio(ttok["access_token"]))
    info(step, f"trader credits before {before}, after {after}")
    schema = "tenant_" + alias.replace("-", "")
    ledger = psql("trading", f"select type, business_key, meta::text from {schema}.ledger_events where business_key = '{wd['id']}'")
    check(step, "trading recorded the debit (ledger WithdrawalDebited, actor client:payments)", "WithdrawalDebited" in ledger and "client:payments" in ledger, ledger[:400])
    metrics = call("GET", f"{METRICS['trading']}/metrics", headers={"accept": "text/plain"}).text
    rest_debits = [x for x in metrics.splitlines() if "/v1/internal/debits" in x]
    check(step, "the debit did not use trading's REST endpoint (no /v1/internal/debits series in trading's metrics), so it came over gRPC", not rest_debits, rest_debits)
    info(step, f"trading log lines on gRPC (trading logs a successful gRPC debit at no level): {log_lines('trading', 'DebitCredits|grpcPort', 2)}")
    STATE["withdrawal"] = wd
    out = nats("stream", "subjects", "PAYMENTS")
    check(step, "WithdrawalSucceeded is on the PAYMENTS stream for the tenant", f"chance.payments.WithdrawalSucceeded.{alias}" in out, out[-900:])
    return ok


def step_g():
    step = "g"
    alias = STATE["tenant"]["id"]
    tad = STATE["tadmin"]
    atok, _ = org_token(tad["browser"], tad["user"], alias, step, "tenant admin")
    r = call("GET", f"{SVC['audit']}/v1/audit?limit=100", token=atok["access_token"])
    items = (r.json or {}).get("items") or (r.json or {}).get("entries") or []
    types = sorted({i.get("eventType") or i.get("type") for i in items})
    check(step, "GET /v1/audit answers 200 with the tenant's entries", r.status == 200 and len(items) > 0, {"status": r.status, "count": len(items), "types": types})
    for t in ("TenantCreated", "MemberJoined", "MarketOpened", "TradeExecuted", "DepositSucceeded", "WithdrawalSucceeded"):
        check(step, f"the audit log holds a {t} entry", any(t in (x or "") for x in types), types)
    v = call("GET", f"{SVC['audit']}/v1/audit/verify", token=atok["access_token"])
    check(step, "GET /v1/audit/verify: valid chain", v.status == 200 and (v.json or {}).get("valid") is True, v.ev())
    return r.status == 200


def step_h():
    step = "h"
    for name, base in METRICS.items():
        r = call("GET", f"{base}/metrics", headers={"accept": "text/plain"})
        total = 0.0
        for line in r.text.splitlines():
            if line.startswith("http_requests_total{") or line.startswith("http_requests_total "):
                try:
                    total += float(line.rsplit(" ", 1)[1])
                except ValueError:
                    pass
        check(step, f"{name}: /metrics on {base} answers 200 and http_requests_total sums to {int(total)} (> 0)", r.status == 200 and total > 0, {"status": r.status})
    # The provider's metrics port is not published by compose.yaml: read it inside its container.
    js = "fetch('http://127.0.0.1:9464/metrics').then(r=>r.text()).then(t=>{let s=0;for(const l of t.split('\\n'))if(l.startsWith('http_requests_total{'))s+=Number(l.split(' ').pop());console.log(s)})"
    cmd = ["docker", "compose", "-f", COMPOSE_FILE, "-f", f"{E2E}/compose.override.yaml", "exec", "-T", "payment-provider", "node", "-e", js]
    p = subprocess.run(cmd, capture_output=True, text=True, env=DOCKER_ENV, timeout=60)
    out = (p.stdout or p.stderr).strip()
    check(step, f"payment-provider: /metrics on its METRICS_PORT 9464 inside the container, http_requests_total sums to {out}", out.isdigit() and int(out) > 0, (p.stdout + p.stderr)[-300:])
    return True


def step_i():
    """Google is offered next to the password form. The local client is a placeholder, so nothing here reaches Google."""
    step = "i"
    idp, realm, flow, steps = kc_google()
    rep = idp.json or {}
    cfg = rep.get("config") or {}
    client_id = compose_value("GOOGLE_CLIENT_ID")
    shown = {k: rep.get(k) for k in ("alias", "providerId", "enabled", "trustEmail", "hideOnLogin", "linkOnly", "authenticateByDefault", "firstBrokerLoginFlowAlias")}
    shown["config"] = {k: cfg.get(k) for k in ("clientId", "defaultScope", "syncMode")}
    check(step, "the Admin REST API shows the provider google: enabled, trustEmail true, not hidden on the login page, the client id of compose.yaml, the scopes openid email profile, the first-login flow 'chance first broker login'",
          idp.status == 200 and rep.get("providerId") == "google" and rep.get("enabled") is True and rep.get("trustEmail") is True and rep.get("hideOnLogin") is not True
          and rep.get("authenticateByDefault") is not True and cfg.get("clientId") == client_id and sorted((cfg.get("defaultScope") or "").split()) == ["email", "openid", "profile"]
          and rep.get("firstBrokerLoginFlowAlias") == "chance first broker login",
          {"status": idp.status, "provider": shown})
    # The value is never printed: the evidence says only whether it is masked.
    check(step, "the Admin REST API masks the provider's client secret (never the value of compose.yaml)",
          bool(cfg.get("clientSecret")) and cfg.get("clientSecret") != compose_value("GOOGLE_CLIENT_SECRET"), {"masked": cfg.get("clientSecret") == "**********"})
    req = {s.get("providerId"): s.get("requirement") for s in steps if s.get("providerId")}
    check(step, "the first-login flow links a Google login to an existing account only after a confirmation (idp-confirm-link REQUIRED) and that account's password (idp-username-password-form REQUIRED); no mailed link, no profile review, never silently",
          req.get("idp-create-user-if-unique") == "ALTERNATIVE" and req.get("idp-confirm-link") == "REQUIRED" and req.get("idp-username-password-form") == "REQUIRED"
          and not {"idp-email-verification", "idp-review-profile", "idp-auto-link"} & set(req),
          {"flow": flow, "realm firstBrokerLoginFlow": realm.get("firstBrokerLoginFlow"), "steps": req})
    b = Browser("google")
    page, _, _ = login_page(b)
    p = Page(page.get("body", ""))
    broker = [urllib.parse.urljoin(page["url"], h) for h in p.links if "/realms/chance/broker/google/login" in h]
    if not check(step, "the login page offers Google next to the login form (a link to /realms/chance/broker/google/login)", bool(broker) and p.form("kc-form-login") is not None,
                 {"links": [h.split("?")[0] for h in p.links][:12], "forms": [f["id"] for f in p.forms]}):
        return False
    status, headers, _ = b.request("GET", broker[0])
    target = urllib.parse.urlparse(headers.get("Location") or "")
    q = urllib.parse.parse_qs(target.query)
    # Keycloak's own redirect is checked, Google is never called: the redirect URI is the one the README asks to register.
    check(step, "the Google link sends the browser to Google's authorization endpoint with the client id of compose.yaml, the redirect URI .../realms/chance/broker/google/endpoint and the scopes openid email profile (the redirect is not followed)",
          status in (302, 303) and target.netloc == "accounts.google.com" and target.path == "/o/oauth2/v2/auth" and q.get("client_id") == [client_id]
          and q.get("redirect_uri") == [f"{ISSUER}/broker/google/endpoint"] and sorted((q.get("scope") or [""])[0].split()) == ["email", "openid", "profile"] and q.get("response_type") == ["code"],
          {"status": status, "to": f"{target.scheme}://{target.netloc}{target.path}", "client_id": q.get("client_id"), "redirect_uri": q.get("redirect_uri"), "scope": q.get("scope"), "response_type": q.get("response_type")})
    return True


def step_j():
    """A password reset through the reset mail: the owner of an address takes an account back (the trader of step b)."""
    step = "j"
    trader = STATE.get("trader")
    if trader:
        user = dict(trader["user"])
    else:
        info(step, "no user from step b in this run: a new user signs up first")
        _, user, _, _ = new_user(step, "reset")
    old = user["password"]
    b = Browser("reset")
    log = []
    page, link, verifier, state = reset_link_page(b, user, log)
    if not check(step, "the login page with the password field offers 'Forgot Password?' (a link to /login-actions/reset-credentials)", bool(link),
                 {"flow": log, "links": [h.split("?")[0] for h in Page(page.get("body", "")).links][:12]}):
        return False
    page = b.walk("GET", link)
    p = Page(page.get("body", ""))
    f = p.form("kc-reset-password-form")
    if not check(step, "the link opens Keycloak's reset form (kc-reset-password-form with the field username)", f is not None and "username" in f["inputs"], {"forms": [x["id"] for x in p.forms], "text": p.text[:200]}):
        return False
    before = {m.get("ID") for m in mails_to(user["email"])}
    sent = b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], username=user["username"]))
    info(step, f"after the reset form: {Page(sent.get('body', '')).text[:160]}")
    ok, mails = poll(lambda: [m for m in mails_to(user["email"]) if m.get("ID") not in before], lambda ms: len(ms) > 0, timeout=60, interval=1)
    if not check(step, f"the reset mail for {user['email']} arrived in Mailpit (the realm's mail server)", ok, {"new mails": [(m.get("ID"), m.get("Subject")) for m in mails or []]}):
        return False
    msg, links = action_link(mails[0]["ID"])
    check(step, "the reset mail holds an action-token link to Keycloak", bool(links), {"subject": msg.get("Subject"), "from": (msg.get("From") or {}).get("Address"), "links": [x[:90] for x in links]})
    if not links:
        return False
    page = b.walk("GET", links[0])
    log.append("followed the reset mail's link")
    finished = None
    try:
        # The same browser goes on: Keycloak asks for the new password (drive sets one and keeps it in user["password"]).
        finished = drive(b, user, page, verifier, state, log)
    except RuntimeError as e:
        if not re.search(r"updated", str(e), re.I):
            raise
        log.append(f"info page after the reset: {str(e)[-120:]}")
    after = log[log.index("followed the reset mail's link"):]
    check(step, "following the link asks for a new password and sets it (the password update form)", user["password"] != old and any(x.startswith("password update form") for x in after),
          {"flow": after, "redirected to the app with a code": bool(finished)})
    kc, creds = kc_user(user["username"])
    check(step, "Keycloak holds a password credential for the user and no required action after the reset", kc is not None and "password" in creds and not (kc.get("requiredActions") or []),
          {"credentials": creds, "requiredActions": (kc or {}).get("requiredActions")})
    refused = []
    try:
        login(Browser("reset-old"), dict(user, password=old), log=refused)
        old_refused = False
    except LoginRejected as e:
        refused.append(f"rejected: {str(e)[:120]}")
        old_refused = True
    check(step, "the old password is refused after the reset", old_refused, refused)
    fresh = []
    try:
        new_works = bool(login(Browser("reset-new"), user, log=fresh).get("access_token"))
    except Exception as e:  # a refused login: the evidence says why
        fresh.append(f"{type(e).__name__}: {str(e)[:160]}")
        new_works = False
    check(step, "the new password logs in (a new browser, code flow with PKCE)", new_works, fresh)
    if trader:
        trader["user"]["password"] = user["password"]
    return new_works


# ----------------------------------------------------------------------------- step k: account takeover attempts -----
# The stand-in for Google is the realm e2e-upstream of this same local Keycloak and the provider e2e-upstream in chance,
# set up like google (trustEmail, syncMode IMPORT, the first-login flow "chance first broker login"); run.sh creates
# both through kcadm (cmd_upstream), never the realm file. Attacker and victim each use their own Browser (cookie store).
UPSTREAM_IDP = "e2e-upstream"
WARNING = "If you did not create this account, do not click the link and ignore this mail"


def upstream_user(label, email, verified=True, last="E2e"):
    """A person's account at the stand-in provider: its user name (the provider's preferred_username), address and password."""
    up = {"username": f"up-{label}-{RUN}", "email": email, "password": "Up-" + secrets.token_hex(8) + "-Aa1"}
    rep = {"username": up["username"], "email": email, "emailVerified": verified, "enabled": True, "firstName": label.capitalize(),
           "credentials": [{"type": "password", "value": up["password"], "temporary": False}]}
    if last:
        rep["lastName"] = last
    r = call("POST", f"{KC}/admin/realms/e2e-upstream/users", body=rep, token=admin_token())
    if r.status != 201:
        raise RuntimeError(f"upstream user {label}: {r.ev()}")
    return up


def kc_links(username):
    """The providers linked to the chance user (Admin REST API); None when there is no such user."""
    at = admin_token()
    users = call("GET", f"{KC}/admin/realms/chance/users?username={urllib.parse.quote(username)}&exact=true", token=at).json or []
    if not users:
        return None
    return [x.get("identityProvider") for x in call("GET", f"{KC}/admin/realms/chance/users/{users[0]['id']}/federated-identity", token=at).json or []]


def kc_users_by_email(email):
    return call("GET", f"{KC}/admin/realms/chance/users?email={urllib.parse.quote(email)}&exact=true", token=admin_token()).json or []


def broker_login(label, up):
    """The provider's button on the login page, then the provider's login; returns the browser, its log and the first page of chance after it."""
    b, log = Browser(label), []
    page, verifier, state = login_page(b)
    p = Page(page.get("body", ""))
    links = [urllib.parse.urljoin(page["url"], h) for h in p.links if f"/realms/chance/broker/{UPSTREAM_IDP}/login" in h]
    if not links:
        raise RuntimeError(f"no {UPSTREAM_IDP} button on the login page: {[h.split('?')[0] for h in p.links][:12]}")
    page = b.walk("GET", links[0])
    for _ in range(2):
        if "redirect" in page or "/realms/e2e-upstream/" not in page.get("url", ""):
            break
        f = Page(page["body"]).form("kc-form-login")
        if not f:
            raise RuntimeError(f"unexpected provider page at {page.get('url')}: {Page(page['body']).text[:200]}")
        log.append("provider login form")
        page = b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], username=up["username"], password=up["password"]))
    return b, log, page, verifier, state


def confirm_form(page):
    """The 'Account already exists' page of the first-login flow (its form, whose buttons are submitAction values)."""
    if "redirect" in page or "/first-broker-login" not in page.get("url", ""):
        return None
    p = Page(page.get("body", ""))
    return p.form("kc-register-form") if re.search(r"already exists", p.text, re.I) else None


def asks_password(page):
    f = Page(page.get("body", "")).form("kc-form-login")
    return f is not None and "password" in f["inputs"]


def link_attempt(label, up, watched):
    """A provider login whose address or user name belongs to an existing account: 'Add to existing account', then the page it leads to."""
    b, log, page, verifier, state = broker_login(label, up)
    out = {"browser": b, "log": log, "verifier": verifier, "state": state, "first": Page(page.get("body", "")).text[:160], "url": page.get("url"), "page": page}
    f = confirm_form(page)
    out["confirm"] = f is not None
    if f is None:
        return out
    before = {m.get("ID") for m in mails_to(watched)}
    out["page"] = b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], submitAction="linkAccount"))
    out["new_mail"] = [m.get("Subject") for m in new_mails(watched, before)]
    return out


def new_mails(address, before, deadline=15):
    """The mails to the address that arrived after `before`, polled until one arrives or the deadline passes.

    A check that expects no mail waits the whole deadline: Keycloak sends from the request thread, so a mail that the
    request triggered is in Mailpit within a few seconds.
    """
    _, mails = poll(lambda: [m for m in mails_to(address) if m.get("ID") not in before], lambda ms: len(ms) > 0, timeout=deadline, interval=1)
    return mails or []


RESTARTED = re.compile(r"expired|timed out|start (the login )?over|restart|sign in to your account|log in to your account", re.I)


def restarted_or_expired(page):
    """Keycloak sent the browser back to the start: the login form, or a page saying the session expired or restarted."""
    if not page or "redirect" in page:
        return False
    p = Page(page.get("body", ""))
    return p.form("kc-form-login") is not None or bool(RESTARTED.search(p.text))


def newest_link(address, before, timeout=60):
    ok, mails = poll(lambda: [m for m in mails_to(address) if m.get("ID") not in before], lambda ms: len(ms) > 0, timeout=timeout, interval=1)
    if not ok:
        return None, None
    msg, links = action_link(mails[0]["ID"])
    return msg, (links[0] if links else None)


def answer(browser, page, user, log, verifier=None, state=None):
    """Answers Keycloak's pages (drive) and returns the token, or None when it ends on an info page or asks for a mail."""
    try:
        return drive(browser, user, page, verifier, state, log)
    except AuthNeedsMail:
        return "mail"
    except RuntimeError as e:
        log.append(f"ended on a page: {str(e)[-200:]}")
        return None


def squat(step, name, label, email):
    """The attacker signs up with the victim's address and stops at the verify-email page; returns its browser and that page's URL."""
    b, log = Browser(f"attacker-{label}"), []
    user = {"username": f"squat-{label}-{RUN}", "email": email, "password": None, "first": "Squat", "last": "E2e"}
    try:
        register(b, user, log)
        waiting = False
    except AuthNeedsMail:
        waiting = True
    form = next((x for x in log if x.startswith("registration form")), "")
    kc, creds = kc_user(user["username"])
    resume = [urllib.parse.urljoin(b.last_url or KC, h) for h in Page(b.last_body).links if "/login-actions/required-action" in h]
    url = resume[0] if resume else b.last_url
    # The waiting page itself is asserted, so the later "stays out" check cannot pass on a session that never waited.
    on_verify_page = waiting and bool(url) and bool(re.search(r"verify your email|email verification|verify email", Page(b.last_body).text, re.I))
    check(step, f"{name}: the attacker's sign-up with the victim's address ends on the verify-email page, with no password field and no password stored", on_verify_page and bool(form) and "'password'" not in form and kc is not None and "password" not in creds,
          {"flow": log, "credentials": creds, "page": url})
    return b, url, user


def waiting_session_stays_out(step, name, attacker, url):
    """The attacker's waiting sign-up session after the victim acted: Keycloak restarts it (login form) or calls it expired."""
    if not url:
        check(step, f"{name}: the attacker's waiting sign-up session does not go on into the account", False, "no URL of the waiting page: the attacker never reached it")
        return
    again = attacker.walk("GET", url)
    p = Page(again.get("body", ""))
    check(step, f"{name}: the attacker's waiting sign-up session does not go on into the account: Keycloak restarts it or calls it expired (no code for the web app, no password form)",
          restarted_or_expired(again) and p.form("kc-passwd-update-form") is None,
          {"redirect": again.get("redirect", "")[:80], "url": again.get("url"), "forms": [x["id"] for x in p.forms], "page": p.text[:200]})


def k_owner_links(step):
    """The legitimate path: a person signs up, verifies and links the provider to their own account with its password."""
    name = "legitimate link"
    _, user, _, claims = new_user(step, "kowner")
    up = upstream_user("owner", user["email"])
    out = link_attempt("k-owner", up, user["email"])
    if not check(step, f"{name}: the provider login with the owner's address shows 'Account already exists' and 'Add to existing account' asks for the account's password", out["confirm"] and asks_password(out["page"]),
                 {"first": out["first"], "after": Page(out["page"].get("body", "")).text[:160]}):
        return
    page = out["page"]
    f = Page(page["body"]).form("kc-form-login")
    page = out["browser"].walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], username=user["username"], password=user["password"]))
    tok = answer(out["browser"], page, user, out["log"], out["verifier"], out["state"])
    sub = jwt_claims(tok["access_token"]).get("sub") if isinstance(tok, dict) else None
    check(step, f"{name}: with the owner's password the provider is linked to the owner's account and the login ends with a code for the web app", sub == claims.get("sub") and UPSTREAM_IDP in (kc_links(user["username"]) or []),
          {"flow": out["log"], "sub matches": sub == claims.get("sub"), "links": kc_links(user["username"])})


def k_t1(step):
    """T1: the address was signed up by someone else; the owner's provider login must not leave a stranger's credential."""
    name = "T1"
    email = f"k-victim1-{RUN}@example.com"
    attacker, waiting, squatter = squat(step, name, "t1", email)
    up = upstream_user("victim1", email)
    out = link_attempt("k-victim1", up, email)
    page = out["page"]
    check(step, f"{name}: the provider login finds the account by address; 'Add to existing account' asks for that account's password and mails no link (no idp-email-verification)",
          out["confirm"] and asks_password(page) and not out.get("new_mail"), {"first": out["first"], "after": Page(page.get("body", "")).text[:160], "new mail": out.get("new_mail")})
    reset = [urllib.parse.urljoin(page["url"], h) for h in Page(page.get("body", "")).links if "/login-actions/reset-credentials" in h] if "url" in page else []
    if not check(step, f"{name}: the password page offers 'Forgot Password?' to the owner, who knows no password of this account", bool(reset), {"links": [h.split("?")[0] for h in Page(page.get("body", "")).links][:12]}):
        return
    b, log = out["browser"], out["log"]
    before = {m.get("ID") for m in mails_to(email)}
    page = b.walk("GET", reset[0])
    f = Page(page.get("body", "")).form("kc-reset-password-form")
    if f:
        page = b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], username=email))
    msg, link = newest_link(email, before)
    if not check(step, f"{name}: the reset mail goes to the account's address", bool(link), {"subject": (msg or {}).get("Subject")}):
        return
    owner = dict(squatter, password=None, logout_sessions=True)
    tok = answer(b, b.walk("GET", link), owner, log, out["verifier"], out["state"])
    if tok == "mail":  # the address is still unverified: the owner follows the verification mail too
        msg, link = newest_link(email, before | {m.get("ID") for m in mails_to(email) if (m.get("Subject") or "").lower().startswith("reset")})
        tok = answer(b, b.walk("GET", link), owner, log, out["verifier"], out["state"]) if link else None
    kc, creds = kc_user(squatter["username"])
    check(step, f"{name}: the owner sets a new password through the reset mail, the provider is linked, and the account holds only that password",
          isinstance(tok, dict) and any(x.startswith("password update form") for x in log) and UPSTREAM_IDP in (kc_links(squatter["username"]) or []) and creds.count("password") == 1,
          {"flow": log[-8:], "links": kc_links(squatter["username"]), "credentials": creds, "emailVerified": (kc or {}).get("emailVerified")})
    waiting_session_stays_out(step, name, attacker, waiting)


def k_t3(step):
    """T2/T3: the owner of the address follows the verification mail that the attacker's sign-up triggered."""
    name = "T2/T3"
    email = f"k-victim3-{RUN}@example.com"
    attacker, waiting, squatter = squat(step, name, "t3", email)
    msg, link = newest_link(email, set())
    text = (msg or {}).get("Text") or ""
    check(step, f"{name}: the verification mail goes to the victim's address and tells a person who did not sign up not to follow the link", bool(link) and WARNING in text, {"subject": (msg or {}).get("Subject"), "text": clip(text, 400)})
    if not link:
        return
    b, log = Browser("k-victim3"), []
    owner = dict(squatter, logout_sessions=True)
    answer(b, b.walk("GET", link), owner, log)
    kc, creds = kc_user(squatter["username"])
    check(step, f"{name}: whoever follows the link chooses the password (UPDATE_PASSWORD after the link); the address is then verified and the account holds that one password",
          any(x.startswith("password update form") for x in log) and (kc or {}).get("emailVerified") is True and creds.count("password") == 1, {"flow": log, "emailVerified": (kc or {}).get("emailVerified"), "credentials": creds})
    waiting_session_stays_out(step, name, attacker, waiting)
    fresh = []
    try:
        works = bool(login(Browser("k-victim3-fresh"), owner, log=fresh).get("access_token"))
    except Exception as e:  # a refused login: the evidence says why
        fresh.append(f"{type(e).__name__}: {str(e)[:160]}")
        works = False
    check(step, f"{name}: the owner logs in with the password they chose", works, fresh)


def k_t4(step):
    """T4: the attacker's own provider login tries to put the victim's address on the account it creates."""
    name = "T4"
    victim = f"k-victim4-{RUN}@example.com"
    up = upstream_user("attacker4", f"k-attacker4-{RUN}@example.com", last=None)
    b, log, page, verifier, state = broker_login("k-attacker4", up)
    seen = []
    for attempt in range(5):
        if "redirect" in page:
            break
        p = Page(page.get("body", ""))
        f = p.form("kc-update-profile-form", "kc-idp-review-profile-form")
        u = p.form("kc-update-email-form")
        seen.append({"url": page.get("url", "").split("?")[0], "forms": [x["id"] for x in p.forms], "fields": sorted(f["inputs"]) if f else None, "text": p.text[:120]})
        if u:
            # A posted address on the profile form becomes Keycloak's verified e-mail change (UPDATE_EMAIL, run 3 of
            # 2026-11-12): the attacker goes on with the victim's address there, which only mails the victim.
            page = b.walk("POST", urllib.parse.urljoin(page["url"], u["action"]), dict(u["inputs"], email=victim))
            continue
        if not f:
            break
        # First with the victim's address added to the form (the attack), then as the form asks (the attacker goes on).
        extra = {"email": victim} if attempt == 0 else {}
        page = b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], firstName="Attacker", lastName="E2e", **extra))
    # A provider-created user is named by the mapper (e2e-upstream.<sub>), so it is found by its address.
    kc = (kc_users_by_email(up["email"]) or [None])[0]
    check(step, f"{name}: no profile review in the first login and no address field on the profile form after it; a posted address is refused and the account keeps the provider's address",
          not any("/first-broker-login" in s["url"] and s["fields"] for s in seen) and not any("email" in (s["fields"] or []) for s in seen) and (kc or {}).get("email") == up["email"] and not kc_users_by_email(victim),
          {"pages": seen, "email": (kc or {}).get("email"), "finished": "redirect" in page})
    # The same through the profile form the web app may open later (kc_action UPDATE_PROFILE). That request needs a
    # signed-in session, and the login above never gets one: it ends on "Confirmation email sent" (run 4 of 2026-11-12,
    # where the request therefore showed the login form). A second account of the attacker signs in plainly first.
    up_c = upstream_user("attacker4c", f"k-attacker4c-{RUN}@example.com")
    bc, logc, page, verifier, state = broker_login("k-attacker4c", up_c)
    tok = answer(bc, page, {"username": None, "password": None, "email": up_c["email"]}, logc, verifier, state)
    signed_in = check(step, f"{name} (setup): a second attacker account's own provider login ends with a code for the web app (the signed-in session the kc_action request needs)",
                      isinstance(tok, dict) and bool(tok.get("access_token")), {"flow": logc[-6:], "result": "token" if isinstance(tok, dict) else tok})
    f = posted = None
    page = {}
    if signed_in:
        verifier = b64url(secrets.token_bytes(32))
        page = bc.walk("GET", authorize_url("openid", b64url(hashlib.sha256(verifier.encode()).digest()), secrets.token_urlsafe(12)) + "&kc_action=UPDATE_PROFILE")
        f = Page(page.get("body", "")).form("kc-update-profile-form")
        if f:
            posted = bc.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], email=victim, firstName="Attacker", lastName="E2e"))
    kc = (kc_users_by_email(up_c["email"]) or [None])[0]
    check(step, f"{name}: the update-profile form (kc_action UPDATE_PROFILE) of a signed-in attacker shows no address field and a posted address changes nothing",
          f is not None and "email" not in f["inputs"] and (kc or {}).get("email") == up_c["email"] and not kc_users_by_email(victim),
          {"signed in": signed_in, "fields": sorted(f["inputs"]) if f else None, "page": None if f else ("not requested: no signed-in session" if not page else "redirect" if "redirect" in page else Page(page.get("body", "")).text[:160]),
           "after the post": None if posted is None else ("redirect" if "redirect" in posted else Page(posted.get("body", "")).text[:120]), "email": (kc or {}).get("email")})
    # The review page forced from 'Account already exists' (submitAction updateProfile), with an address the provider has not verified.
    _, _, owner_user = squat(step, f"{name} (setup)", "t4", victim)
    up2 = upstream_user("attacker4b", victim, verified=False)
    b2, log2, page, _, _ = broker_login("k-attacker4b", up2)
    f = confirm_form(page)
    after = b2.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], submitAction="updateProfile")) if f else page
    forms = Page(after.get("body", "")).forms
    # Explicitly the same 'Account already exists' page again (the flow restarts without a review step), not an error page.
    check(step, f"{name}: 'Review profile' on 'Account already exists' leads back to that page and opens no form with an address field (the flow has no review step)",
          f is not None and confirm_form(after) is not None and not any("email" in x["inputs"] for x in forms),
          {"forms": [(x["id"], sorted(x["inputs"])) for x in forms], "url": (after.get("url") or "").split("?")[0], "page": Page(after.get("body", "")).text[:160]})
    check(step, f"{name}: the provider login with an unverified copy of the address is not linked to the account", UPSTREAM_IDP not in (kc_links(owner_user["username"]) or []), {"links": kc_links(owner_user["username"])})


def k_t5(step):
    """T5: a signed-in attacker changes the account's address to the victim's."""
    name = "T5"
    victim = f"k-victim5-{RUN}@example.com"
    b, user, tok, _ = new_user(step, "kattacker5")
    r = call("POST", f"{ISSUER}/account/", body={"username": user["username"], "email": victim, "firstName": "Kattacker5", "lastName": "E2e"}, token=tok["access_token"])
    kc, _ = kc_user(user["username"])
    check(step, f"{name}: the Account REST API (account console) refuses a new address; the address stays", r.status >= 400 and (kc or {}).get("email") == user["email"], {"status": r.status, "body": clip(r.text, 200), "email": (kc or {}).get("email")})
    # No path lets a user change their own address (review finding 1): the e-mail change mails no link, and a link that a
    # mail might still hold expires after one second, so the victim's click moves nothing.
    before = {m.get("ID") for m in mails_to(victim)}
    verifier = b64url(secrets.token_bytes(32))
    page = b.walk("GET", authorize_url("openid", b64url(hashlib.sha256(verifier.encode()).digest()), secrets.token_urlsafe(12)) + "&kc_action=UPDATE_EMAIL")
    f = Page(page.get("body", "")).form("kc-update-email-form")
    if f:
        b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], email=victim))
    mails = new_mails(victim, before, deadline=20)
    msg, links = action_link(mails[0]["ID"]) if mails else ({}, [])
    clicked = None
    if links:  # the victim follows it from their own browser, as in the review's attack
        time.sleep(2)
        clicked = Browser("k-victim5").walk("GET", links[0])
    kc, _ = kc_user(user["username"])
    check(step, f"{name}: the e-mail change (kc_action UPDATE_EMAIL) moves nobody to the victim's address: its mail holds no link (or the link changes nothing), and the account keeps its address",
          (kc or {}).get("email") == user["email"] and (kc or {}).get("emailVerified") is True and not kc_users_by_email(victim) and not links,
          {"form": f is not None, "mail": (msg or {}).get("Subject"), "text": clip((msg or {}).get("Text") or "", 240), "links": [x[:60] for x in links],
           "after the click": Page((clicked or {}).get("body", "")).text[:120] if clicked else None, "email": (kc or {}).get("email")})


def post_signup(label, fields, from_login_page=False):
    """A sign-up POST with the given fields: from the sign-up form's own action, or (from_login_page) straight to the
    sign-up link of a session that only showed the login page. Returns the page Keycloak answers with."""
    b, log = Browser(label), []
    if from_login_page:
        page, _, _ = login_page(b)
        reg = [urllib.parse.urljoin(page["url"], h) for h in Page(page.get("body", "")).links if "/login-actions/registration" in h]
        return b.walk("POST", reg[0], fields) if reg else page
    page, _, _ = registration_page(b, log)
    f = Page(page.get("body", "")).form("kc-register-form")
    return b.walk("POST", urllib.parse.urljoin(page["url"], f["action"]), dict(f["inputs"], **fields)) if f else page


def k_password_fields(step):
    """Review finding 8: password fields added to the sign-up POST store no password."""
    name = "T3 (password fields)"
    for label, from_login in (("form", False), ("login-page", True)):
        user = f"kpw-{label}-{RUN}"
        pw = "Pw-" + secrets.token_hex(8) + "-Aa1"
        page = post_signup(f"k-pw-{label}", {"username": user, "email": f"{user}@example.com", "firstName": "Pw", "lastName": "E2e", "password": pw, "password-confirm": pw}, from_login)
        kc, creds = kc_user(user)
        check(step, f"{name}: a sign-up POST with password fields ({'the sign-up form' if not from_login else 'a session that only showed the login page'}) stores no password",
              "password" not in creds, {"user created": kc is not None, "credentials": creds, "requiredActions": (kc or {}).get("requiredActions"), "page": Page(page.get("body", "")).text[:120]})


def k_t9(step):
    """T9: a stranger's user name must not catch the victim's first provider login."""
    name = "T9"
    victim_email = f"k-victim9-{RUN}@example.com"
    # (a) A user name that is the victim's address is refused (user profile: no "@" in a user name).
    page = post_signup("k-stranger9a", {"username": victim_email, "email": f"k-stranger9a-{RUN}@example.com", "firstName": "Stranger", "lastName": "E2e"})
    kc, _ = kc_user(victim_email)
    check(step, f"{name}: a sign-up whose user name is the victim's address is refused (the form comes back, no user)",
          kc is None and Page(page.get("body", "")).form("kc-register-form") is not None, {"page": Page(page.get("body", "")).text[:200], "user created": kc is not None})
    # (b) A stranger takes the victim's user name at the provider; the provider-created user is named e2e-upstream.<sub>.
    _, stranger, _, _ = new_user(step, "up-victim9")  # the user name up-victim9-<run>, the victim's name at the provider
    up = upstream_user("victim9", victim_email)
    b, log, page, verifier, state = broker_login("k-victim9", up)
    tok = answer(b, page, {"username": None, "password": None, "email": victim_email}, log, verifier, state)
    own = (kc_users_by_email(victim_email) or [None])[0]
    check(step, f"{name}: the victim's first provider login creates the victim's own account, named {UPSTREAM_IDP}.<sub>, not the stranger's with the same provider user name",
          confirm_form(page) is None and isinstance(tok, dict) and own is not None and str(own.get("username", "")).startswith(f"{UPSTREAM_IDP}.") and UPSTREAM_IDP not in (kc_links(stranger["username"]) or []),
          {"first page": Page(page.get("body", "")).text[:120], "username": (own or {}).get("username"), "flow": log[-4:], "stranger's links": kc_links(stranger["username"])})


def k_t7(step):
    """T7: a provider account with the platform admin's address. Step a has changed the admin's password before this step."""
    name = "T7"
    admin = compose_value("PLATFORM_ADMIN_USERNAME")
    address = compose_value("PLATFORM_ADMIN_EMAIL")
    kc, _ = kc_user(admin)
    state = "before the admin's first login" if "UPDATE_PASSWORD" in ((kc or {}).get("requiredActions") or []) else "after the admin's first login (step a)"
    up = upstream_user("admin7", address)
    out = link_attempt("k-admin7", up, address)
    check(step, f"{name} ({state}): a provider login with the platform admin's address asks for the admin's own password, mails no link, and links nothing",
          out["confirm"] and asks_password(out["page"]) and not out.get("new_mail") and UPSTREAM_IDP not in (kc_links(admin) or []),
          {"first": out["first"], "new mail": out.get("new_mail"), "links": kc_links(admin), "requiredActions": (kc or {}).get("requiredActions")})
    # Review finding 3: "Forgot Password?" there fills in the admin and its reset finishes the link, so the admin's mailbox
    # is the proof. The harness asserts that the reset mail goes to the admin's address only, and does not follow it.
    page = out["page"]
    reset = [urllib.parse.urljoin(page["url"], h) for h in Page(page.get("body", "")).links if "/login-actions/reset-credentials" in h] if page.get("url") else []
    every = lambda: (call("GET", f"{MAILPIT}/api/v1/messages?limit=100").json or {}).get("messages", [])  # noqa: E731
    before = {m.get("ID") for m in every()}
    if reset:
        # Keycloak shows the reset form with the admin filled in (ResetCredentialChooseUser); the attacker submits it.
        form_page = out["browser"].walk("GET", reset[0])
        f = Page(form_page.get("body", "")).form("kc-reset-password-form")
        if f:
            out["browser"].walk("POST", urllib.parse.urljoin(form_page["url"], f["action"]), dict(f["inputs"], username=f["inputs"].get("username") or address))
    _, mails = poll(lambda: [m for m in every() if m.get("ID") not in before], lambda ms: len(ms) > 0, timeout=20, interval=1)
    to = sorted({(t.get("Address") or "").lower() for m in mails or [] for t in m.get("To") or []})
    check(step, f"{name} ({state}): 'Forgot Password?' on that page mails the reset link to the admin's address only (the admin's mailbox is the proof)",
          bool(reset) and to == [address.lower()] and UPSTREAM_IDP not in (kc_links(admin) or []), {"reset link": bool(reset), "recipients": to, "subjects": [m.get("Subject") for m in mails or []]})


def step_k():
    """Account takeover attempts (README 'Account takeover protections'): each must fail, the legitimate paths must work."""
    step = "k"
    idp = call("GET", f"{KC}/admin/realms/chance/identity-provider/instances/{UPSTREAM_IDP}", token=admin_token())
    rep = idp.json or {}
    if not check(step, "the stand-in provider e2e-upstream is set up like google (trustEmail, IMPORT, the first-login flow 'chance first broker login'; created by run.sh upstream)",
                 idp.status == 200 and rep.get("trustEmail") is True and (rep.get("config") or {}).get("syncMode") == "IMPORT" and rep.get("firstBrokerLoginFlowAlias") == "chance first broker login",
                 {"status": idp.status, "provider": {k: rep.get(k) for k in ("alias", "providerId", "trustEmail", "firstBrokerLoginFlowAlias")}}):
        return False
    mappers = call("GET", f"{KC}/admin/realms/chance/identity-provider/instances/{UPSTREAM_IDP}/mappers", token=admin_token()).json or []
    check(step, "the stand-in provider has the same user name mapper as google (oidc-username-idp-mapper, e2e-upstream.${CLAIM.sub}, target LOCAL)",
          [(m.get("identityProviderMapper"), (m.get("config") or {}).get("template"), (m.get("config") or {}).get("target")) for m in mappers] == [("oidc-username-idp-mapper", UPSTREAM_IDP + ".${CLAIM.sub}", "LOCAL")],
          {"mappers": [(m.get("identityProviderMapper"), m.get("config")) for m in mappers]})
    for scenario in (k_owner_links, k_t1, k_t3, k_password_fields, k_t4, k_t5, k_t9, k_t7):
        try:
            scenario(step)
        except Exception:
            check(step, f"{scenario.__name__}: ran to its end without an exception", False, traceback.format_exc()[-590:])
    return True


def main():
    which = sys.argv[1:] or list("abcdefghijk")
    steps = {"a": step_a, "b": step_b, "c": step_c, "d": step_d, "e": step_e, "f": step_f, "g": step_g, "h": step_h, "i": step_i, "j": step_j, "k": step_k}
    for s in which:
        print(f"\n== step {s}", flush=True)
        try:
            steps[s]()
        except Exception as e:
            check(s, "the step ran to its end without an exception", False, traceback.format_exc()[-590:])
    with open(f"{E2E}/results.json", "w", encoding="utf-8") as fh:
        json.dump({"run": RUN, "results": RESULTS, "tenant": STATE.get("tenant"), "market": (STATE.get("market") or {}).get("id")}, fh, indent=1, default=str)
    fails = [r for r in RESULTS if r.get("ok") is False]
    print(f"\n{len([r for r in RESULTS if r.get('ok')])} PASS, {len(fails)} FAIL", flush=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
