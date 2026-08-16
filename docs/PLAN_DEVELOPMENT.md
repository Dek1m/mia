# План развития Mia Framework

> Версия: 0.1  
> Дата: 2026-08-16  
> Статус: Draft  
> Связанные репозитории: mia, mia-db, mia-auth, mia-sessions, mia-llm, selti

## 1. Цель

Сделать Mia полноценным runtime для AI-агентов и приложений вместо OpenCode:

- модульная система + Universal Task System (уже есть);
- REST API;
- React UI;
- сессии и LLM;
- семантическая память (selti);
- multi-agent чаты в рамках workspace → session.

Итог: локальный/self-hosted стек «runtime + память + интерфейс», в котором агенты работают через задачи, а не через один монолитный цикл.

## 2. Текущее состояние (as-is)

| Компонент | Статус | Репозиторий |
|-----------|--------|-------------|
| Core (Application, DI, modules) | ✅ | mia |
| Universal Task System | ✅ | mia |
| SmartDispatcher / Classifier / AdaptiveRouter | ✅ | mia |
| ThreadPool / WorkerManager / metrics | ✅ | mia |
| Database module | ✅ | mia-db |
| Auth module | ✅ (базовый) | mia-auth |
| Sessions module | 🧱 scaffold | mia-sessions |
| LLM module | 🧱 scaffold | mia-llm |
| REST API | ❌ | — |
| React UI | ❌ | — |
| Memory (selti) integration | ❌ | selti |
| Workspace / multi-tenant | ❌ | — |

## 3. Целевая архитектура

```
React UI
    │
    ▼
REST API module (mia-rest / gateway)
    │
    ├── sessions  (mia-sessions)
    ├── llm       (mia-llm)
    ├── auth      (mia-auth)
    ├── db        (mia-db)
    └── memory    (selti MCP / client)
         │
         ▼
    Mia Application (core)
         │
    Task System → ThreadPool / Workers
```

Иерархия данных:

```
Workspace (Project)
└── Session
    ├── Participants (user + agents)
    ├── Messages
    └── Runtime state
```

- **Agent definitions** — в `mia-llm`
- **Участники и история** — в `mia-sessions`
- **Вызов модели** — `mia-llm`
- **Память** — selti (отдельный сервис)

## 4. Фазы развития

### Фаза 0 — Стабилизация ядра (сейчас)

- Довести Task System до production-ready (тесты, метрики, GC истории).
- Синхронизировать README с реальным API (`Application` вместо старого `State`).
- Единый стиль модулей (как mia-db / mia-sessions / mia-llm).
- Версионирование и pyproject во всех модулях.

**Критерий готовности:** `pytest` зелёный, документация соответствует коду, модули ставятся через `pip install -e`.

### Фаза 1 — Sessions + LLM (1–2 недели)

1. **mia-sessions**
   - In-memory → Postgres backend (через mia-db).
   - Compaction hooks (порог сообщений / токенов).
   - Привязка к workspace_id.
   - Экспорт/импорт истории.

2. **mia-llm**
   - Стабильный OpenAI-compatible клиент (httpx + retries).
   - Streaming (SSE / async generator).
   - Agent definitions CRUD + персистентность.
   - Опциональная интеграция с sessions (`chat_as_agent` + session_id).

3. Связка:
   - Сообщение пользователя → sessions.add_message
   - Вызов агента → llm.chat_as_agent
   - Ответ → sessions.add_message

**Критерий:** multi-agent сессия в одном workspace работает end-to-end из Python.

### Фаза 2 — REST API (1–2 недели)

Модуль `mia-rest` (или `gateway`):

- FastAPI / Starlette.
- Auth через mia-auth (JWT).
- Эндпоинты:
  - `/workspaces`, `/sessions`, `/messages`
  - `/agents`, `/chat`, `/chat/stream`
  - `/memory/*` (прокси в selti)
- OpenAPI, CORS, rate limit (базовый).
- Интеграция с Task System (долгие операции — async tasks + polling/websocket).

**Критерий:** React (или curl) может создать сессию, отправить сообщение и получить стрим ответа.

### Фаза 3 — React UI (2–3 недели)

- Минимальный, но удобный интерфейс:
  - список workspaces / sessions
  - чат (user + несколько агентов)
  - переключение агентов, статус задач
  - просмотр памяти (поиск по selti)
- Стек на выбор: React + Vite + TanStack Query (или аналог).
- Только API-клиент, без бизнес-логики в фронте.

**Критерий:** полноценный день работы через UI без Python-консоли.

### Фаза 4 — Память и умные агенты

- Клиент selti внутри Mia (MCP или HTTP).
- Грануляция диалогов → память (батчи из sessions).
- Retrieval в system prompt / tool `memory_search`.
- Иерархическая память (Level 0–5) по плану selti.
- Фоновые tasks: clustering, confidence decay, schema generation.

**Критерий:** агент помнит факты между сессиями и корректно обновляет противоречивые знания.

### Фаза 5 — Production hardening

- Workspaces / multi-user изоляция.
- Quota и лимиты (токены, задачи).
- Observability: трейсы, дашборды Grafana.
- Backup / restore сессий и памяти.
- Документация для разработчиков модулей.
- CI/CD для всех репозиториев.

## 5. Приоритет модулей (очередь)

| # | Модуль | Зависимости | Приоритет |
|---|--------|-------------|-----------|
| 1 | Стабилизация core + Task System | — | P0 |
| 2 | mia-sessions (Postgres) | mia-db | P0 |
| 3 | mia-llm (streaming + persist agents) | — | P0 |
| 4 | mia-rest | auth, sessions, llm | P1 |
| 5 | React UI | mia-rest | P1 |
| 6 | selti client + memory tools | sessions, llm | P1 |
| 7 | Workspace / multi-tenant | rest, auth | P2 |
| 8 | Advanced multi-agent orchestration | sessions, llm | P2 |

## 6. Принципы

1. **Тонкие модули** — каждый репозиторий делает одно дело.
2. **Task System везде** — IO/CPU/network/DB идут через `@task`.
3. **DI через ServiceRegistry** — модули регистрируют провайдеры, не ходят друг к другу напрямую без необходимости.
4. **Обратная совместимость** — старые `@api_method` / `_db_type` поддерживаются, пока есть пользователи.
5. **Сначала работает, потом красиво** — scaffold → working → polished.
6. **Память отдельно** — selti остаётся отдельным сервисом, Mia с ним интегрируется, а не поглощает.

## 7. Риски

| Риск | Митигация |
|------|----------|
| Расползание scope | Жёсткие фазы и критерии готовности |
| Дублирование State vs Application | Один entrypoint — Application, README обновить |
| Сложность Task System | Хорошие тесты + метрики, не добавлять уровни без нужды |
| UI отстаёт от API | Сначала REST, UI — тонкий клиент |
| Интеграция selti | Чёткий контракт (MCP tools), не tight coupling |

## 8. Ближайшие шаги (следующие 7–14 дней)

1. Обновить README mia под Application + Task System.
2. Довести mia-sessions: Postgres store + базовые тесты.
3. Довести mia-llm: нормальный streaming + сохранение agent definitions.
4. Набросать mia-rest (скелет FastAPI + auth middleware).
5. Связать sessions ↔ llm в одном e2e-тесте.

---

Документ можно уточнять по мере реализации. Все крупные изменения — через PR и ревью.
