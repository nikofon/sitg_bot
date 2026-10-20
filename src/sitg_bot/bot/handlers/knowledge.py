"""Telegram presentation for the trainee knowledge library (Agent B).

The router stays isolated from gateway wiring: Agent A registers a
``knowledge_backend`` workflow dependency implementing ``KnowledgeBackend``
and includes ``router`` in the root router. All authorization and data live in
the service layer; handlers only translate projections into localized
messages with authenticated inline navigation.
"""

from __future__ import annotations

from dataclasses import replace
from html import escape, unescape
from typing import Protocol
from uuid import UUID

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from sitg_bot.bot.i18n import LocalizationService
from sitg_bot.bot.presenters.game import plain_chunks
from sitg_bot.bot.presenters.models import (
    InlineButtonModel,
    InlineKeyboardModel,
    MessageModel,
)
from sitg_bot.bot.presenters.render import send_message_model as _send_message_model
from sitg_bot.domain.knowledge import FACT_PAGE_SIZE

router = Router(name=__name__)


async def send_message_model(message: Message, model: MessageModel) -> None:
    """Knowledge projections contain escaped plain text, not HTML markup."""
    chunks = list(plain_chunks(unescape(model.text)))
    for index, chunk in enumerate(chunks):
        await _send_message_model(
            message,
            replace(
                model,
                text=chunk,
                keyboard=model.keyboard if index == len(chunks) - 1 else None,
            ),
        )


FACTS_CALLBACK_PREFIX = "klib"
STATISTIC_LABELS = {
    "unavailable": "knowledge.statistic.unavailable",
    "denied": "knowledge.statistic.denied",
    "failed": "knowledge.statistic.failed",
    "none": "knowledge.statistic.none",
    "pending": "knowledge.statistic.pending",
    "fresh": "knowledge.statistic.fresh",
    "stale": "knowledge.statistic.stale",
}


class KnowledgeBackend(Protocol):
    """Port Agent A implements against the typed gateway and services.

    Telegram navigates by bounded page number; the adapter resolves pages to
    the service's stable keyset cursors. The Mini App uses cursors directly.
    """

    async def facts_page(
        self,
        claim: object,
        *,
        owner_id: UUID | None = None,
        page: int = 1,
        search: str | None = None,
        tag: str | None = None,
        order: str = "title_asc",
        limit: int = FACT_PAGE_SIZE,
        resume: bool = False,
    ) -> dict: ...

    async def fact_detail(self, claim: object, fact_id: UUID) -> dict: ...

    async def shared_collections(self, claim: object) -> dict: ...

    async def dissection_requests(self, claim: object) -> dict: ...


def _statistic_label(status: object) -> str:
    return STATISTIC_LABELS.get(str(status), "knowledge.statistic.pending")


def facts_message(page: dict, localization: LocalizationService, locale: str) -> MessageModel:
    """List projection: bounded page, tag names and statistic state."""
    items = page.get("items", [])
    rows: list[tuple[InlineButtonModel, ...]] = []
    for item in items:
        title = str(item.get("title", ""))
        fact_id = str(item.get("fact_id", ""))
        rows.append(
            (
                InlineButtonModel(
                    text=title[:60],
                    callback_data=f"{FACTS_CALLBACK_PREFIX}:fact:{fact_id}",
                ),
            )
        )
    next_page = page.get("next_page")
    total = int(page.get("total", len(items)))
    navigation: tuple[InlineButtonModel, ...] = ()
    if next_page is not None:
        navigation = navigation + (
            InlineButtonModel(
                text=localization.text("knowledge.facts.more", locale, total=total),
                callback_data=f"{FACTS_CALLBACK_PREFIX}:page:{int(next_page)}",
            ),
        )
    current_page = int(page.get("page", 1))
    if current_page > 1:
        navigation = (
            InlineButtonModel(
                text=localization.text("knowledge.facts.previous", locale),
                callback_data=f"{FACTS_CALLBACK_PREFIX}:page:{current_page - 1}",
            ),
        ) + navigation
    if navigation:
        rows.append(navigation)
    lines = []
    if not items:
        lines.append(localization.text("knowledge.facts.empty", locale))
    for item in items:
        tags = ", ".join(item.get("tag_names", ()))
        label = localization.text(_statistic_label(item.get("statistic_status")), locale)
        entry = localization.text(
            "knowledge.facts.entry", locale, title=str(item.get("title", "")), statistics=label
        )
        if tags:
            entry += "\n" + localization.text("knowledge.facts.tags", locale, tags=tags)
        lines.append(entry)
    keyboard = InlineKeyboardModel(rows=tuple(rows)) if rows else None
    return MessageModel(
        localization.text("knowledge.facts.title", locale, total=total)
        + "\n\n"
        + "\n\n".join(lines),
        keyboard=keyboard,
    )


