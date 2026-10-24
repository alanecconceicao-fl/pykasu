import { Inject, Injectable, OnModuleDestroy, Optional } from '@nestjs/common';
import { Worker, WorkerOptions } from 'node:worker_threads';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { DatabaseSync } from 'node:sqlite';
import { DatabaseService } from './database.service';
import { FeedService } from './feed.service';
import { SafeHttpService, SafeFetchError } from './http.service';
import { ArticleService } from './article.service';
import { SummaryService, summaryInputFingerprint } from './summary.service';
import { InvalidSummaryError, ModelQuotaError, ModelRequestError } from './summary-errors';
import { RefreshStatus } from './models';
import { fingerprint, nowIso, normalizeText } from './util';
import { REFRESH_WORKER_FACTORY } from './config';

export type RefreshWorkerFactory = (filename: string, options: WorkerOptions) => Worker;

@Injectable()
export class RefreshService implements OnModuleDestroy {
  private activeWorker: Worker | null = null;
  private activeRunId: number | null = null;
  private refreshStartSequence = 0;

  constructor(
    private readonly database: DatabaseService,
    private readonly feeds: FeedService,
    private readonly http: SafeHttpService,
    private readonly articles: ArticleService,
    private readonly summaries: SummaryService,
    @Optional() @Inject(REFRESH_WORKER_FACTORY) private readonly workerFactory: RefreshWorkerFactory = (filename, options) => new Worker(filename, options)
  ) {}

  startOrJoin(): number | null {
    if (this.activeWorker) return this.activeRunId;
    const db = this.database.db;
    const sources = db.prepare('SELECT * FROM sources WHERE enabled=1 ORDER BY name').all() as any[];
    const runId = this.database.startRefresh(db);
    this.database.setRefreshTotals(db, runId, sources.length);
    this.refreshStartSequence = Number((db.prepare('SELECT COALESCE(MAX(discovery_sequence),0) n FROM items').get() as { n: number }).n);
    this.activeRunId = runId;
    const adjacentWorkerPath = path.join(__dirname, 'refresh.worker.js');
    const workerPath = existsSync(adjacentWorkerPath) ? adjacentWorkerPath : path.resolve(process.cwd(), 'dist', 'src', 'refresh.worker.js');
    let worker: Worker;
    try { worker = this.workerFactory(workerPath, { workerData: { runId, sources, settings: this.database.settings } }); }
    catch {
      this.failRunIfNeeded(runId);
      this.activeRunId = null;
      return runId;
    }
    this.activeWorker = worker;
    worker.once('error', () => this.failRunIfNeeded(runId));
    worker.once('exit', code => {
      if (code !== 0) this.failRunIfNeeded(runId);
      else this.failRunIfNeeded(runId);
      if (this.activeWorker === worker) this.activeWorker = null;
      this.activeRunId = null;
    });
    return runId;
  }

  async executeRefresh(runId: number, sources: any[]): Promise<void> {
    try { await this.run(runId, sources); }
    catch { this.failRunIfNeeded(runId); }
  }

  status(): RefreshStatus & { max_discovery_sequence: number } {
    const db = this.database.db;
    const status = this.database.refreshStatus(db) as RefreshStatus;
    return { ...status, active: Boolean(this.activeWorker), run_id: this.activeRunId ?? status.run_id,
      max_discovery_sequence: Number((db.prepare('SELECT COALESCE(MAX(discovery_sequence),0) n FROM items').get() as { n: number }).n),
      new_items_available: Number((db.prepare('SELECT COALESCE(MAX(discovery_sequence),0) n FROM items').get() as { n: number }).n) > this.refreshStartSequence };
  }

  async onModuleDestroy(): Promise<void> {
    if (this.activeWorker) await this.activeWorker.terminate();
  }

  private failRunIfNeeded(runId: number): void {
    const row = this.database.db.prepare('SELECT status FROM refresh_runs WHERE id=?').get(runId) as { status: string } | undefined;
    if (row?.status === 'running') this.database.db.prepare("UPDATE refresh_runs SET status='failed',completed_at=?,errors_json=? WHERE id=?")
      .run(nowIso(), JSON.stringify([{ source: 'refresh', message: 'Refresh worker stopped unexpectedly' }]), runId);
  }

  private async run(runId: number, sources: any[]): Promise<void> {
    const db = this.database.workerConnection();
    const errors: Array<{ source: string; message: string }> = [];
    for (const source of sources) {
      let inserted = 0;
      try {
        const headers: Record<string, string> = { accept: 'application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9' };
        if (source.etag) headers['if-none-match'] = source.etag;
        if (source.last_modified) headers['if-modified-since'] = source.last_modified;
        const response = await this.http.fetchText(source.feed_url, { headers, maxBytes: this.database.settings.responseSizeLimit });
        if (response.status === 304) {
          this.database.sourceFetchSuccess(db, source.id, response.headers.get('etag') ?? source.etag, response.headers.get('last-modified') ?? source.last_modified);
        } else if (response.status >= 200 && response.status < 300) {
          const entries = this.feeds.parse(response.body.toString('utf8'), this.database.settings.entriesPerFeed, source.feed_url);
          const current = this.database.getSource(db, Number(source.id));
          if (!current || !current.enabled || current.feed_url !== source.feed_url) throw new Error('Source configuration changed during refresh; result discarded');
          db.exec('BEGIN IMMEDIATE');
          try {
            for (const entry of entries) {
              const result = this.database.insertOrUpdateItem(db, Number(source.id), entry);
              if (result.inserted) inserted += 1;
            }
            db.exec('COMMIT');
          } catch (error) { db.exec('ROLLBACK'); throw error; }
          this.database.sourceFetchSuccess(db, Number(source.id), response.headers.get('etag'), response.headers.get('last-modified'));
        } else {
          throw new Error(`Feed request returned HTTP ${response.status}`);
        }
      } catch (error) {
        const message = safeMessage(error);
        this.database.sourceFetchFailure(db, Number(source.id), message);
        errors.push({ source: String(source.name), message });
      } finally {
        this.database.advanceRefresh(db, runId, inserted, errors);
      }
    }
    await this.summarizePending(db, runId, errors);
    this.database.finishRefresh(db, runId, errors);
  }

