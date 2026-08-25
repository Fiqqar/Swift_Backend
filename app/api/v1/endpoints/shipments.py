import time
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.dependencies import current_kurir_or_error, get_session
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
from app.core.logging import get_logger
from app.services.cloudinary_service import (
    CloudinaryNotConfiguredError,
    UPLOAD_FOLDER,
    cloudinary_configured,
    sniff_image_format,
    upload_image,
)

router = APIRouter(tags=["Shipment"])

logger = get_logger("shipment")
# PoW: skip POD photo logging for now (app-dev only, sesuai instruksi)

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
        "nomor_telepon": paket.nomor_telepon,
        "alamat": paket.alamat,
        "latitude": paket.latitude,
        "longitude": paket.longitude,
        "jenis_pengiriman": paket.jenis_pengiriman,
        "service_type": paket.service_type,
    }


def _cod_dict(s: Shipment) -> dict:
    return {
        "status": s.cod_status,
        "is_cod": s.cod_status != "not_applicable",
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


@router.post("/batches", status_code=201, summary="Assign batch ke kurir",
             description=(
                 "Membuat batch baru dan men-assign beberapa paket ke satu "
                 "kurir. Kurir harus aktif & belum punya batch aktif; paket "
                 "tidak boleh punya shipment aktif.\n\n"
                 "- **Wajib:** `paket_ids` (min 1), `kurir_id`.\n"
                 "- **Opsional:** `hub_id`.\n"
                 "- Error `404` kurir/hub/paket tidak ditemukan, `409` batch "
                 "aktif duplikat atau paket sudah punya shipment aktif."))
async def assign_batch(
    payload: BatchAssignRequest, session: AsyncSession = Depends(get_session)
):
    logger.info("shipment.batch_assign_attempt", kurir_id=payload.kurir_id, paket_ids=payload.paket_ids, hub_id=payload.hub_id)
    kurir = await session.get(Kurir, payload.kurir_id)
    if kurir is None or not kurir.is_active:
        logger.warning("shipment.batch_assign_failed", reason="kurir_not_found", kurir_id=payload.kurir_id)
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
    logger.info("shipment.batch_created", batch_id=batch.id, batch_no=batch.batch_no, kurir_id=kurir.id, total_paket=len(shipments))
    return ok(
        f"Batch {batch.batch_no} dibuat: {len(shipments)} paket di-assign ke kurir {kurir.nama}",
        data,
    )


@router.get("/batches", summary="Daftar batch",
            description=(
                "Menampilkan daftar batch dengan filter opsional.\n\n"
                "- **Query opsional:** `status`, `kurir_id`, `hub_id`.\n"
                "- Tidak memerlukan autentikasi."))
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


@router.get("/batches/{batch_id}", summary="Detail batch",
            description=(
                "Menampilkan detail batch termasuk kurir, hub, dan semua "
                "shipment di dalamnya.\n\n"
                "- **Path wajib:** `batch_id`.\n"
                "- Error `404` bila batch tidak ditemukan."))
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


@router.patch("/batches/{batch_id}/status", summary="Update status batch",
              description=(
                  "Mengubah status batch dan meng-cascade ke semua shipment di "
                  "dalamnya (menulis TrackingHistory).\n\n"
                  "- **Path wajib:** `batch_id`.\n"
                  "- **Body wajib:** `status` (`picked_up`/`delivered`/`returned`).\n"
                  "- Transisi: `assigned → picked_up → delivered`, atau "
                  "`returned` dari `assigned`/`picked_up`.\n"
                  "- Error `404` batch tidak ditemukan, `409` transisi tidak valid."))
async def update_batch_status(
    batch_id: int,
    payload: BatchStatusUpdate,
    session: AsyncSession = Depends(get_session),
):
    logger.info("shipment.batch_status_attempt", batch_id=batch_id, target_status=payload.status)
    batch = await session.get(Batch, batch_id)
    if batch is None:
        logger.warning("shipment.batch_status_failed", batch_id=batch_id, reason="not_found")
        return err("Batch tidak ditemukan", 404)

    new, cur = payload.status, batch.status
    valid = {
        "picked_up": cur == "assigned",
        "delivered": cur == "picked_up",
        "returned": cur in ("assigned", "picked_up"),
    }
    if not valid.get(new):
        logger.warning("shipment.batch_status_failed", batch_id=batch_id, reason="invalid_transition", cur=cur, new=new)
        return err(f"Tidak bisa ubah status batch dari '{cur}' ke '{new}'", 409)

    now = datetime.now(timezone.utc)
    result = await session.execute(
        select(Shipment).where(Shipment.batch_id == batch.id)
    )
    shipments = result.scalars().all()

    if new == "picked_up":
        batch.picked_up_at = now
        for s in shipments:
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
        for s in shipments:
            if s.status != "picked_up":
                continue
            s.status = "delivered"
            s.delivered_at = now
            if s.cod_status == "pending":
                s.cod_status = "collected"
                s.cod_collected_at = now
            session.add(
                TrackingHistory(
                    shipment_id=s.id,
                    event="delivered",
                    keterangan="Paket diterima penerima",
                    hub_id=batch.hub_id,
                )
            )
    elif new == "returned":
        for s in shipments:
            if s.status not in ("assigned", "picked_up"):
                continue
            s.status = "returned"
            if s.cod_status in ("pending", "collected"):
                s.cod_status = "not_applicable"
            session.add(
                TrackingHistory(
                    shipment_id=s.id,
                    event="returned",
                    keterangan="Paket dikembalikan",
                    hub_id=batch.hub_id,
                )
            )
    batch.status = new
    await session.commit()

    logger.info("shipment.batch_status_updated", batch_id=batch.id, batch_no=batch.batch_no, old_status=cur, new_status=new, shipments=len(shipments))
    return ok(f"Status batch {batch.batch_no} menjadi {new}", {"id": batch.id, "status": new})


# --- Shipment ---


@router.get("/shipments", summary="Daftar shipment kurir",
            description=(
                "Menampilkan shipment milik kurir yang sedang login "
                "(berdasarkan token JWT).\n\n"
                "- **Wajib:** header `Authorization: Bearer <token>`.\n"
                "- **Query opsional:** `status`, `batch_id`.\n"
                "- Error `401` bila token tidak ada/tidak valid."))
async def list_shipments(
    status: str | None = None,
    batch_id: int | None = None,
    request: Request = Request,
    session: AsyncSession = Depends(get_session),
):
    kurir, error = await current_kurir_or_error(request, session)
    if error:
        return err(error, 401)

    stmt = select(Shipment).join(Batch, Shipment.batch_id == Batch.id)
    if status:
        stmt = stmt.where(Shipment.status == status)
    if batch_id:
        stmt = stmt.where(Shipment.batch_id == batch_id)
    stmt = stmt.where(Batch.kurir_id == kurir.id)
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


@router.get("/shipments/{shipment_id}", summary="Detail shipment",
            description=(
                "Menampilkan detail shipment lengkap: paket, batch, kurir, hub.\n\n"
                "- **Path wajib:** `shipment_id`.\n"
                "- Error `404` bila shipment/batch/paket/kurir tidak ditemukan."))
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


@router.patch("/shipments/{shipment_id}/status", summary="Update status shipment",
              description=(
                  "Mengubah status satu shipment dan menulis TrackingHistory. "
                  "Saat `delivered`, COD otomatis menjadi `collected`, dan cache "
                  "tracking kurir (stops/geofence) di-invalidasi.\n\n"
                  "- **Path wajib:** `shipment_id`.\n"
                  "- **Body wajib:** `status` (`picked_up`/`delivered`/`failed`/`returned`).\n"
                  "- Transisi: `assigned → picked_up → delivered`, `failed` dari "
                  "`picked_up`, `returned` dari `assigned`/`picked_up`.\n"
                  "- Error `404` tidak ditemukan, `409` transisi tidak valid."))
async def update_shipment_status(
    shipment_id: int,
    payload: ShipmentStatusUpdate,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    logger.info("shipment.status_attempt", shipment_id=shipment_id, target_status=payload.status)
    s = await session.get(Shipment, shipment_id)
    if s is None:
        logger.warning("shipment.status_failed", shipment_id=shipment_id, reason="not_found")
        return err("Shipment tidak ditemukan", 404)

    new, cur = payload.status, s.status
    valid = {
        "picked_up": cur == "assigned",
        "delivered": cur == "picked_up",
        "failed": cur == "picked_up",
        "returned": cur in ("assigned", "picked_up"),
    }
    if not valid.get(new):
        logger.warning("shipment.status_failed", shipment_id=shipment_id, reason="invalid_transition", cur=cur, new=new)
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

    logger.info("shipment.status_updated", shipment_id=s.id, old_status=cur, new_status=new, paket_id=s.paket_id, batch_id=s.batch_id)
    await _invalidate_tracking_cache(request, s)
    return ok(f"Status shipment {shipment_id} menjadi {new}", {"shipment_id": s.id, "status": new})


async def _invalidate_tracking_cache(request: Request, s: Shipment) -> None:
    """Invalidasi cache tracking (stops & geofence) saat paket terkirim.

    Mengikuti design doc courier_gps_tracking.md Bagian 6.4: saat shipment
    `delivered`, hapus `driver:stops:{kurir_id}` dan
    `driver:geofence:{kurir_id}:{package_id}` dari Redis.
    """
    if s.status != "delivered":
        return
    from app.core.database import SessionLocal
    from app.services.tracking import del_geofence_state, del_stops_cache
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    try:
        async with SessionLocal() as session:
            batch = await session.get(Batch, s.batch_id)
        if batch is None:
            return
        await del_stops_cache(redis, batch.kurir_id)
        await del_geofence_state(redis, batch.kurir_id, s.paket_id)
    except Exception as exc:
        import logging
        logging.getLogger("shipments").warning(
            "Invalidasi cache tracking gagal shipment %s: %s", s.id, exc)


@router.patch("/shipments/{shipment_id}/cod", summary="Tandai COD remitted",
              description=(
                  "Menandai COD shipment sebagai `remitted` (dana sudah disetor). "
                  "Membutuhkan status COD `collected` sebelumnya (otomatis saat "
                  "shipment delivered).\n\n"
                  "- **Path wajib:** `shipment_id`.\n"
                  "- **Body wajib:** `status` = `remitted`.\n"
                  "- Error `404` tidak ditemukan, `409` bila COD belum collected."))
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


@router.patch("/shipments/{shipment_id}/billing", summary="Update status billing",
              description=(
                  "Menandai billing shipment sebagai `paid` atau `refunded`.\n\n"
                  "- **Path wajib:** `shipment_id`.\n"
                  "- **Body wajib:** `status` = `paid`/`refunded`.\n"
                  "- Error `404` bila shipment tidak ditemukan."))
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


@router.get("/shipments/{shipment_id}/tracking", summary="Timeline tracking shipment",
            description=(
                "Menampilkan riwayat tracking (timeline event) untuk sebuah "
                "shipment beserta data hub, penerima, koordinat, dan foto.\n\n"
                "- **Path wajib:** `shipment_id`.\n"
                "- Error `404` bila shipment/paket tidak ditemukan."))
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
            "recipient_name": h.recipient_name,
            "latitude": h.latitude,
            "longitude": h.longitude,
            "photo_urls": h.photo_urls,
            "waktu": _dt(h.created_at),
        }
        for h in history_rows
    ]
    return ok(
        "Tracking berhasil diambil",
        {"resi": paket.resi, "status": s.status, "history": history},
    )


