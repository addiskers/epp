"""
Admin API — `/api/epp/*`. Session-gated (bearer token). Two roles:

* admin      — everything: tickets, routing config, agents, users, call logs, audit log.
* dept_user  — the tickets assigned to THEIR department, and the transcripts/recordings
               reachable through those tickets. Nothing else.

Every state change and every transcript/recording view writes an audit row.
"""

import csv
import io
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import agent_tools
import audit
import calling_window
import campaign_runner
import campaigns
import contacts_import
import eo_auth
import eo_db
import languages
import prompt_render
import data_reset
import routing
import store
import subscription
import tickets

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/epp")


def _invalidate_call_cache():
    """Drop main's cached agent/routing context after an edit, so the next call speaks the
    new configuration. Imported lazily: main imports this module."""
    try:
        import main
        main.invalidate_ctx_cache()
    except Exception:
        logger.debug("could not invalidate the call context cache", exc_info=True)


def _active_calls():
    try:
        import main
        return main.active_calls()
    except Exception:
        return []


async def _body(request: Request) -> dict:
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _clean_str(body, key, *, max_len=500, required=False, label=None):
    val = str(body.get(key) or "").strip()
    if required and not val:
        raise HTTPException(status_code=400, detail=f"{label or key} is required")
    if len(val) > max_len:
        raise HTTPException(status_code=400, detail=f"{label or key} is too long (max {max_len} characters)")
    return val


def _whole_int(v, name, lo, hi):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number")
    if f != int(f):
        raise HTTPException(status_code=400, detail=f"{name} must be a whole number (no decimals)")
    n = int(f)
    if n < lo or n > hi:
        raise HTTPException(status_code=400, detail=f"{name} must be between {lo} and {hi}")
    return n


def _user_public(user):
    dep = eo_db.get_department(user.get("department_id")) if user.get("department_id") else None
    return {"id": user["id"], "username": user["username"], "name": user.get("name"),
            "role": user["role"], "department_id": user.get("department_id"),
            "department_name": (dep or {}).get("name")}


# Admin pages that can be taken off the client's menu. The super admin picks them on the Super
# admin page (stored in settings["hidden_pages"]); until then EPP_HIDDEN_PAGES decides (a
# comma-separated list of these keys; blank = show everything). The super admin always sees
# every page, with the hidden ones marked. The routes stay wired either way.
UI_PAGES = ("campaigns", "contacts", "scheduler", "routing", "agents", "call-logs", "users", "audit",
            "subscription")
_DEFAULT_HIDDEN_PAGES = "campaigns,contacts,scheduler"


def client_hidden_pages() -> tuple:
    """(hidden page keys for the client's admins, 'db' | 'env')."""
    try:
        stored = eo_db.get_setting("hidden_pages")
    except Exception:
        stored = None
    if isinstance(stored, list):
        wanted = {str(p).strip().lower() for p in stored}
        return [p for p in UI_PAGES if p in wanted], "db"
    raw = os.getenv("EPP_HIDDEN_PAGES")
    if raw is None:
        raw = _DEFAULT_HIDDEN_PAGES
    wanted = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return [p for p in UI_PAGES if p in wanted], "env"


def ui_config(user=None) -> dict:
    hidden, _source = client_hidden_pages()
    superadmin = eo_auth.is_superadmin(user)
    return {"hidden_pages": [] if superadmin else hidden, "client_hidden_pages": hidden,
            "superadmin": superadmin}


# ---------------------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------------------
@router.post("/login")
async def login(request: Request):
    body = await _body(request)
    username = (body.get("username") or "").strip().lower()
    user = eo_auth.authenticate(username, body.get("password") or "")
    if not user:
        audit.log("login_failed", user={"username": username}, request=request)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    audit.log("login", user=user, request=request)
    return {"ok": True, "token": eo_auth.issue_token(user), "user": _user_public(user), "ui": ui_config(user)}


@router.post("/logout")
async def logout(request: Request):
    try:
        user = eo_auth.require_user(request)
        audit.log("logout", user=user, request=request)
    except HTTPException:
        pass
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = eo_auth.require_user(request)
    return {"ok": True, "user": _user_public(user), "ui": ui_config(user)}


@router.post("/me/password")
async def me_password(request: Request):
    user = eo_auth.require_user(request)
    body = await _body(request)
    if not eo_auth.verify_password(body.get("current") or "", user["password_hash"], user["password_salt"]):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    new = body.get("new") or ""
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters")
    h, s = eo_auth.hash_password(new)
    eo_db.update_user_password(user["id"], h, s)
    audit.log("password_changed", user=user, target=f"user:{user['id']}", request=request)
    return {"ok": True}


# ---------------------------------------------------------------------------------------
# Users (admin)
# ---------------------------------------------------------------------------------------
@router.get("/users")
async def users_list(request: Request):
    eo_auth.require_admin(request)
    return JSONResponse({"items": eo_db.list_users()})


