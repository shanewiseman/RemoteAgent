# RemoteAgent Cron

`remoteagent-cron` is the internal, durable scheduler for RemoteAgent. It owns
its PostgreSQL tables and calls agents exclusively through the router's MCP
transport. Its HTTP API is private and bearer-authenticated.

Run migrations before starting the service:

```sh
alembic -c alembic.ini upgrade head
remoteagent-cron
```

The liveness endpoints are `/health` and `/healthz`. Readiness is available at
`/readyz` and requires the internal bearer token.