@router.post("/shipments/{shipment_id}/history", status_code=201, summary="Tambah event history",
             description=(
                 "Menambahkan event riwayat manual untuk sebuah shipment.\n\n"
                 "- **Path wajib:** `shipment_id`.\n"
                 "- **Body wajib:** `event` (`received_at_hub`/`departed_hub`/`pod_submitted`).\n"
                 "- **Opsional:** `keterangan`, `hub_id`, `recipient_name`, "
                 "`latitude`, `longitude`, `photo_urls` (array URL Cloudinary).\n"
                 "- Error `404` bila shipment/hub tidak ditemukan."))
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
        recipient_name=payload.recipient_name,
        latitude=payload.latitude,
        longitude=payload.longitude,
        photo_urls=payload.photo_urls,
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
            "recipient_name": history.recipient_name,
            "latitude": history.latitude,
            "longitude": history.longitude,
            "photo_urls": history.photo_urls,
            "waktu": _dt(history.created_at),
        },
    )


_POD_ALLOWED_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/heic",
    "image/heif",
    "image/gif",
}
_MAX_POD_FILE_BYTES = 10 * 1024 * 1024
_MAX_POD_FILES = 5


def _read_photo(upload: UploadFile, index: int):
    """Baca satu file foto lalu validasi tipe (header + magic bytes) dan ukuran.

    Mengembalikan (data, error, status). Hanya satu yang terisi: bila valid,
    data terisi dan sisanya None; bila tidak valid, error + status HTTP terisi.
    """
    label = f"Foto #{index + 1}"
    ctype = (upload.content_type or "").lower()
    if ctype and ctype not in _POD_ALLOWED_TYPES:
        return None, f"Tipe {label} tidak didukung: {ctype}", 415
    # Baca maksimal batas + 1 byte supaya file raksasa tidak dimuat penuh.
    data = upload.file.read(_MAX_POD_FILE_BYTES + 1)
    if not data:
        return None, f"{label} kosong", 400
    if len(data) > _MAX_POD_FILE_BYTES:
        return None, f"{label} terlalu besar (maks 10 MB)", 400
    if sniff_image_format(data) is None:
        return None, (
            f"{label} bukan file gambar yang valid "
            "(isi dicek via magic bytes, bukan sekadar Content-Type)"
        ), 415
    return data, None, None


