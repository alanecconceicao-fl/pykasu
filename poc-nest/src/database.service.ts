import { Inject, Injectable, OnModuleDestroy } from '@nestjs/common';
import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { SETTINGS, Settings } from './config';
import { nowIso } from './util';

@Injectable()
export class DatabaseService implements OnModuleDestroy {
  readonly settings: Settings;
  readonly db: DatabaseSync;
  private workerDb?: DatabaseSync;

  constructor(@Inject(SETTINGS) settings: Settings, recoverStaleRuns = true) {
    this.settings = settings;
    mkdirSync(settings.dataDir, { recursive: true, mode: 0o700 });
    this.db = this.open(settings.databasePath);
    this.migrate(this.db, recoverStaleRuns);
  }

  private open(filename: string): DatabaseSync {
    const db = new DatabaseSync(filename, { timeout: 5000, enableForeignKeyConstraints: true });
    db.exec('PRAGMA journal_mode = WAL');
    return db;
  }

  workerConnection(): DatabaseSync {
    if (!this.workerDb) this.workerDb = this.open(this.settings.databasePath);
    return this.workerDb;
  }

  private migrate(db: DatabaseSync, recoverStaleRuns: boolean): void {
    db.exec(`
      CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
      INSERT INTO schema_version(version) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);
      CREATE TABLE IF NOT EXISTS sources (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 120),
        feed_url TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
        extract_articles INTEGER NOT NULL DEFAULT 1 CHECK(extract_articles IN (0,1)),
        etag TEXT,
        last_modified TEXT,
        last_checked_at TEXT,
        last_success_at TEXT,
        last_error TEXT
      );
      CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY,
        url TEXT NOT NULL UNIQUE,
        headline TEXT NOT NULL,
        published_at TEXT,
        discovered_at TEXT NOT NULL,
        discovery_sequence INTEGER NOT NULL UNIQUE,
        metadata_fingerprint TEXT NOT NULL,
        feed_excerpt TEXT NOT NULL DEFAULT '',
        summary TEXT,
        summary_status TEXT NOT NULL DEFAULT 'pending',
        summary_input_fingerprint TEXT,
        supporting_excerpts TEXT NOT NULL DEFAULT '[]'
      );
      CREATE TABLE IF NOT EXISTS item_sources (
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        PRIMARY KEY(item_id, source_id)
      );
      CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS refresh_runs (
        id INTEGER PRIMARY KEY,
        started_at TEXT NOT NULL,
        completed_at TEXT,
        status TEXT NOT NULL,
        sources_total INTEGER NOT NULL DEFAULT 0,
        sources_done INTEGER NOT NULL DEFAULT 0,
        headlines_saved INTEGER NOT NULL DEFAULT 0,
        summaries_attempted INTEGER NOT NULL DEFAULT 0,
        errors_json TEXT NOT NULL DEFAULT '[]'
      );
      CREATE TABLE IF NOT EXISTS model_attempts (
        id INTEGER PRIMARY KEY,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        input_fingerprint TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        outcome TEXT NOT NULL,
        failure_reason TEXT,
        token_usage INTEGER,
        retry_after_at TEXT
      );
      CREATE INDEX IF NOT EXISTS items_pub_idx ON items(published_at DESC, id DESC);
      CREATE INDEX IF NOT EXISTS model_attempt_day_idx ON model_attempts(attempted_at);
      CREATE INDEX IF NOT EXISTS model_attempt_item_idx ON model_attempts(item_id, input_fingerprint);
    `);
    if (recoverStaleRuns) {
      db.prepare("UPDATE refresh_runs SET status='failed',completed_at=?,errors_json=? WHERE status='running'")
        .run(nowIso(), JSON.stringify([{ source: 'refresh', message: 'Previous process stopped during refresh' }]));
    }
  }

  onModuleDestroy(): void {
    this.workerDb?.close();
    this.db.close();
  }

