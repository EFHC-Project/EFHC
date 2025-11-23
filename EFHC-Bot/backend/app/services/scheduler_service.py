# -*- coding: utf-8 -*-
# backend/app/services/scheduler_service.py
# =============================================================================
# Назначение кода:
#   Централизованный планировщик фоновых задач EFHC Bot. Будит все задачи каждые
#   10 минут, а не по часам/дням. «Ежедневные» задачи запускаются через дневные
#   ворота (gate) внутри того же 10-минутного цикла. Любые сбои — не роняют цикл.
#
# Канон/инварианты:
#   • Единый ритм: автоцикл каждые 10 минут. Время — триггер пробуждения, НЕ
#     фильтр данных. Что обрабатывать — решают сами сервисы по статусу/логам.
#   • ИИ-самовосстановление: короткие ретраи, экспоненциальный backoff (≤5 мин),
#     защита таймаутами, отдельные блокировки на задачу, наблюдаемость.
#   • Денежная логика вне планировщика. Здесь только вызовы сервисов.
#
# ИИ-защита/самовосстановление:
#   • Каждая задача исполняется с таймаутом, ошибки логируются, backoff растёт.
#   • Даже при падении одной задачи цикл продолжается для остальных.
#   • «Ежедневные» задачи имеют DailyGate — запускаются не чаще 1 раза/24 ч.
#
# Запреты:
#   • Нет длительных «снов» на час/сутки. Нет блокирующих ожиданий внешних ИО.
#   • Нет прямого доступа к БД из планировщика — всё через сервисы.
# =============================================================================

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from pydantic import BaseSettings, Field, validator

from backend.app.core.logging_core import get_logger

# Тип сигнатуры выполняемой корутины: async def job() -> None
JobCallable = Callable[[], Awaitable[None]]

# -----------------------------------------------------------------------------
# Настройки планировщика (через .env)
# -----------------------------------------------------------------------------
class SchedulerSettings(BaseSettings):
    """
    Конфигурация планировщика (все значения могут быть переопределены через .env):

      SCHED_INTERVAL_SEC=600             — единый интервал будильника (сек)
      SCHED_TASK_TIMEOUT_SEC=300         — таймаут на выполнение одной задачи (сек)
      SCHED_BACKOFF_START_SEC=5          — начальный backoff после ошибки (сек)
      SCHED_BACKOFF_MAX_SEC=300          — максимум backoff (сек)
      SCHED_MAX_PARALLEL_TASKS=3         — ограничение параллельных задач
      SCHED_JITTER_SEC=7                 — случайный джиттер к интервалу (сек, 0..N)
    """
    INTERVAL_SEC: int = Field(600)
    TASK_TIMEOUT_SEC: int = Field(300)
    BACKOFF_START_SEC: int = Field(5)
    BACKOFF_MAX_SEC: int = Field(300)
    MAX_PARALLEL_TASKS: int = Field(3)
    JITTER_SEC: int = Field(7)

    class Config:
        env_prefix = "SCHED_"

    @validator("INTERVAL_SEC", "TASK_TIMEOUT_SEC", "BACKOFF_START_SEC", "BACKOFF_MAX_SEC", "MAX_PARALLEL_TASKS")
    def _positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("значение должно быть > 0")
        return v

SETTINGS = SchedulerSettings()

# -----------------------------------------------------------------------------
# Логи
# -----------------------------------------------------------------------------
logger = get_logger("efhc.scheduler")

# -----------------------------------------------------------------------------
# Мягкие импорты сервисов (если нет — задачи будут скипаться, приложение не падает)
# -----------------------------------------------------------------------------
def _soft_import(path: str, name: str) -> Any:
    try:
        module = __import__(path, fromlist=[name])
        return getattr(module, name)
    except Exception as e:  # pragma: no cover
        logger.debug("soft import failed: %s.%s -> %s", path, name, e)
        return None