@router.post("/shipments/{shipment_id}/history/photo", status_code=201,
             summary="Upload foto POD (multipart)",
             description=(
                 "Mengunggah 1–5 foto POD ke Cloudinary lalu otomatis membuat "
                 "riwayat event `pod_submitted` dalam satu panggilan (upload "
                 "server-side).\n\n"
                 "- **Path wajib:** `shipment_id`.\n"
                 "- **Form wajib:** `files` (1–5 file; image: "
                 "jpeg/png/webp/heic/heif/gif, maks 10 MB per file).\n"
                 "- **Form opsional:** `recipient_name`, `latitude`, "
                 "`longitude`, `keterangan`.\n"
                 "- Isi file diverifikasi via magic bytes, jadi file non-gambar "
                 "seperti `shell.php.jpg` (Content-Type dipalsukan) ditolak.\n"
                 "- Error `415` tipe/isi bukan gambar, `400` kosong/terlalu "
                 "besar/terlalu banyak, `503` Cloudinary belum dikonfigurasi, "
                 "`502` upload gagal."))
async def upload_pod_photo(
    shipment_id: int,
    files: list[UploadFile] = File(...),
    recipient_name: str | None = Form(default=None),
    latitude: float | None = Form(default=None),
    longitude: float | None = Form(default=None),
    keterangan: str | None = Form(default=None),
    session: AsyncSession = Depends(get_session),
):
    """Unggah 1–N foto POD ke Cloudinary lalu buat satu riwayat
    `pod_submitted` dalam satu panggilan."""
    logger.info("pod.upload_attempt", shipment_id=shipment_id, files=len(files), recipient=recipient_name, lat=latitude, lon=longitude)
    s = await session.get(Shipment, shipment_id)
    if s is None:
        logger.warning("pod.upload_failed", shipment_id=shipment_id, reason="shipment_not_found")
        return err("Shipment tidak ditemukan", 404)

    if not cloudinary_configured():
        logger.warning("pod.upload_failed", shipment_id=shipment_id, reason="cloudinary_not_configured")
        return err("Cloudinary belum dikonfigurasi (CLOUDINARY_* tidak terisi)", 503)

    if len(files) > _MAX_POD_FILES:
        logger.warning("pod.upload_failed", shipment_id=shipment_id, reason="too_many_files", files=len(files), max=_MAX_POD_FILES)
        return err(f"Maksimal {_MAX_POD_FILES} foto per unggahan", 400)

    photo_urls: list[str] = []
    base_folder = f"{UPLOAD_FOLDER}/shipments/{shipment_id}"
    for idx, upload in enumerate(files):
        data, error, status = _read_photo(upload, idx)
        if error:
            logger.warning("pod.upload_failed", shipment_id=shipment_id, reason="validation_failed", file_index=idx, error=error)
            return err(error, status)
        assert data is not None  # valid => data pasti terisi
        logger.info("pod.cloudinary_uploading", shipment_id=shipment_id, file_index=idx, size_bytes=len(data), content_type=upload.content_type)
        try:
            photo_url = upload_image(
                data,
                f"{base_folder}/photo",
                f"pod_{int(time.time())}_{uuid4().hex[:8]}",
            )
            logger.info("pod.cloudinary_success", shipment_id=shipment_id, file_index=idx, url=photo_url)
        except CloudinaryNotConfiguredError as e:
            logger.warning("pod.cloudinary_failed", shipment_id=shipment_id, file_index=idx, error=str(e))
            return err(str(e), 503)
        except Exception as e:  # noqa: BLE001 - error upload diteruskan sebagai respons
            logger.warning("pod.cloudinary_failed", shipment_id=shipment_id, file_index=idx, error=str(e))
            return err(f"Upload Cloudinary gagal: {e}", 502)
        photo_urls.append(photo_url)

    history = TrackingHistory(
        shipment_id=s.id,
        event="pod_submitted",
        keterangan=keterangan,
        recipient_name=recipient_name,
        latitude=latitude,
        longitude=longitude,
        photo_urls=photo_urls,
    )
    session.add(history)
    await session.commit()

    logger.info("pod.upload_success", shipment_id=s.id, history_id=history.id, photo_count=len(photo_urls), recipient=recipient_name, photo_urls=photo_urls)

    return ok(
        "POD diunggah dan riwayat dibuat",
        {
            "id": history.id,
            "shipment_id": s.id,
            "event": history.event,
            "keterangan": history.keterangan,
            "recipient_name": history.recipient_name,
            "latitude": history.latitude,
            "longitude": history.longitude,
            "photo_urls": history.photo_urls,
            "waktu": _dt(history.created_at),
        },
    )


# --- Kurir & Hub (untuk dropdown) ---


@router.get("/kurir", summary="Daftar kurir aktif",
            description=(
                "Menampilkan semua kurir aktif. Berguna untuk memilih "
                "`kurir_id` saat assign batch."))
async def list_kurir(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Kurir).where(Kurir.is_active == True).order_by(Kurir.id)  # noqa: E712
    )
    data = [_kurir_dict(k) for k in result.scalars().all()]
    return ok(f"{len(data)} kurir ditemukan", data)


@router.get("/hubs", summary="Daftar hub aktif",
            description=(
                "Menampilkan semua hub aktif beserta koordinatnya. Berguna "
                "untuk `hub_origin` di pathfinding dan `hub_id` saat assign "
                "batch."))
async def list_hubs(session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        select(Hub).where(Hub.is_active == True).order_by(Hub.id)  # noqa: E712
    )
    data = [_hub_dict(h) for h in result.scalars().all()]
    return ok(f"{len(data)} hub ditemukan", data)