  private async summarizePending(db: DatabaseSync, runId: number, errors: Array<{ source: string; message: string }>): Promise<void> {
    if (!this.database.settings.groqApiKey || !this.database.settings.groqZeroDataRetention) return;
    const candidates = this.database.eligibleItems(db);
    let attemptedThisRun = 0;
    for (const item of candidates) {
      if (attemptedThisRun >= this.database.settings.summariesPerRefresh) break;
      const articleAllowed = Number(item.extract_articles) === 1;
      let extracted;
      try {
        extracted = articleAllowed
          ? await this.articles.paragraphs(String(item.url), String(item.headline), String(item.feed_excerpt || ''))
          : await this.excerptParagraphs(String(item.headline), String(item.feed_excerpt || ''));
      } catch { extracted = { paragraphs: [], basedOnExcerpt: false }; }
      if (!extracted.paragraphs.length) {
        this.database.setSummary(db, Number(item.id), String(item.metadata_fingerprint), null, 'unavailable', []);
        continue;
      }
      const metadata = { headline: String(item.headline), source_names: String(item.source_names || ''), published_at: String(item.published_at || '') };
      const version = summaryInputFingerprint(metadata, extracted.paragraphs);
      const priorCount = this.database.summaryAttemptsForVersion(db, Number(item.id), version);
      if (priorCount >= 2) {
        this.database.setSummary(db, Number(item.id), version, null, 'unavailable', []);
        continue;
      }
      const budget = this.database.modelBudget(db);
      if (budget.attemptsToday >= this.database.settings.summariesPerDay) break;
      const delayUntil = maxDate(budget.nextAllowed, budget.retryAfter);
      if (delayUntil && Date.parse(delayUntil) > Date.now()) {
        const waitMs = Date.parse(delayUntil) - Date.now();
        if (budget.retryAfter && Date.parse(budget.retryAfter) > Date.now()) break;
        await new Promise(resolve => setTimeout(resolve, waitMs));
      }
      this.database.reserveModelAttempt(db, runId, Number(item.id), version);
      attemptedThisRun += 1;
      try {
        const output = await this.summaries.summaryForItem(metadata, extracted.paragraphs);
        this.database.finishModelAttempt(db, Number(item.id), version, output.insufficient_information ? 'insufficient' : 'ready', undefined, undefined, this.summaries.lastTokenUsage);
        if (output.insufficient_information) {
          this.database.setSummary(db, Number(item.id), version, null, 'unavailable', []);
        } else {
          const supporting = output.evidence.map(reference => ({ paragraph_id: reference.paragraph_id, quote: reference.quote }));
          this.database.setSummary(db, Number(item.id), version, output.summary, extracted.basedOnExcerpt ? 'feed-excerpt' : 'ready', supporting);
        }
      } catch (error) {
        if (error instanceof ModelQuotaError) {
          this.database.finishModelAttempt(db, Number(item.id), version, 'quota_exhausted', 'Provider quota exhausted', error.retryAfterAt);
          this.database.setSummary(db, Number(item.id), version, null, 'pending', []);
          break;
        }
        if (error instanceof InvalidSummaryError) {
          this.database.finishModelAttempt(db, Number(item.id), version, 'invalid', 'Summary response did not pass validation');
          const failures = this.database.summaryAttemptsForVersion(db, Number(item.id), version);
          this.database.setSummary(db, Number(item.id), version, null, failures >= 2 ? 'unavailable' : 'pending', []);
          continue;
        }
        this.database.finishModelAttempt(db, Number(item.id), version, 'failed', 'Model request failed');
        this.database.setSummary(db, Number(item.id), version, null, 'pending', []);
        errors.push({ source: String(item.source_names || 'summary'), message: 'Summary service request failed' });
        break;
      }
    }
  }

  private async excerptParagraphs(headline: string, excerpt: string): Promise<{ paragraphs: any[]; basedOnExcerpt: boolean }> {
    const normalized = normalizeText(excerpt);
    if (normalized.length < 40 || normalized.toLowerCase() === normalizeText(headline).toLowerCase()) return { paragraphs: [], basedOnExcerpt: false };
    const { numberedParagraphs } = await import('./article.service');
    return { paragraphs: numberedParagraphs(normalized), basedOnExcerpt: true };
  }
}

function safeMessage(error: unknown): string {
  if (error instanceof SafeFetchError) return error.message.slice(0, 300);
  if (error instanceof Error && /^Feed request returned HTTP \d{3}$/.test(error.message)) return error.message;
  if (error instanceof Error && error.message.startsWith('Source configuration changed')) return error.message;
  return 'Feed could not be refreshed safely';
}

function maxDate(first: string | null, second: string | null): string | null {
  if (!first) return second;
  if (!second) return first;
  return Date.parse(first) > Date.parse(second) ? first : second;
}
