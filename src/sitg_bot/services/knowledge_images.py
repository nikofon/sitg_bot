"""Bounded immutable raster uploads; no remote fetches or executable image formats."""

import base64
import binascii
import struct
from uuid import UUID, uuid4

from sqlalchemy import String, func, or_, select
from sqlalchemy.orm import defer

from sitg_bot.services.training_access import require_trainee
from sitg_bot.storage.models import KnowledgeFactRecord, KnowledgeImageRecord

MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000


def raster_content_type(data: bytes) -> str:
    """Inspect raster headers and dimensions without invoking an untrusted decoder.

    The browser decodes the raster. We never serve SVG, infer MIME from a filename,
    or allow an arbitrarily large decoded canvas. Invalid/truncated headers fail.
    """
    if not 1 <= len(data) <= MAX_IMAGE_BYTES:
        raise ValueError("Image must be between 1 byte and 2 MiB")
    width = height = 0
    kind = None
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 45:
        if data[8:16] != b"\x00\x00\x00\rIHDR" or data[-12:] != b"\x00\x00\x00\x00IEND\xaeB`\x82":
            raise ValueError("Invalid PNG image")
        width, height = struct.unpack(">II", data[16:24])
        kind = "image/png"
    elif data.startswith(b"\xff\xd8") and data.endswith(b"\xff\xd9"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 0xFF:
                break
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset + 3 > len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xD8, 0xD9, 0xDA):
                break
            size = int.from_bytes(data[offset:offset + 2], "big")
            if size < 2 or offset + size > len(data):
                break
            if marker in (0xC0, 0xC1, 0xC2) and size >= 8:
                height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
                kind = "image/jpeg"
                break
            offset += size
    elif data.startswith(b"RIFF") and len(data) >= 30 and data[8:12] == b"WEBP":
        if int.from_bytes(data[4:8], "little") + 8 != len(data):
            raise ValueError("Invalid WebP image")
        chunk = data[12:16]
        if chunk == b"VP8X" and len(data) >= 30:
            # Animated attachments are deliberately unsupported.
            if data[20] & 2:
                raise ValueError("Animated images are not supported")
            width = 1 + int.from_bytes(data[24:27], "little")
            height = 1 + int.from_bytes(data[27:30], "little")
        elif chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            width, height = (value & 0x3FFF for value in struct.unpack("<HH", data[26:30]))
        elif chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
        kind = "image/webp"
    if kind is None or width < 1 or height < 1 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Upload a bounded PNG, JPEG or WebP raster image")
    return kind


def image_view(image):
    return {"id": image.id, "url": f"/api/miniapp/knowledge/images/{image.id}",
            "content_type": image.content_type}


def accessible_image(actor):
    # Copies keep immutable bytes even if the source fact is later deleted.
    # Referencing a UUID alone grants nothing: only the service can attach images.
    return or_(
        KnowledgeImageRecord.owner_id == actor,
        select(KnowledgeFactRecord.id).where(
            KnowledgeFactRecord.owner_id == actor,
            KnowledgeFactRecord.deleted_at.is_(None),
            KnowledgeFactRecord.image_ids.contains(
                func.jsonb_build_array(KnowledgeImageRecord.id.cast(String))
            ),
        ).exists(),
    )


async def authorized_images(session, actor, ids):
    if not ids:
        return []
    rows = list(await session.scalars(select(KnowledgeImageRecord).options(
        defer(KnowledgeImageRecord.data)
    ).where(KnowledgeImageRecord.id.in_(ids), accessible_image(actor))))
    mapping = {row.id: row for row in rows}
    if set(ids) - mapping.keys():
        raise LookupError("Image not found")
    return [mapping[identity] for identity in ids]


class KnowledgeImageService:
    def __init__(self, database):
        self.database = database

    async def upload(self, actor: UUID, source_base64: str):
        try:
            data = base64.b64decode(source_base64, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("Image upload is not valid base64") from error
        content_type = raster_content_type(data)
        async with self.database.transaction() as session:
            await require_trainee(session, actor, lock=True)
            row = KnowledgeImageRecord(id=uuid4(), owner_id=actor,
                                       content_type=content_type, data=data)
            session.add(row)
            await session.flush()
            return image_view(row)

    async def read(self, actor: UUID, image_id: UUID):
        async with self.database.transaction() as session:
            await require_trainee(session, actor)
            row = await session.scalar(select(KnowledgeImageRecord).where(
                KnowledgeImageRecord.id == image_id, accessible_image(actor)
            ))
            if row is None:
                raise LookupError("Image not found")
            return {**image_view(row), "source_base64": base64.b64encode(row.data).decode("ascii")}
