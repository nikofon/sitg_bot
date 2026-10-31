"""Explicit approval helpers for existing saved-copy/analysis regression scenarios."""


async def approve_all(app, recipient, staged):
    detail = await app.reviews.detail(recipient, staged["import_id"])
    result = await app.reviews.decide(
        recipient,
        import_id=staged["import_id"],
        decision="approve",
        selected_candidate_ids=[
            value["id"] for values in detail["sections"].values() for value in values
        ],
    )
    return {
        **staged,
        "imported_count": result["approved_count"],
        "skipped_count": result["skipped_count"],
    }


async def send_and_approve(app, actor, command, values):
    staged = await app.mutate(actor, command, values)
    return await approve_all(app, staged["recipient_id"], staged)
