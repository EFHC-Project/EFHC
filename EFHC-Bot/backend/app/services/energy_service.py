"""Сервис расчёта генерации энергии по панелям.

Простая функция для новичков: вычисляем энергию за фиксированный период,
применяем VIP-ставку при наличии NFT и аккумулируем в балансе пользователя.
Все значения квантуются до 8 знаков вниз, как требует канон EFHC.
"""

from __future__ import annotations

from decimal import Decimal

from ..core.config_core import GEN_PER_SEC_BASE_KWH, GEN_PER_SEC_VIP_KWH
from ..core.utils_core import quantize_decimal
from ..models import Panel, User


class EnergyService:
    """Расчёт прироста kWh, учитывая VIP-ставку и число панелей."""

    def __init__(self, user: User, panels: list[Panel]):
        self.user = user
        self.panels = panels

    def accrue(self, seconds: int) -> Decimal:
        """Начислить энергию за заданное число секунд.

        Если пользователь VIP, используется повышенная ставка; иначе базовая.
        Результат округляется вниз до 8 знаков и сохраняется в балансе
        пользователя, а также накапливается в ``total_generated_kwh``.
        """

        if seconds <= 0 or not self.panels:
            return Decimal("0")

        rate = GEN_PER_SEC_VIP_KWH if self.user.is_vip else GEN_PER_SEC_BASE_KWH
        produced = quantize_decimal(rate * Decimal(seconds) * Decimal(len(self.panels)))
        self.user.available_kwh += produced
        self.user.total_generated_kwh += produced
        return produced
