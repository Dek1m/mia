# План: belle кладёт в Redis, mia-worker исполняет

## Решение

belle — один процесс, тонкая обёртка: `Application()`. Свой воркер не поднимает.

`@task` уходит в Redis (очередь `mia`, задача `mia.run`). Исполняет mia-worker:

```
python -m modules.worker
```

- Клиент очереди живёт в mia (`QueueDispatcher`). belle брокер не импортирует.
- `Application()` без `dispatcher=` шлёт в Redis-очередь.
- `Application(dispatcher=LocalInvokeDispatcher())` / `MIA_DISPATCH=local` — in-process.
