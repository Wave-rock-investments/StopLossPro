"""Admin dashboard (Phase 7) — server-rendered HTML, cookie-authenticated.

Deliberately NOT a second call engine or a second source of truth: every
number on every page here is read directly off the same tables the API and
the Telegram bot write to (calls, subscriptions, payments, performance.py's
pure functions). This module only ever reads + performs the small set of
explicitly human actions the business actually needs an admin for (confirm
a manually-reported payment, revoke access, create/retire a plan) — it never
duplicates services.py's or subscriptions.py's business logic.

Auth: signed session cookie (app/security.py), same design note as that
file documents (stateless, 12h TTL, no per-session revocation without
rotating ADMIN_SESSION_SECRET). Bootstrapping the first admin account uses a
one-time shared-secret endpoint (ADMIN_BOOTSTRAP_TOKEN), fails closed (404)
if that token isn't configured — same fail-closed pattern as
require_adapter_key and the Telegram webhook route.

Rendering: hand-built HTML via small helper functions, not a template engine
— this repo already avoids adding dependencies it doesn't need (see
telegram_bot.py's docstring on stdlib-first choices), and a dashboard this
size doesn't need one. Every value interpolated from the database goes
through esc() (html.escape) — the one XSS rule that matters here, since
support-ticket messages and admin-entered plan names are free text.
"""
from __future__ import annotations

import hmac
import html as html_mod
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import performance, security, services, subscriptions as subs
from app.config import get_settings
from app.database import get_db
from app.models import (
    AdminRole, AdminUser, AuditEvent, Call, CallMessage, CallStatus,
    DeliveryStatus, Payment, PaymentStatus, Plan, Subscriber, Subscription,
    SubscriptionStatus, SupportTicket, TelegramChat, TicketStatus,
)
from app.rate_limit import admin_bootstrap_limit, admin_login_limit, check_rate_limit

router = APIRouter(prefix="/admin", tags=["admin"])

_COOKIE_NAME = "sterling_admin_session"
_MAX_FAILED_LOGINS = 5
_LOCKOUT_MINUTES = 15
_WRITE_ROLES = {AdminRole.SUPER_ADMIN.value, AdminRole.ADMIN.value}

esc = html_mod.escape


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite (used in dev/tests) doesn't actually preserve tzinfo on
    DateTime(timezone=True) columns — a value written as UTC-aware comes
    back naive on read. Comparing that directly against
    datetime.now(timezone.utc) raises TypeError, so every value read back
    from the DB and compared in Python (not inside a SQL WHERE clause, which
    doesn't have this problem — see subscriptions.py's expiry queries) gets
    normalized through here first. Values are always written as UTC
    (app.models.utcnow), so re-attaching UTC on read is correct, not a
    guess."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ══════════════════════════════════════════════════════════════════════════
# Auth
# ══════════════════════════════════════════════════════════════════════════
def _lookup_admin(db: Session, admin_id: str) -> AdminUser | None:
    try:
        aid = uuid.UUID(admin_id)
    except (ValueError, AttributeError, TypeError):
        return None
    return db.get(AdminUser, aid)


def _current_admin(request: Request, db: Session) -> AdminUser | None:
    token = request.cookies.get(_COOKIE_NAME)
    if not token:
        return None
    admin_id = security.verify_admin_session(token)
    if not admin_id:
        return None
    return _lookup_admin(db, admin_id)


def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    admin = _current_admin(request, db)
    if admin is None:
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})
    return admin


def _require_write_role(admin: AdminUser) -> None:
    """Gate mutating actions (confirm payment, revoke access, create/retire
    a plan) to SUPER_ADMIN/ADMIN. ANALYST/SUPPORT/VIEWER are read-only —
    a support agent filing a ticket in Telegram is not the same authority
    as one that can move money-adjacent state."""
    if admin.role not in _WRITE_ROLES:
        raise HTTPException(status_code=403, detail="This action requires the ADMIN or SUPER_ADMIN role")


# ══════════════════════════════════════════════════════════════════════════
# Rate limiting (Phase 9) — scoped per authenticated admin (admin.id), not
# per IP. An office full of admins behind one NAT shouldn't share a quota,
# and an attacker who steals a session cookie can't get a bigger budget by
# rotating IPs. Depends on require_admin so identity is always the resolved
# admin, never the caller's address — FastAPI caches require_admin's result
# per request, so this doesn't re-run auth a second time on routes that
# also declare `admin: AdminUser = Depends(require_admin)` directly.
# ══════════════════════════════════════════════════════════════════════════
def _admin_read_limit(request: Request, admin: AdminUser = Depends(require_admin)) -> None:
    check_rate_limit("admin_read", str(admin.id), request)


