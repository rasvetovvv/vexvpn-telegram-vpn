"""Web admin API for VexVPN Mini App."""
from __future__ import annotations

import csv
import html
import io
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from bot.config import PLANS, Plan, settings
from bot.db.database import session_maker
from bot.db.models import BotEvent, GrantQueue, MarzbanLog, Payment, Subscription, TariffSetting, User
from bot.db.repo import (
    SUPPORT_TOPIC_TITLES,
    add_payment,
    add_support_message,
    admin_audit_logs,
    count_open_tickets,
    ensure_tariff_settings,
    get_subscription,
    get_ticket,
    get_ticket_messages,
    list_admin_tickets,
    log_admin_action,
    log_marzban,
    payment_reconciliation,
    set_ticket_status,
    set_trial_used,
    should_send_alert,
    update_payment_status,
    upsert_subscription,
)
from bot.services.marzban import MarzbanError, marzban
from bot.services.notify import tg_send
from bot.services.ops import collect_health
from bot.services.plans import plan_from_tariff
from bot.utils import fmt_date, fmt_traffic
from bot.web.auth import telegram_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


class ManualGrantIn(BaseModel):
    telegram_id: int
    days: int = Field(ge=0, le=3650)
    traffic_gb: int = Field(ge=0, le=100000)
    title: str = "Админская выдача"
    reason: str = "manual"


class RefundIn(BaseModel):
    charge_id: str = Field(min_length=1, max_length=128)
    confirm: bool = False


class TariffIn(BaseModel):
    title: str
    days: int = Field(ge=0, le=3650)
    stars: int = Field(ge=0, le=100000)
    traffic_gb: int = Field(ge=0, le=100000)
    devices: int = Field(ge=1, le=100)
    badge: str = ""
    visible: bool = True
    is_trial: bool = False
    traffic_only: bool = False
    unlimited: bool = False


async def admin_user(tg_user: dict = Depends(telegram_user)) -> dict:
    if not settings.is_admin(int(tg_user["id"])):
        raise HTTPException(status_code=403, detail="Нет доступа")
    return tg_user


async def super_admin_user(tg_user: dict = Depends(telegram_user)) -> dict:
    if not settings.is_super_admin(int(tg_user["id"])):
        raise HTTPException(status_code=403, detail="Только для супер-админа")
    return tg_user


class UserActionIn(BaseModel):
    telegram_id: int
    confirm: bool = False


def _dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    return fmt_date(value if value.tzinfo else value.replace(tzinfo=timezone.utc))


def _plan_from_tariff(t: TariffSetting) -> Plan:
    return plan_from_tariff(t)


async def get_admin_plan(session, key: str) -> Plan | None:
    await ensure_tariff_settings(session, PLANS)
    tariff = await session.get(TariffSetting, key)
    if tariff:
        return _plan_from_tariff(tariff)
    return PLANS.get(key)


