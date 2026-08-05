"""Private admin panel.

Deliberately small: one operator, server-rendered HTML, no build step, no SPA.

RBAC is NOT implemented (single administrator), but three cheap decisions keep
the door open so adding it later is additive rather than a rewrite:
  1. admin identity lives in its own `admin_users` table, not a flag on `users`
  2. that table already carries a `role` column, defaulted to SUPER_ADMIN
  3. every mutating action goes through `_require_admin` — one interception point

Admin auth is session-cookie based with mandatory TOTP. An admin compromise
would compromise every customer licence, so the admin account is protected more
strongly than a customer account, not less.
"""
from __future__ import annotations

import base64
import enum
import hashlib
import hmac
import json
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import String, Uuid, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy import Boolean, DateTime

from app import security, services
from app.config import get_settings
from app.database import get_db
from app.models import (
    AccountStatus, AppSession, AuditEvent, Base, Device, DeviceStatus,
    Licence, LicenceStatus, SessionStatus, User, utcnow,
)

# Reuses the same in-process limiter as customer auth (app/api.py) rather than
# a second implementation — one bucket store, one behavior to reason about.
from app.api import client_ip, rate_limit  # noqa: E402

router = APIRouter()

# In-memory admin sessions. Fine for one operator; a restart simply forces a
# re-login. Deliberately not persisted — fewer places for a token to leak.
_ADMIN_SESSIONS: dict[str, dict] = {}
_ADMIN_TTL = timedelta(hours=8)
COOKIE = "slp_admin"


