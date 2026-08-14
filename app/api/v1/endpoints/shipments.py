from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import get_session
from app.api.v1.response import err, ok
from app.models.batch import Batch
from app.models.hub import Hub
from app.models.kurir import Kurir
from app.models.paket import Paket
from app.models.shipment import Shipment
from app.models.tracking_history import TrackingHistory
from app.schemas.shipment import (
    BatchAssignRequest,
    BatchStatusUpdate,
    BillingUpdate,
    CodUpdate,
    HistoryCreate,
    ShipmentStatusUpdate,
)

router = APIRouter(tags=["Shipment"])

ACTIVE_STATUSES = ("assigned", "picked_up")
COD_STATUSES = ("pending", "collected", "remitted", "not_applicable")
BILLING_STATUSES = ("unpaid", "paid", "refunded")


def _dt(value) -> str | None:
    return value.isoformat() if value is not None else None


def _kurir_dict(kurir: Kurir) -> dict:
    return {
        "id": kurir.id,
        "nama": kurir.nama,
        "nomor_telepon": kurir.nomor_telepon,
        "kendaraan": kurir.kendaraan,
    }


def _hub_dict(hub: Hub | None) -> dict | None:
    if hub is None:
        return None
    return {
        "id": hub.id,
        "nama": hub.nama,
        "alamat": hub.alamat,
    }


def _paket_dict(paket: Paket) -> dict:
    return {
        "id": paket.id,
        "nama": paket.nama,
        "alamat": paket.alamat,
        "jenis_pengiriman": paket.jenis_pengiriman,
    }


def _cod_dict(s: Shipment) -> dict:
    return {
        "status": s.cod_status,
        "amount": s.cod_amount,
        "collected_at": _dt(s.cod_collected_at),
        "remitted_at": _dt(s.cod_remitted_at),
    }


def _billing_dict(s: Shipment) -> dict:
    return {
        "ongkir": s.ongkir,
        "status": s.billing_status,
        "paid_at": _dt(s.billing_paid_at),
    }


def _shipment_dict(s: Shipment, paket: Paket) -> dict:
    return {
        "shipment_id": s.id,
        "resi": paket.resi,
        "paket": _paket_dict(paket),
        "status": s.status,
        "cod": _cod_dict(s),
        "billing": _billing_dict(s),
        "picked_up_at": _dt(s.picked_up_at),
        "delivered_at": _dt(s.delivered_at),
    }


async def _next_batch_no(session: AsyncSession) -> str:
    prefix = f"BATCH-{datetime.now(timezone.utc).strftime('%Y%m%d')}-"
    result = await session.execute(
        select(Batch.batch_no).where(Batch.batch_no.like(prefix + "%"))
    )
    count = len(result.scalars().all())
    return f"{prefix}{count + 1:04d}"


async def _get_shipments_map(session: AsyncSession, shipment_ids) -> dict:
    if not shipment_ids:
        return {}
    result = await session.execute(select(Paket).where(Paket.id.in_(shipment_ids)))
    return {p.id: p for p in result.scalars().all()}


# --- Batch ---


@router.post("/batches", status_code=201)
async def assign_batch(
    payload: BatchAssignRequest, session: AsyncSession = Depends(get_session)
):
    kurir = await session.get(Kurir, payload.kurir_id)
    if kurir is None or not kurir.is_active:
        return err("Kurir tidak ditemukan", 404)

    active = await session.execute(
        select(Batch.id).where(
            Batch.kurir_id == kurir.id, Batch.status.in_(ACTIVE_STATUSES)
        )
    )
    if active.scalars().first() is not None:
        return err("Kurir masih punya batch aktif", 409)

    paket_ids = list(dict.fromkeys(payload.paket_ids))
    pakets = []
    for pid in paket_ids:
        paket = await session.get(Paket, pid)
        if paket is None:
            return err(f"Paket {pid} tidak ditemukan", 404)
        pakets.append(paket)

    for paket in pakets:
        dupe = await session.execute(
            select(Shipment.id).where(
                Shipment.paket_id == paket.id,
                Shipment.status.in_(ACTIVE_STATUSES),
            )
        )
        if dupe.scalars().first() is not None:
            return err(
                f"Paket {paket.id} (resi {paket.resi}) sudah punya shipment aktif",
                409,
            )

    hub = None
    if payload.hub_id:
        hub = await session.get(Hub, payload.hub_id)
        if hub is None or not hub.is_active:
            return err("Hub tidak ditemukan", 404)

    batch = Batch(
        batch_no=await _next_batch_no(session),
        kurir_id=kurir.id,
        hub_id=hub.id if hub else None,
    )
    session.add(batch)
    await session.flush()

    for paket in pakets:
        session.add(
            Shipment(
                batch_id=batch.id,
                paket_id=paket.id,
                cod_status="pending" if paket.cod else "not_applicable",
                cod_amount=paket.harga if paket.cod else None,
                ongkir=paket.ongkir or 0.0,
            )
        )
    await session.commit()

    result = await session.execute(
        select(Shipment).where(Shipment.batch_id == batch.id).order_by(Shipment.id)
    )
    shipments = result.scalars().all()
    paket_map = await _get_shipments_map(session, [s.paket_id for s in shipments])

    data = {
        "batch": {
            "id": batch.id,
            "batch_no": batch.batch_no,
            "status": batch.status,
            "assigned_at": _dt(batch.assigned_at),
            "hub": _hub_dict(hub),
            "kurir": _kurir_dict(kurir),
        },
        "shipments": [_shipment_dict(s, paket_map[s.paket_id]) for s in shipments],
    }
    return ok(
        f"Batch {batch.batch_no} dibuat: {len(shipments)} paket di-assign ke kurir {kurir.nama}",
        data,
    )