@router.get("/summary")
async def summary(admin: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        if await should_send_alert(session, f"admin_panel_open:{admin['id']}", cooldown_minutes=60, message="admin panel opened"):
            await log_admin_action(session, int(admin["id"]), "admin_panel_open", str(admin["id"]))
        await ensure_tariff_settings(session, PLANS)
        now = datetime.now(timezone.utc)
        day = now - timedelta(days=1)
        week = now - timedelta(days=7)
        month = now - timedelta(days=30)

        users = await session.scalar(select(func.count(User.id))) or 0
        active = await session.scalar(select(func.count(Subscription.id)).where(Subscription.expire_at > now)) or 0
        payments = await session.scalar(select(func.count(Payment.id))) or 0
        problem = await session.scalar(select(func.count(Payment.id)).where(Payment.status.in_(["marzban_error", "validation_error"]))) or 0

        async def revenue(since: datetime) -> int:
            return await session.scalar(
                select(func.coalesce(func.sum(Payment.stars_amount), 0)).where(
                    Payment.created_at >= since,
                    Payment.status.in_(["success", "manual"]),
                )
            ) or 0

        starts = await session.scalar(select(func.count(BotEvent.id)).where(BotEvent.event == "start")) or 0
        choices = await session.scalar(select(func.count(BotEvent.id)).where(BotEvent.event == "plan_select")) or 0
        paid = await session.scalar(select(func.count(BotEvent.id)).where(BotEvent.event == "paid")) or 0

        servers = await marzban.servers_online()
        return {
            "users": users,
            "active_subscriptions": active,
            "payments": payments,
            "problem_payments": problem,
            "servers_online": servers if servers is not None else settings.servers_online,
            "is_super": settings.is_super_admin(int(admin["id"])),
            "revenue_day": await revenue(day),
            "revenue_week": await revenue(week),
            "revenue_month": await revenue(month),
            "conversion": {
                "starts": starts,
                "plan_choices": choices,
                "payments": paid,
                "start_to_choice": round((choices / starts * 100), 1) if starts else 0,
                "choice_to_pay": round((paid / choices * 100), 1) if choices else 0,
                "start_to_pay": round((paid / starts * 100), 1) if starts else 0,
            },
        }


@router.get("/users")
async def users(
    q: str = Query("", max_length=128),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: dict = Depends(admin_user),
) -> dict:
    async with session_maker() as session:
        stmt = select(User, Subscription).join(Subscription, Subscription.telegram_id == User.telegram_id, isouter=True)
        if q:
            like = f"%{q.strip()}%"
            conds = [User.username.ilike(like), Subscription.marzban_username.ilike(like)]
            if q.strip().isdigit():
                conds.append(User.telegram_id == int(q.strip()))
            stmt = stmt.where(or_(*conds))
        total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        rows = (await session.execute(stmt.order_by(User.created_at.desc()).offset(offset).limit(limit))).all()
        now = datetime.now(timezone.utc)
        return {
            "total": total,
            "items": [
                {
                    "telegram_id": u.telegram_id,
                    "username": u.username,
                    "created_at": _dt(u.created_at),
                    "trial_used": bool(u.trial_used),
                    "active_promo_code": u.active_promo_code,
                    "plan": s.plan if s else None,
                    "marzban_username": s.marzban_username if s else None,
                    "expire_at": _dt(s.expire_at) if s else None,
                    "active": bool(s and (s.expire_at if s.expire_at.tzinfo else s.expire_at.replace(tzinfo=timezone.utc)) > now),
                    "traffic": fmt_traffic(s.traffic_limit) if s else None,
                    "subscription_url": s.subscription_url if s else None,
                }
                for u, s in rows
            ],
        }


@router.get("/payments")
async def payments(
    q: str = Query("", max_length=128),
    problem_only: bool = False,
    limit: int = Query(80, ge=1, le=300),
    _: dict = Depends(admin_user),
) -> dict:
    async with session_maker() as session:
        stmt = select(Payment, User).join(User, User.telegram_id == Payment.telegram_id, isouter=True)
        if problem_only:
            stmt = stmt.where(Payment.status.in_(["marzban_error", "validation_error"]))
        if q:
            like = f"%{q.strip()}%"
            conds = [User.username.ilike(like), Payment.plan.ilike(like), Payment.charge_id.ilike(like)]
            if q.strip().isdigit():
                conds.append(Payment.telegram_id == int(q.strip()))
            stmt = stmt.where(or_(*conds))
        rows = (await session.execute(stmt.order_by(Payment.created_at.desc()).limit(limit))).all()
        return {"items": [
            {
                "id": p.id,
                "telegram_id": p.telegram_id,
                "username": u.username if u else None,
                "plan": p.plan,
                "stars": p.stars_amount,
                "promo": p.promo_code,
                "status": p.status,
                "error": p.error_message,
                "charge_id": p.charge_id,
                "date": _dt(p.created_at),
            }
            for p, u in rows
        ]}


@router.get("/payments.csv")
async def payments_csv(_: dict = Depends(admin_user)) -> Response:
    async with session_maker() as session:
        rows = (await session.execute(select(Payment).order_by(Payment.created_at.desc()).limit(5000))).scalars().all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "telegram_id", "plan", "stars", "promo", "status", "charge_id", "date", "error"])
    for p in rows:
        writer.writerow([p.id, p.telegram_id, p.plan, p.stars_amount, p.promo_code or "", p.status, p.charge_id, _dt(p.created_at), p.error_message or ""])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=vexvpn_payments.csv"})


