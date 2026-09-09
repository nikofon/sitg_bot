from aiogram.fsm.state import State, StatesGroup


class SettingEditState(StatesGroup):
    selecting = State()
    entering = State()


class AdminAuthenticationState(StatesGroup):
    entering_credential = State()


class TournamentTokenRequestState(StatesGroup):
    entering_name = State()
    entering_commentary = State()


class AdminTokenDecisionState(StatesGroup):
    entering_commentary = State()


class TournamentCreationState(StatesGroup):
    confirming = State()
    entering_name = State()
    entering_slug = State()
    entering_type = State()
    entering_registration_end = State()
    entering_start = State()
    entering_planned_end = State()
    entering_ruleset = State()
    entering_visibility = State()
    entering_language = State()
    entering_other_language = State()


class PacketUploadState(StatesGroup):
    waiting_document = State()