  createSource(db: DatabaseSync, input: { name: string; feedUrl: string; enabled: boolean; extractArticles: boolean }): number {
    const enabledCount = Number((db.prepare('SELECT count(*) n FROM sources WHERE enabled=1').get() as { n: number }).n);
    if (input.enabled && enabledCount >= this.settings.maxEnabledSources) throw new Error(`At most ${this.settings.maxEnabledSources} sources can be enabled`);
    const result = db.prepare('INSERT INTO sources(name,feed_url,enabled,extract_articles) VALUES(?,?,?,?)')
      .run(input.name, input.feedUrl, input.enabled ? 1 : 0, input.extractArticles ? 1 : 0);
    return Number(result.lastInsertRowid);
  }

  updateSource(db: DatabaseSync, id: number, input: { name: string; feedUrl: string; enabled: boolean; extractArticles: boolean }): void {
    const current = db.prepare('SELECT enabled FROM sources WHERE id=?').get(id) as { enabled: number } | undefined;
    if (!current) throw new Error('Source not found');
    if (input.enabled && !current.enabled) {
      const count = Number((db.prepare('SELECT count(*) n FROM sources WHERE enabled=1').get() as { n: number }).n);
      if (count >= this.settings.maxEnabledSources) throw new Error(`At most ${this.settings.maxEnabledSources} sources can be enabled`);
    }
    db.prepare('UPDATE sources SET name=?,feed_url=?,enabled=?,extract_articles=? WHERE id=?')
      .run(input.name, input.feedUrl, input.enabled ? 1 : 0, input.extractArticles ? 1 : 0, id);
  }

  listSources(db = this.db, enabledOnly = false): Record<string, unknown>[] {
    return db.prepare(`SELECT id,name,feed_url,enabled,extract_articles,last_checked_at,last_success_at,last_error FROM sources ${enabledOnly ? 'WHERE enabled=1' : ''} ORDER BY name COLLATE NOCASE`)
      .all() as Record<string, unknown>[];
  }

  getSource(db: DatabaseSync, id: number): any {
    return db.prepare('SELECT * FROM sources WHERE id=?').get(id);
  }

  insertOrUpdateItem(db: DatabaseSync, sourceId: number, entry: { url: string; title: string; publishedAt: string | null; excerpt: string; fingerprint: string }): { inserted: boolean; id: number } {
    const old = db.prepare('SELECT id,headline,published_at,metadata_fingerprint FROM items WHERE url=?').get(entry.url) as any;
    let id: number;
    let inserted = false;
    if (old) {
      id = Number(old.id);
      if (old.metadata_fingerprint !== entry.fingerprint) {
        db.prepare(`UPDATE items SET headline=?,published_at=?,metadata_fingerprint=?,feed_excerpt=?,summary=NULL,
          summary_status='pending',summary_input_fingerprint=NULL,supporting_excerpts='[]' WHERE id=?`)
          .run(entry.title, entry.publishedAt, entry.fingerprint, entry.excerpt, id);
      }
    } else {
      const next = Number((db.prepare('SELECT COALESCE(MAX(discovery_sequence),0)+1 n FROM items').get() as { n: number }).n);
      const result = db.prepare(`INSERT INTO items(url,headline,published_at,discovered_at,discovery_sequence,metadata_fingerprint,feed_excerpt)
        VALUES(?,?,?,?,?,?,?)`).run(entry.url, entry.title, entry.publishedAt, nowIso(), next, entry.fingerprint, entry.excerpt);
      id = Number(result.lastInsertRowid);
      inserted = true;
    }
    db.prepare('INSERT OR IGNORE INTO item_sources(item_id,source_id) VALUES(?,?)').run(id, sourceId);
    return { inserted, id };
  }

