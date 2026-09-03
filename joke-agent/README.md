# Joke Agent

The built-in joke agent is enabled in the default phonebook and provides a
small, visible end-to-end check of discovery, asynchronous jobs, and exact
Codex-session continuation. Every response is one clean topical joke beginning
with `JOKE: `.

Build and validate it with:

```sh
scripts/remotectl agent validate joke-agent
scripts/remotectl agent build joke-agent
```

After the router is running and Codex authentication is configured, run the
manual live smoke test:

```sh
scripts/remotectl smoke live --agent joke-agent
```

Live smoke is deliberately excluded from CI because it consumes authenticated
Codex subscription capacity. CI uses a deterministic fake runner instead.

