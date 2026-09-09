from aiogram import Router

from sitg_bot.bot.handlers import (
    admin,
    chat,
    common,
    fallback,
    game,
    lobby,
    manager,
    navigation,
    player,
    registration,
)

root_router = Router(name="root")
root_router.include_router(common.router)
root_router.include_router(navigation.router)
root_router.include_router(registration.router)
root_router.include_router(chat.commands)
root_router.include_router(game.router)
root_router.include_router(lobby.router)
root_router.include_router(player.router)
root_router.include_router(manager.router)
root_router.include_router(admin.router)
root_router.include_router(chat.router)
root_router.include_router(fallback.router)