# Сервисы-исполнители «единичных» тиков (каждые 10 минут)
energy_backfill_once     = _soft_import("backend.app.scheduler.generate_energy", "run_once")
ton_inbox_run_once       = _soft_import("backend.app.scheduler.check_ton_inbox", "run_once")
vip_check_run_once       = _soft_import("backend.app.scheduler.check_vip_nft", "run_once")
archive_panels_run_once  = _soft_import("backend.app.scheduler.archive_panels", "run_once")
update_rating_run_once   = _soft_import("backend.app.scheduler.update_rating", "run_once")
reports_daily_run_once   = _soft_import("backend.app.scheduler.reports_daily", "run_once")

# -----------------------------------------------------------------------------
# Утилиты времени
# -----------------------------------------------------------------------------
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

# -----------------------------------------------------------------------------
# DailyGate — врата разовой «ежедневной» отработки внутри 10-минутного цикла
# -----------------------------------------------------------------------------
@dataclass
class DailyGate:
    """
    Делает задачу «ежедневной», не вводя отдельного «крона».
    Запускает job не чаще 1 раза в window (обычно 24 ч).
    """
    window: timedelta = field(default=timedelta(hours=24))
    last_run_at: Optional[datetime] = None

    def due(self, now: Optional[datetime] = None) -> bool:
        now = now or _utcnow()
        if self.last_run_at is None:
            return True
        return (now - self.last_run_at) >= self.window

    def mark(self, now: Optional[datetime] = None) -> None:
        self.last_run_at = now or _utcnow()

# -----------------------------------------------------------------------------
# Структуры задач
# -----------------------------------------------------------------------------
@dataclass
class _Job:
    name: str
    factory: Callable[[], JobCallable]               # фабрика корутины
    daily_gate: Optional[DailyGate] = None           # если указано — «ежедневная»
    backoff_sec: int = field(default=SETTINGS.BACKOFF_START_SEC)
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    running: bool = False
    next_allowed_at: Optional[datetime] = None       # когда можно запускать после ошибки