@router.post("/users")
async def users_create(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    username = (body.get("username") or "").strip().lower()
    if not username:
        raise HTTPException(status_code=400, detail="Username is required")
    if eo_db.get_user_by_username(username):
        raise HTTPException(status_code=409, detail="That username already exists")
    password = body.get("password") or ""
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    role = body.get("role") if body.get("role") in eo_db.ROLES else "dept_user"
    department_id = body.get("department_id") or None
    if role == "dept_user" and not department_id:
        raise HTTPException(status_code=400, detail="A department user needs a department")
    if department_id and not eo_db.get_department(department_id):
        raise HTTPException(status_code=400, detail="Department not found")
    h, s = eo_auth.hash_password(password)
    uid = eo_db.create_user(username, (body.get("name") or "").strip(), h, s, role, department_id=department_id)
    audit.log("user_created", user=admin, target=f"user:{uid}",
              detail={"username": username, "role": role, "department_id": department_id}, request=request)
    return {"ok": True, "id": uid}


@router.patch("/users/{user_id}")
async def users_update(user_id: int, request: Request):
    """Edit a user (the Users page's Save). Every field is checked before any is written, so a
    bad department never leaves a half-applied role or status behind."""
    admin = eo_auth.require_admin(request)
    target = eo_db.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    body = await _body(request)
    changes = {}
    if "name" in body:
        changes["name"] = _clean_str(body, "name", max_len=120)
    if "active" in body:
        if int(user_id) == int(admin["id"]) and not body["active"]:
            raise HTTPException(status_code=400, detail="You cannot disable your own account")
        changes["active"] = bool(body["active"])
    role = target["role"]
    if "role" in body:
        if body["role"] not in eo_db.ROLES:
            raise HTTPException(status_code=400, detail="Role must be admin or dept_user")
        if int(user_id) == int(admin["id"]) and body["role"] != "admin":
            raise HTTPException(status_code=400, detail="You cannot demote your own account")
        changes["role"] = role = body["role"]
    dep = target.get("department_id")
    if "department_id" in body:
        dep = body.get("department_id") or None
        if dep and not eo_db.get_department(dep):
            raise HTTPException(status_code=400, detail="Department not found")
        changes["department_id"] = dep
    if role == "dept_user" and not dep:
        raise HTTPException(status_code=400, detail="A department user needs a department")
    password = body.get("password") or ""
    if password:
        if len(password) < 8:
            raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not changes and not password:
        return {"ok": True, "user": eo_db.get_user(user_id)}

    if "name" in changes:
        eo_db.set_user_name(int(user_id), changes["name"])
    if "active" in changes:
        eo_db.set_user_active(int(user_id), changes["active"])
    if "role" in changes:
        eo_db.set_user_role(int(user_id), changes["role"])
    if "department_id" in changes:
        eo_db.set_user_department(int(user_id), changes["department_id"])
    if password:
        h, s = eo_auth.hash_password(password)
        eo_db.update_user_password(int(user_id), h, s)
        changes["password"] = "reset"
    audit.log("user_updated", user=admin, target=f"user:{user_id}", detail=changes, request=request)
    return {"ok": True, "user": eo_db.get_user(user_id)}


# ---------------------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------------------
@router.get("/languages")
async def languages_list(request: Request):
    eo_auth.require_user(request)
    return JSONResponse({"items": languages.enabled(), "all": languages.LANGUAGES})


@router.get("/departments")
async def departments_list(request: Request):
    eo_auth.require_user(request)
    active = request.query_params.get("active")
    return JSONResponse({"items": eo_db.list_departments(active_only=active in ("1", "true"))})


@router.post("/departments")
async def departments_create(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    name = _clean_str(body, "name", max_len=80, required=True, label="Department name")
    code = _clean_str(body, "code", max_len=16, required=True, label="Code").upper()
    if eo_db.get_department_by_code(code):
        raise HTTPException(status_code=409, detail="That code is already used")
    order = _whole_int(body.get("sort_order", len(eo_db.list_departments())), "Sort order", 0, 999)
    did = eo_db.create_department(name, code, order)
    _invalidate_call_cache()
    audit.log("department_created", user=admin, target=f"department:{did}",
              detail={"name": name, "code": code}, request=request)
    return JSONResponse(eo_db.get_department(did), status_code=201)


@router.patch("/departments/{department_id}")
async def departments_update(department_id: int, request: Request):
    admin = eo_auth.require_admin(request)
    if not eo_db.get_department(department_id):
        raise HTTPException(status_code=404, detail="Department not found")
    body = await _body(request)
    fields = {}
    if "name" in body:
        fields["name"] = _clean_str(body, "name", max_len=80, required=True, label="Department name")
    if "code" in body:
        code = _clean_str(body, "code", max_len=16, required=True, label="Code").upper()
        other = eo_db.get_department_by_code(code)
        if other and other["id"] != department_id:
            raise HTTPException(status_code=409, detail="That code is already used")
        fields["code"] = code
    if "active" in body:
        fields["active"] = 1 if body["active"] else 0
    if "sort_order" in body:
        fields["sort_order"] = _whole_int(body["sort_order"], "Sort order", 0, 999)
    eo_db.update_department(department_id, **fields)
    _invalidate_call_cache()
    audit.log("department_updated", user=admin, target=f"department:{department_id}", detail=fields, request=request)
    return JSONResponse(eo_db.get_department(department_id))


@router.get("/categories")
async def categories_list(request: Request):
    eo_auth.require_user(request)
    qp = request.query_params
    return JSONResponse({"items": eo_db.list_categories(
        active_only=qp.get("active") in ("1", "true"), caller_type=qp.get("caller_type") or None),
        "caller_types": list(routing.CALLER_TYPES),
        "priority_reasons": list(routing.PRIORITY_REASONS)})


def _category_fields(body, *, creating=False):
    fields = {}
    if creating or "caller_type" in body:
        ct = routing.normalize_caller_type(body.get("caller_type"))
        if not ct:
            raise HTTPException(status_code=400, detail="caller_type must be customer, vendor or employee")
        fields["caller_type"] = ct
    if creating or "name" in body:
        fields["name"] = _clean_str(body, "name", max_len=80, required=True, label="Category name")
    if "department_id" in body:
        dep = body.get("department_id") or None
        if dep and not eo_db.get_department(dep):
            raise HTTPException(status_code=400, detail="Department not found")
        fields["department_id"] = dep
    if "keywords" in body:
        kws = body.get("keywords") or []
        if isinstance(kws, str):
            kws = [k.strip() for k in kws.split(",")]
        fields["keywords"] = [str(k).strip()[:60] for k in kws if str(k).strip()][:40]
    if "high_priority" in body:
        fields["high_priority"] = 1 if body["high_priority"] else 0
    if "active" in body:
        fields["active"] = 1 if body["active"] else 0
    if "sort_order" in body:
        fields["sort_order"] = _whole_int(body["sort_order"], "Sort order", 0, 999)
    return fields


@router.post("/categories")
async def categories_create(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    f = _category_fields(body, creating=True)
    if routing.resolve_category(f["caller_type"], f["name"], eo_db.list_categories()) and any(
            c["name"].lower() == f["name"].lower() and c["caller_type"] == f["caller_type"]
            for c in eo_db.list_categories()):
        raise HTTPException(status_code=409, detail="That category already exists for this caller type")
    cid = eo_db.create_category(f["caller_type"], f["name"], department_id=f.get("department_id"),
                                keywords=f.get("keywords"), high_priority=f.get("high_priority", 0),
                                sort_order=f.get("sort_order", 0))
    _invalidate_call_cache()
    audit.log("category_created", user=admin, target=f"category:{cid}", detail=f, request=request)
    return JSONResponse(eo_db.get_category(cid), status_code=201)


@router.patch("/categories/{category_id}")
async def categories_update(category_id: int, request: Request):
    admin = eo_auth.require_admin(request)
    if not eo_db.get_category(category_id):
        raise HTTPException(status_code=404, detail="Category not found")
    body = await _body(request)
    f = _category_fields(body)
    eo_db.update_category(category_id, **f)
    _invalidate_call_cache()
    audit.log("category_updated", user=admin, target=f"category:{category_id}", detail=f, request=request)
    return JSONResponse(eo_db.get_category(category_id))


@router.delete("/categories/{category_id}")
async def categories_delete(category_id: int, request: Request):
    admin = eo_auth.require_admin(request)
    cat = eo_db.get_category(category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if cat["name"].lower() == "other":
        raise HTTPException(status_code=400, detail="'Other' is the fallback for its caller type and cannot be deleted")
    eo_db.delete_category(category_id)
    _invalidate_call_cache()
    audit.log("category_deleted", user=admin, target=f"category:{category_id}",
              detail={"caller_type": cat["caller_type"], "name": cat["name"]}, request=request)
    return {"ok": True}


# ---------------------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------------------
def _ticket_or_404(user, ticket_id):
    """The ticket, if this user may see it. 404 (not 403) so an id probe reveals nothing."""
    t = eo_db.get_ticket(ticket_id)
    if not t:
        raise HTTPException(status_code=404, detail="Ticket not found")
    scope = eo_auth.scope_department(user)
    if scope is not None and t.get("assigned_department_id") != scope:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return t


def _ticket_filters(request: Request, user):
    qp = request.query_params
    esc = qp.get("escalated")
    return dict(
        q=qp.get("q") or None, status=qp.get("status") or None, priority=qp.get("priority") or None,
        department_id=qp.get("department_id") or None, caller_type=qp.get("caller_type") or None,
        escalated=(esc in ("1", "true")) if esc not in (None, "") else None,
        date_from=qp.get("from") or None, date_to=qp.get("to") or None,
        assigned_user_id=qp.get("assigned_user_id") or None,
        scope_department_id=eo_auth.scope_department(user),
        sort=qp.get("sort") or "created_at", direction=qp.get("dir") or "desc",
    )


def _decorate(t):
    t = dict(t)
    t["status_label"] = tickets.STATUS_LABEL.get(t.get("status"), t.get("status"))
    try:
        t["escalation_flags"] = json.loads(t.get("escalation_flags") or "[]")
    except ValueError:
        t["escalation_flags"] = []
    t["has_recording"] = store.has_recording(t.get("call_sid"))
    return t


@router.get("/tickets")
async def tickets_list(request: Request):
    user = eo_auth.require_user(request)
    qp = request.query_params
    f = _ticket_filters(request, user)
    data = eo_db.list_tickets(limit=int(qp.get("limit") or 50), offset=int(qp.get("offset") or 0), **f)
    data["items"] = [_decorate(t) for t in data["items"]]
    return JSONResponse(data)


@router.get("/tickets.csv")
async def tickets_csv(request: Request):
    user = eo_auth.require_user(request)
    data = eo_db.list_tickets(limit=None, **_ticket_filters(request, user))
    buf = io.StringIO()
    cols = ["ticket_id", "created_at", "status", "priority", "escalation_flag", "caller_type",
            "caller_name", "contact_number", "company_name", "vendor_code", "employee_id",
            "caller_department", "plant_location", "language", "category", "subcategory",
            "assigned_department", "assigned_user_name", "ai_summary", "source"]
    w = csv.writer(buf)
    w.writerow(cols)
    for t in data["items"]:
        w.writerow([t.get(c) for c in cols])
    audit.log("tickets_exported", user=user, detail={"rows": len(data["items"])}, request=request)
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=tickets.csv"})


@router.get("/tickets/{ticket_id}")
async def ticket_detail(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    t = _decorate(_ticket_or_404(user, ticket_id))
    t["events"] = eo_db.list_ticket_events(ticket_id)
    return JSONResponse(t)


@router.post("/tickets")
async def ticket_create_manual(request: Request):
    """A ticket typed in by hand (a walk-in, an email) — same routing as a call."""
    user = eo_auth.require_user(request)
    body = await _body(request)
    res = tickets.create_from_tool({
        "caller_type": body.get("caller_type"), "language": body.get("language") or "en",
        "caller_name": _clean_str(body, "caller_name", max_len=120, required=True, label="Caller name"),
        "contact_number": body.get("contact_number") or "",
        "company_name": body.get("company_name") or "", "vendor_code": body.get("vendor_code") or "",
        "employee_id": body.get("employee_id") or "", "caller_department": body.get("caller_department") or "",
        "plant_location": body.get("plant_location") or "",
        "description": _clean_str(body, "description", max_len=6000, required=True, label="Description"),
        "category": body.get("category") or "Other", "subcategory": body.get("subcategory") or "",
        "high_priority_reason": body.get("high_priority_reason") or "none",
    }, {})
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error") or "Could not create the ticket")
    eo_db.update_ticket(res["ticket_id"], source="manual")
    eo_db.add_ticket_event(res["ticket_id"], "note", actor_type="user", actor_user_id=user["id"],
                           actor_name=user.get("name") or user["username"], note="Created manually")
    audit.log("ticket_created_manual", user=user, target=f"ticket:{res['ticket_id']}", request=request)
    return JSONResponse(_decorate(eo_db.get_ticket(res["ticket_id"])), status_code=201)


def _run_ticket_edit(user, ticket_id, fn, action, request, detail=None):
    t = _ticket_or_404(user, ticket_id)
    try:
        updated = fn(t)
    except tickets.TicketError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log(action, user=user, target=f"ticket:{ticket_id}", detail=detail, request=request)
    return JSONResponse(_decorate(updated))


@router.post("/tickets/{ticket_id}/status")
async def ticket_status(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    body = await _body(request)
    status = str(body.get("status") or "").strip().lower()
    note = _clean_str(body, "note", max_len=2000)
    return _run_ticket_edit(user, ticket_id, lambda t: tickets.change_status(t, status, user, note),
                            "ticket_status", request, {"status": status, "note": note})


@router.post("/tickets/{ticket_id}/assign")
async def ticket_assign(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    body = await _body(request)
    dep = body.get("department_id") if "department_id" in body else None
    uid = body.get("user_id") if "user_id" in body else None
    if dep not in (None, "") and not eo_auth.is_admin(user):
        raise HTTPException(status_code=403, detail="Only an admin can move a ticket to another department")
    if dep is not None and dep != "":
        dep = _whole_int(dep, "department_id", 1, 10 ** 9)
    if uid is not None and uid != "":
        uid = _whole_int(uid, "user_id", 1, 10 ** 9)
    note = _clean_str(body, "note", max_len=2000)
    return _run_ticket_edit(user, ticket_id,
                            lambda t: tickets.assign(t, department_id=dep, user_id=uid, user=user, note=note),
                            "ticket_assigned", request, {"department_id": dep, "user_id": uid})


@router.post("/tickets/{ticket_id}/note")
async def ticket_note(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    body = await _body(request)
    note = _clean_str(body, "note", max_len=2000, required=True, label="Note")
    return _run_ticket_edit(user, ticket_id, lambda t: tickets.add_note(t, note, user),
                            "ticket_note", request, {"chars": len(note)})


@router.post("/tickets/{ticket_id}/priority")
async def ticket_priority(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    body = await _body(request)
    pr = str(body.get("priority") or "").strip().lower()
    note = _clean_str(body, "note", max_len=2000)
    return _run_ticket_edit(user, ticket_id, lambda t: tickets.set_priority(t, pr, user, note),
                            "ticket_priority", request, {"priority": pr})


@router.post("/tickets/{ticket_id}/update")
async def ticket_update(ticket_id: str, request: Request):
    """The ticket page's single Save: any of category / department_id / user_id / priority /
    status / note in one request, all-or-nothing."""
    user = eo_auth.require_user(request)
    body = await _body(request)
    changes = {k: body[k] for k in tickets.EDIT_KEYS if k in body}
    for key in ("department_id", "user_id"):
        if changes.get(key) not in (None, ""):
            changes[key] = _whole_int(changes[key], key, 1, 10 ** 9)
    if "note" in changes:
        changes["note"] = _clean_str(body, "note", max_len=2000)
    detail = {k: v for k, v in changes.items() if k != "note"}
    if changes.get("note"):
        detail["note_chars"] = len(changes["note"])
    return _run_ticket_edit(user, ticket_id, lambda t: tickets.apply_edit(t, changes, user),
                            "ticket_updated", request, detail)


@router.post("/tickets/{ticket_id}/reclassify")
async def ticket_reclassify(ticket_id: str, request: Request):
    user = eo_auth.require_admin(request)
    body = await _body(request)
    cat = _clean_str(body, "category", max_len=80, required=True, label="Category")
    note = _clean_str(body, "note", max_len=2000)
    return _run_ticket_edit(user, ticket_id, lambda t: tickets.reclassify(t, cat, user, note),
                            "ticket_reclassified", request, {"category": cat})


@router.get("/tickets/{ticket_id}/transcript")
async def ticket_transcript(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    t = _ticket_or_404(user, ticket_id)
    call = await store.load_call(t.get("call_id")) if t.get("call_id") else None
    audit.log("transcript_viewed", user=user, target=f"ticket:{ticket_id}", request=request)
    if not call:
        return JSONResponse({"ticket_id": ticket_id, "messages": [], "call": None})
    return JSONResponse({
        "ticket_id": ticket_id,
        "messages": call.get("transcript") or [],
        "call": {"id": call.get("id"), "started_at": call.get("started_at"),
                 "duration_seconds": call.get("duration_seconds"), "language": call.get("language"),
                 "source": call.get("source"), "status": call.get("status"),
                 "analysis": call.get("analysis"), "has_recording": store.has_recording(call.get("call_sid"))},
    })


@router.get("/tickets/{ticket_id}/audio")
async def ticket_audio(ticket_id: str, request: Request):
    user = eo_auth.require_user(request)
    t = _ticket_or_404(user, ticket_id)
    sid = t.get("call_sid")
    if not store.has_recording(sid):
        raise HTTPException(status_code=404, detail="Recording not found")
    audit.log("recording_played", user=user, target=f"ticket:{ticket_id}", request=request)
    return FileResponse(store.recording_path(sid), media_type="audio/wav", filename=f"{ticket_id}.wav")


# ---------------------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------------------
@router.get("/summary")
async def summary(request: Request):
    user = eo_auth.require_user(request)
    scope = eo_auth.scope_department(user)
    out = {"tickets": eo_db.ticket_stats(scope_department_id=scope)}
    out["tickets"]["recent_escalated"] = [_decorate(t) for t in out["tickets"]["recent_escalated"]]
    if eo_auth.is_admin(user):
        calls = await store.summary({})
        # Model cost is ours, not the client's: the Subscription page shows minutes and ₹.
        calls = {k: v for k, v in calls.items() if k not in ("total_cost_usd", "avg_cost_per_call")}
        calls["by_day"] = [{k: v for k, v in d.items() if k != "cost_usd"} for d in calls.get("by_day") or []]
        out["calls"] = calls
        out["live_calls"] = _active_calls()
        out["gemini_status"] = _gemini_status()
    return JSONResponse(out)


def _gemini_status():
    """The last voice-model failure, if recent, so the dashboard can say why calls are silent."""
    try:
        import main
        return main.gemini_status()
    except Exception:
        return {}


@router.post("/live/token")
async def live_token(request: Request):
    user = eo_auth.require_admin(request)
    return {"token": eo_auth.issue_live_token(user)}


# ---------------------------------------------------------------------------------------
# Super admin (the service provider): the client's menu, and clearing test data
# ---------------------------------------------------------------------------------------
def _superadmin_state() -> dict:
    hidden, source = client_hidden_pages()
    return {"pages": list(UI_PAGES), "client_hidden_pages": hidden, "source": source,
            "counts": data_reset.counts(), "live_calls": len(_active_calls()),
            "backup_root": data_reset.backup_root()}


@router.get("/superadmin")
async def superadmin_get(request: Request):
    eo_auth.require_superadmin(request)
    return JSONResponse(_superadmin_state())


@router.put("/superadmin/pages")
async def superadmin_pages(request: Request):
    """Which pages the client's admins see. {"hidden_pages": [...]} or {"reset": true} (back to .env)."""
    user = eo_auth.require_superadmin(request)
    body = await _body(request)
    if body.get("reset"):
        eo_db.delete_setting("hidden_pages")
        audit.log("client_pages_reset", user=user, request=request)
    else:
        raw = body.get("hidden_pages")
        if not isinstance(raw, list):
            raise HTTPException(status_code=400, detail="hidden_pages must be a list of page keys")
        wanted = {str(p).strip().lower() for p in raw}
        clean = [p for p in UI_PAGES if p in wanted]
        eo_db.set_setting("hidden_pages", clean, updated_by=user["username"])
        audit.log("client_pages_updated", user=user, detail={"hidden_pages": clean}, request=request)
    return JSONResponse(_superadmin_state())


@router.post("/superadmin/reset-data")
async def superadmin_reset_data(request: Request):
    """Delete test data before go-live (backed up first). Body: {"confirm": "DELETE",
    "parts": ["tickets", "calls", "audit", "outbound"]}. Refused while a call is on the line."""
    user = eo_auth.require_superadmin(request)
    body = await _body(request)
    if str(body.get("confirm") or "").strip() != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm")
    if _active_calls():
        raise HTTPException(status_code=409, detail="A call is on the line right now. Try again when it has ended.")
    try:
        result = await data_reset.reset(body.get("parts"), user)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse(dict(result, state=_superadmin_state()))


# ---------------------------------------------------------------------------------------
# Subscription (the client's plan and usage)
# ---------------------------------------------------------------------------------------
@router.get("/subscription")
async def subscription_get(request: Request):
    user = eo_auth.require_admin(request)
    return JSONResponse(subscription.snapshot(user))


@router.put("/subscription")
async def subscription_put(request: Request):
    """Override the .env plan (service provider only). {"reset": true} goes back to .env."""
    user = eo_auth.require_superadmin(request)
    body = await _body(request)
    if body.get("reset"):
        eo_db.delete_setting(subscription.SETTING_KEY)
        audit.log("subscription_reset", user=user, request=request)
    else:
        try:
            clean = subscription.validate(body)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        if not clean:
            raise HTTPException(status_code=400, detail="Nothing to save")
        eo_db.set_setting(subscription.SETTING_KEY, clean, updated_by=user["username"])
        audit.log("subscription_updated", user=user, detail=clean, request=request)
    return JSONResponse(subscription.snapshot(user))


# ---------------------------------------------------------------------------------------
# Call logs (admin)
# ---------------------------------------------------------------------------------------
# Model cost and token counts stay on the record for us; the client UI never sees them.
_CALL_COST_KEYS = {"gemini_cost_usd", "tokens", "gemini_model"}


def _strip_cost(call: dict) -> dict:
    for key in _CALL_COST_KEYS:
        call.pop(key, None)
    return call


def _call_filters(request: Request) -> dict:
    qp = request.query_params
    wt = qp.get("with_ticket")
    return {"source": qp.get("source") or None, "from": qp.get("from") or None,
            "to": qp.get("to") or None, "q": qp.get("q") or None,
            "with_ticket": (wt in ("1", "true")) if wt not in (None, "") else None,
            "campaign_id": qp.get("campaign_id") or None,
            "limit": qp.get("limit"), "offset": qp.get("offset")}


@router.get("/calls")
async def calls_list(request: Request):
    eo_auth.require_admin(request)
    filters = _call_filters(request)
    if filters.get("limit") is None:
        filters["limit"] = 100
    data = await store.list_calls(filters)
    for c in data["items"]:
        _strip_cost(c)
        c["has_recording"] = store.has_recording(c.get("call_sid"))
        c.setdefault("caller_name", "")
    return JSONResponse(data)


@router.get("/calls/{call_id}")
async def call_detail(call_id: str, request: Request):
    user = eo_auth.require_admin(request)
    call = await store.load_call(call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    call = _strip_cost(dict(call))
    call.setdefault("messages", call.get("transcript") or [])
    call["has_recording"] = store.has_recording(call.get("call_sid"))
    call["tickets"] = [_decorate(t) for t in eo_db.tickets_by_call(call_id)]
    if not call.get("caller_name"):
        call["caller_name"] = next((t.get("caller_name") for t in call["tickets"] if t.get("caller_name")), "")
    audit.log("transcript_viewed", user=user, target=f"call:{call_id}", request=request)
    return JSONResponse(call)


@router.get("/calls/{call_id}/audio")
async def call_audio(call_id: str, request: Request):
    user = eo_auth.require_admin(request)
    call = await store.load_call(call_id)
    if not call or not store.has_recording(call.get("call_sid")):
        raise HTTPException(status_code=404, detail="Recording not found")
    audit.log("recording_played", user=user, target=f"call:{call_id}", request=request)
    return FileResponse(store.recording_path(call["call_sid"]), media_type="audio/wav",
                        filename=f"call-{call_id}.wav")


# ---------------------------------------------------------------------------------------
# Agents (admin)
# ---------------------------------------------------------------------------------------
def _agent_public(a):
    """The row plus whether its script is the shipped one (auto-updates on deploy) or customised."""
    return dict(a, script_state=eo_db.agent_script_state(a))


@router.get("/agents")
async def agents_list(request: Request):
    eo_auth.require_admin(request)
    return JSONResponse({"items": [_agent_public(a) for a in eo_db.list_agents()],
                         "placeholders": sorted(prompt_render.KNOWN_PLACEHOLDERS)})


def _agent_or_404(agent_id):
    a = eo_db.get_agent(agent_id)
    if not a:
        raise HTTPException(status_code=404, detail="Agent not found")
    return a


@router.get("/agents/{agent_id}")
async def agents_detail(agent_id: int, request: Request):
    eo_auth.require_admin(request)
    return JSONResponse(_agent_public(_agent_or_404(agent_id)))


@router.get("/agents/{agent_id}/versions")
async def agents_versions(agent_id: int, request: Request):
    """Every script text an auto-update or a reset replaced on this agent, newest first."""
    eo_auth.require_admin(request)
    _agent_or_404(agent_id)
    return JSONResponse({"items": eo_db.list_agent_versions(agent_id)})


@router.patch("/agents/{agent_id}")
async def agents_update(agent_id: int, request: Request):
    admin = eo_auth.require_admin(request)
    _agent_or_404(agent_id)
    body = await _body(request)
    fields = {}
    for key in ("description", "voice_name", "speech_language_code"):
        if key in body:
            fields[key] = _clean_str(body, key, max_len=200)
    if "name" in body:
        fields["name"] = _clean_str(body, "name", max_len=120, required=True, label="Agent name")
    if "active" in body:
        fields["active"] = 1 if body["active"] else 0
    for tkey in ("prompt_template", "trigger_template"):
        if tkey in body:
            text = str(body.get(tkey) or "").strip()
            if tkey == "prompt_template" and not text:
                raise HTTPException(status_code=400, detail="Prompt is required")
            unknown = prompt_render.validate_template(text)
            if unknown:
                raise HTTPException(status_code=400,
                                    detail="Unknown placeholder(s): " + ", ".join("{%s}" % u for u in unknown)
                                           + ". Check the spelling against the placeholder list.")
            fields[tkey] = text
    if fields.get("prompt_template"):
        for bad in ("record_outcome", "record_rsvp"):
            if bad in fields["prompt_template"]:
                raise HTTPException(status_code=400,
                                    detail=f"The prompt refers to '{bad}', which does not exist. The tools are "
                                           f"create_ticket, lookup_ticket and end_call.")
    eo_db.update_agent(agent_id, **fields)
    _invalidate_call_cache()
    audit.log("agent_updated", user=admin, target=f"agent:{agent_id}", detail=sorted(fields.keys()), request=request)
    return JSONResponse(_agent_public(eo_db.get_agent(agent_id)))


@router.post("/agents/{agent_id}/reset")
async def agents_reset(agent_id: int, request: Request):
    admin = eo_auth.require_admin(request)
    a = _agent_or_404(agent_id)
    if not eo_db.refresh_seed_agent(a["slug"], by=admin["username"]):
        raise HTTPException(status_code=400, detail="This agent has no shipped template to reset to")
    _invalidate_call_cache()
    audit.log("agent_reset", user=admin, target=f"agent:{agent_id}", request=request)
    return JSONResponse(_agent_public(eo_db.get_agent(agent_id)))


@router.post("/agents/{agent_id}/preview")
async def agents_preview(agent_id: int, request: Request):
    """Render this agent exactly as a call would see it."""
    eo_auth.require_admin(request)
    agent = _agent_or_404(agent_id)
    body = await _body(request)
    cats = eo_db.list_categories(active_only=True)
    langs = languages.enabled()
    rendered = prompt_render.render_prompt(
        agent, caller_phone=body.get("caller_phone") or "+919876543210",
        categories=cats, departments=eo_db.list_departments(active_only=True), langs=langs)
    return JSONResponse({"system_instruction": rendered["system_instruction"], "trigger": rendered["trigger"],
                         "missing": rendered["missing"], "tools": agent_tools.build_tools(cats, langs)})


@router.post("/agents/{agent_id}/test-token")
async def agents_test_token(agent_id: int, request: Request):
    user = eo_auth.require_admin(request)
    _agent_or_404(agent_id)
    audit.log("agent_test_browser", user=user, target=f"agent:{agent_id}", request=request)
    return {"token": eo_auth.issue_test_token(user, agent_id=agent_id), "ttl_seconds": eo_auth._TEST_TOKEN_TTL}


@router.post("/agents/{agent_id}/test-call")
async def agents_test_call(agent_id: int, request: Request):
    """Ring a number and let this agent speak — the phone half of the Test panel."""
    user = eo_auth.require_admin(request)
    _agent_or_404(agent_id)
    body = await _body(request)
    phone = tickets._clean_phone(body.get("phone"))
    if len(phone) < 11:
        raise HTTPException(status_code=400, detail="A valid phone number is required")
    import dialer
    res = await dialer.place_call(phone, base_url=os.getenv("PUBLIC_URL"), agent_id=agent_id)
    if res.get("error"):
        raise HTTPException(status_code=502, detail=res["error"])
    audit.log("agent_test_call", user=user, target=f"agent:{agent_id}", detail={"phone": phone}, request=request)
    return JSONResponse(res)


# ---------------------------------------------------------------------------------------
# Outbound campaigns (admin)
# ---------------------------------------------------------------------------------------
_OUTCOME_LABELS = {
    "no_concern": ("No concern", "green"), "confirmed": ("Confirmed", "green"),
    "has_update": ("Gave an update", "green"), "acknowledged": ("Acknowledged", "green"),
    "ticket_created": ("Ticket created", "green"), "answered": ("Answered", "green"),
    "declined": ("Declined", "red"), "wrong_number": ("Wrong number", "red"),
    "callback": ("Callback requested", "amber"), "not_reachable": ("Not reachable", "amber"),
}


def _outcome_label(value):
    return _OUTCOME_LABELS.get(value, (value, "amber"))[0] if value else None


def _parse_iso(value):
    """ISO-8601 -> aware datetime, or None for blank/unparseable (a retry with no due time)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _contact_display(cc, campaign=None, runner_on=True, now=None, now_min=None):
    """Human status for a campaign_contacts row: (display_status, display_variant).
    Explains WHY a past-due retry isn't dialing instead of showing a stale 'Pending'."""
    st = cc.get("call_status")
    outcome = cc.get("outcome")
    if st == "calling":
        return ("In progress", "blue")
    if st == "cancelled":
        return ("Cancelled", "red")
    if st == "failed":
        return ("Unreachable — max attempts", "red")
    if st == "done":
        if outcome in _OUTCOME_LABELS:
            return _OUTCOME_LABELS[outcome]
        return ((str(outcome), "green") if outcome else ("Answered — nothing recorded", "amber"))
    if int(cc.get("attempts") or 0) == 0:
        return ("Queued", "amber")
    reason = _outcome_label(outcome) or (cc.get("last_error") or "no answer")
    nxt = _parse_iso(cc.get("next_attempt_at"))
    if nxt and nxt > (now or datetime.now(timezone.utc)):
        return (f"Retry scheduled — {reason}", "amber")
    if campaign and campaign.get("status") != "live":
        return ("Waiting — campaign not active", "amber")
    if not runner_on:
        return ("Waiting — scheduler off", "amber")
    if campaign is not None:
        start_min, end_min = calling_window.campaign_window(campaign)
        if not calling_window.in_call_window(start_min, end_min, now_min=now_min):
            return ("Waiting for calling hours", "amber")
    return (f"Due now — {reason}", "amber")


def _attach_contact_display(items, campaign=None, runner_on=True):
    now = datetime.now(timezone.utc)
    now_min = calling_window.now_ist_min()
    for cc in items:
        camp = campaign
        if camp is None and cc.get("campaign_status") is not None:
            camp = {"status": cc.get("campaign_status"), "call_start_min": cc.get("campaign_call_start_min"),
                    "call_end_min": cc.get("campaign_call_end_min")}
        label, variant = _contact_display(cc, camp, runner_on, now=now, now_min=now_min)
        cc["display_status"] = label
        cc["display_variant"] = variant
        cc["outcome_label"] = _outcome_label(cc.get("outcome"))
    return items


def _campaign_or_404(campaign_id):
    c = eo_db.get_campaign(campaign_id)
    if not c:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return c


def _cc_or_404(campaign_id, cc_id):
    cc = eo_db.get_campaign_contact(cc_id)
    if not cc or int(cc.get("campaign_id") or 0) != int(campaign_id):
        raise HTTPException(status_code=404, detail="Recipient not found")
    return cc


@router.get("/campaign-types")
async def campaign_types(request: Request):
    eo_auth.require_admin(request)
    import epp_seeds
    return JSONResponse({"items": [
        {"type": t, "label": campaigns.TYPE_LABEL[t], "outcomes": epp_seeds.OUTCOMES[t],
         "agent_slug": epp_seeds.AGENT_FOR_TYPE[t]} for t in eo_db.CAMPAIGN_TYPES]})


# Contacts
@router.get("/contacts")
async def contacts_list(request: Request):
    eo_auth.require_admin(request)
    qp = request.query_params
    return JSONResponse(eo_db.list_contacts(
        q=qp.get("q") or None, caller_type=qp.get("caller_type") or None, status=qp.get("status") or None,
        limit=int(qp.get("limit") or 25), offset=int(qp.get("offset") or 0)))


@router.post("/contacts")
async def contacts_add(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    e164, valid = contacts_import.normalize_phone(body.get("phone"))
    if not e164 or not valid:
        raise HTTPException(status_code=400, detail="A valid phone number is required")
    caller_type = routing.normalize_caller_type(body.get("caller_type")) if body.get("caller_type") else ""
    cid, created = eo_db.add_contact(_clean_str(body, "name", max_len=120), e164, caller_type=caller_type,
                                     notes=_clean_str(body, "notes", max_len=500), source="manual",
                                     created_by=admin["id"])
    audit.log("contact_added", user=admin, target=f"contact:{cid}", detail={"phone": e164, "created": created},
              request=request)
    return {"ok": True, "id": cid, "created": created, "phone": e164}


@router.post("/contacts/import")
async def contacts_upload(request: Request, file: UploadFile = File(...)):
    admin = eo_auth.require_admin(request)
    data = await file.read()
    try:
        rows, rejected, total, unknown = contacts_import.parse_upload(file.filename, data)
    except Exception as e:
        logger.warning("Contact import parse failed: %s", e)
        raise HTTPException(status_code=400, detail="Could not read that file. Use the sample .xlsx / .csv format.")
    added, updated = eo_db.bulk_upsert_contacts(rows, source="upload", created_by=admin["id"])
    invalid = sum(1 for r in rows if r[2] == "invalid")
    audit.log("contacts_imported", user=admin, detail={"file": file.filename, "rows": total, "added": added,
                                                       "updated": updated, "rejected": rejected}, request=request)
    return {"ok": True, "rows_read": total, "added": added, "updated": updated, "invalid": invalid,
            "rejected": rejected, "unknown_headers": unknown[:12]}


@router.get("/contacts/template")
async def contacts_template(request: Request):
    eo_auth.require_admin(request)
    return Response(content=contacts_import.build_template(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=contacts_template.xlsx"})


@router.post("/contacts/delete")
async def contacts_delete(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    ids = body.get("ids") or []
    n = eo_db.delete_contacts(ids)
    audit.log("contacts_deleted", user=admin, detail={"ids": ids[:50], "deleted": n}, request=request)
    return {"ok": True, "deleted": n}


# Campaigns
@router.get("/campaigns")
async def campaigns_list(request: Request):
    eo_auth.require_admin(request)
    qp = request.query_params
    data = eo_db.list_campaigns(q=qp.get("q") or None, status=qp.get("status") or None,
                                limit=int(qp.get("limit") or 50), offset=int(qp.get("offset") or 0))
    for c in data["items"]:
        c["progress"] = eo_db.campaign_progress(c["id"])
        c["type_label"] = campaigns.TYPE_LABEL.get(c["campaign_type"], c["campaign_type"])
    return JSONResponse(data)


@router.post("/campaigns")
async def campaigns_create(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    try:
        c = campaigns.create(admin, body)
    except campaigns.CampaignError as e:
        raise HTTPException(status_code=400, detail=str(e))
    audit.log("campaign_created", user=admin, target=f"campaign:{c['id']}",
              detail={"name": c["name"], "type": c["campaign_type"], "recipients": c["contact_count"],
                      "start_at": c["start_at"]}, request=request)
    return JSONResponse(c, status_code=201)


@router.get("/campaigns/{campaign_id}")
async def campaign_detail(campaign_id: int, request: Request):
    eo_auth.require_admin(request)
    _campaign_or_404(campaign_id)
    c = eo_db.get_campaign_full(campaign_id)
    c["type_label"] = campaigns.TYPE_LABEL.get(c["campaign_type"], c["campaign_type"])
    c["outcome_labels"] = {k: _outcome_label(k) for k in c.get("outcomes", {})}
    return JSONResponse(c)


@router.post("/campaigns/{campaign_id}/cancel")
async def campaign_cancel(campaign_id: int, request: Request):
    admin = eo_auth.require_admin(request)
    _campaign_or_404(campaign_id)
    if not eo_db.cancel_campaign(campaign_id):
        raise HTTPException(status_code=400, detail="Campaign is not cancellable (already completed or cancelled)")
    audit.log("campaign_cancelled", user=admin, target=f"campaign:{campaign_id}", request=request)
    return {"ok": True}


@router.get("/campaigns/{campaign_id}/contacts")
async def campaign_contacts(campaign_id: int, request: Request):
    eo_auth.require_admin(request)
    campaign = _campaign_or_404(campaign_id)
    qp = request.query_params
    data = eo_db.list_campaign_contacts(campaign_id, status=qp.get("status") or None, q=qp.get("q") or None,
                                        limit=int(qp.get("limit") or 500), offset=int(qp.get("offset") or 0))
    _attach_contact_display(data["items"], campaign, campaign_runner.is_enabled())
    return JSONResponse(data)


@router.post("/campaigns/{campaign_id}/contacts/{cc_id}/retry")
async def campaign_contact_retry(campaign_id: int, cc_id: int, request: Request):
    """'Call now' — dial this recipient immediately (promotes a scheduled campaign to live)."""
    admin = eo_auth.require_admin(request)
    _campaign_or_404(campaign_id)
    cc = _cc_or_404(campaign_id, cc_id)
    res = await campaign_runner.dial_contact_now(campaign_id, cc_id)
    if res.get("error"):
        raise HTTPException(status_code=400, detail=res["error"])
    audit.log("campaign_call_now", user=admin, target=f"campaign:{campaign_id}",
              detail={"cc_id": cc_id, "phone": cc.get("phone")}, request=request)
    return {"ok": True}


@router.post("/campaigns/{campaign_id}/contacts/{cc_id}/cancel")
async def campaign_contact_cancel(campaign_id: int, cc_id: int, request: Request):
    """Cancel a PENDING retry for this recipient — no more automatic dials."""
    admin = eo_auth.require_admin(request)
    _campaign_or_404(campaign_id)
    cc = _cc_or_404(campaign_id, cc_id)
    st = cc.get("call_status")
    if st == "calling":
        raise HTTPException(status_code=409, detail="A call to this recipient is in progress")
    if st != "pending":
        raise HTTPException(status_code=400, detail="Nothing to cancel — this recipient has no pending call")
    eo_db.cc_update(cc_id, call_status="cancelled", next_attempt_at=None)
    audit.log("campaign_contact_cancelled", user=admin, target=f"campaign:{campaign_id}",
              detail={"cc_id": cc_id}, request=request)
    return {"ok": True}


@router.get("/campaigns/{campaign_id}/contacts/{cc_id}/call")
async def campaign_contact_call(campaign_id: int, cc_id: int, request: Request):
    """The most recent call record for one recipient — transcript, tool calls, tickets."""
    admin = eo_auth.require_admin(request)
    _campaign_or_404(campaign_id)
    _cc_or_404(campaign_id, cc_id)
    meta = await store.find_campaign_call(cc_id)
    call = await store.load_call(meta["id"]) if meta else None
    if not call:
        raise HTTPException(status_code=404, detail="No answered call recorded for this recipient yet")
    call = dict(call)
    call.setdefault("messages", call.get("transcript") or [])
    call["has_recording"] = store.has_recording(call.get("call_sid"))
    call["tickets"] = [_decorate(t) for t in eo_db.tickets_by_call(call["id"])]
    audit.log("transcript_viewed", user=admin, target=f"call:{call['id']}", request=request)
    return JSONResponse(call)


@router.patch("/campaigns/{campaign_id}/contacts/{cc_id}/remark")
async def campaign_contact_remark(campaign_id: int, cc_id: int, request: Request):
    eo_auth.require_admin(request)
    _campaign_or_404(campaign_id)
    _cc_or_404(campaign_id, cc_id)
    body = await _body(request)
    remark = _clean_str(body, "remark", max_len=500)
    eo_db.cc_update(cc_id, remark=remark)
    return {"ok": True, "remark": remark}


# Scheduler (the dial loop's kill switch + retry queue)
@router.get("/scheduler/queue")
async def scheduler_queue(request: Request):
    eo_auth.require_admin(request)
    data = eo_db.cc_upcoming(limit=int(request.query_params.get("limit") or 200))
    on = campaign_runner.is_enabled()
    _attach_contact_display(data["items"], None, on)
    data["scheduler_enabled"] = on
    data["active_campaigns"] = len(eo_db.active_campaigns())
    data["max_active_campaigns"] = campaigns.max_active()
    return JSONResponse(data)


@router.post("/scheduler/toggle")
async def scheduler_toggle(request: Request):
    admin = eo_auth.require_admin(request)
    body = await _body(request)
    enabled = bool(body.get("enabled", not campaign_runner.is_enabled()))
    campaign_runner.set_override(enabled, by=admin["username"])
    audit.log("scheduler_toggled", user=admin, detail={"enabled": enabled}, request=request)
    return {"ok": True, "enabled": enabled}


# ---------------------------------------------------------------------------------------
# Audit log (admin)
# ---------------------------------------------------------------------------------------
@router.get("/audit")
async def audit_list(request: Request):
    eo_auth.require_admin(request)
    qp = request.query_params
    return JSONResponse(eo_db.list_audit(
        q=qp.get("q") or None, action=qp.get("action") or None, user_id=qp.get("user_id") or None,
        date_from=qp.get("from") or None, date_to=qp.get("to") or None,
        limit=int(qp.get("limit") or 100), offset=int(qp.get("offset") or 0)))


@router.get("/audit/actions")
async def audit_actions(request: Request):
    eo_auth.require_admin(request)
    return JSONResponse({"items": eo_db.audit_actions()})