def _admin_write_limit(request: Request, admin: AdminUser = Depends(require_admin)) -> None:
    check_rate_limit("admin_write", str(admin.id), request)


# ══════════════════════════════════════════════════════════════════════════
# Layout
# ══════════════════════════════════════════════════════════════════════════
_NAV = [
    ("Dashboard", "/admin"),
    ("Calls", "/admin/calls"),
    ("Active Calls", "/admin/calls?status=ACTIVE"),
    ("Performance", "/admin/performance"),
    ("Subscribers", "/admin/subscribers"),
    ("Payments", "/admin/payments"),
    ("Plans", "/admin/plans"),
    ("Telegram", "/admin/telegram"),
    ("Audit Logs", "/admin/audit"),
    ("System Health", "/admin/health"),
    ("Settings", "/admin/settings"),
]

_STYLE = """
body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0b0e13;color:#e6e9ef;margin:0}
header{display:flex;justify-content:space-between;align-items:center;padding:14px 24px;background:#111620;border-bottom:1px solid #232a38}
header h1{font-size:16px;letter-spacing:.08em;margin:0}
header .who{font-size:13px;color:#9aa4b2}
header button{background:none;border:1px solid #333c4d;color:#9aa4b2;padding:4px 10px;border-radius:4px;cursor:pointer}
nav{display:flex;flex-wrap:wrap;gap:2px;background:#0f131b;padding:6px 20px;border-bottom:1px solid #1c2230}
nav a{color:#9aa4b2;text-decoration:none;font-size:13px;padding:8px 12px;border-radius:4px}
nav a:hover{background:#1b2130;color:#fff}
main{padding:24px;max-width:1200px;margin:0 auto}
.cards{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:24px}
.card{background:#141a24;border:1px solid #232a38;border-radius:8px;padding:16px 18px;min-width:160px;flex:1}
.card .label{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:#7c8798;margin-bottom:6px}
.card .value{font-size:24px;font-weight:600}
.pos{color:#3ddc97}.neg{color:#ff5c7a}
table{width:100%;border-collapse:collapse;margin-bottom:20px;font-size:13px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid #1c2230}
th{color:#7c8798;font-weight:500;text-transform:uppercase;font-size:11px;letter-spacing:.05em}
tr:hover td{background:#151b26}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;background:#232a38}
.badge.ok{background:#123326;color:#3ddc97}
.badge.bad{background:#3a1620;color:#ff5c7a}
.badge.warn{background:#3a2f12;color:#f0b23d}
a.btn,button.btn{display:inline-block;background:#2450e0;color:#fff;border:none;padding:6px 12px;border-radius:5px;font-size:12px;cursor:pointer;text-decoration:none}
form.inline{display:inline}
.error{background:#3a1620;color:#ff9aac;padding:10px 14px;border-radius:6px;margin-bottom:16px;font-size:13px}
.section h2{font-size:15px;color:#9aa4b2;text-transform:uppercase;letter-spacing:.05em;margin:28px 0 10px}
input,select{background:#141a24;border:1px solid #333c4d;color:#e6e9ef;padding:6px 10px;border-radius:4px;font-size:13px}
label{font-size:12px;color:#9aa4b2;display:block;margin-bottom:4px}
.login-box{max-width:340px;margin:80px auto;background:#141a24;border:1px solid #232a38;border-radius:8px;padding:28px}
.login-box h1{font-size:18px;margin-top:0}
"""


def _page(title: str, admin: AdminUser | None, body: str) -> str:
    nav_html = "".join(f'<a href="{href}">{esc(label)}</a>' for label, href in _NAV)
    who = ""
    if admin is not None:
        who = (
            f'<span class="who">{esc(admin.email)} · {esc(admin.role)}</span> '
            f'<form class="inline" method="post" action="/admin/logout">'
            f'<button type="submit">Logout</button></form>'
        )
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{esc(title)} — Sterling_Room Admin</title><style>{_STYLE}</style></head>"
        f"<body><header><h1>STERLING_ROOM ADMIN</h1><div>{who}</div></header>"
        f"<nav>{nav_html}</nav><main>{body}</main></body></html>"
    )


