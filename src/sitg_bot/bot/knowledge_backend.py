"""Telegram projection adapter; cursor caches hold no facts or permissions."""

from sitg_bot.application.contracts import (
    ActionCode,
    KnowledgeDissectionsOperation,
    KnowledgeFactOperation,
    KnowledgeFactsOperation,
    KnowledgeSharingOperation,
)
from sitg_bot.bot.state import GatewayCallError


class KnowledgeTelegramBackend:
    def __init__(self, backend):
        self.backend = backend
        self.cursors = {}
        self.contexts = {}

    async def _call(self, claim, operation):
        try:
            return (await self.backend._execute(claim, operation)).data
        except GatewayCallError as error:
            code = error.error.code.value
            if code == "forbidden":
                raise PermissionError("Knowledge access denied") from error
            if code == "not_found":
                raise LookupError("Knowledge not found") from error
            raise ValueError("Knowledge request failed") from error

    async def facts_page(
        self,
        claim,
        *,
        owner_id=None,
        page=1,
        search=None,
        tag=None,
        order="title_asc",
        limit=25,
        resume=False,
    ):
        actor = getattr(claim, "telegram_user_id", None)
        if resume and actor in self.contexts:
            owner_id, search, tag, order = self.contexts[actor]
        key = (actor, str(owner_id), search, tag, order)
        cursor = self.cursors.get((key, page)) if page > 1 else None
        if page > 1 and cursor is None:
            page = 1
        result = await self._call(
            claim,
            KnowledgeFactsOperation(
                action=ActionCode.KNOWLEDGE_FACTS,
                owner_id=owner_id,
                cursor=cursor,
                search=search,
                tag=tag,
                order=order,
                limit=limit,
            ),
        )
        if len(self.cursors) >= 256:
            self.cursors.clear()
        if len(self.contexts) >= 256:
            self.contexts.clear()
        self.contexts[actor] = (owner_id, search, tag, order)
        if result["next_cursor"]:
            self.cursors[(key, page + 1)] = result["next_cursor"]
        return {**result, "page": page, "next_page": page + 1 if result["next_cursor"] else None}

    async def fact_detail(self, claim, fact_id):
        return await self._call(
            claim, KnowledgeFactOperation(action=ActionCode.KNOWLEDGE_FACT, fact_id=fact_id)
        )

    async def shared_collections(self, claim):
        result = await self._call(
            claim, KnowledgeSharingOperation(action=ActionCode.KNOWLEDGE_SHARING)
        )
        return {"grants": result["shared"]}

    async def dissection_requests(self, claim):
        return await self._call(
            claim, KnowledgeDissectionsOperation(action=ActionCode.KNOWLEDGE_DISSECTIONS)
        )