# -----------------------------------------------------------------------------
# Планировщик
# -----------------------------------------------------------------------------
class SchedulerService:
    """
    Централизованный планировщик с единым будильником 10 минут.
    Ничего не знает о бизнес-логике — только вызывает run_once() у конкретных задач.
    """

    def __init__(self, settings: SchedulerSettings = SETTINGS):
        self.s = settings
        self._jobs: Dict[str, _Job] = {}
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._sem = asyncio.Semaphore(self.s.MAX_PARALLEL_TASKS)

    # ----------------------------- Регистрация ------------------------------

    def register_defaults(self) -> None:
        """
        Регистрирует стандартные задачи EFHC, все — под единый 10-минутный цикл:
          • generate_energy       — догон/начисление энергии по панелям
          • check_ton_inbox       — входящие TON (депозиты/Shop), read-through
          • check_vip_nft         — проверка NFT-VIP (разрешено чаще, чем раз/сутки)
          • archive_panels        — перевод просроченных панелей в архив
          • update_rating         — пересчёт кэшей рейтинга Я+TOP
          • reports_daily         — сводные отчёты админам (через DailyGate)
        """
        def _factory_or_skip(
            callable_ref: Optional[JobCallable],
            label: str,
        ) -> Callable[[], JobCallable]:
            async def _noop() -> None:
                logger.warning("%s unavailable — skip", label)

            async def _wrap() -> None:
                await callable_ref()  # type: ignore[misc]

            return (lambda: _noop) if callable_ref is None else (lambda: _wrap)

        # Ежедневная только reports_daily — через ворота (остальное можно выполнять каждый тик)
        self._add_job("generate_energy", _factory_or_skip(energy_backfill_once, "generate_energy"))
        self._add_job("check_ton_inbox", _factory_or_skip(ton_inbox_run_once, "check_ton_inbox"))
        self._add_job("check_vip_nft", _factory_or_skip(vip_check_run_once, "check_vip_nft"))
        self._add_job("archive_panels", _factory_or_skip(archive_panels_run_once, "archive_panels"))
        self._add_job("update_rating", _factory_or_skip(update_rating_run_once, "update_rating"))
        self._add_job("reports_daily", _factory_or_skip(reports_daily_run_once, "reports_daily"), daily=True)

        logger.info("Scheduler: registered jobs: %s", list(self._jobs.keys()))

    def _add_job(self, name: str, factory: Callable[[], JobCallable], daily: bool = False) -> None:
        if name in self._jobs:
            raise ValueError(f"job '{name}' already registered")
        self._jobs[name] = _Job(
            name=name,
            factory=factory,
            daily_gate=(DailyGate() if daily else None),
        )

    # ------------------------------- Жизненный цикл -------------------------

    async def start(self) -> None:
        """
        Запускает фоновый 10-минутный цикл.
        Фактический цикл идёт в отдельной задаче; остановка — через stop().
        """
        async with self._lock:
            if self._stop.is_set():
                raise RuntimeError("Scheduler is stopping/stopped")
            logger.info(
                "Scheduler: start (%d jobs, interval=%ss, timeout=%ss, max_parallel=%s)",
                len(self._jobs),
                self.s.INTERVAL_SEC,
                self.s.TASK_TIMEOUT_SEC,
                self.s.MAX_PARALLEL_TASKS,
            )
        asyncio.create_task(self._loop(), name="scheduler:main")

    async def stop(self) -> None:
        """
        Запрашивает остановку цикла. Текущий тик будет завершён,
        новые тики после этого не запустятся.
        """
        self._stop.set()

    async def run_single_tick(self) -> None:
        """
        Одноразовый запуск одного тика без вечного цикла.
        Удобно для админских ручных запусков/тестов.
        """
        await self._run_tick()

    async def _loop(self) -> None:
        """
        Главный цикл: каждые INTERVAL_SEC±jitter пытается запустить все задачи,
        учитывая backoff и DailyGate. Никакие ошибки не роняют цикл.
        """
        try:
            while not self._stop.is_set():
                start = _utcnow()
                await self._run_tick()

                # Джиттер для рассинхронизации кластеров/воркеров
                jitter = 0
                if self.s.JITTER_SEC > 0:
                    try:
                        import random
                        jitter = random.randint(0, self.s.JITTER_SEC)
                    except Exception:  # pragma: no cover
                        jitter = 0

                # Ожидание до следующего тика (если не попросили stop)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=max(1, self.s.INTERVAL_SEC + jitter),
                    )
                except asyncio.TimeoutError:
                    # Нормальный случай — просто идём на следующий тик
                    pass
                except Exception:
                    logger.exception("Scheduler: unexpected wait error")
                finally:
                    duration = (_utcnow() - start).total_seconds()
                    logger.debug("Scheduler tick finished in %.3fs", duration)
        except asyncio.CancelledError:
            logger.info("Scheduler: cancelled")
        except Exception:
            logger.exception("Scheduler: critical failure (loop)")
        finally:
            logger.info("Scheduler: stopped")

    # ------------------------------- Один тик --------------------------------

    async def _run_tick(self) -> None:
        """
        Пытается запустить все задачи параллельно (ограничено семафором).
        Задачи с backoff не запускаются до наступления next_allowed_at.
        Ежедневные задачи — запускаются, только если due() в DailyGate.
        """
        now = _utcnow()
        jobs_to_run: List[_Job] = []

        for job in self._jobs.values():
            # DailyGate: если задана — выполняем не чаще, чем раз в window
            if job.daily_gate is not None and not job.daily_gate.due(now):
                logger.debug("Job %s: daily gate not due, skip", job.name)
                continue

            # Backoff: если ещё не подошло время — пропускаем
            if job.next_allowed_at is not None and now < job.next_allowed_at:
                logger.debug(
                    "Job %s: backoff until %s (now=%s)",
                    job.name,
                    job.next_allowed_at.isoformat(),
                    now.isoformat(),
                )
                continue

            jobs_to_run.append(job)

        if not jobs_to_run:
            logger.debug("Scheduler: nothing to run in this tick")
            return

        async def _guarded(job: _Job) -> None:
            async with self._sem:
                await self._run_job(job)

        tasks = [
            asyncio.create_task(_guarded(job), name=f"scheduler:job:{job.name}")
            for job in jobs_to_run
        ]

        await asyncio.gather(*tasks, return_exceptions=True)

    # -------------------------- Выполнение одной задачи ----------------------

    async def _run_job(self, job: _Job) -> None:
        if job.running:
            logger.warning("Job %s: already running, skip", job.name)
            return

        job.running = True
        try:
            coro_func = job.factory()
            # Таймаут на корутину
            await asyncio.wait_for(coro_func(), timeout=self.s.TASK_TIMEOUT_SEC)

            # Успех: сбросить счётчики/ошибки/backoff/next_allowed_at
            job.consecutive_failures = 0
            job.last_error = None
            job.backoff_sec = self.s.BACKOFF_START_SEC
            job.next_allowed_at = None
            if job.daily_gate is not None:
                job.daily_gate.mark()
            logger.info("Job %s: done", job.name)
        except asyncio.TimeoutError:
            job.consecutive_failures += 1
            job.last_error = "timeout"
            job.backoff_sec = min(job.backoff_sec * 2, self.s.BACKOFF_MAX_SEC)
            job.next_allowed_at = _utcnow() + timedelta(seconds=job.backoff_sec)
            logger.warning(
                "Job %s: timeout (fail=%s, backoff=%ss, next_at=%s)",
                job.name,
                job.consecutive_failures,
                job.backoff_sec,
                job.next_allowed_at.isoformat(),
            )
        except Exception as e:
            job.consecutive_failures += 1
            job.last_error = str(e)
            job.backoff_sec = min(job.backoff_sec * 2, self.s.BACKOFF_MAX_SEC)
            job.next_allowed_at = _utcnow() + timedelta(seconds=job.backoff_sec)
            logger.exception(
                "Job %s: error (fail=%s, backoff=%ss, next_at=%s): %s",
                job.name,
                job.consecutive_failures,
                job.backoff_sec,
                job.next_allowed_at.isoformat(),
                e,
            )
        finally:
            job.running = False

    # ------------------------------- Наблюдаемость ---------------------------

    def list_jobs(self) -> List[Dict[str, Any]]:
        """
        Возвращает краткую сводку по задачам для health/админки.
        """
        out: List[Dict[str, Any]] = []
        for j in self._jobs.values():
            out.append(
                {
                    "name": j.name,
                    "daily": bool(j.daily_gate is not None),
                    "running": j.running,
                    "failures": j.consecutive_failures,
                    "last_error": j.last_error,
                    "backoff_sec": j.backoff_sec,
                    "next_allowed_at": j.next_allowed_at.isoformat() if j.next_allowed_at else None,
                    "last_daily_run_at": (
                        j.daily_gate.last_run_at.isoformat()
                        if j.daily_gate and j.daily_gate.last_run_at
                        else None
                    ),
                }
            )
        return out