@router.get("/batches")
async def list_batches(
    status: str | None = None,
    kurir_id: int | None = None,
    hub_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Batch)
    if status:
        stmt = stmt.where(Batch.status == status)
    if kurir_id:
        stmt = stmt.where(Batch.kurir_id == kurir_id)
    if hub_id:
        stmt = stmt.where(Batch.hub_id == hub_id)
    stmt = stmt.order_by(Batch.id.desc())

    batches = (await session.execute(stmt)).scalars().all()

    kurir_map = {}
    kurir_ids = {b.kurir_id for b in batches}
    if kurir_ids:
        rows = await session.execute(select(Kurir).where(Kurir.id.in_(kurir_ids)))
        kurir_map = {k.id: k for k in rows.scalars().all()}

    hub_map = {}
    hub_ids = {b.hub_id for b in batches if b.hub_id}
    if hub_ids:
        rows = await session.execute(select(Hub).where(Hub.id.in_(hub_ids)))
        hub_map = {h.id: h for h in rows.scalars().all()}

    counts = {}
    batch_ids = [b.id for b in batches]
    if batch_ids:
        rows = await session.execute(
            select(Shipment.batch_id, func.count(Shipment.id))
            .where(Shipment.batch_id.in_(batch_ids))
            .group_by(Shipment.batch_id)
        )
        counts = {bid: c for bid, c in rows.all()}

    data = [
        {
            "id": b.id,
            "batch_no": b.batch_no,
            "status": b.status,
            "total_paket": counts.get(b.id, 0),
            "assigned_at": _dt(b.assigned_at),
            "hub": _hub_dict(hub_map.get(b.hub_id)),
            "kurir": _kurir_dict(kurir_map[b.kurir_id]),
        }
        for b in batches
    ]
    return ok(f"{len(data)} batch ditemukan", data)


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: int, session: AsyncSession = Depends(get_session)):
    batch = await session.get(Batch, batch_id)
    if batch is None:
        return err("Batch tidak ditemukan", 404)

    kurir = await session.get(Kurir, batch.kurir_id)
    if kurir is None:
        return err("Kurir tidak ditemukan", 404)
    hub = await session.get(Hub, batch.hub_id) if batch.hub_id else None

    result = await session.execute(
        select(Shipment).where(Shipment.batch_id == batch.id).order_by(Shipment.id)
    )
    shipments = result.scalars().all()
    paket_map = await _get_shipments_map(session, [s.paket_id for s in shipments])

    data = {
        "batch": {
            "id": batch.id,
            "batch_no": batch.batch_no,
            "status": batch.status,
            "assigned_at": _dt(batch.assigned_at),
            "picked_up_at": _dt(batch.picked_up_at),
            "delivered_at": _dt(batch.delivered_at),
            "assigned_by": batch.assigned_by,
            "hub": _hub_dict(hub),
            "kurir": _kurir_dict(kurir),
        },
        "shipments": [_shipment_dict(s, paket_map[s.paket_id]) for s in shipments],
    }
    return ok("Detail batch berhasil diambil", data)