def _card(label: str, value: str, cls: str = "") -> str:
    return f'<div class="card"><div class="label">{esc(label)}</div><div class="value {cls}">{value}</div></div>'


def _r_class(v: float) -> str:
    return "pos" if v >= 0 else "neg"


# ══════════════════════════════════════════════════════════════════════════
# Bootstrap — creates the first admin account. Fails closed if
# ADMIN_BOOTSTRAP_TOKEN is unset, matching require_adapter_key's pattern.
# ══════════════════════════════════════════════════════════════════════════
@router.post("/bootstrap")
def bootstrap_admin(
    email: str = Form(...), password: str = Form(...),
    x_bootstrap_token: str | None = Header(default=None, alias="X-Bootstrap-Token"),
    db: Session = Depends(get_db),
    _rl: None = Depends(admin_bootstrap_limit),
):
    settings = get_settings()
    if not settings.ADMIN_BOOTSTRAP_TOKEN or not hmac.compare_digest(
        x_bootstrap_token or "", settings.ADMIN_BOOTSTRAP_TOKEN
    ):
        raise HTTPException(status_code=404)
    if db.query(AdminUser).count() > 0:
        raise HTTPException(status_code=409, detail="An admin account already exists — use /admin/login")
    if len(password) < 12:
        raise HTTPException(status_code=422, detail="Password must be at least 12 characters")

    admin = AdminUser(
        email=email.strip().lower(), password_hash=security.hash_password(password),
        role=AdminRole.SUPER_ADMIN.value,
    )
    db.add(admin)
    db.flush()
    services.audit(db, "ADMIN_BOOTSTRAPPED", actor=admin.email, detail=f"admin_id={admin.id}")
    db.commit()
    return {"ok": True, "email": admin.email}


# ══════════════════════════════════════════════════════════════════════════
# Login / logout
# ══════════════════════════════════════════════════════════════════════════
def _login_page(error: str | None = None) -> str:
    err = f'<div class="error">{esc(error)}</div>' if error else ""
    body = f"""
    <div class="login-box">
      <h1>Sterling_Room Admin</h1>
      {err}
      <form method="post" action="/admin/login">
        <label>Email</label>
        <input name="email" type="email" required style="width:100%;margin-bottom:12px">
        <label>Password</label>
        <input name="password" type="password" required style="width:100%;margin-bottom:16px">
        <button class="btn" type="submit" style="width:100%">Sign in</button>
      </form>
    </div>"""
    return _page("Sign in", None, body)


@router.get("/login", response_class=HTMLResponse)
def login_form():
    return _login_page()


@router.post("/login")
def login_submit(
    email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db),
    _rl: None = Depends(admin_login_limit),
):
    now = datetime.now(timezone.utc)
    admin = db.execute(select(AdminUser).where(AdminUser.email == email.strip().lower())).scalar_one_or_none()

    if admin is not None and _aware(admin.locked_until) is not None and _aware(admin.locked_until) > now:
        return HTMLResponse(_login_page("Account temporarily locked after repeated failed attempts. Try again later."))

    if admin is None or not security.verify_password(password, admin.password_hash):
        if admin is not None:
            admin.failed_login_count += 1
            if admin.failed_login_count >= _MAX_FAILED_LOGINS:
                admin.locked_until = now + timedelta(minutes=_LOCKOUT_MINUTES)
            services.audit(db, "ADMIN_LOGIN_FAILED", actor=email, detail=f"failed_count={admin.failed_login_count}")
        else:
            services.audit(db, "ADMIN_LOGIN_FAILED", actor=email, detail="unknown email")
        db.commit()
        return HTMLResponse(_login_page("Invalid email or password."))

    admin.failed_login_count = 0
    admin.locked_until = None
    admin.last_login_at = now
    services.audit(db, "ADMIN_LOGIN_SUCCESS", actor=admin.email)
    db.commit()

    token = security.issue_admin_session(str(admin.id))
    resp = RedirectResponse(url="/admin", status_code=303)
    resp.set_cookie(
        _COOKIE_NAME, token, httponly=True, samesite="lax",
        secure=get_settings().is_production, max_age=12 * 3600,
    )
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(_COOKIE_NAME)
    return resp


