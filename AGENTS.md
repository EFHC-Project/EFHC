# AGENTS.md — EFHC Bot Canon v2.8

**Единый агент-канон для Codex / любых ИИ-агентов**

Этот файл — **единственный и верховный источник правил** для Codex и любых агентных действий по репозиторию EFHC-Bot.
Любой код, правка, перемещение файлов, коммит или PR **обязаны соответствовать** этому файлу.

Если есть конфликт между **AGENTS.md** и любыми другими источниками (доками, комментариями, старым кодом) — **приоритет всегда у AGENTS.md**.

---

## 1. Миссия проекта EFHC Bot

1.1. EFHC Bot — это Telegram-бот + WebApp, реализующие игровую экономику EFHC (Energy for Humanity Coin), где:

* внутренняя единица энергии **kWh конвертируется в EFHC строго 1:1**;
* все денежные потоки проходят через единый **«Банк EFHC»**;
* пользователи никогда не уходят в минус, банк может уйти в минус (режим дефицита);
* обмен работает **только в сторону kWh → EFHC**, обратной конверсии нет и быть не может;
* **P2P-переводы EFHC user→user запрещены**.

1.2. Проект строится на принципах:

* прозрачные инварианты экономики;
* строгая идемпотентность всех денежных операций;
* автономная, самовосстанавливающаяся работа (scheduler, ретраи, dog-run);
* отсутствие скрытых механизмов эмиссии и «магии»;
* жёсткое следование канону репозитория v2.8.

---

## 2. Режим работы Codex с проектом

### 2.1. Итерационный режим (обязательный)

2.1.1. Каждая итерация Codex:

1. **Сначала понимает**:

   * кратко для себя (и в ответе) формулирует, что он понял из текущих файлов/контекста;
   * если данных не хватает — **задаёт вопросы** пользователю.

2. **Не имеет права коммитить / предлагать финальные правки до ответов**
   Если нужны уточнения — любые изменения в коде считаются «черновыми» и не могут считаться канон-реализацией.

3. **После получения ответов**:

   * вносит правки до конца (не половину файла);
   * возвращает **полный текст всех затронутых файлов**.

---

### 2.2. Миграция и пересборка под схему EFHC-Bot v2.8

2.2.1. Codex обязан:

* раскладывать и пересобирать код **строго 1:1 по канонической схеме EFHC-Bot v2.8**;
* использовать **ровно те имена файлов и папок**, которые указаны в каноне структуры;
* **не создавать новых имён/файлов сверх схемы**, если только пользователь не дал прямое указание.

2.2.2. Любой «старый» или некорректно названный файл должен быть:

* либо перенесён/объединён в канонический файл (с обновлением импортов);
* либо удалён как мусор (после переноса логики), если явно согласовано.

2.2.3. Если по канонической схеме требуется файл, но кода пока нет:

* создаётся **канонический скелет** (минимальный рабочий модуль, без бизнес-логики);
* в комментарии допустима только пометка вида:
  `# Логика будет добавлена в следующих итерациях по канону EFHC. TODO запрещён.`

---

### 2.3. Запрет на заглушки и обрезки

2.3.1. Если Codex трогает файл, он обязан:

* вернуть **весь файл целиком**, даже если в нём сотни/тысячи строк;
* не обрезать код и не оставлять «фрагменты» (5–10 строк вместо реального файла);
* **не использовать `TODO`, `pass`, заглушки вместо логики**, если модуль уже должен работать по канону.

2.3.2. Если контекста декларируемо не хватает:

* Codex задаёт вопросы;
* пока нет ответов — **не переписывает бизнес-логику**, а максимум аккуратно комментирует, что часть поведения требует уточнения.

---

### 2.4. «Живая» ИИ-защита и самовосстановление

2.4.1. Во всех затрагиваемых доменах Codex обязан сохранять/внедрять:

* фоновые циклы/планировщик **не падают из-за одной ошибки**;
* любые ошибки локализуются:

  * в статусах (например, `error_*`);
  * в логах (bank log, ton log, task submissions log);
* механизм ретраев через поля `next_retry_at`, `retries_count`;
* **read-through идемпотентность**:

  * по `Idempotency-Key` для денежных запросов;
  * по `tx_hash` для TON-входящих.

