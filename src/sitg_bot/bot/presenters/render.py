from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo,
)

from sitg_bot.bot.presenters.models import (
    InlineKeyboardModel,
    KeyboardModel,
    MessageModel,
    RemoveKeyboardModel,
    ReplyKeyboardModel,
)


def telegram_keyboard(keyboard: KeyboardModel):  # type: ignore[no-untyped-def]
    if isinstance(keyboard, ReplyKeyboardModel):
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=label) for label in row] for row in keyboard.rows],
            resize_keyboard=True,
            one_time_keyboard=keyboard.one_time,
            is_persistent=keyboard.persistent,
        )
    if isinstance(keyboard, InlineKeyboardModel):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=button.text,
                        callback_data=button.callback_data,
                        url=button.url,
                        web_app=(
                            WebAppInfo(url=button.web_app_url)
                            if button.web_app_url is not None
                            else None
                        ),
                    )
                    for button in row
                ]
                for row in keyboard.rows
            ]
        )
    if isinstance(keyboard, RemoveKeyboardModel):
        return ReplyKeyboardRemove()
    return None


async def send_message_model(message: Message, model: MessageModel) -> Message:
    return await message.answer(
        model.text,
        reply_markup=telegram_keyboard(model.keyboard),
        protect_content=model.protect_content,
    )
