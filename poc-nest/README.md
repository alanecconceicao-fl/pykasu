# PYKASU PoC 2

NestJS and TypeScript implementation of the feed reader described in
[POC-PLAN.md](POC-PLAN.md). It uses Fastify, Node's built-in `node:sqlite` API
with explicit SQL, one `worker_threads` refresh worker, and plain browser
JavaScript.

## Requirements and local run

Use Node.js 22.16 or newer. Install the pinned dependencies and compile:

```sh
npm ci
npm run start:dev
```

Open <http://127.0.0.1:8001>. Production mode is `npm run build` followed by
`npm run start:prod`. The listener defaults to `127.0.0.1:8001`.

Configuration is read when the process starts:

| Variable | Default | Meaning |
| --- | --- | --- |
| `PYKASU_BIND` | `127.0.0.1` | Listening address; keep loopback when Caddy fronts the app |
| `PYKASU_PORT` | `8001` | TCP port |
| `PYKASU_DATA_DIR` | `./data` | Private persistent SQLite data directory |
| `PYKASU_GROQ_API_KEY` | unset | Groq Free API key; `GROQ_API_KEY` is a fallback |
| `PYKASU_GROQ_ZERO_DATA_RETENTION` | unset | Must be enabled only after enabling account-level Groq Zero Data Retention |

Restart after changing configuration. Keep credentials out of source control,
shell history, command arguments, and readable files. Without the key and the
account-level retention setting, feed ingestion and reading continue while AI
summaries remain unavailable.

## Storage and summaries

SQLite stores sources, item metadata, source associations, summary text,
minimal supporting quotations, visit sequence, refresh history, and model
usage. Full feed responses, article HTML, extracted article bodies, and model
prompts stay in memory. Network response bodies are size-limited; public feed,
article, and model requests validate DNS destinations and redirects. Article
requests follow the site's robots policy.

Summary calls use only Groq Free and `openai/gpt-oss-120b`, with strict JSON
output and evidence quotes checked against normalized source paragraphs. There
is no paid-provider fallback. Processing limits and cooldowns are persisted.

## Backup and restore

Back up the live database with SQLite's online backup API:

```sh
npm run build
npm run backup -- backup /var/backups/pykasu/pykasu-2026-10-24.sqlite3
```

Restore only while the service is stopped. The command validates the backup and
preserves the current database as a `.pre-restore-<timestamp>` file before
replacing it:

```sh
sudo systemctl stop pykasu-node
npm run restore -- /var/backups/pykasu/pykasu-2026-10-24.sqlite3
sudo systemctl start pykasu-node
sudo systemctl status pykasu-node
```

Run these commands as the service account or an administrator with access to
the data directory. Move completed backups off the VM and protect them as
application data.

## VM deployment: systemd and Caddy

The included `deploy/pykasu.service`, `deploy/pykasu.env.example`, and
`deploy/Caddyfile` assume the checkout is installed at `/opt/pykasu/poc-nest`,
the built output is under `dist/src/`, and persistent data is at
`/var/lib/pykasu-node`. Replace `news.example.com` in the Caddy configuration
with the supplied hostname. DNS, firewall rules, and Caddy installation are
hosting concerns outside this repository.

Install dependencies and build as an administrator, create a restricted
service account and private data directory, configure the environment file,
then install and start the systemd unit. Caddy should be the only public
HTTP(S) listener; the NestJS process stays bound to `127.0.0.1`.

## Routes

- `GET /updates` and `GET /sources` serve the two application pages and start or
  join the current refresh.
- `GET /api/state` returns saved items, sources, and refresh status; it never
  starts a refresh.
- `GET /api/refresh-status` polls progress.
- `POST /api/refresh` starts or joins a refresh.
- `POST /api/sources` and `PATCH /api/sources/:id` manage source settings.
- `GET /health` reports process health.

Run automated checks with `npm test` and a production compile with
`npm run build`.