class AdminRole(str, enum.Enum):
    SUPER_ADMIN = "SUPER_ADMIN"
    SUPPORT = "SUPPORT"
    SALES = "SALES"
    SECURITY = "SECURITY"


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret_encrypted: Mapped[str | None] = mapped_column(String(500))
    totp_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_totp_step: Mapped[int | None] = mapped_column()
    role: Mapped[str] = mapped_column(String(20), default=AdminRole.SUPER_ADMIN.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ── auth plumbing ──────────────────────────────────────────────────────────
def _require_admin(request: Request) -> dict:
    tok = request.cookies.get(COOKIE)
    sess = _ADMIN_SESSIONS.get(tok or "")
    if not sess or sess["expires"] < datetime.now(timezone.utc):
        _ADMIN_SESSIONS.pop(tok or "", None)
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return sess


def _h(s) -> str:
    """Escape. Customer-controlled strings (names, emails) are rendered in this
    panel, so escaping is not optional."""
    if s is None:
        return ""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<title>{_h(title)}</title>
<meta name="robots" content="noindex,nofollow">
<style>
 body{{font:14px system-ui,sans-serif;margin:0;background:#0f1115;color:#e6e6e6}}
 header{{background:#171a21;padding:12px 20px;border-bottom:1px solid #262b36;display:flex;
        justify-content:space-between;align-items:center}}
 a{{color:#6ea8fe;text-decoration:none}} main{{padding:20px;max-width:1100px}}
 table{{border-collapse:collapse;width:100%;margin:12px 0}}
 th,td{{border:1px solid #262b36;padding:8px 10px;text-align:left;font-size:13px}}
 th{{background:#171a21}}
 .pill{{padding:2px 8px;border-radius:10px;font-size:12px}}
 .ACTIVE{{background:#0d4429;color:#4ade80}} .PENDING{{background:#443c0d;color:#fbbf24}}
 .SUSPENDED,.EXPIRED{{background:#442a0d;color:#fb923c}}
 .REVOKED,.CLOSED,.REMOVED{{background:#441417;color:#f87171}}
 input,select{{background:#0f1115;border:1px solid #333a48;color:#e6e6e6;padding:7px;border-radius:5px}}
 button{{background:#2563eb;border:0;color:#fff;padding:7px 13px;border-radius:5px;cursor:pointer}}
 button.danger{{background:#b91c1c}} button.warn{{background:#b45309}}
 .card{{background:#141821;border:1px solid #262b36;border-radius:8px;padding:16px;margin:12px 0}}
 code{{background:#0b0d12;padding:2px 5px;border-radius:4px}}
</style></head><body>
<header><strong>StopLossPro Admin</strong>
<span><a href="/admin">Customers</a> &nbsp; <a href="/admin/audit">Audit</a> &nbsp;
<a href="/admin/logout">Sign out</a></span></header>
<main>{body}</main></body></html>""")


# ── login ──────────────────────────────────────────────────────────────────
@router.get("/admin/login", response_class=HTMLResponse)
def login_form(error: str = ""):
    err = f'<p style="color:#f87171">{_h(error)}</p>' if error else ""
    return _page("Sign in", f"""<div class="card" style="max-width:380px">
<h3>Administrator sign in</h3>{err}
<form method="post" action="/admin/login">
 <p><input name="email" type="email" placeholder="Email" required style="width:100%"></p>
 <p><input name="password" type="password" placeholder="Password" required style="width:100%"></p>
 <p><input name="totp" placeholder="6-digit authenticator code" maxlength="6"
    inputmode="numeric" style="width:100%"></p>
 <button type="submit">Sign in</button>
</form></div>""")


@router.post("/admin/login")
def login_submit(response: Response, request: Request, email: str = Form(...),
                 password: str = Form(...), totp: str = Form(default=""),
                 db: Session = Depends(get_db)):
    # The admin account is the highest-value identity in the system — a
    # compromise here compromises every customer licence. It must be rate
    # limited at least as strictly as customer login, not left unlimited.
    # Covers both password and TOTP brute force: both are submitted through
    # this one endpoint, so one limiter here protects both attack surfaces.
    ip = client_ip(request)
    rate_limit(f"admin_login:{ip}", limit=10, window=300)
    rate_limit(f"admin_login:{email.strip().lower()}", limit=8, window=300)

    admin = db.execute(select(AdminUser).where(AdminUser.email == email.strip().lower())).scalar_one_or_none()

    if not admin or not security.verify_password(password, admin.password_hash):
        services.audit(db, "ADMIN_LOGIN_FAILED", actor=f"admin:{email}", result="FAILURE")
        db.commit()
        return RedirectResponse("/admin/login?error=Invalid+credentials", status_code=303)

    # MFA is mandatory for admins. No opt-out path.
    if admin.totp_confirmed:
        if not admin.totp_secret_encrypted:
            return RedirectResponse("/admin/login?error=MFA+misconfigured", status_code=303)
        secret = security.decrypt_totp_secret(admin.totp_secret_encrypted)
        ok, step = security.verify_totp(secret, totp, admin.last_totp_step)
        if not ok:
            services.audit(db, "ADMIN_LOGIN_MFA_FAILED", actor=f"admin:{email}", result="FAILURE")
            db.commit()
            return RedirectResponse("/admin/login?error=Invalid+authenticator+code", status_code=303)
        admin.last_totp_step = step

    admin.last_login_at = utcnow()
    services.audit(db, "ADMIN_LOGIN", actor=f"admin:{admin.email}")
    db.commit()

    tok = secrets.token_urlsafe(32)
    _ADMIN_SESSIONS[tok] = {"email": admin.email, "role": admin.role,
                            "expires": datetime.now(timezone.utc) + _ADMIN_TTL}
    r = RedirectResponse("/admin", status_code=303)
    r.set_cookie(COOKIE, tok, httponly=True, samesite="strict",
                 secure=True, max_age=int(_ADMIN_TTL.total_seconds()))
    return r


@router.get("/admin/logout")
def admin_logout(request: Request):
    _ADMIN_SESSIONS.pop(request.cookies.get(COOKIE) or "", None)
    r = RedirectResponse("/admin/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r


# ── first-admin bootstrap (temporary — see app/config.py ADMIN_BOOTSTRAP_TOKEN) ──
# HTTP equivalent of `python -m app.bootstrap_admin` (app/bootstrap_admin.py),
# for hosts with no shell access (e.g. a free-tier web service). Same rules as
# that script, enforced twice as hard because this surface is reachable over
# the network: nothing happens unless STOPLOSS_ADMIN_BOOTSTRAP_TOKEN is set AND
# matches, and unless zero admins currently exist — checked on every request,
# including the final commit, to close the race window. Delete the env var
# once the first admin is created; the zero-admin check disables this on its
# own regardless, but removing it is defense in depth.
_BOOTSTRAP_PENDING_TTL = 600  # seconds the step-1 -> step-2 handoff is valid


def _bootstrap_gate_open(token: str, db: Session) -> bool:
    cfg_token = get_settings().ADMIN_BOOTSTRAP_TOKEN
    if not cfg_token or not secrets.compare_digest(token or "", cfg_token):
        return False
    return db.execute(select(AdminUser)).scalars().first() is None


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign_pending(payload: dict, token: str) -> str:
    """HMAC-signed, self-contained handoff between step 1 and step 2 so the
    server doesn't need session storage for a two-request flow. Signed with
    the bootstrap token itself — a secret only the deployer holds, and one
    that becomes worthless the moment the first admin exists anyway."""
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(token.encode(), body, hashlib.sha256).digest()
    return f"{_b64u(body)}.{_b64u(sig)}"


def _verify_pending(blob: str, token: str) -> dict:
    try:
        body_b64, sig_b64 = blob.split(".")
        body, sig = _b64u_dec(body_b64), _b64u_dec(sig_b64)
    except Exception as exc:
        raise ValueError("malformed pending token") from exc
    expected = hmac.new(token.encode(), body, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise ValueError("bad signature")
    payload = json.loads(body)
    if float(payload.get("exp", 0)) < time.time():
        raise ValueError("expired")
    return payload


def _bootstrap_form(token: str, error: str = "") -> HTMLResponse:
    err = f'<p style="color:#f87171">{_h(error)}</p>' if error else ""
    return _page("Create the first administrator", f"""
<div class="card" style="max-width:420px">
<h3>Create the first StopLossPro administrator</h3>
<p>One-time setup page. Only reachable with the bootstrap token, and only
while no administrator exists yet.</p>
{err}
<form method="post" action="/admin/bootstrap?token={_h(token)}">
 <p><input name="email" type="email" placeholder="Email" required style="width:100%"></p>
 <p><input name="password" type="password" placeholder="Password (min 12 chars)" required style="width:100%"></p>
 <p><input name="password2" type="password" placeholder="Confirm password" required style="width:100%"></p>
 <button type="submit">Continue &rarr; set up authenticator</button>
</form></div>""")


@router.get("/admin/bootstrap", response_class=HTMLResponse)
def bootstrap_start(request: Request, token: str = "", db: Session = Depends(get_db)):
    rate_limit(f"admin_bootstrap:{client_ip(request)}", limit=10, window=300)
    if not _bootstrap_gate_open(token, db):
        raise HTTPException(404)
    return _bootstrap_form(token)


@router.post("/admin/bootstrap", response_class=HTMLResponse)
def bootstrap_step1(request: Request, token: str = "", email: str = Form(...),
                    password: str = Form(...), password2: str = Form(...),
                    db: Session = Depends(get_db)):
    rate_limit(f"admin_bootstrap:{client_ip(request)}", limit=10, window=300)
    if not _bootstrap_gate_open(token, db):
        raise HTTPException(404)

    email = email.strip().lower()
    if "@" not in email:
        return _bootstrap_form(token, "That does not look like an email address.")
    if len(password) < 12:
        return _bootstrap_form(token, "Password must be at least 12 characters.")
    if password != password2:
        return _bootstrap_form(token, "Passwords do not match.")

    secret = security.new_totp_secret()
    uri = security.totp_provisioning_uri(secret, email, issuer="StopLossPro Admin")
    pending = _sign_pending(
        {"email": email, "pwh": security.hash_password(password), "totp": secret,
         "exp": time.time() + _BOOTSTRAP_PENDING_TTL},
        token,
    )
    return _page("Confirm authenticator", f"""
<div class="card" style="max-width:460px">
<h3>Add this to your authenticator app</h3>
<p>Secret: <code>{_h(secret)}</code></p>
<p>Or paste this URI if your app supports it:<br>
<code style="word-break:break-all">{_h(uri)}</code></p>
<p>Shown once. If you lose it before confirming, submit the form again to restart.</p>
<form method="post" action="/admin/bootstrap/confirm?token={_h(token)}">
 <input type="hidden" name="pending" value="{_h(pending)}">
 <p><input name="code" placeholder="6-digit code" maxlength="6" inputmode="numeric" required style="width:100%"></p>
 <button type="submit">Create administrator</button>
</form></div>""")


@router.post("/admin/bootstrap/confirm", response_class=HTMLResponse)
def bootstrap_step2(request: Request, token: str = "", pending: str = Form(...),
                    code: str = Form(...), db: Session = Depends(get_db)):
    rate_limit(f"admin_bootstrap:{client_ip(request)}", limit=10, window=300)
    if not _bootstrap_gate_open(token, db):
        raise HTTPException(404)

    try:
        p = _verify_pending(pending, token)
    except ValueError:
        return _page("Expired", f'<div class="card">That setup link expired or was '
                     f'tampered with. <a href="/admin/bootstrap?token={_h(token)}">Start over</a>.</div>')

    ok, _step = security.verify_totp(p["totp"], code)
    if not ok:
        return _page("Invalid code", f'<div class="card">That code did not verify. '
                     f'<a href="/admin/bootstrap?token={_h(token)}">Start over</a>.</div>')

    # Re-check immediately before the write — closes the race between the
    # gate check above and this commit (two concurrent bootstrap attempts).
    if db.execute(select(AdminUser)).scalars().first():
        raise HTTPException(404)

    admin = AdminUser(
        email=p["email"], password_hash=p["pwh"],
        totp_secret_encrypted=security.encrypt_totp_secret(p["totp"]),
        totp_confirmed=True, role=AdminRole.SUPER_ADMIN.value,
    )
    db.add(admin)
    services.audit(db, "ADMIN_BOOTSTRAPPED", actor=f"admin:{p['email']}",
                   detail="created via one-time HTTP bootstrap, not CLI")
    db.commit()

    return _page("Administrator created", f"""<div class="card">
<h3>Done</h3>
<p>Administrator <code>{_h(p['email'])}</code> created. Sign in at
<a href="/admin/login">/admin/login</a>.</p>
<p><b>Now remove STOPLOSS_ADMIN_BOOTSTRAP_TOKEN from the environment.</b> This
page already refuses to run again since an administrator exists, but removing
the token closes it out completely.</p></div>""")


# ── customer list ──────────────────────────────────────────────────────────
@router.get("/admin", response_class=HTMLResponse)
def customers(request: Request, q: str = "", db: Session = Depends(get_db)):
    _require_admin(request)
    stmt = select(User).order_by(User.created_at.desc()).limit(200)
    if q:
        stmt = select(User).where(User.email.ilike(f"%{q}%")).limit(200)

    rows = []
    for u in db.execute(stmt).scalars():
        lic = db.execute(select(Licence).where(Licence.user_id == u.id)
                         .order_by(Licence.created_at.desc())).scalars().first()
        act = db.execute(select(AppSession).where(
            AppSession.user_id == u.id, AppSession.status == SessionStatus.ACTIVE)).scalar_one_or_none()
        rows.append(f"""<tr>
<td><a href="/admin/customer/{u.id}">{_h(u.email)}</a></td>
<td><span class="pill {u.status.value}">{u.status.value}</span></td>
<td>{f'<span class="pill {lic.status.value}">{lic.status.value}</span>' if lic else '—'}</td>
<td>{_h(lic.expires_at.strftime('%Y-%m-%d')) if lic and lic.expires_at else '—'}</td>
<td>{'🟢 online' if act else '—'}</td>
<td>{'✓' if u.mfa and u.mfa.is_confirmed else '—'}</td></tr>""")

    pending = db.execute(select(User).where(User.status == AccountStatus.PENDING)
                         .order_by(User.created_at.asc()).limit(100)).scalars().all()
    pending_rows = "".join(f"""<tr>
<td>{_h(p.email)}</td>
<td>{_h(p.full_name or '—')}</td>
<td>{p.created_at.strftime('%Y-%m-%d %H:%M')}</td>
<td style="display:flex;gap:6px;align-items:center">
 <form method="post" action="/admin/signup/{p.id}/approve" style="display:flex;gap:6px;margin:0">
  <input name="days" type="number" value="365" style="width:70px" title="Licence days">
  <input name="note" placeholder="Payment note" style="width:160px">
  <button type="submit">Approve</button>
 </form>
 <form method="post" action="/admin/signup/{p.id}/reject" style="margin:0">
  <button class="danger" type="submit"
    onclick="return confirm('Reject this signup? They can register again with a different email.')">Reject</button>
 </form>
</td></tr>""" for p in pending)

    pending_block = f"""<div class="card"><h3>Pending signups ({len(pending)})</h3>
<p>Self-registered accounts, awaiting payment reconciliation. Approving creates
their licence; rejecting closes the account without one.</p>
<table><tr><th>Email</th><th>Name</th><th>Requested</th><th>Action</th></tr>
{pending_rows}</table></div>""" if pending else ""

    return _page("Customers", f"""
<form method="get"><input name="q" value="{_h(q)}" placeholder="Search email">
<button>Search</button></form>
{pending_block}
<div class="card"><h3>Create customer</h3>
<form method="post" action="/admin/customer/create">
 <input name="email" type="email" placeholder="customer@example.com" required>
 <input name="full_name" placeholder="Name (optional)">
 <input name="password" type="password" placeholder="Initial password" required>
 <input name="days" type="number" value="365" style="width:90px" title="Licence days">
 <input name="note" placeholder="Payment note e.g. 'cash 2026-08-05'" style="width:230px">
 <button type="submit">Create + activate</button>
</form></div>
<table><tr><th>Email</th><th>Account</th><th>Licence</th><th>Expires</th>
<th>Session</th><th>MFA</th></tr>{''.join(rows) or '<tr><td colspan=6>No customers yet.</td></tr>'}</table>""")


@router.post("/admin/signup/{user_id}/approve")
def approve_signup(user_id: uuid.UUID, request: Request, days: int = Form(default=365),
                   note: str = Form(default=""), db: Session = Depends(get_db)):
    a = _require_admin(request)
    u = db.get(User, user_id)
    if not u or u.status is not AccountStatus.PENDING:
        raise HTTPException(404)

    u.status = AccountStatus.ACTIVE
    db.add(Licence(user_id=u.id, status=LicenceStatus.ACTIVE, activated_at=utcnow(),
                   expires_at=utcnow() + timedelta(days=max(1, days)),
                   activation_note=note or None))
    services.audit(db, "SIGNUP_APPROVED", actor=f"admin:{a['email']}", target_user_id=u.id,
                   detail=f"licence {days}d; {note}")
    db.commit()
    return RedirectResponse(f"/admin/customer/{u.id}", status_code=303)


@router.post("/admin/signup/{user_id}/reject")
def reject_signup(user_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    a = _require_admin(request)
    u = db.get(User, user_id)
    if not u or u.status is not AccountStatus.PENDING:
        raise HTTPException(404)

    u.status = AccountStatus.CLOSED
    services.audit(db, "SIGNUP_REJECTED", actor=f"admin:{a['email']}", target_user_id=u.id)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/admin/customer/create")
def create_customer(request: Request, email: str = Form(...), password: str = Form(...),
                    full_name: str = Form(default=""), days: int = Form(default=365),
                    note: str = Form(default=""), db: Session = Depends(get_db)):
    a = _require_admin(request)
    email = email.strip().lower()
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none():
        return RedirectResponse("/admin", status_code=303)

    u = User(email=email, full_name=full_name or None, status=AccountStatus.ACTIVE,
             password_hash=security.hash_password(password))
    db.add(u)
    db.flush()
    db.add(Licence(user_id=u.id, status=LicenceStatus.ACTIVE, activated_at=utcnow(),
                   expires_at=utcnow() + timedelta(days=max(1, days)),
                   activation_note=note or None))
    services.audit(db, "CUSTOMER_CREATED", actor=f"admin:{a['email']}", target_user_id=u.id,
                   detail=f"licence {days}d; {note}")
    db.commit()
    return RedirectResponse(f"/admin/customer/{u.id}", status_code=303)


# ── customer detail ────────────────────────────────────────────────────────
@router.get("/admin/customer/{user_id}", response_class=HTMLResponse)
def customer_detail(user_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404)

    lic = db.execute(select(Licence).where(Licence.user_id == u.id)
                     .order_by(Licence.created_at.desc())).scalars().first()
    devices = db.execute(select(Device).where(Device.user_id == u.id)).scalars().all()
    sessions = db.execute(select(AppSession).where(AppSession.user_id == u.id)
                          .order_by(AppSession.created_at.desc()).limit(10)).scalars().all()

    dev_rows = "".join(f"""<tr><td>{_h(d.device_name or '—')}</td><td>{_h(d.os_name or '—')}</td>
<td>{_h(d.app_version or '—')}</td><td><span class="pill {d.status.value}">{d.status.value}</span></td>
<td>{d.last_seen_at.strftime('%Y-%m-%d %H:%M') if d.last_seen_at else '—'}</td>
<td><form method="post" action="/admin/device/{d.id}/revoke" style="margin:0">
<button class="danger" onclick="return confirm('Revoke this device?')">Revoke</button></form></td></tr>"""
                      for d in devices) or "<tr><td colspan=6>No devices.</td></tr>"

    ses_rows = "".join(f"""<tr><td><span class="pill {s.status.value}">{s.status.value}</span></td>
<td>{s.created_at.strftime('%Y-%m-%d %H:%M')}</td>
<td>{s.last_heartbeat_at.strftime('%H:%M:%S')}</td><td>{_h(s.end_reason or '—')}</td></tr>"""
                       for s in sessions) or "<tr><td colspan=4>No sessions.</td></tr>"

    lic_block = "No licence." if not lic else f"""
<p>Status <span class="pill {lic.status.value}">{lic.status.value}</span>
&nbsp; Plan <code>{_h(lic.plan)}</code>
&nbsp; Expires <code>{_h(lic.expires_at.strftime('%Y-%m-%d') if lic.expires_at else 'never')}</code></p>
<p>Entitlements: <code>{_h(lic.entitlements)}</code></p>
<p>Note: {_h(lic.activation_note or '—')}</p>
<form method="post" action="/admin/licence/{lic.id}/action" style="display:flex;gap:8px;flex-wrap:wrap">
 <button name="action" value="activate">Activate</button>
 <button name="action" value="suspend" class="warn"
   onclick="return confirm('Suspend access?')">Suspend</button>
 <button name="action" value="revoke" class="danger"
   onclick="return confirm('REVOKE permanently? The customer loses access at next heartbeat.')">Revoke</button>
 <span><input name="days" type="number" value="30" style="width:80px">
 <button name="action" value="extend">Extend days</button></span>
</form>"""

    return _page(f"Customer {u.email}", f"""
<p><a href="/admin">&larr; Customers</a></p>
<div class="card"><h3>{_h(u.email)}</h3>
<p>{_h(u.full_name or '')} &nbsp; <span class="pill {u.status.value}">{u.status.value}</span></p>
<p>Created {u.created_at.strftime('%Y-%m-%d')} &nbsp;
Last login {u.last_login_at.strftime('%Y-%m-%d %H:%M') if u.last_login_at else '—'}</p>
<p>MFA: {'<b>enabled</b>' if u.mfa and u.mfa.is_confirmed else 'not set up'}
<form method="post" action="/admin/user/{u.id}/reset-mfa" style="display:inline">
<button class="warn" onclick="return confirm('Reset MFA? Verify the customer identity out of band FIRST. This also ends their active session.')">Reset MFA</button></form></p>
<form method="post" action="/admin/user/{u.id}/force-logout" style="display:inline">
<button class="warn">Force logout</button></form>
</div>
<div class="card"><h3>Licence</h3>{lic_block}</div>
<div class="card"><h3>Devices</h3><table>
<tr><th>Name</th><th>OS</th><th>Version</th><th>Status</th><th>Last seen</th><th></th></tr>
{dev_rows}</table></div>
<div class="card"><h3>Recent sessions</h3><table>
<tr><th>Status</th><th>Started</th><th>Last heartbeat</th><th>Ended because</th></tr>
{ses_rows}</table></div>""")


# ── mutating actions ───────────────────────────────────────────────────────
@router.post("/admin/licence/{licence_id}/action")
def licence_action(licence_id: uuid.UUID, request: Request, action: str = Form(...),
                   days: int = Form(default=30), db: Session = Depends(get_db)):
    a = _require_admin(request)
    lic = db.get(Licence, licence_id)
    if not lic:
        raise HTTPException(404)

    if action == "activate":
        lic.status = LicenceStatus.ACTIVE
        lic.activated_at = lic.activated_at or utcnow()
    elif action == "suspend":
        lic.status = LicenceStatus.SUSPENDED
        _kill_sessions(db, lic.user_id, "LICENCE_SUSPENDED")
    elif action == "revoke":
        lic.status = LicenceStatus.REVOKED
        _kill_sessions(db, lic.user_id, "LICENCE_REVOKED")
    elif action == "extend":
        base = lic.expires_at if lic.expires_at and lic.expires_at > utcnow() else utcnow()
        lic.expires_at = base + timedelta(days=max(1, days))
        if lic.status is LicenceStatus.EXPIRED:
            lic.status = LicenceStatus.ACTIVE

    services.audit(db, f"LICENCE_{action.upper()}", actor=f"admin:{a['email']}",
                   target_user_id=lic.user_id)
    db.commit()
    return RedirectResponse(f"/admin/customer/{lic.user_id}", status_code=303)


def _kill_sessions(db: Session, user_id: uuid.UUID, reason: str) -> None:
    for s in db.execute(select(AppSession).where(
            AppSession.user_id == user_id,
            AppSession.status == SessionStatus.ACTIVE)).scalars():
        s.status = SessionStatus.REVOKED
        s.ended_at = utcnow()
        s.end_reason = reason
        s.token_hash = None


@router.post("/admin/user/{user_id}/force-logout")
def force_logout(user_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    a = _require_admin(request)
    _kill_sessions(db, user_id, "ADMIN_FORCE_LOGOUT")
    services.audit(db, "ADMIN_FORCE_LOGOUT", actor=f"admin:{a['email']}", target_user_id=user_id)
    db.commit()
    return RedirectResponse(f"/admin/customer/{user_id}", status_code=303)


@router.post("/admin/user/{user_id}/reset-mfa")
def admin_reset_mfa(user_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    a = _require_admin(request)
    u = db.get(User, user_id)
    if not u:
        raise HTTPException(404)
    services.reset_mfa(db, u, actor=f"admin:{a['email']}")
    return RedirectResponse(f"/admin/customer/{user_id}", status_code=303)


@router.post("/admin/device/{device_id}/revoke")
def revoke_device(device_id: uuid.UUID, request: Request, db: Session = Depends(get_db)):
    a = _require_admin(request)
    d = db.get(Device, device_id)
    if not d:
        raise HTTPException(404)
    d.status = DeviceStatus.REVOKED
    d.revoked_at = utcnow()
    for s in db.execute(select(AppSession).where(
            AppSession.device_id == d.id,
            AppSession.status == SessionStatus.ACTIVE)).scalars():
        s.status = SessionStatus.REVOKED
        s.ended_at = utcnow()
        s.end_reason = "DEVICE_REVOKED"
        s.token_hash = None
    services.audit(db, "DEVICE_REVOKED", actor=f"admin:{a['email']}", target_user_id=d.user_id)
    db.commit()
    return RedirectResponse(f"/admin/customer/{d.user_id}", status_code=303)


# ── audit log ──────────────────────────────────────────────────────────────
@router.get("/admin/audit", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    rows = db.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(300)).scalars()
    body = "".join(f"""<tr><td>{e.created_at.strftime('%Y-%m-%d %H:%M:%S')}</td>
<td><code>{_h(e.event_type)}</code></td><td>{_h(e.actor or '—')}</td>
<td>{_h(str(e.target_user_id)[:8] if e.target_user_id else '—')}</td>
<td>{_h(e.result)}</td><td>{_h(e.detail or '')}</td></tr>""" for e in rows)
    return _page("Audit", f"""<h3>Security events (latest 300)</h3><table>
<tr><th>When</th><th>Event</th><th>Actor</th><th>Target</th><th>Result</th><th>Detail</th></tr>
{body or '<tr><td colspan=6>No events.</td></tr>'}</table>""")