@router.patch("/batches/{batch_id}/status")
async def update_batch_status(
    batch_id: int,
    payload: BatchStatusUpdate,
    session: AsyncSession = Depends(get_session),
):
    batch = await session.get(Batch, batch_id)
    if batch is None:
        return err("Batch tidak ditemukan", 404)

    new, cur = payload.status, batch.status
    valid = {
        "picked_up": cur == "assigned",
        "delivered": cur == "picked_up",
        "returned": cur in ("assigned", "picked_up"),
    }
    if not valid.get(new):
        return err(f"Tidak bisa ubah status batch dari '{cur}' ke '{new}'", 409)

    now = datetime.now(timezone.utc)
    if new == "picked_up":
        batch.picked_up_at = now
        result = await session.execute(
            select(Shipment).where(Shipment.batch_id == batch.id)
        )
        for s in result.scalars().all():
            if s.status == "assigned":
                s.status = "picked_up"
                s.picked_up_at = now
                session.add(
                    TrackingHistory(
                        shipment_id=s.id,
                        event="picked_up",
                        keterangan="Paket diambil kurir",
                        hub_id=batch.hub_id,
                    )
                )
    elif new == "delivered":
        batch.delivered_at = now
    batch.status = new
    await session.commit()

    return ok(f"Status batch {batch.batch_no} menjadi {new}", {"id": batch.id, "status": new})


# --- Shipment ---


@router.get("/shipments")
async def list_shipments(
    status: str | None = None,
    batch_id: int | None = None,
    kurir_id: int | None = None,
    session: AsyncSession = Depends(get_session),
):
    stmt = select(Shipment).join(Batch, Shipment.batch_id == Batch.id)
    if status:
        stmt = stmt.where(Shipment.status == status)
    if batch_id:
        stmt = stmt.where(Shipment.batch_id == batch_id)
    if kurir_id:
        stmt = stmt.where(Batch.kurir_id == kurir_id)
    stmt = stmt.order_by(Shipment.id.desc())

    shipments = (await session.execute(stmt)).scalars().all()
    paket_map = await _get_shipments_map(session, [s.paket_id for s in shipments])

    batch_map = {}
    batch_ids = {s.batch_id for s in shipments}
    if batch_ids:
        rows = await session.execute(select(Batch).where(Batch.id.in_(batch_ids)))
        batch_map = {b.id: b for b in rows.scalars().all()}

    data = []
    for s in shipments:
        item = _shipment_dict(s, paket_map[s.paket_id])
        batch = batch_map[s.batch_id]
        item["batch_no"] = batch.batch_no
        item["kurir_id"] = batch.kurir_id
        data.append(item)
    return ok(f"{len(data)} shipment ditemukan", data)


@router.get("/shipments/{shipment_id}")
async def get_shipment(shipment_id: int, session: AsyncSession = Depends(get_session)):
    s = await session.get(Shipment, shipment_id)
    if s is None:
        return err("Shipment tidak ditemukan", 404)

    paket = await session.get(Paket, s.paket_id)
    batch = await session.get(Batch, s.batch_id)
    if batch is None:
        return err("Batch tidak ditemukan", 404)
    if paket is None:
        return err("Paket tidak ditemukan", 404)
    kurir = await session.get(Kurir, batch.kurir_id)
    if kurir is None:
        return err("Kurir tidak ditemukan", 404)
    hub = await session.get(Hub, batch.hub_id) if batch.hub_id else None

    data = _shipment_dict(s, paket)
    data["batch"] = {
        "id": batch.id,
        "batch_no": batch.batch_no,
        "status": batch.status,
        "hub": _hub_dict(hub),
        "kurir": _kurir_dict(kurir),
    }
    return ok("Detail shipment berhasil diambil", data)


@router.patch("/shipments/{shipment_id}/status")
async def update_shipment_status(
    shipment_id: int,
    payload: ShipmentStatusUpdate,
    session: AsyncSession = Depends(get_session),
):
    s = await session.get(Shipment, shipment_id)
    if s is None:
        return err("Shipment tidak ditemukan", 404)

    new, cur = payload.status, s.status
    valid = {
        "picked_up": cur == "assigned",
        "delivered": cur == "picked_up",
        "failed": cur == "picked_up",
        "returned": cur in ("assigned", "picked_up"),
    }
    if not valid.get(new):
        return err(f"Tidak bisa ubah status shipment dari '{cur}' ke '{new}'", 409)

    now = datetime.now(timezone.utc)
    keterangan_map = {
        "delivered": "Paket diterima penerima",
        "failed": "Pengiriman gagal",
        "returned": "Paket dikembalikan",
        "picked_up": "Paket diambil kurir",
    }
    if new == "delivered":
        s.delivered_at = now
        if s.cod_status == "pending":
            s.cod_status = "collected"
            s.cod_collected_at = now
    elif new in ("failed", "returned"):
        if s.cod_status in ("pending", "collected"):
            s.cod_status = "not_applicable"
    elif new == "picked_up":
        s.picked_up_at = now

    s.status = new
    session.add(
        TrackingHistory(
            shipment_id=s.id,
            event=new,
            keterangan=keterangan_map[new],
        )
    )
    await session.commit()

    return ok(f"Status shipment {shipment_id} menjadi {new}", {"shipment_id": s.id, "status": new})


