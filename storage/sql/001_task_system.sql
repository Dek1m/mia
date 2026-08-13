-- Universal Task System: SQL Schema
-- Шаг 2 из PLAN_TASK_SYSTEM.md

-- Таблица истории выполненных задач
CREATE TABLE IF NOT EXISTS task_history (
    id BIGSERIAL PRIMARY KEY,
    task_id UUID NOT NULL,
    module_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    fn_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    duration_ms REAL,
    error TEXT,
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Индексы для task_history
CREATE INDEX IF NOT EXISTS idx_task_history_module_type
    ON task_history(module_id, task_type);
CREATE INDEX IF NOT EXISTS idx_task_history_created
    ON task_history(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_history_status
    ON task_history(status);

-- Агрегированная статистика по модулям и типам задач
CREATE TABLE IF NOT EXISTS task_stats (
    id BIGSERIAL PRIMARY KEY,
    module_id TEXT NOT NULL,
    task_type TEXT NOT NULL,
    count INT DEFAULT 0,
    avg_duration_ms REAL,
    p95_duration_ms REAL,
    p99_duration_ms REAL,
    last_updated TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(module_id, task_type)
);

-- Правила классификатора задач
CREATE TABLE IF NOT EXISTS task_classifier_rules (
    id BIGSERIAL PRIMARY KEY,
    priority INT NOT NULL,
    condition_type TEXT NOT NULL,  -- module_name, function_pattern, explicit
    condition_value TEXT NOT NULL,
    target_type TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_classifier_enabled
    ON task_classifier_rules(enabled);