2.4.2. Генерация энергии:

* только per-sec ставки `GEN_PER_SEC_BASE_KWH`, `GEN_PER_SEC_VIP_KWH`;
* никаких «суток» и «дневных коэффициентов» в логике.

2.4.3. Защита от гонок и дублей:

* advisоry-locks / `FOR UPDATE SKIP LOCKED` / `UNIQUE + read-through` — там, где канон требует;
* повторные запросы (по одному и тому же ключу) **не создают двойных начислений**.

2.4.4. Codex **не придумывает новые домены/механики** — только реализует уже существующие (panels, exchange, rating, referrals, tasks, lotteries, shop, ads, admin).

---

### 2.5. Стиль кода и проверки качества

2.5.1. Любой новый или обновлённый код обязан соответствовать:

1. **PEP8** (отступы, имена, порядок импортов, длина строки ≤ 79 символов);
2. **Black-подобному форматированию** (общий стиль скобок/отступов);
3. **ruff/flake8-подобному линтингу**:

   * отсутствие неиспользуемых импортов и переменных;
   * отсутствие «голых» `except` без логирования;
4. **mypy-подобной типизации**:

   * type hints у аргументов и возвращаемых значений;
   * аккуратная типизация `Decimal`, `dict[str, Any]`, ORM-моделей.

2.5.2. Отдельный слой — **канон EFHC**:

* нет P2P-переводов EFHC;
* нет EFHC → kWh;
* банк может уйти в минус, пользователь — никогда;
* денежные операции **только через `transactions_service.py`**;
* генерация — только per-sec ставки из `config_core.py`.

---

### 2.6. Комментарии (Эталон EFHC v1.1 — строго обязателен)

2.6.1. Для каждого файла Codex обязан соблюдать структуру комментариев:

1. **Шапка файла (обязательно):**

   * краткое назначение (1–3 строки);
   * указание, влияет ли модуль на деньги/балансы;
   * перечисление критичных инвариантов (P2P запрет, отсутствие EFHC→kWh, NFT-ограничения);
   * краткое описание ИИ-защит/самовосстановления, если есть.

2. **Комментарии к импортам/константам:**

   * зачем нужны нетривиальные зависимости;
   * любые денежные/энергетические числа:
     `Decimal(8)`, округление вниз, через общий хелпер `quantize_decimal`;
   * per-sec ставки **только** из `config_core.py`.

3. **Докстринги функций/роутов:**

   * назначение, входные/выходные данные;
   * побочные эффекты (движение EFHC, изменение статусов);
   * идемпотентность (по какому ключу обеспечивается);
   * когда и какие HTTP-коды/исключения могут быть.

4. **Комментарии к SQL/алгоритмам:**

   * что делает запрос, зачем нужен индекс/курсорный ключ;
   * как добиваемся «мягкой деградации» при ошибках.

5. **Запрет `TODO`/`FIXME`:**

   * вместо них — явное описание, чего **не делает** модуль (если запрещено каноном).

6. **Внизу файла — блок “Пояснения для чайника” (4–6 пунктов):**

   * простыми словами описать, что делает модуль;
   * указать несколько типичных сценариев использования.

---

### 2.7. Формат ответа Codex по результатам итерации

После любой итерации Codex обязан в ответе (в чате) указывать:

1. **MAPPING (при необходимости):**
   `old_path_or_name → new_path_by_v2.8`

2. **FILES (для всех изменённых):**

```text
FILE: backend/app/services/transactions_service.py
<полный готовый код файла>

FILE: backend/app/routes/panels_routes.py
<полный готовый код файла>
```

---

## 3. Канон экономики и безопасности EFHC

### 3.1. Константы генерации (единственные допустимые)

3.1.1. Значения:

* `GEN_PER_SEC_BASE_KWH = 0.00000692` — базовая ставка, кВт·ч/сек (без VIP),
* `GEN_PER_SEC_VIP_KWH  = 0.00000741` — VIP-ставка, кВт·ч/сек.

3.1.2. Правила:

* задаются **только** в `config_core.py`;
* проверяются в `system_locks.py::assert_per_sec_canon()`;
* **запрещено дублировать** суточными константами (`0.598`, `0.64`) в коде;
* суточные величины допустимы только как вычисляемые поля в ответах/комментариях.

---

### 3.2. Денежная точность

* все суммы EFHC и kWh — `Decimal` с 8 знаками после запятой;
* округление **всегда вниз**;
* в БД — `Numeric(30, 8)`;
* округление только через `quantize_decimal(..., decimals=8, rounding="DOWN")` в `utils_core.py`.

---

### 3.3. Экономические инварианты

1. Обмен только **kWh → EFHC 1:1**; EFHC→kWh нет.

2. P2P-переводы EFHC между пользователями запрещены.

3. Пользователь **никогда** не уходит в минус (ни `main`, ни `bonus`, ни `available_kwh`).

4. Банк EFHC может уходить в минус:

   * это не блокирует операции;
   * дефицит помечается флагом `processed_with_deficit` в `efhc_transfers_log`.

5. Все проверки сосредоточены в:

   * `system_locks.py`,
   * `transactions_service.py` (банк).

---

## 4. VIP / NFT

4.1. VIP-статус определяется **только** наличием NFT из канонической коллекции в кошельке пользователя.

4.2. Проверка VIP:

* каждые 10 минут (планировщик `check_vip_nft.py`);
* при входе в раздел (API-ручки, где нужен актуальный `is_vip`).

4.3. Выдача NFT:

* только вручную, по заявке со статусом `PAID_PENDING_MANUAL`;
* авто-минт категорически запрещён.

---

## 5. TON-входящие и MEMO

### 5.1. Канонические форматы MEMO

1. `EFHC<tgid>` → прямое пополнение EFHC пользователю `<tgid>`.
2. `SKU:EFHC|Q:<INT>|TG:<id>` → покупка EFHC-пакета.
3. `SKU:NFT_VIP|Q:1|TG:<id>` → оплата VIP NFT, создаёт заявку `PAID_PENDING_MANUAL`.

Парсер MEMO живёт **строго** в `integrations/ton_api.py` и является детерминированным.

---

### 5.2. Идемпотентность TON-входящих

* `tx_hash` в `ton_inbox_logs` — `UNIQUE`;
* повтор того же `tx_hash` реализуется как read-through:

  * либо находим уже обработанный лог и не создаём дубль;
  * либо догоняем обработку, если кредит не завершён.

Статусы: `received`, `parsed`, `credited`, `error_*`.
Поля ретраев: `next_retry_at`, `retries_count`.

---

## 6. Идемпотентность запросов и банковский сервис

### 6.1. Денежные операции и Idempotency-Key

6.1.1. Любой денежный `POST/PUT/PATCH/DELETE` обязан иметь заголовок `Idempotency-Key`.

6.1.2. Это относится ко всем операциям:

* покупка панелей;
* покупка EFHC-пакетов;
* покупка лотерейных билетов;
* выплаты за задания;
* любые корректировки банк ↔ пользователь;
* заявки на вывод EFHC.

6.1.3. Проверка:

* `require_idempotency_key` (Depends) в `system_locks.py`;
* `MonetaryIdempotencyMiddleware`.

6.1.4. В `efhc_transfers_log`:

* `idempotency_key` — `UNIQUE`;
* повтор того же ключа возвращает прежний результат и **не создаёт новый перевод**.

---

### 6.2. Банк EFHC — единственная точка движения денег

* Только `transactions_service.py` имеет право изменять денежные балансы банка/пользователя.
* Любой сервис верхнего уровня (panels, exchange, tasks, lotteries, shop, referrals, withdrawals) обязан вызывать банк, а не писать напрямую в балансы.

---

## 7. Списки, курсоры, ETag

7.1. Все списки имеют:

* cursor-based пагинацию (keyset), **OFFSET запрещён**;
* формат ответа:

```json
{
  "items": [...],
  "next_cursor": "str | null",
  "has_more": true/false
}
```

7.2. ETag:

* на списки и ключевые ресурсы выдаётся стабильный ETag;
* при `If-None-Match` возможен ответ `304 Not Modified`.

---

## 8. Планировщик и «догон»

