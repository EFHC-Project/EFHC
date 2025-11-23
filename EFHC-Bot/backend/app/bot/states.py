"""FSM состояния бота для простых сценариев."""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class TaskSubmissionStates(StatesGroup):
    """Состояния отправки доказательств по заданиям."""

    waiting_for_proof: State = State()
    confirmation: State = State()