def fact_detail_message(
    detail: dict, localization: LocalizationService, locale: str
) -> MessageModel:
    """Detail projection; provenance and links render as plain escaped text."""
    title = str(detail.get("title", ""))
    description = str(detail.get("description", ""))
    links = detail.get("links", ())
    origin = str(detail.get("origin", "manual"))
    editable = bool(detail.get("editable", False))
    lines = [
        localization.text("knowledge.fact.title", locale, title=title),
        escape(description),
    ]
    for link in links:
        lines.append(
            localization.text(
                "knowledge.fact.link",
                locale,
                title=str(link.get("title", "")),
                url=str(link.get("url", "")),
            )
        )
    lines.append(
        localization.text(
            "knowledge.fact.origin",
            locale,
            origin=localization.text(f"knowledge.origin.{origin}", locale),
        )
    )
    tags = detail.get("tags", ())
    if tags:
        lines.append(
            localization.text(
                "knowledge.facts.tags", locale, tags=", ".join(str(tag["name"]) for tag in tags)
            )
        )
    statistics = detail.get("statistics")
    if statistics:
        mentions = statistics.get("mentions")
        if mentions:
            lines.append(
                localization.text(
                    "knowledge.statistic.mentions"
                    if mentions.get("complete")
                    else "knowledge.statistic.mentions_partial",
                    locale,
                    count=mentions["count"],
                )
            )
        if statistics.get("common_knowledge") is not None:
            lines.append(
                localization.text(
                    "knowledge.statistic.common", locale, value=statistics["common_knowledge"]
                )
            )
        lines.append(localization.text(_statistic_label(statistics.get("status")), locale))
        lines.append(localization.text("knowledge.statistic.pending_calibration", locale))
    if not editable:
        lines.append(localization.text("knowledge.fact.read_only", locale))
    return MessageModel("\n".join(lines))


def shared_message(shared: dict, localization: LocalizationService, locale: str) -> MessageModel:
    grants = shared.get("grants", ())
    lines = [localization.text("knowledge.shared.title", locale)]
    if not grants:
        lines.append(localization.text("knowledge.shared.empty", locale))
    for grant in grants:
        lines.append(
            localization.text(
                "knowledge.shared.entry",
                locale,
                owner=str(grant.get("owner_name", grant.get("owner_id", ""))),
            )
        )
    return MessageModel("\n".join(lines))


def dissection_status_message(
    requests: dict, localization: LocalizationService, locale: str
) -> MessageModel:
    items = requests.get("items", ())
    lines = [localization.text("knowledge.dissection.title", locale)]
    if not items:
        lines.append(localization.text("knowledge.dissection.empty", locale))
    for item in items:
        status = str(item.get("status", "pending"))
        applied = len(item.get("applied_fact_ids", ()))
        lines.append(
            localization.text(
                "knowledge.dissection.entry",
                locale,
                packet=str(item.get("packet_name", item.get("packet_id", ""))),
                status=localization.text(f"knowledge.dissection.status.{status}", locale),
                applied=applied,
            )
        )
        if item.get("failed_candidate_ids"):
            lines.append(localization.text("knowledge.dissection.partial", locale))
    return MessageModel("\n".join(lines))


def _error_message(
    error: Exception, localization: LocalizationService, locale: str
) -> MessageModel:
    if isinstance(error, PermissionError):
        return MessageModel(localization.text("knowledge.error.forbidden", locale))
    if isinstance(error, LookupError):
        return MessageModel(localization.text("knowledge.error.not_found", locale))
    return MessageModel(localization.text("knowledge.error.invalid", locale))


async def handle_facts(
    message: Message,
    knowledge_backend: KnowledgeBackend,
    localization: LocalizationService,
    locale: str,
    telegram_update_claim: object,
) -> None:
    try:
        args = (getattr(message, "text", None) or "").partition(" ")[2]
        filters = {}
        if args:
            search, *options = args.split(" | ", 2)
            filters = {
                "search": search,
                "tag": options[0] if options else None,
                "order": options[1] if len(options) > 1 else "title_asc",
            }
        page = await knowledge_backend.facts_page(telegram_update_claim, **filters)
    except (PermissionError, LookupError, ValueError) as error:
        await send_message_model(message, _error_message(error, localization, locale))
        return
    await send_message_model(message, facts_message(page, localization, locale))


async def _answer_callback(
    callback: CallbackQuery,
    model: MessageModel,
) -> None:
    if callback.message is not None:
        await send_message_model(callback.message, model)
    await callback.answer()


@router.callback_query(lambda query: (query.data or "").startswith(f"{FACTS_CALLBACK_PREFIX}:"))
async def handle_knowledge_callback(
    callback: CallbackQuery,
    knowledge_backend: KnowledgeBackend,
    localization: LocalizationService,
    locale: str,
    telegram_update_claim: object,
) -> None:
    # Service-level checks authorize every read; stale references only fail.
    data = callback.data or ""
    try:
        if data.startswith(f"{FACTS_CALLBACK_PREFIX}:page:"):
            page_number = int(data.removeprefix(f"{FACTS_CALLBACK_PREFIX}:page:"))
            if page_number < 1:
                raise ValueError("Invalid facts page")
            page = await knowledge_backend.facts_page(
                telegram_update_claim, page=page_number, resume=True
            )
            model = facts_message(page, localization, locale)
        elif data.startswith(f"{FACTS_CALLBACK_PREFIX}:fact:"):
            fact_id = UUID(data.removeprefix(f"{FACTS_CALLBACK_PREFIX}:fact:"))
            detail = await knowledge_backend.fact_detail(telegram_update_claim, fact_id)
            model = fact_detail_message(detail, localization, locale)
        else:
            model = _error_message(ValueError("Unknown action"), localization, locale)
    except (PermissionError, LookupError, ValueError):
        await callback.answer(localization.text("knowledge.error.not_found", locale))
        return
    await _answer_callback(callback, model)