  queryItems(db: DatabaseSync, options: { window: '24h' | '7d'; sourceId?: number; search?: string; acknowledge: boolean; newSince?: number }): { items: any[]; acknowledgedSequence: number; deliveredSequence: number } {
    const acknowledgedSequence = Number((db.prepare("SELECT COALESCE((SELECT value FROM settings WHERE key='acknowledged_sequence'),'0') value").get() as { value: string }).value);
    const cutoff = new Date(Date.now() - (options.window === '7d' ? 7 : 1) * 86400_000).toISOString();
    const search = options.search ? `%${options.search.replace(/[\\%_]/g, '\\$&')}%` : null;
    const items = db.prepare(`SELECT i.id,i.url,i.headline,i.published_at,i.discovered_at,i.discovery_sequence,
        i.summary,i.summary_status,GROUP_CONCAT(DISTINCT s.name) source_names
      FROM items i JOIN item_sources a ON a.item_id=i.id JOIN sources s ON s.id=a.source_id
      WHERE s.enabled=1 AND (i.published_at >= ? OR i.published_at IS NULL)
        AND (? IS NULL OR s.id=?) AND (? IS NULL OR i.headline LIKE ? ESCAPE '\\' OR i.summary LIKE ? ESCAPE '\\')
      GROUP BY i.id ORDER BY COALESCE(i.published_at,i.discovered_at) DESC,i.id DESC LIMIT 500`)
      .all(cutoff, options.sourceId ?? null, options.sourceId ?? null, search, search, search) as any[];
    const deliveredSequence = items.reduce((max, item) => Math.max(max, Number(item.discovery_sequence)), 0);
    if (options.acknowledge && deliveredSequence > acknowledgedSequence) {
      db.prepare("INSERT INTO settings(key,value) VALUES('acknowledged_sequence',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        .run(String(deliveredSequence));
    }
    const newSince = options.newSince ?? acknowledgedSequence;
    return { items: items.map(item => ({ ...item, is_new: Number(item.discovery_sequence) > newSince })), acknowledgedSequence, deliveredSequence };
  }

  startRefresh(db: DatabaseSync): number {
    const result = db.prepare("INSERT INTO refresh_runs(started_at,status) VALUES(?,'running')").run(nowIso());
    return Number(result.lastInsertRowid);
  }

  setRefreshTotals(db: DatabaseSync, runId: number, total: number): void {
    db.prepare('UPDATE refresh_runs SET sources_total=? WHERE id=?').run(total, runId);
  }

  advanceRefresh(db: DatabaseSync, runId: number, inserted: number, errors: Array<{ source: string; message: string }>): void {
    db.prepare('UPDATE refresh_runs SET sources_done=sources_done+1,headlines_saved=headlines_saved+?,errors_json=? WHERE id=?')
      .run(inserted, JSON.stringify(errors), runId);
  }

  finishRefresh(db: DatabaseSync, runId: number, errors: Array<{ source: string; message: string }>): void {
    db.prepare("UPDATE refresh_runs SET completed_at=?,status=?,errors_json=? WHERE id=?")
      .run(nowIso(), errors.length ? 'completed_with_errors' : 'completed', JSON.stringify(errors), runId);
  }

  refreshStatus(db = this.db): any {
    const row = db.prepare('SELECT * FROM refresh_runs ORDER BY id DESC LIMIT 1').get() as any;
    if (!row) return { active: false, run_id: null, started_at: null, completed_at: null, sources_total: 0, sources_done: 0, headlines_saved: 0, summaries_attempted: 0, errors: [] };
    return { active: row.status === 'running', run_id: row.id, started_at: row.started_at, completed_at: row.completed_at,
      sources_total: row.sources_total, sources_done: row.sources_done, headlines_saved: row.headlines_saved,
      summaries_attempted: row.summaries_attempted, errors: JSON.parse(row.errors_json || '[]') };
  }

  eligibleItems(db: DatabaseSync): any[] {
    return db.prepare(`SELECT i.*, GROUP_CONCAT(DISTINCT s.name) source_names, MAX(s.extract_articles) extract_articles
      FROM items i JOIN item_sources a ON a.item_id=i.id JOIN sources s ON s.id=a.source_id
      WHERE i.summary_status='pending' AND s.enabled=1
      GROUP BY i.id ORDER BY COALESCE(i.published_at,i.discovered_at) DESC,i.id DESC LIMIT 100`).all() as any[];
  }

  summaryAttemptsForVersion(db: DatabaseSync, itemId: number, version: string): number {
    return Number((db.prepare('SELECT count(*) n FROM model_attempts WHERE item_id=? AND input_fingerprint=?').get(itemId, version) as { n: number }).n);
  }

  modelBudget(db: DatabaseSync): { attemptsToday: number; nextAllowed: string | null; retryAfter: string | null } {
    const today = new Date().toISOString().slice(0, 10);
    const attemptsToday = Number((db.prepare("SELECT count(*) n FROM model_attempts WHERE substr(attempted_at,1,10)=?").get(today) as { n: number }).n);
    const nextAllowed = (db.prepare("SELECT value FROM settings WHERE key='model_next_allowed_at'").get() as any)?.value ?? null;
    const retryAfter = (db.prepare("SELECT value FROM settings WHERE key='model_retry_after_at'").get() as any)?.value ?? null;
    return { attemptsToday, nextAllowed, retryAfter };
  }

  reserveModelAttempt(db: DatabaseSync, runId: number, itemId: number, version: string): void {
    db.exec('BEGIN IMMEDIATE');
    try {
      const now = nowIso();
      db.prepare("INSERT INTO model_attempts(item_id,input_fingerprint,attempted_at,outcome) VALUES(?,?,?,'pending')").run(itemId, version, now);
      db.prepare("INSERT INTO settings(key,value) VALUES('model_next_allowed_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value")
        .run(new Date(Date.now() + this.settings.modelMinIntervalSeconds * 1000).toISOString());
      db.prepare('UPDATE refresh_runs SET summaries_attempted=summaries_attempted+1 WHERE id=?').run(runId);
      db.exec('COMMIT');
    } catch (error) { db.exec('ROLLBACK'); throw error; }
  }

  finishModelAttempt(db: DatabaseSync, itemId: number, version: string, outcome: string, failureReason?: string, retryAfter?: string, tokenUsage?: number | null): void {
    const latest = db.prepare('SELECT id FROM model_attempts WHERE item_id=? AND input_fingerprint=? ORDER BY id DESC LIMIT 1').get(itemId, version) as any;
    if (latest) db.prepare('UPDATE model_attempts SET outcome=?,failure_reason=?,retry_after_at=?,token_usage=? WHERE id=?').run(outcome, failureReason ?? null, retryAfter ?? null, tokenUsage ?? null, latest.id);
    if (retryAfter) db.prepare("INSERT INTO settings(key,value) VALUES('model_retry_after_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value").run(retryAfter);
  }

  setSummary(db: DatabaseSync, itemId: number, version: string, summary: string | null, status: string, excerpts: unknown[]): void {
    db.prepare(`UPDATE items SET summary=?,summary_status=?,summary_input_fingerprint=?,supporting_excerpts=? WHERE id=?`)
      .run(summary, status, version, JSON.stringify(excerpts), itemId);
  }

  sourceFetchSuccess(db: DatabaseSync, sourceId: number, etag: string | null, lastModified: string | null): void {
    db.prepare('UPDATE sources SET etag=?,last_modified=?,last_checked_at=?,last_success_at=?,last_error=NULL WHERE id=?')
      .run(etag, lastModified, nowIso(), nowIso(), sourceId);
  }

  sourceFetchFailure(db: DatabaseSync, sourceId: number, error: string): void {
    db.prepare('UPDATE sources SET last_checked_at=?,last_error=? WHERE id=?').run(nowIso(), error.slice(0, 500), sourceId);
  }
}