@router.get("/users.csv")
async def users_csv(_: dict = Depends(admin_user)) -> Response:
    async with session_maker() as session:
        rows = (
            await session.execute(
                select(User, Subscription)
                .join(Subscription, Subscription.telegram_id == User.telegram_id, isouter=True)
                .order_by(User.created_at.desc())
                .limit(10000)
            )
        ).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["telegram_id", "username", "created_at", "trial_used", "plan", "marzban_username", "expire_at", "traffic_limit"])
    for u, s in rows:
        writer.writerow([
            u.telegram_id, u.username or "", _dt(u.created_at), u.trial_used,
            s.plan if s else "", s.marzban_username if s else "",
            _dt(s.expire_at) if s else "", s.traffic_limit if s else "",
        ])
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": "attachment; filename=vexvpn_users.csv"})


@router.get("/marzban-logs")
async def marzban_logs(limit: int = Query(100, ge=1, le=300), _: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        rows = (await session.scalars(select(MarzbanLog).order_by(MarzbanLog.created_at.desc()).limit(limit))).all()
        return {"items": [
            {"id": r.id, "telegram_id": r.telegram_id, "action": r.action, "status": r.status, "message": r.message, "date": _dt(r.created_at)}
            for r in rows
        ]}


@router.post("/grant")
async def manual_grant(payload: ManualGrantIn, admin: dict = Depends(admin_user)) -> dict:
    key = f"admin_{payload.days}d_{payload.traffic_gb}gb"
    plan = Plan(key, payload.title, payload.days, 0, payload.traffic_gb, visible=False, traffic_only=(payload.days == 0 and payload.traffic_gb > 0))
    async with session_maker() as session:
        payment = await add_payment(
            session,
            telegram_id=payload.telegram_id,
            plan=key,
            stars_amount=0,
            charge_id=f"ADMIN-{payload.reason}-{int(datetime.now(timezone.utc).timestamp())}",
            status="manual",
        )
    try:
        result = await marzban.create_or_renew(payload.telegram_id, plan)
    except MarzbanError as exc:
        async with session_maker() as session:
            await log_marzban(session, payload.telegram_id, "manual_grant", "error", str(exc))
            await log_admin_action(
                session,
                int(admin["id"]),
                "manual_grant_error",
                str(payload.telegram_id),
                f"days={payload.days}; traffic_gb={payload.traffic_gb}; reason={payload.reason}; error={exc}",
            )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    expire_at = datetime.fromtimestamp(result["expire"], tz=timezone.utc)
    async with session_maker() as session:
        # Чистая компенсация трафика (0 дней) не должна переименовывать тариф пользователя.
        store_plan_key = key
        if plan.traffic_only:
            existing_sub = await get_subscription(session, payload.telegram_id)
            if existing_sub and existing_sub.plan:
                store_plan_key = existing_sub.plan
        await upsert_subscription(
            session,
            telegram_id=payload.telegram_id,
            marzban_username=result["username"],
            subscription_url=result["subscription_url"],
            plan=store_plan_key,
            expire_at=expire_at,
            traffic_limit=result["data_limit"],
        )
        if payload.days <= 1 and payload.traffic_gb <= 5:
            await set_trial_used(session, payload.telegram_id)
        await log_marzban(session, payload.telegram_id, "manual_grant", "success", f"payment_id={payment.id}; {payload.reason}")
        await log_admin_action(
            session,
            int(admin["id"]),
            "manual_grant",
            str(payload.telegram_id),
            f"payment_id={payment.id}; days={payload.days}; traffic_gb={payload.traffic_gb}; reason={payload.reason}",
        )
    return {"ok": True, "expire_at": _dt(expire_at), "traffic": fmt_traffic(result["data_limit"])}


@router.get("/user/usage")
async def user_usage(telegram_id: int = Query(...), _: dict = Depends(admin_user)) -> dict:
    usage = await marzban.get_usage(telegram_id)
    if not usage:
        return {"ok": False, "message": "Пользователь не найден в Marzban или панель недоступна"}
    limit = usage["data_limit"]
    used = usage["used_traffic"]
    return {
        "ok": True,
        "status": usage["status"],
        "used": used,
        "used_label": fmt_traffic(used) if used else "0 МБ",
        "limit": limit,
        "limit_label": fmt_traffic(limit),
        "remaining_label": "∞ безлимит" if not limit else fmt_traffic(max(0, limit - used)),
        "expire": _dt(datetime.fromtimestamp(usage["expire"], tz=timezone.utc)) if usage["expire"] else None,
    }


@router.post("/user/reset-traffic")
async def user_reset_traffic(payload: UserActionIn, admin: dict = Depends(admin_user)) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Нужно подтверждение confirm=true")
    try:
        await marzban.reset_traffic(payload.telegram_id)
    except MarzbanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with session_maker() as session:
        await log_admin_action(session, int(admin["id"]), "reset_traffic", str(payload.telegram_id))
    return {"ok": True}


@router.post("/user/disable")
async def user_disable(payload: UserActionIn, admin: dict = Depends(admin_user)) -> dict:
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Нужно подтверждение confirm=true")
    try:
        await marzban.set_status(payload.telegram_id, "disabled")
    except MarzbanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with session_maker() as session:
        await log_admin_action(session, int(admin["id"]), "disable_user", str(payload.telegram_id))
    return {"ok": True}


@router.post("/user/enable")
async def user_enable(payload: UserActionIn, admin: dict = Depends(admin_user)) -> dict:
    try:
        await marzban.set_status(payload.telegram_id, "active")
    except MarzbanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with session_maker() as session:
        await log_admin_action(session, int(admin["id"]), "enable_user", str(payload.telegram_id))
    return {"ok": True}


@router.post("/user/delete")
async def user_delete(payload: UserActionIn, admin: dict = Depends(super_admin_user)) -> dict:
    """Удалить пользователя в Marzban и его локальную подписку (только супер-админ)."""
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Нужно подтверждение confirm=true")
    try:
        await marzban.delete_user(payload.telegram_id)
    except MarzbanError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    async with session_maker() as session:
        sub = await get_subscription(session, payload.telegram_id)
        if sub:
            await session.delete(sub)
            await session.commit()
        await log_admin_action(session, int(admin["id"]), "delete_user", str(payload.telegram_id))
    return {"ok": True}


@router.post("/refund")
async def refund(payload: RefundIn, admin: dict = Depends(super_admin_user)) -> dict:
    """Вернуть Telegram Stars за платёж (refundStarPayment).

    Возможно только для реальных Stars-оплат: charge_id = telegram_payment_charge_id.
    Бесплатные/тестовые/ручные выдачи (PROMO-/TEST-/ADMIN-) вернуть нельзя.
    """
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Нужно подтверждение confirm=true")
    charge_id = payload.charge_id.strip()
    async with session_maker() as session:
        p = await session.scalar(select(Payment).where(Payment.charge_id == charge_id))
        if not p:
            raise HTTPException(status_code=404, detail="Платёж не найден")
        if p.status == "refunded":
            raise HTTPException(status_code=409, detail="Платёж уже возвращён")
        if p.stars_amount <= 0 or charge_id.startswith(("PROMO-", "TEST-", "ADMIN-")):
            raise HTTPException(status_code=400, detail="Это не Stars-оплата (бесплатная/тестовая/ручная выдача) — вернуть нельзя")
        target_id = p.telegram_id
        stars = p.stars_amount

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"https://api.telegram.org/bot{settings.bot_token}/refundStarPayment",
            json={"user_id": target_id, "telegram_payment_charge_id": charge_id},
        )
    data = resp.json()
    if not data.get("ok"):
        async with session_maker() as session:
            await log_admin_action(session, int(admin["id"]), "refund_error", str(target_id), f"charge={charge_id}; err={data.get('description')}")
        raise HTTPException(status_code=502, detail=data.get("description", "Telegram refund error"))

    async with session_maker() as session:
        again = await session.scalar(select(Payment).where(Payment.charge_id == charge_id))
        if again:
            await update_payment_status(session, again.id, "refunded", "Возврат Stars через админку")
        await log_admin_action(session, int(admin["id"]), "refund", str(target_id), f"charge={charge_id}; stars={stars}")
    return {"ok": True, "refunded": charge_id, "stars": stars}