# Экземпляр планировщика по умолчанию
default_scheduler = SchedulerService(SETTINGS)

# Хелперы запуска/остановки (для main/worker)
async def startup_scheduler() -> None:
    """
    Что делает:
      • Регистрирует дефолтные задачи и запускает основной цикл.
      • Не падает при отсутствии каких-то модулей — соответствующие задачи будут
        логировать warning и пропускаться.
    """
    default_scheduler.register_defaults()
    await default_scheduler.start()
    logger.info("Scheduler started")


async def shutdown_scheduler() -> None:
    """
    Аккуратно запрашивает остановку цикла; текущий тик дорабатывает до конца.
    """
    await default_scheduler.stop()
    logger.info("Scheduler stop requested")

# =============================================================================
# Пояснения «для чайника»:
#   • Планировщик ничего сам не «считает» и не трогает балансы — он только зовёт
#     функции run_once() в модулях scheduler/*.py. Эти функции сами читают БД,
#     выбирают «нефинальные» записи по статусам/next_retry_at и делают работу.
#   • «Ежедневные» задачи (например, отчёт) не ждут реальных суток — цикл будит
#     их каждые 10 минут, а DailyGate гарантирует не чаще 1 раза/24 ч.
#   • Backoff реализован по времени: при ошибках и таймаутах задача «отдыхает»
#     до next_allowed_at, затем пробуется снова. Успех полностью сбрасывает backoff.
#   • MAX_PARALLEL_TASKS ограничивает реальный параллелизм исполнения задач.
#   • Если какого-то модуля нет — соответствующая задача просто логирует warning
#     и пропускается (через _noop), не ломая остальной цикл.
# =============================================================================