@router.patch("/shipments/{shipment_id}/cod")
async def update_cod(
    shipment_id: int,
    payload: CodUpdate,
    session: AsyncSession = Depends(get_session),
):
    s = await session.get(Shipment, shipment_id)
    if s is None:
        return err("Shipment tidak ditemukan", 404)
    if s.cod_status != "collected":
        return err("COD belum terkumpul, tidak bisa di-set remitted", 409)

    s.cod_status = "remitted"
    s.cod_remitted_at = datetime.now(timezone.utc)
    await session.commit()
    return ok("COD ditandai remitted", {"shipment_id": s.id, "cod": _cod_dict(s)})


@router.patch("/shipments/{shipment_id}/billing")
async def update_billing(
    shipment_id: int,
    payload: BillingUpdate,
    session: AsyncSession = Depends(get_session),
):
    s = await session.get(Shipment, shipment_id)
    if s is None:
        return err("Shipment tidak ditemukan", 404)

    if payload.status == "paid":
        s.billing_status = "paid"
        s.billing_paid_at = datetime.now(timezone.utc)
    else:
        s.billing_status = "refunded"
    await session.commit()
    return ok(f"Billing ditandai {payload.status}", {"shipment_id": s.id, "billing": _billing_dict(s)})


# --- Tracking ---


@router.get("/shipments/{shipment_id}/tracking")
async def get_tracking(shipment_id: int, session: AsyncSession = Depends(get_session)):
    s = await session.get(Shipment, shipment_id)
    if s is None:
        return err("Shipment tidak ditemukan", 404)

    paket = await session.get(Paket, s.paket_id)
    if paket is None:
        return err("Paket tidak ditemukan", 404)
    result = await session.execute(
        select(TrackingHistory)
        .where(TrackingHistory.shipment_id == s.id)
        .order_by(TrackingHistory.created_at, TrackingHistory.id)
    )
    history_rows = result.scalars().all()

    hub_map = {}
    hub_ids = {h.hub_id for h in history_rows if h.hub_id}
    if hub_ids:
        rows = await session.execute(select(Hub).where(Hub.id.in_(hub_ids)))
        hub_map = {h.id: h for h in rows.scalars().all()}

    history = [
        {
            "event": h.event,
            "keterangan": h.keterangan,
            "hub": _hub_dict(hub_map.get(h.hub_id)),
            "waktu": _dt(h.created_at),
        }
        for h in history_rows
    ]
    return ok(
        "Tracking berhasil diambil",
        {"resi": paket.resi, "status": s.status, "history": history},
    )


@router.post("/shipments/{shipment_id}/history", status_code=201)
async def create_history(
    shipment_id: int,
    payload: HistoryCreate,
    session: AsyncSession = Depends(get_session),
):
    s = await session.get(Shipment, shipment_id)
    if s is None:
        return err("Shipment tidak ditemukan", 404)

    if payload.hub_id:
        hub = await session.get(Hub, payload.hub_id)
        if hub is None or not hub.is_active:
            return err("Hub tidak ditemukan", 404)

    history = TrackingHistory(
        shipment_id=s.id,
        event=payload.event,
        keterangan=payload.keterangan,
        hub_id=payload.hub_id,
    )
    session.add(history)
    await session.commit()

    return ok(
        "History ditambahkan",
        {
            "id": history.id,
            "shipment_id": s.id,
            "event": history.event,
            "keterangan": history.keterangan,
            "waktu": _dt(history.created_at),
        },
    )


# --- Kurir & Hub (untuk dropdown) ---


@router.get("/kurir")
async def list_kurir(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Kurir).where(Kurir.is_active == True).order_by(Kurir.id)  # noqa: E712
    )
    data = [_kurir_dict(k) for k in result.scalars().all()]
    return ok(f"{len(data)} kurir ditemukan", data)


@router.get("/hubs")
async def list_hubs(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Hub).where(Hub.is_active == True).order_by(Hub.id)  # noqa: E712
    )
    data = [_hub_dict(h) for h in result.scalars().all()]
    return ok(f"{len(data)} hub ditemukan", data)
