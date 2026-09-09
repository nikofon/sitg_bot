from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InlineButtonModel:
    text: str
    callback_data: str | None = None
    url: str | None = None
    web_app_url: str | None = None


@dataclass(frozen=True, slots=True)
class InlineKeyboardModel:
    rows: tuple[tuple[InlineButtonModel, ...], ...]


@dataclass(frozen=True, slots=True)
class ReplyKeyboardModel:
    rows: tuple[tuple[str, ...], ...]
    one_time: bool = False
    persistent: bool = False


@dataclass(frozen=True, slots=True)
class RemoveKeyboardModel:
    pass


KeyboardModel = InlineKeyboardModel | ReplyKeyboardModel | RemoveKeyboardModel | None


@dataclass(frozen=True, slots=True)
class MessageModel:
    text: str
    keyboard: KeyboardModel = None
    protect_content: bool = False