8.1. Единый тик фоновых задач — каждые **10 минут**.

8.2. Принцип: «**время — триггер, не фильтр**»:

* не «обработать последние N минут»;
* обрабатываются все записи с не финальным статусом и `next_retry_at <= now`.

8.3. В случае ошибок:

* задача не падает навсегда;
* запись получает `error_*`, `retries_count++`, `next_retry_at`;
* при следующем тике — пробуем снова.

8.4. При старте scheduler выполняет backfill всех незавершённых записей.

---

## 9. Доменная логика

### 9.1. Панели (Panels)

* Цена панели: `100 EFHC`.
* Лимит: `MAX_PANELS = 1000` активных панелей на пользователя.
* Срок жизни: 180 дней (фиксирован в миграции).
* Генерация энергии:

  * только per-sec ставки;
  * начисление через scheduler (`generate_energy.py`) + dog-run при входе в Panels/главный экран.
* Покупка панели:

  * списание сначала с `bonus`, затем с `main`;
  * операции, ведущие к отрицательному балансу, запрещены (`ensure_user_non_negative_after`).

---

### 9.2. Обменник (Exchange)

* Работает только в направлении **kWh → EFHC (1:1)**.
* Алгоритм:

  1. уменьшаем `available_kwh`;
  2. через банк кредитуем EFHC (main/bonus — по правилам);
  3. обратного пути EFHC→kWh не существует.

---

### 9.3. Рейтинг (Rating)

* Формат: «Я + TOP-100».
* Истина — `total_generated_kwh`, не `available_kwh`.
* Для ускорения используются `rating_snapshots`.

---

### 9.4. Рефералы (Referrals)

* Активный реферал — тот, кто купил минимум одну панель (статус необратим).
* Неактивные — до первой покупки.
* UI: отдельные витрины для активных и неактивных.
* Бонусы начисляются через банк (идемпотентно).

---

### 9.5. Задания и реклама (Tasks / Ads)

* Витрина задач/рекламных активностей:

  * пользователь видит список;
  * отправляет доказательства (скрины, ссылки);
  * админ модерирует.
* Выплаты бонусов:

  * только через банк;
  * фиксируются в `efhc_transfers_log`.

---

### 9.6. Лотереи (Lotteries)

* В коде, моделях, схемах и путях **всегда используется форма `lotteries`**, не `lottery`:

  * `lotteries_models.py`,
  * `lotteries_service.py`,
  * `lotteries_routes.py`,
  * `lotteries_crud.py`,
  * `admin_lotteries_*`.
* Билеты покупаются только за EFHC.
* Таблицы: `lotteries`, `lottery_tickets`.
* Каждая покупка билета:

  * денежная операция;
  * требует `Idempotency-Key`;
  * идёт через `transactions_service.py`;
  * создаёт запись в `efhc_transfers_log` (например, `domain="lottery", op_type="ticket_purchase"`).
* Повтор по тому же `Idempotency-Key` **не создаёт второй билет**.

---

### 9.7. Магазин (Shop)

* EFHC-пакеты: 10/50/100/200/300/400/500/1000 EFHC.
* Оплата снаружи (TON/USDT) → TON watcher → ton_inbox_logs → банк → баланс пользователя.
* Карточка с `price = 0` считается **деактивированной**.
* VIP NFT:

  * оплата любым разрешённым способом (TON/USDT/EFHC);
  * создаётся заявка со статусом `PAID_PENDING_MANUAL`;
  * выдача NFT — всегда вручную.
* Любая покупка EFHC-пакета / VIP / панели / билета:

  * денежная операция;
  * только через банк;
  * с Idempotency-Key;
  * с логированием в `efhc_transfers_log`.

---

## 10. Доступ и админка

10.1. Админ-доступ разрешён, если выполняется хотя бы одно:

1. Telegram ID пользователя входит в список `ADMIN_TELEGRAM_ID` (в настройках/БД);
2. у пользователя есть админ-NFT в привязанном кошельке;
3. запрос содержит валидный `X-Admin-Api-Key`.

10.2. В `deps.py` / `security_core.py` реализуется единая зависимость:

* `require_admin_or_nft_or_key`
* все admin-роуты используют **только её**, любые локальные `if user.id in ...` запрещены.

