# EFHC Bot — «живые» ИИ-защиты и самовосстановление (канон v2.8)

## Назначение
Эта памятка перечисляет реальные защитные механизмы в коде EFHC Bot с прямыми ссылками на функции и их обязанности. Все элементы основаны на каноне v2.8: пер-секундная генерация, строгая идемпотентность, курсорные списки и мягкие ретраи планировщиков.

## Энергия и панели
- `services/energy_service.py::health_snapshot` — быстрая диагностика активных пользователей/панелей и долга по генерации; не меняет балансы, пригодно для алёртов.
- `services/energy_service.py::backfill_all` + `rescue_fix_last_generated_after_expire` + `rescue_fill_null_last_tick` — «догоняющий» алгоритм и самолечение last_tick; использует `FOR UPDATE SKIP LOCKED` и advisory-локи, чтобы параллельные воркеры не дублировали начисление.
- `scheduler/generate_energy.py::_run_once_guarded` и `_run_forever` — защищённый тик раз в 10 минут с мягкими ретраями и корректным завершением цикла.

## TON watcher и банковский мост
- `scheduler/check_ton_inbox.py::_run_once_guarded` и `_run_forever` — вечный цикл с advisory-локом и джиттером сна, не падает при сетевых/БД-сбоях.
- `services/watcher_service.py::process_incoming_payments` и `process_existing_backlog` — идемпотентная обработка входящих TON по `tx_hash`, статусам `received/parsed/credited/error_*`, с `next_retry_at` и счётчиком ретраев.
- `integrations/ton_api.py::parse_memo` — детерминированный парсер MEMO (`EFHC<tgid>`, `SKU:EFHC|Q:x|TG:y`, `SKU:NFT_VIP|Q:1|TG:y`) с жёсткими проверками.

## Банковская идемпотентность и денежные операции
- `services/transactions_service.py` — единый read-through слой для всех движений EFHC, `idempotency_key` уникален в `efhc_transfers_log`; повтор запроса возвращает уже зафиксированный результат.
- Все маршруты POST/PUT денежных операций требуют `Idempotency-Key` через `MonetaryIdempotencyMiddleware` и зависимость `require_idempotency_key` в `core/system_locks.py`.
- Пользовательские балансы не уходят в минус (жёсткое правило сервисов панелей, обмена, задач), банк может уходить в минус с фиксацией `processed_with_deficit`.

## Планировщики и догон пропусков
- Все scheduler-модули работают с шагом 10 минут и принципом «время — триггер, не фильтр»: обрабатывается всё, что `next_retry_at <= now`, без окна «последние N минут».
- Ретрай-логика: при ошибке ставится статус `error_*`, `next_retry_at` и `retries_count`, что исключает падение цикла.
- Advisory-локи защищают от параллельного дубля воркера в кластере (energy, TON watcher, NFT/VIP и др.).

## Кэширование, курсоры и ETag
- Списки и ресурсные GET выдают стабильный `ETag` через `core/utils_core.make_etag`; фронт может использовать `If-None-Match` для 304.
- Пагинация реализована только через курсоры `next_cursor/has_more` без `OFFSET`, чтобы выдерживать большие объёмы данных без дырок.

## Дополнительные заметки
- Файл `core/system_locks.py` выполняет стартовые канонические проверки (пер-секундные ставки, запрет обратной конверсии, требование Idempotency-Key).
- Админ-доступ во всех админ-API — через `require_admin_or_nft_or_key`, допускающий либо Telegram ID из allowlist, либо NFT VIP, либо серверный ключ.
- Все денежные POST/PUT/DELETE в сервисах панелей, обмена, лотерей, задач и магазина используют `transactions_service` для записи зеркальных логов банка.