@router.get("/health")
async def health(_: dict = Depends(admin_user)) -> dict:
    return await collect_health(None)


@router.get("/grant-queue")
async def grant_queue(limit: int = Query(80, ge=1, le=300), _: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        rows = await session.scalars(select(GrantQueue).order_by(GrantQueue.created_at.desc()).limit(limit))
        return {"items": [
            {
                "id": r.id,
                "telegram_id": r.telegram_id,
                "plan": r.plan_key,
                "status": r.status,
                "attempts": r.attempts,
                "charge_id": r.charge_id,
                "last_error": r.last_error,
                "next_attempt_at": _dt(r.next_attempt_at),
                "created_at": _dt(r.created_at),
            }
            for r in rows
        ]}


@router.get("/abuse-flags")
async def abuse_flags(limit: int = Query(80, ge=1, le=300), severity: str = Query("", max_length=12), _: dict = Depends(admin_user)) -> dict:
    from bot.db.models import AbuseFlag
    async with session_maker() as session:
        stmt = select(AbuseFlag).order_by(AbuseFlag.created_at.desc()).limit(limit)
        if severity:
            stmt = select(AbuseFlag).where(AbuseFlag.severity == severity).order_by(AbuseFlag.created_at.desc()).limit(limit)
        rows = (await session.scalars(stmt)).all()
        return {"items": [
            {
                "id": r.id,
                "telegram_id": r.telegram_id,
                "kind": r.kind,
                "severity": r.severity,
                "fingerprint": (r.fingerprint_hash or "")[:10] + "…" if r.fingerprint_hash else None,
                "ip": (r.ip_hash or "")[:10] + "…" if r.ip_hash else None,
                "details": r.details,
                "created_at": _dt(r.created_at),
            }
            for r in rows
        ]}


@router.get("/payments-check")
async def payments_check(admin: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        report = await payment_reconciliation(session)
        await log_admin_action(session, int(admin["id"]), "payments_check", str(admin["id"]))

    def payment_item(p: Payment) -> dict:
        return {"id": p.id, "telegram_id": p.telegram_id, "plan": p.plan, "stars": p.stars_amount, "status": p.status, "charge_id": p.charge_id, "date": _dt(p.created_at), "error": p.error_message}

    return {
        "success_without_subscription": [payment_item(p) for p in report["success_without_subscription"]],
        "problem_payments": [payment_item(p) for p in report["problem_payments"]],
        "missing_charge_id": [payment_item(p) for p in report["missing_charge_id"]],
        "duplicate_charge_ids": [{"charge_id": c, "count": n} for c, n in report["duplicate_charge_ids"]],
        "strange_amounts": [payment_item(p) for p in report["strange_amounts"]],
    }


@router.get("/audit-log")
async def audit_log(
    limit: int = Query(100, ge=1, le=300),
    action: str = Query("", max_length=64),
    admin_id: int = Query(0, ge=0),
    _: dict = Depends(admin_user),
) -> dict:
    async with session_maker() as session:
        rows = await admin_audit_logs(session, limit, action=action or None, admin_id=admin_id or None)
        return {"items": [
            {"id": r.id, "admin_id": r.admin_id, "action": r.action, "target": r.target, "details": r.details, "date": _dt(r.created_at)}
            for r in rows
        ]}


class AdminTicketReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


def _admin_ticket_dict(t, messages=None) -> dict:
    d = {
        "id": t.id,
        "telegram_id": t.telegram_id,
        "topic": t.topic,
        "topic_title": SUPPORT_TOPIC_TITLES.get(t.topic, "Другое"),
        "status": "open" if t.status == "new" else t.status,
        "created": _dt(t.created_at),
        "updated": _dt(t.updated_at),
        "preview": (t.message or "")[:160],
    }
    if messages is not None:
        d["messages"] = [
            {"sender": m.sender, "admin_id": m.admin_id, "text": m.text, "date": _dt(m.created_at)}
            for m in messages
        ]
    return d


@router.get("/tickets")
async def tickets(status: str = Query("active", max_length=16), limit: int = Query(50, ge=1, le=200), _: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        rows = await list_admin_tickets(session, status=status or None, limit=limit)
        open_count = await count_open_tickets(session)
    return {"items": [_admin_ticket_dict(t) for t in rows], "open_count": open_count}


@router.get("/ticket/{ticket_id}")
async def ticket_thread(ticket_id: int, _: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        msgs = await get_ticket_messages(session, ticket_id)
    return _admin_ticket_dict(t, msgs)


@router.post("/ticket/{ticket_id}/reply")
async def ticket_reply(ticket_id: int, payload: AdminTicketReplyIn, admin: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        target_id = t.telegram_id
        await add_support_message(session, ticket_id, "admin", payload.text, admin_id=int(admin["id"]))
        await log_admin_action(session, int(admin["id"]), "support_reply", str(target_id), f"ticket={ticket_id}")
    await tg_send(
        target_id,
        f"💬 <b>Ответ поддержки</b> по заявке #{ticket_id}:\n\n{html.escape(payload.text[:1500])}",
        {"inline_keyboard": [[{"text": "Ответить", "callback_data": f"uticket:{ticket_id}"}]]},
    )
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        msgs = await get_ticket_messages(session, ticket_id)
    return _admin_ticket_dict(t, msgs)


@router.post("/ticket/{ticket_id}/close")
async def ticket_close(ticket_id: int, admin: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        t = await get_ticket(session, ticket_id)
        if not t:
            raise HTTPException(status_code=404, detail="Тикет не найден")
        target_id = t.telegram_id
        await set_ticket_status(session, ticket_id, "closed")
        await log_admin_action(session, int(admin["id"]), "support_close", str(target_id), f"ticket={ticket_id}")
    await tg_send(target_id, f"✅ Заявка #{ticket_id} закрыта поддержкой. Если вопрос остался — напиши ещё раз, тикет откроется снова.")
    return {"ok": True}


@router.get("/tariffs")
async def tariffs(_: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        await ensure_tariff_settings(session, PLANS)
        rows = (await session.scalars(select(TariffSetting).order_by(TariffSetting.key))).all()
        return {"items": [
            {"key": t.key, "title": t.title, "days": t.days, "stars": t.stars, "traffic_gb": t.traffic_gb, "devices": t.devices, "badge": t.badge, "visible": t.visible, "is_trial": t.is_trial, "traffic_only": t.traffic_only, "unlimited": t.unlimited}
            for t in rows
        ]}


@router.put("/tariffs/{key}")
async def update_tariff(key: str, payload: TariffIn, admin: dict = Depends(admin_user)) -> dict:
    async with session_maker() as session:
        await ensure_tariff_settings(session, PLANS)
        t = await session.get(TariffSetting, key)
        before = None
        if t is not None:
            before = f"title={t.title}; days={t.days}; stars={t.stars}; traffic_gb={t.traffic_gb}; visible={t.visible}; is_trial={t.is_trial}; traffic_only={t.traffic_only}; unlimited={t.unlimited}"
        if t is None:
            t = TariffSetting(key=key, title=payload.title, days=payload.days, stars=payload.stars, traffic_gb=payload.traffic_gb, devices=payload.devices, badge=payload.badge, visible=payload.visible, is_trial=payload.is_trial, traffic_only=payload.traffic_only, unlimited=payload.unlimited)
            session.add(t)
        else:
            t.title = payload.title
            t.days = payload.days
            t.stars = payload.stars
            t.traffic_gb = payload.traffic_gb
            t.devices = payload.devices
            t.badge = payload.badge
            t.visible = payload.visible
            t.is_trial = payload.is_trial
            t.traffic_only = payload.traffic_only
            t.unlimited = payload.unlimited
        await session.commit()
        PLANS[key] = _plan_from_tariff(t)
        after = f"title={payload.title}; days={payload.days}; stars={payload.stars}; traffic_gb={payload.traffic_gb}; visible={payload.visible}; is_trial={payload.is_trial}; traffic_only={payload.traffic_only}; unlimited={payload.unlimited}"
        await log_admin_action(session, int(admin["id"]), "tariff_update", key, f"before=({before}); after=({after})")
        return {"ok": True}