# ══════════════════════════════════════════════════════════════════════════
# Dashboard home — TODAY snapshot, every number sourced from the DB.
# ══════════════════════════════════════════════════════════════════════════
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard_home(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    from app.models import TERMINAL_CALL_STATUSES

    today_stats = performance.daily_results(db)
    active_calls = db.query(Call).filter(Call.status.notin_(TERMINAL_CALL_STATUSES)).count()

    total_subscribers = db.query(Subscriber).count()
    premium_subscribers = db.query(Subscription).filter(
        Subscription.status.in_((SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING_SOON))
    ).count()
    expiring_subscribers = db.query(Subscription).filter_by(status=SubscriptionStatus.EXPIRING_SOON).count()

    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    revenue_today = db.execute(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.CONFIRMED, Payment.updated_at >= day_start,
        )
    ).scalar_one()

    net_r_cls = _r_class(today_stats.net_r)
    cards = "".join([
        _card("Active Calls", str(active_calls)),
        _card("Closed Calls (today)", str(today_stats.total_trades)),
        _card("Net R (today)", f"{'+' if today_stats.net_r >= 0 else ''}{today_stats.net_r}R", net_r_cls),
        _card("Drawdown (today)", f"{today_stats.max_drawdown_r}R", "neg" if today_stats.max_drawdown_r < 0 else ""),
        _card("Total Subscribers", str(total_subscribers)),
        _card("Premium Subscribers", str(premium_subscribers)),
        _card("Expiring Subscribers", str(expiring_subscribers)),
        _card("Revenue (today)", f"{float(revenue_today):.2f}"),
    ])
    body = f'<div class="cards">{cards}</div>'
    return _page("Dashboard", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# Calls — list + detail. Read-only: StopLossPro remains the only call
# source (services.create_call / services.transition_call), never this page.
# ══════════════════════════════════════════════════════════════════════════
@router.get("/calls", response_class=HTMLResponse)
def calls_list(
    status: str | None = None, db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    q = select(Call).order_by(Call.created_at.desc()).limit(200)
    if status:
        try:
            q = q.where(Call.status == CallStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Unknown status {status!r}")
    calls = db.execute(q).scalars().all()

    rows = "".join(
        f"<tr><td><a href='/admin/calls/{esc(c.trade_id)}'>{esc(c.trade_id)}</a></td>"
        f"<td>{esc(c.instrument)}</td><td>{esc(c.direction.value)}</td>"
        f"<td><span class='badge'>{esc(c.status.value)}</span></td>"
        f"<td>{c.result_r if c.result_r is not None else '—'}</td>"
        f"<td>{c.created_at.strftime('%Y-%m-%d %H:%M')}</td></tr>"
        for c in calls
    )
    title = f"Calls — {status.upper()}" if status else "Calls"
    body = f"""
    <div class="section"><h2>{esc(title)} ({len(calls)})</h2>
    <table><tr><th>Trade ID</th><th>Instrument</th><th>Dir</th><th>Status</th><th>Result R</th><th>Created</th></tr>
    {rows or '<tr><td colspan="6">No calls.</td></tr>'}
    </table></div>"""
    return _page(title, admin, body)


@router.get("/calls/{trade_id}", response_class=HTMLResponse)
def call_detail(
    trade_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    call = db.execute(select(Call).where(Call.trade_id == trade_id)).scalar_one_or_none()
    if call is None:
        raise HTTPException(status_code=404, detail="Call not found")

    event_rows = "".join(
        f"<tr><td>{e.created_at.strftime('%Y-%m-%d %H:%M:%S')}</td><td>{esc(e.event_type.value)}</td>"
        f"<td>{esc(e.actor or '—')}</td>"
        f"<td>{esc(e.old_status.value if e.old_status else '—')} → {esc(e.new_status.value if e.new_status else '—')}</td>"
        f"<td>{esc(e.detail or '')}</td></tr>"
        for e in call.events
    )
    msg_rows = "".join(
        f"<tr><td>{esc(m.telegram_chat_id)}</td><td>{esc(m.message_type.value)}</td>"
        f"<td><span class='badge {'ok' if m.delivery_status == DeliveryStatus.SENT else 'bad' if m.delivery_status == DeliveryStatus.FAILED else 'warn'}'>"
        f"{esc(m.delivery_status.value)}</span></td>"
        f"<td>{m.retry_count}</td><td>{esc(m.error or '—')}</td></tr>"
        for m in call.messages
    )
    body = f"""
    <div class="section"><h2>{esc(call.trade_id)}</h2>
    <div class="cards">
      {_card("Status", esc(call.status.value))}
      {_card("Instrument", esc(f"{call.instrument} {call.direction.value}"))}
      {_card("Stop Loss", str(call.stop_loss))}
      {_card("Result R", str(call.result_r) if call.result_r is not None else "—", _r_class(float(call.result_r)) if call.result_r is not None else "")}
    </div>
    <p>Entry: {call.entry_min or '—'} – {call.entry_max or '—'} &nbsp; TP1: {call.tp1 or '—'} &nbsp; TP2: {call.tp2 or '—'} &nbsp; TP3: {call.tp3 or '—'}</p>
    <p>Source: {esc(call.source)} · source_call_id: {esc(call.source_call_id)} · Created: {call.created_at.strftime('%Y-%m-%d %H:%M:%S')}
    {f" · Closed: {call.closed_at.strftime('%Y-%m-%d %H:%M:%S')}" if call.closed_at else ""}</p>
    </div>
    <div class="section"><h2>Lifecycle events</h2>
    <table><tr><th>When</th><th>Event</th><th>Actor</th><th>Transition</th><th>Detail</th></tr>
    {event_rows or '<tr><td colspan="5">No events.</td></tr>'}</table></div>
    <div class="section"><h2>Telegram delivery</h2>
    <table><tr><th>Chat</th><th>Type</th><th>Status</th><th>Retries</th><th>Error</th></tr>
    {msg_rows or '<tr><td colspan="5">No messages sent.</td></tr>'}</table></div>
    """
    return _page(f"Call {call.trade_id}", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# Performance
# ══════════════════════════════════════════════════════════════════════════
def _perf_table(title: str, s: performance.PerformanceStats) -> str:
    if s.total_trades == 0:
        return f'<div class="section"><h2>{esc(title)}</h2><p>No completed trades.</p></div>'
    cards = "".join([
        _card("Trades", str(s.total_trades)),
        _card("Win rate", f"{s.win_rate}%"),
        _card("Net R", f"{'+' if s.net_r >= 0 else ''}{s.net_r}R", _r_class(s.net_r)),
        _card("Expectancy", f"{s.expectancy_r}R" if s.expectancy_r is not None else "—"),
        _card("Profit factor", str(s.profit_factor) if s.profit_factor is not None else "—"),
        _card("Max drawdown", f"{s.max_drawdown_r}R", "neg" if s.max_drawdown_r < 0 else ""),
        _card("Max win streak", str(s.max_consecutive_wins)),
        _card("Max loss streak", str(s.max_consecutive_losses)),
    ])
    return f'<div class="section"><h2>{esc(title)}</h2><div class="cards">{cards}</div></div>'


@router.get("/performance", response_class=HTMLResponse)
def performance_page(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    body = (
        _perf_table("All-time", performance.compute_stats(db))
        + _perf_table("Today", performance.daily_results(db))
        + _perf_table("This week", performance.weekly_results(db))
        + _perf_table("This month", performance.monthly_results(db))
    )
    return _page("Performance", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# Subscribers
# ══════════════════════════════════════════════════════════════════════════
@router.get("/subscribers", response_class=HTMLResponse)
def subscribers_list(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    subscribers = db.execute(select(Subscriber).order_by(Subscriber.created_at.desc()).limit(200)).scalars().all()
    rows = []
    for s in subscribers:
        active_sub = next(
            (sub for sub in sorted(s.subscriptions, key=lambda x: x.created_at, reverse=True)
             if sub.status in (SubscriptionStatus.ACTIVE, SubscriptionStatus.EXPIRING_SOON)),
            None,
        )
        status_html = f"<span class='badge ok'>{esc(active_sub.status.value)}</span>" if active_sub else "<span class='badge'>none</span>"
        rows.append(
            f"<tr><td>{esc(s.telegram_user.telegram_username or s.telegram_user.telegram_user_id)}</td>"
            f"<td>{status_html}</td><td>{s.renewal_count}</td><td>{float(s.lifetime_value):.2f}</td>"
            f"<td>{s.last_payment_date.strftime('%Y-%m-%d') if s.last_payment_date else '—'}</td>"
            f"<td>{s.created_at.strftime('%Y-%m-%d')}</td></tr>"
        )
    body = f"""
    <div class="section"><h2>Subscribers ({len(subscribers)})</h2>
    <table><tr><th>Telegram</th><th>Subscription</th><th>Renewals</th><th>Lifetime Value</th><th>Last Payment</th><th>Since</th></tr>
    {''.join(rows) or '<tr><td colspan="6">No subscribers yet.</td></tr>'}</table></div>"""
    return _page("Subscribers", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# Payments — the only page that mutates money-adjacent state: confirming a
# manually-reported payment. This is the ONLY thing that calls
# subscriptions.confirm_payment() — bot.py's "I'VE PAID" tap deliberately
# never does (see app/bot.py's module docstring).
# ══════════════════════════════════════════════════════════════════════════
@router.get("/payments", response_class=HTMLResponse)
def payments_list(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    payments = db.execute(select(Payment).order_by(Payment.created_at.desc()).limit(200)).scalars().all()
    rows = []
    for p in payments:
        action = ""
        if p.status == PaymentStatus.PENDING:
            action = (
                f"<form class='inline' method='post' action='/admin/payments/{p.id}/confirm'>"
                f"<button class='btn' type='submit'>Confirm</button></form>"
            )
        badge_cls = "ok" if p.status == PaymentStatus.CONFIRMED else "bad" if p.status == PaymentStatus.FAILED else "warn"
        rows.append(
            f"<tr><td>{esc(p.provider_payment_id or '—')}</td><td>{esc(p.provider)}</td>"
            f"<td>{float(p.amount):.2f} {esc(p.currency)}</td>"
            f"<td><span class='badge {badge_cls}'>{esc(p.status.value)}</span></td>"
            f"<td>{p.created_at.strftime('%Y-%m-%d %H:%M')}</td><td>{action}</td></tr>"
        )
    body = f"""
    <div class="section"><h2>Payments ({len(payments)})</h2>
    <table><tr><th>Reference</th><th>Provider</th><th>Amount</th><th>Status</th><th>Created</th><th></th></tr>
    {''.join(rows) or '<tr><td colspan="6">No payments yet.</td></tr>'}</table></div>"""
    return _page("Payments", admin, body)


@router.post("/payments/{payment_id}/confirm")
def confirm_payment_action(
    payment_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_write_limit),
):
    _require_write_role(admin)
    try:
        pid = uuid.UUID(payment_id)
    except ValueError:
        raise HTTPException(status_code=404)
    payment = db.get(Payment, pid)
    if payment is None:
        raise HTTPException(status_code=404, detail="Payment not found")

    try:
        subs.confirm_payment(db, payment, actor=f"admin:{admin.email}")
    except subs.PaymentAlreadyProcessed:
        db.rollback()
    else:
        db.commit()
    return RedirectResponse(url="/admin/payments", status_code=303)


# ══════════════════════════════════════════════════════════════════════════
# Plans
# ══════════════════════════════════════════════════════════════════════════
@router.get("/plans", response_class=HTMLResponse)
def plans_list(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    plans = db.execute(select(Plan).order_by(Plan.created_at)).scalars().all()
    rows = []
    for p in plans:
        toggle_label = "Deactivate" if p.active else "Activate"
        rows.append(
            f"<tr><td>{esc(p.plan_id)}</td><td>{esc(p.name)}</td><td>{p.duration_days}d</td>"
            f"<td>{float(p.price):.2f} {esc(p.currency)}</td>"
            f"<td><span class='badge {'ok' if p.active else ''}'>{'active' if p.active else 'inactive'}</span></td>"
            f"<td><form class='inline' method='post' action='/admin/plans/{p.id}/toggle'>"
            f"<button class='btn' type='submit'>{toggle_label}</button></form></td></tr>"
        )
    body = f"""
    <div class="section"><h2>Plans</h2>
    <table><tr><th>ID</th><th>Name</th><th>Duration</th><th>Price</th><th>Status</th><th></th></tr>
    {''.join(rows) or '<tr><td colspan="6">No plans configured.</td></tr>'}</table></div>
    <div class="section"><h2>Create plan</h2>
    <form method="post" action="/admin/plans">
      <label>Plan ID (e.g. MONTHLY)</label><input name="plan_id" required>
      <label>Name</label><input name="name" required>
      <label>Duration (days)</label><input name="duration_days" type="number" min="1" required>
      <label>Price</label><input name="price" type="number" step="0.01" min="0" required>
      <label>Currency</label><input name="currency" value="USD">
      <p><button class="btn" type="submit">Create</button></p>
    </form></div>"""
    return _page("Plans", admin, body)


@router.post("/plans")
def create_plan_action(
    plan_id: str = Form(...), name: str = Form(...), duration_days: int = Form(...),
    price: float = Form(...), currency: str = Form("USD"),
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_write_limit),
):
    _require_write_role(admin)
    if duration_days <= 0 or price < 0:
        raise HTTPException(status_code=422, detail="duration_days must be positive and price non-negative")
    existing = db.execute(select(Plan).where(Plan.plan_id == plan_id.strip().upper())).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"Plan {plan_id!r} already exists")

    plan = Plan(plan_id=plan_id.strip().upper(), name=name.strip(), duration_days=duration_days,
                price=price, currency=currency.strip().upper() or "USD")
    db.add(plan)
    services.audit(db, "PLAN_CREATED", actor=f"admin:{admin.email}", detail=f"plan_id={plan.plan_id}")
    db.commit()
    return RedirectResponse(url="/admin/plans", status_code=303)


@router.post("/plans/{plan_id}/toggle")
def toggle_plan_action(
    plan_id: str, db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_write_limit),
):
    _require_write_role(admin)
    try:
        pid = uuid.UUID(plan_id)
    except ValueError:
        raise HTTPException(status_code=404)
    plan = db.get(Plan, pid)
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    plan.active = not plan.active
    services.audit(db, "PLAN_TOGGLED", actor=f"admin:{admin.email}",
                    detail=f"plan_id={plan.plan_id} active={plan.active}")
    db.commit()
    return RedirectResponse(url="/admin/plans", status_code=303)


# ══════════════════════════════════════════════════════════════════════════
# Telegram — configuration status + recent delivery failures. Read-only:
# channel config comes from environment (app/config.py), not this page.
# ══════════════════════════════════════════════════════════════════════════
@router.get("/telegram", response_class=HTMLResponse)
def telegram_page(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    settings = get_settings()
    chats = db.execute(select(TelegramChat).order_by(TelegramChat.created_at)).scalars().all()
    chat_rows = "".join(
        f"<tr><td>{esc(c.chat_id)}</td><td>{esc(c.role.value)}</td><td>{esc(c.title or '—')}</td>"
        f"<td><span class='badge {'ok' if c.active else ''}'>{'active' if c.active else 'inactive'}</span></td></tr>"
        for c in chats
    )
    failed = db.execute(
        select(CallMessage).where(CallMessage.delivery_status == DeliveryStatus.FAILED)
        .order_by(CallMessage.created_at.desc()).limit(50)
    ).scalars().all()
    failed_rows = "".join(
        f"<tr><td>{esc(m.call.trade_id) if m.call else '—'}</td><td>{esc(m.telegram_chat_id)}</td>"
        f"<td>{esc(m.message_type.value)}</td><td>{m.retry_count}</td><td>{esc(m.error or '')}</td></tr>"
        for m in failed
    )
    cfg_cards = "".join([
        _card("Bot configured", "yes" if settings.telegram_configured else "no", "ok" if settings.telegram_configured else "neg"),
        _card("Free chat (also results)", "set" if settings.TELEGRAM_FREE_CHAT_ID else "unset"),
        _card("Premium chat", "set" if settings.TELEGRAM_PREMIUM_CHAT_ID else "unset"),
        _card("Webhook secret", "set" if settings.TELEGRAM_WEBHOOK_SECRET else "unset",
              "" if settings.TELEGRAM_WEBHOOK_SECRET else "neg"),
        _card("Free channel link", "set" if settings.TELEGRAM_FREE_CHANNEL_LINK else "unset"),
    ])
    body = f"""
    <div class="section"><h2>Configuration</h2><div class="cards">{cfg_cards}</div></div>
    <div class="section"><h2>Configured chats</h2>
    <table><tr><th>Chat ID</th><th>Role</th><th>Title</th><th>Status</th></tr>
    {chat_rows or '<tr><td colspan="4">No chats registered in telegram_chats.</td></tr>'}</table></div>
    <div class="section"><h2>Recent delivery failures</h2>
    <table><tr><th>Trade ID</th><th>Chat</th><th>Type</th><th>Retries</th><th>Error</th></tr>
    {failed_rows or '<tr><td colspan="5">No delivery failures on record.</td></tr>'}</table></div>
    """
    return _page("Telegram", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# Audit logs
# ══════════════════════════════════════════════════════════════════════════
@router.get("/audit", response_class=HTMLResponse)
def audit_log_page(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    events = db.execute(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(300)).scalars().all()
    rows = "".join(
        f"<tr><td>{e.created_at.strftime('%Y-%m-%d %H:%M:%S')}</td><td>{esc(e.event_type)}</td>"
        f"<td>{esc(e.actor or '—')}</td>"
        f"<td><span class='badge {'ok' if e.result == 'SUCCESS' else 'bad'}'>{esc(e.result)}</span></td>"
        f"<td>{esc(e.detail or '')}</td></tr>"
        for e in events
    )
    body = f"""
    <div class="section"><h2>Audit log (most recent 300)</h2>
    <table><tr><th>When</th><th>Event</th><th>Actor</th><th>Result</th><th>Detail</th></tr>
    {rows or '<tr><td colspan="5">No audit events yet.</td></tr>'}</table></div>"""
    return _page("Audit Logs", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# System health
# ══════════════════════════════════════════════════════════════════════════
@router.get("/health", response_class=HTMLResponse)
def system_health_page(
    db: Session = Depends(get_db), admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    settings = get_settings()

    db_ok = True
    try:
        db.execute(select(func.count()).select_from(Call))
    except Exception:
        db_ok = False

    failed_messages = db.query(CallMessage).filter_by(delivery_status=DeliveryStatus.FAILED).count()
    open_tickets = db.query(SupportTicket).filter_by(status=TicketStatus.OPEN).count()
    pending_payments = db.query(Payment).filter_by(status=PaymentStatus.PENDING).count()
    prod_problems = settings.assert_production_ready()

    cards = "".join([
        _card("Database", "reachable" if db_ok else "UNREACHABLE", "ok" if db_ok else "neg"),
        _card("Environment", esc(settings.ENV)),
        _card("Telegram bot", "configured" if settings.telegram_configured else "not configured",
              "ok" if settings.telegram_configured else "warn"),
        _card("Payment provider", esc(settings.PAYMENT_PROVIDER)),
        _card("Failed Telegram deliveries", str(failed_messages), "neg" if failed_messages else ""),
        _card("Open support tickets", str(open_tickets), "warn" if open_tickets else ""),
        _card("Pending payments awaiting confirmation", str(pending_payments), "warn" if pending_payments else ""),
    ])
    prod_html = ""
    if prod_problems:
        items = "".join(f"<li>{esc(p)}</li>" for p in prod_problems)
        prod_html = f'<div class="error"><strong>Not production-ready:</strong><ul>{items}</ul></div>'
    elif settings.is_production:
        prod_html = '<p><span class="badge ok">Production configuration checks passed</span></p>'

    body = f'<div class="cards">{cards}</div>{prod_html}'
    return _page("System Health", admin, body)


# ══════════════════════════════════════════════════════════════════════════
# Settings — read-only. Never renders a secret VALUE, only whether one is
# configured, matching System Health's pattern above.
# ══════════════════════════════════════════════════════════════════════════
@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    admin: AdminUser = Depends(require_admin),
    _rl: None = Depends(_admin_read_limit),
):
    settings = get_settings()
    rows = [
        ("ENV", settings.ENV), ("DEBUG", str(settings.DEBUG)),
        ("DATABASE_URL", "sqlite (dev)" if settings.is_sqlite else "postgresql (configured)"),
        ("API_PREFIX", settings.API_PREFIX),
        ("ADAPTER_API_KEYS configured", "yes" if settings.adapter_api_key_list else "no"),
        ("ADMIN_SESSION_SECRET set", "yes" if settings.ADMIN_SESSION_SECRET else "no (falls back to ADAPTER_API_KEYS in dev)"),
        ("ADMIN_BOOTSTRAP_TOKEN set", "yes" if settings.ADMIN_BOOTSTRAP_TOKEN else "no"),
        ("PAYMENT_PROVIDER", settings.PAYMENT_PROVIDER),
        ("TELEGRAM_BOT_TOKEN set", "yes" if settings.TELEGRAM_BOT_TOKEN else "no"),
        ("TELEGRAM_WEBHOOK_SECRET set", "yes" if settings.TELEGRAM_WEBHOOK_SECRET else "no"),
        ("CORS_ORIGINS", ", ".join(settings.cors_origin_list) or "(none configured)"),
    ]
    table_rows = "".join(f"<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>" for k, v in rows)
    body = f"""
    <div class="section"><h2>Configuration (read-only — set via environment variables)</h2>
    <table><tr><th>Setting</th><th>Value</th></tr>{table_rows}</table>
    <p style="color:#7c8798;font-size:12px">Secrets are never displayed here — only whether a value is configured.</p>
    </div>"""
    return _page("Settings", admin, body)