10.3. Админ может:

* корректировки «банк ↔ пользователь» (main/bonus, debit/credit) с логированием;
* управлять витринами Shop / Tasks / Lotteries / Ads;
* запускать TON-реконсиляции;
* смотреть отчёты, метрики, логи.

---

## 11. Архитектура и стек

* Backend: FastAPI (async) + SQLAlchemy 2.0 (async) + asyncpg + Alembic + PostgreSQL (Neon).
* Scheduler: фоновые корутины раз в 10 минут:

  * `generate_energy.py`,
  * `check_vip_nft.py`,
  * `check_ton_inbox.py`,
  * `update_rating.py`,
  * `archive_panels.py`,
  * `lotteries_autorestart.py`,
  * `tasks_autorestart.py`,
  * `ads_rotation.py`,
  * `reports_daily.py`.
* Integrations:

  * `ton_api.py` (таймауты, фолбэк узлов, детерминированный парсер MEMO),
  * `telegram_ads_api.py`.
* Bot: aiogram v3.
* Frontend: Next.js/React + Tailwind (Telegram WebApp):

  * разделы Energy, Panels, Exchange, Shop, Rating, Referrals, Tasks, Ads, Admin.

---

## 12. Ключевые таблицы и индексы

Минимальный набор таблиц:

* `users` — балансы, kWh, VIP, кошельки;
* `panels` — панели, срок жизни, статус;
* `efhc_transfers_log` — журнал банка (idempotency_key, processed_with_deficit и т.д.);
* `ton_inbox_logs` — входящие TON (tx_hash UNIQUE, статусы, ретраи);
* `referrals` — дерево рефералов, активность;
* `user_tasks`, `task_submissions` — задачи и факты выполнения;
* `rating_snapshots` — снепшоты рейтинга;
* `shop_items`, `shop_orders` — карточки и заказы (tx_hash UNIQUE при оплате в TON/USDT);
* `lotteries`, `lottery_tickets`;
* `ads_campaigns`, `ads_impressions` (если реализуется детальная аналитика рекламы).

Индексы для курсоров: `(created_at, id)` по всем основным витринным таблицам.

---

## 13. Канонические адреса

* TON-кошелёк проекта (депозиты):
  `UQAyCoxmxzb2D6cmlf4M8zWYFYkaQuHbN_dgH-IfraFP8QKW`

* NFT-коллекция VIP (TON):
  `EQASPXkEI0NsZQzqkPjk6O_i752LfwSWRFT9WzDc2SJ2zgi0`

---

## 14. Жёсткие запреты

Codex и проект **никогда не имеют права**:

1. Добавлять P2P EFHC user→user.
2. Добавлять обратную конверсию EFHC→kWh.
3. Вводить автодоставку/автоминт VIP NFT.
4. Использовать суточные ставки вместо per-sec в логике.
5. Менять экономику банка без прямого указания пользователя.
6. Вводить временные заглушки (`TODO`, `pass`) вместо обязанных по канону реализаций.

---

## 15. Контроль перед PR

Перед любым PR Codex обязан сам убедиться, что:

1. Константы генерации — только `GEN_PER_SEC_BASE_KWH` и `GEN_PER_SEC_VIP_KWH`.
2. Все денежные операции идут только через `transactions_service.py`.
3. Везде, где деньги, есть `Idempotency-Key` и идемпотентность.
4. Все списки используют cursor-based пагинацию и возвращают ETag.
5. Scheduler тикает раз в 10 минут, ошибки его не «убивают», ретраи работают.
6. Нет `TODO`/`FIXME` и несанкционированных заглушек.
7. Комментарии соответствуют Эталону v1.1.
8. Код проходит PEP8/black/ruff/mypy-проверку.

---

## 16. Каноническая канон схема бота EFHC-Bot

Структура репозитория фиксирована каноном v2.8.  
Codex обязан раскладывать и поддерживать код строго 1:1 по этой карте, не создавая новых имён/папок без прямого указания пользователя.

