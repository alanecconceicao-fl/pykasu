# PYKASU PoC

PYKASU is a small, public, server-hosted feed reader. It shows configured RSS
or Atom updates in one chronological view and can enrich individual items with
short Groq summaries. It is intentionally a single Python 3.12 process with a
SQLite database—there is no container, queue, ORM, or frontend build step.

The public site has no login. All visitors therefore share source configuration
and the browser visit state. Keep the model credential and processing limits on
the server.

## Run locally

Use Python 3.12, then install the exact runtime dependencies from this
directory:

```sh
cd poc-py
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

export PYKASU_BIND=127.0.0.1
export PYKASU_PORT=8000
export PYKASU_DATA_DIR="$PWD/data"
# Optional: updates still work without this; only AI summaries are unavailable.
read -rsp 'Groq API key (optional): ' PYKASU_GROQ_API_KEY; echo
export PYKASU_GROQ_API_KEY
# Required before summaries are sent; enable this in Groq Data Controls first.
export PYKASU_GROQ_ZERO_DATA_RETENTION=enabled

python -m pykasu
```

Open <http://127.0.0.1:8000>. `python -m pykasu` uses Waitress; do not use
Flask's development server for a deployed instance.

## Configuration

All configuration is read at process start. Restart the application after
changing an environment variable.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PYKASU_BIND` | `127.0.0.1` | Address on which Waitress listens. Keep the loopback default when Caddy fronts the app. |
| `PYKASU_PORT` | `8000` | TCP port for Waitress. |
| `PYKASU_DATA_DIR` | `./data` | Directory holding `pykasu.sqlite3` and SQLite's transient journal files. It must persist across restarts. |
| `PYKASU_GROQ_API_KEY` | unset | Groq Free API key for `openai/gpt-oss-120b` summaries. Missing credentials leave feed ingestion and reading available. |
| `PYKASU_GROQ_ZERO_DATA_RETENTION` | unset | Set to `enabled` only after enabling Groq Zero Data Retention in its account-level Data Controls. PYKASU refuses model calls until both this and the API key are present. |
| `GROQ_API_KEY` | unset | Backward-compatible fallback when `PYKASU_GROQ_API_KEY` is unset. |

`PYKASU_GROQ_API_KEY` takes precedence over `GROQ_API_KEY`. Do not put either
credential in source control, a shell history, a process command line, or a
world-readable file. The application stores configuration, item metadata,
summaries, visit state, and usage limits in SQLite; it must not store full
article bodies, prompts, or API credentials.

Create the key in a dedicated Groq Free-tier account and enable **Zero Data
Retention** for that account in Groq's Data Controls before using summaries;
then set `PYKASU_GROQ_ZERO_DATA_RETENTION=enabled`. The control is account-level
rather than an API request header. PYKASU uses only the `openai/gpt-oss-120b`
Groq adapter and has no paid-provider fallback.

## VM deployment: systemd and Caddy

The included files assume the application is installed at `/opt/pykasu`, the
virtual environment is `/opt/pykasu/.venv`, and persistent data belongs in
`/var/lib/pykasu`. Replace the example hostname in
[`deploy/Caddyfile`](deploy/Caddyfile) before deploying. DNS, firewall rules,
and Caddy installation are hosting concerns outside this repository; the host
must accept ports 80 and 443 for Caddy's automatic HTTPS.

On a Debian/Ubuntu-style VM, first create the restricted service account. If
the account already exists, omit this command:

```sh
sudo useradd --system --home /var/lib/pykasu --shell /usr/sbin/nologin pykasu
```

Place this checkout at `/opt/pykasu` (so that the application package is
`/opt/pykasu/poc-py/pykasu`) and ensure the `pykasu` user can read it. Build the
read-only runtime environment as an administrator before starting the service:

```sh
cd /opt/pykasu
sudo python3.12 -m venv .venv
sudo .venv/bin/python -m pip install --upgrade pip
sudo .venv/bin/python -m pip install -r poc-py/requirements.txt
```

Create the private data and configuration directories:

```sh
sudo install -d -o pykasu -g pykasu -m 700 /var/lib/pykasu /etc/pykasu
sudo install -m 600 -o root -g pykasu poc-py/deploy/pykasu.env.example /etc/pykasu/pykasu.env
sudoedit /etc/pykasu/pykasu.env
```

Set the actual `PYKASU_GROQ_API_KEY` in `/etc/pykasu/pykasu.env`; keep the
bind address as `127.0.0.1`. Do not use `0.0.0.0` when Caddy is on the same VM.

Install and start the application service:

```sh
sudo install -m 644 deploy/pykasu.service /etc/systemd/system/pykasu.service
sudo systemctl daemon-reload
sudo systemctl enable --now pykasu
sudo systemctl status pykasu
```

Copy `deploy/Caddyfile` into the active Caddy configuration after replacing
`news.example.com` with the supplied hostname. Validate before reloading:

```sh
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

If the server has other Caddy sites, merge the site block into its existing
`/etc/caddy/Caddyfile` instead of overwriting that file. A successful
`https://your-hostname/` request should reach PYKASU; Caddy is the only process
that should accept public HTTP(S) connections.

Use `journalctl -u pykasu` for operational errors. Logs must not contain API
keys, full article bodies, or model prompts.

## Backup and restore

Use the supplied backup command rather than copying a live SQLite file: SQLite
may have current data in a write-ahead log. A backup includes sources, items,
visit state, refresh history, and persisted usage limits.

The application service may stay online while creating a backup:

```sh
sudo install -d -o pykasu -g pykasu -m 700 /var/backups/pykasu
cd /opt/pykasu/poc-py
sudo -u pykasu env PYKASU_DATA_DIR=/var/lib/pykasu \
  /opt/pykasu/.venv/bin/python -m pykasu.backup backup \
  /var/backups/pykasu/pykasu-2026-09-05.sqlite3
```

Move completed backups off the VM and protect them as application data. Before
relying on a backup, test it in an isolated data directory using the restore
procedure below.

Restoring replaces the active database, so stop the writer first. Preserve the
current database separately if you may need to return to it:

```sh
sudo systemctl stop pykasu
cd /opt/pykasu/poc-py
sudo -u pykasu env PYKASU_DATA_DIR=/var/lib/pykasu \
  /opt/pykasu/.venv/bin/python -m pykasu.backup restore \
  /var/backups/pykasu/pykasu-2026-09-05.sqlite3
sudo systemctl start pykasu
sudo systemctl status pykasu
```

The restore command validates the backup before replacing the database. If it
reports an error, leave the service stopped, retain the existing files, and
investigate the backup rather than manually copying SQLite database and journal
files into place.

## Updating

Stop the service before changing application files or dependencies, retain a
fresh backup, install the pinned requirements into the same virtual environment,
then start it again:

```sh
sudo systemctl stop pykasu
cd /opt/pykasu/poc-py
sudo /opt/pykasu/.venv/bin/python -m pip install -r requirements.txt
sudo systemctl start pykasu
```

Check the service status and the Updates and Sources pages after every upgrade.

## Vercel services

The repository-root `vercel.json` exposes this Flask PoC under `/poc-py` and
the NestJS PoC under `/poc-nest`. Select **Services** as the project framework
in Vercel. This PoC uses temporary SQLite data at `/tmp/pykasu-py` when running
on Vercel; data is not durable or shared across function instances. Its
refresh worker is also best-effort in a serverless runtime and is not guaranteed
to finish after a response.
