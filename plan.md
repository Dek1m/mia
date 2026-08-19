# План: belle кладёт в Redis, shaltir исполняет

## Решение

belle — один процесс, тонкая обёртка: `Application()`. Свой воркер не поднимает.

`@task` уходит в Redis (очередь `mia`, задача `mia.run`). Исполняет shaltir worker:

```
SHALTIR_INCLUDE=core.dispatch.tasks SHALTIR_CELERY_QUEUE=mia python -m shaltir worker
```

- Клиент shaltir живёт в mia (`ShaltirDispatcher`). belle shaltir не импортирует.
- `Application()` без `dispatcher=` шлёт в shaltir.
- `Application(dispatcher=LocalInvokeDispatcher())` / `MIA_DISPATCH=local` — in-process.
- Отдельного `mia-worker` нет.