```text
EFHC-Bot/
├─ README.md
├─ CHANGELOG.md
├─ LICENSE
├─ CODE_OF_CONDUCT.md
├─ CONTRIBUTING.md
├─ SECURITY.md
├─ .gitignore
├─ .editorconfig
├─ .gitattributes
├─ .pre-commit-config.yaml
├─ pyproject.toml
├─ requirements.txt
├─ requirements-dev.txt
├─ Makefile
│
├─ .env.neon.example
├─ .env.local.example
├─ .env.prod.example
├─ .env.ci.example
│
├─ docs/
│  ├─ EFHC_CANON.md
│  ├─ ARCHITECTURAL_LOCKS.md
│  ├─ ADMIN_PANEL_SPEC.md
│  ├─ API_GUIDE.md
│  ├─ TON_MEMO_SPEC.md
│  ├─ RATING_RULES.md
│  ├─ REFERRALS_RULES.md
│  ├─ ENERGY_RULES.md
│  ├─ SCHEDULER_PLAYBOOK.md
│  ├─ DB_SCHEMA.md
│  ├─ RUNBOOKS/
│  │  ├─ INCIDENTS.md
│  │  ├─ RECONCILIATION.md
│  │  └─ BANK_DEFICIT.md
│  └─ ADR/
│     ├─ 0001-db-choice-neon.md
│     ├─ 0002-idempotency-readthrough.md
│     └─ 0003-per-sec-only.md
│
├─ ops/
│  ├─ docker/
│  │  ├─ Dockerfile.backend
│  │  ├─ Dockerfile.frontend
│  │  └─ docker-compose.yml
│  ├─ k8s/
│  │  ├─ backend-deployment.yaml
│  │  ├─ backend-service.yaml
│  │  ├─ frontend-deployment.yaml
│  │  ├─ frontend-service.yaml
│  │  ├─ ingress.yaml
│  │  └─ secrets.example.yaml
│  ├─ render/
│  │  └─ render.yaml
│  ├─ vercel/
│  │  └─ vercel.json
│  ├─ monitoring/
│  │  ├─ prometheus.yml
│  │  ├─ grafana/
│  │  │  ├─ dashboards/
│  │  │  │  ├─ backend.json
│  │  │  │  ├─ scheduler.json
│  │  │  │  └─ ton_watcher.json
│  │  │  └─ datasources.yaml
│  │  └─ sentry.example.yaml
│  └─ scripts/
│     ├─ bootstrap_neon.sql
│     ├─ seed_data.py
│     ├─ export_openapi.py
│     └─ recon_ton.py
│
├─ .github/
│  └─ workflows/
│     ├─ ci.yml
│     ├─ cd-backend.yml
│     └─ cd-frontend.yml
│
├─ backend/
│  ├─ run.py
│  ├─ openapi/
│  │  └─ openapi.json
│  ├─ alembic.ini
│  ├─ migrations/
│  │  ├─ env.py
│  │  ├─ script.py.mako
│  │  └─ versions/
│  │     ├─ 0001_init.py
│  │     ├─ 0002_indexes.py
│  │     ├─ 0003_unique_keys.py
│  │     └─ 0004_rating_snapshots.py
│  └─ app/
│     ├─ __init__.py
│     ├─ core/
│     │  ├─ __init__.py
│     │  ├─ config_core.py
│     │  ├─ database_core.py
│     │  ├─ logging_core.py
│     │  ├─ security_core.py
│     │  ├─ system_locks.py
│     │  ├─ utils_core.py
│     │  ├─ errors_core.py
│     │  └─ deps.py
│     ├─ integrations/
│     │  ├─ ton_api.py
│     │  └─ telegram_ads_api.py
│     ├─ models/
│     │  ├─ __init__.py
│     │  ├─ user_models.py
│     │  ├─ panels_models.py
│     │  ├─ shop_models.py
│     │  ├─ tasks_models.py
│     │  ├─ referral_models.py
│     │  ├─ rating_models.py
│     │  ├─ lotteries_models.py
│     │  ├─ order_models.py
│     │  ├─ achievements_models.py
│     │  ├─ bank_models.py
│     │  ├─ transactions_models.py
│     │  └─ ads_models.py
│     ├─ schemas/
│     │  ├─ __init__.py
│     │  ├─ common_schemas.py
│     │  ├─ user_schemas.py
│     │  ├─ panels_schemas.py
│     │  ├─ exchange_schemas.py
│     │  ├─ shop_schemas.py
│     │  ├─ tasks_schemas.py
│     │  ├─ referrals_schemas.py
│     │  ├─ rating_schemas.py
│     │  ├─ orders_schemas.py
│     │  ├─ lotteries_schemas.py
│     │  ├─ transactions_schemas.py
│     │  └─ ads_schemas.py
│     ├─ crud/
│     │  ├─ __init__.py
│     │  ├─ user_crud.py
│     │  ├─ panels_crud.py
│     │  ├─ transactions_crud.py
│     │  ├─ tasks_crud.py
│     │  ├─ referrals_crud.py
│     │  ├─ ranks_crud.py
│     │  ├─ shop_crud.py
│     │  ├─ order_crud.py
│     │  ├─ lotteries_crud.py
│     │  └─ admin/
│     │     ├─ __init__.py
│     │     ├─ admin_users_crud.py
│     │     ├─ admin_panels_crud.py
│     │     ├─ admin_bank_crud.py
│     │     ├─ admin_referrals_crud.py
│     │     ├─ admin_lotteries_crud.py
│     │     ├─ admin_tasks_crud.py
│     │     ├─ admin_shop_crud.py
│     │     ├─ admin_withdrawals_crud.py
│     │     ├─ admin_stats_crud.py
│     │     └─ admin_ads_crud.py
│     ├─ services/
│     │  ├─ __init__.py
│     │  ├─ transactions_service.py
│     │  ├─ energy_service.py
│     │  ├─ panels_service.py
│     │  ├─ exchange_service.py
│     │  ├─ ranks_service.py
│     │  ├─ referral_service.py
│     │  ├─ tasks_service.py
│     │  ├─ shop_service.py
│     │  ├─ orders_service.py
│     │  ├─ lotteries_service.py
│     │  ├─ watcher_service.py
│     │  ├─ nft_check_service.py
│     │  ├─ scheduler_service.py
│     │  ├─ reports_service.py
│     │  ├─ admin_service.py
│     │  └─ admin/
│     │     ├─ __init__.py
│     │     ├─ admin_facade.py
│     │     ├─ admin_rbac.py
│     │     ├─ admin_logging.py
│     │     ├─ admin_settings.py
│     │     ├─ admin_notifications.py
│     │     ├─ admin_bank_service.py
│     │     ├─ admin_users_service.py
│     │     ├─ admin_panels_service.py
│     │     ├─ admin_referral_service.py
│     │     ├─ admin_wallets_service.py
│     │     ├─ admin_stats_service.py
│     │     ├─ admin_lotteries_service.py
│     │     ├─ admin_withdrawals_service.py
│     │     ├─ admin_tasks_service.py
│     │     └─ admin_ads_service.py
│     ├─ routes/
│     │  ├─ __init__.py
│     │  ├─ user_routes.py
│     │  ├─ panels_routes.py
│     │  ├─ exchange_routes.py
│     │  ├─ shop_routes.py
│     │  ├─ tasks_routes.py
│     │  ├─ rating_routes.py
│     │  ├─ referrals_routes.py
│     │  ├─ withdraw_routes.py
│     │  ├─ lotteries_routes.py
│     │  ├─ ads_routes.py
│     │  └─ admin/
│     │     ├─ admin_routes.py
│     │     ├─ admin_users_routes.py
│     │     ├─ admin_panels_routes.py
│     │     ├─ admin_bank_routes.py
│     │     ├─ admin_referrals_routes.py
│     │     ├─ admin_lotteries_routes.py
│     │     ├─ admin_tasks_routes.py
│     │     ├─ admin_shop_routes.py
│     │     ├─ admin_withdrawals_routes.py
│     │     ├─ admin_stats_routes.py
│     │     └─ admin_ads_routes.py
│     ├─ scheduler/
│     │  ├─ generate_energy.py
│     │  ├─ check_vip_nft.py
│     │  ├─ check_ton_inbox.py
│     │  ├─ update_rating.py
│     │  ├─ archive_panels.py
│     │  ├─ lotteries_autorestart.py
│     │  ├─ tasks_autorestart.py
│     │  ├─ ads_rotation.py
│     │  └─ reports_daily.py
│     └─ bot/
│        ├─ __init__.py
│        ├─ bot.py
│        ├─ keyboards.py
│        ├─ middlewares.py
│        ├─ states.py
│        ├─ texts.py
│        └─ handlers/
│           ├─ start_handlers.py
│           ├─ panels_handlers.py
│           ├─ exchange_handlers.py
│           ├─ shop_handlers.py
│           ├─ tasks_handlers.py
│           ├─ referrals_handlers.py
│           ├─ rating_handlers.py
│           ├─ lotteries_handlers.py
│           ├─ ads_handlers.py
│           ├─ health_handlers.py
│           └─ admin/
│              ├─ admin_main_handlers.py
│              ├─ admin_users_handlers.py
│              ├─ admin_panels_handlers.py
│              ├─ admin_shop_handlers.py
│              ├─ admin_tasks_handlers.py
│              ├─ admin_lotteries_handlers.py
│              ├─ admin_referrals_handlers.py
│              ├─ admin_withdrawals_handlers.py
│              ├─ admin_stats_handlers.py
│              └─ admin_ads_handlers.py
│
├─ frontend/
│  ├─ package.json
│  ├─ yarn.lock
│  ├─ tsconfig.json
│  ├─ next.config.js
│  ├─ postcss.config.js
│  ├─ tailwind.config.js
│  ├─ public/
│  │  ├─ icons/
│  │  ├─ images/
│  │  └─ locale/
│  │     ├─ ru.json
│  │     ├─ en.json
│  │     ├─ ua.json
│  │     ├─ de.json
│  │     ├─ fr.json
│  │     ├─ es.json
│  │     ├─ it.json
│  │     └─ pl.json
│  └─ src/
│     ├─ pages/
│     │  ├─ _app.tsx
│     │  ├─ index.tsx
│     │  ├─ panels.tsx
│     │  ├─ exchange.tsx
│     │  ├─ shop.tsx
│     │  ├─ tasks.tsx
│     │  ├─ ads.tsx
│     │  ├─ rating.tsx
│     │  ├─ referrals.tsx
│     │  └─ admin/
│     │     ├─ index.tsx
│     │     ├─ users.tsx
│     │     ├─ panels.tsx
│     │     ├─ shop.tsx
│     │     ├─ tasks.tsx
│     │     ├─ lotteries.tsx
│     │     ├─ withdrawals.tsx
│     │     ├─ referrals.tsx
│     │     ├─ ads.tsx
│     │     └─ reports.tsx
│     ├─ components/
│     │  ├─ EnergyGauge.tsx
│     │  ├─ PanelsList.tsx
│     │  ├─ ExchangePanel.tsx
│     │  ├─ ShopGrid.tsx
│     │  ├─ RatingTable.tsx
│     │  ├─ ReferralsTabs.tsx
│     │  ├─ AdsBanner.tsx
│     │  ├─ AdminCharts.tsx
│     │  ├─ AdminTable.tsx
│     │  └─ ui/
│     │     ├─ Button.tsx
│     │     ├─ Card.tsx
│     │     ├─ Badge.tsx
│     │     ├─ Modal.tsx
│     ├─ lib/
│     │  ├─ api.ts
│     │  ├─ auth.ts
│     │  ├─ i18n.ts
│     │  └─ store.ts
│     ├─ hooks/
│     │  ├─ useForceSync.ts
│     │  └─ useCursorList.ts
│     └─ styles/
│        └─ globals.css
│
└─ tests/
   ├─ backend/
   │  ├─ unit/
   │  ├─ integration/
   │  ├─ contract/
   │  ├─ performance/
   │  └─ data/
   ├─ frontend/
   │  ├─ unit/
   │  ├─ e2e/
   │  └─ mocks/
   └─ load/
      └─ k6-scenarios/
         ├─ generation.js
         ├─ exchange.js
         ├─ lotteries.js
         ├─ ads.js
         └─ traffic_mix.js

Примечание: “+ дополнительные необходимые файлы для работы бота” допускается только по прямому указанию пользователя и после согласования названий/места в схеме.
