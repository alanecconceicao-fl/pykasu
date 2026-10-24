import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { EventEmitter } from 'node:events';
import { DatabaseService } from '../src/database.service';
import { FeedService } from '../src/feed.service';
import { RefreshService } from '../src/refresh.service';
import { nowIso } from '../src/util';

const response = (xml: string) => ({ status: 200, headers: new Headers(), body: Buffer.from(xml), url: 'https://example.org/feed' });
const entry = (title: string, url: string) => `<item><title>${title}</title><link>${url}</link><pubDate>${new Date().toUTCString()}</pubDate><description>A source excerpt containing enough details for the feed reader to keep this item visible.</description></item>`;

describe('refresh coordination and feed visibility', () => {
  let dir: string;
  let db: DatabaseService;
  const waitUntil = async (predicate: () => boolean) => {
    const deadline = Date.now() + 3000;
    while (!predicate() && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 5));
    if (!predicate()) throw new Error('Timed out waiting for refresh worker');
  };

  beforeEach(() => { dir = mkdtempSync(path.join(os.tmpdir(), 'pykasu-refresh-')); });
  afterEach(() => { db?.onModuleDestroy(); rmSync(dir, { recursive: true, force: true }); });

  it('shares one active refresh, deduplicates URLs, and continues after a source failure', async () => {
    const settings = { dataDir: dir, databasePath: path.join(dir, 'pykasu.sqlite3'), maxEnabledSources: 10, entriesPerFeed: 20, summariesPerRefresh: 10, summariesPerDay: 30, modelMinIntervalSeconds: 65, groqApiKey: undefined, groqZeroDataRetention: false } as any;
    db = new DatabaseService(settings);
    const ids = [
      db.createSource(db.db, { name: 'Alpha', feedUrl: 'https://alpha.example/feed', enabled: true, extractArticles: false }),
      db.createSource(db.db, { name: 'Beta', feedUrl: 'https://beta.example/feed', enabled: true, extractArticles: false }),
      db.createSource(db.db, { name: 'Down', feedUrl: 'https://down.example/feed', enabled: true, extractArticles: false })
    ];
    let release!: (value: ReturnType<typeof response>) => void;
    const firstFeed = new Promise<ReturnType<typeof response>>(resolve => { release = resolve; });
    const http = { fetchText: jest.fn(async (url: string) => {
      if (url.includes('alpha')) return firstFeed;
      if (url.includes('beta')) return response(`<rss><channel>${entry('Shared', 'https://news.example/story#copy')}${entry('Distinct', 'https://news.example/other')}</channel></rss>`);
      throw new Error('sensitive network detail');
    }) };
    let service!: RefreshService;
    const workerFactory = (_filename: string, options: any) => {
      const worker: any = new EventEmitter();
      worker.terminate = async () => 0;
      setImmediate(async () => {
        await service.executeRefresh(options.workerData.runId, options.workerData.sources);
        worker.emit('exit', 0);
      });
      return worker;
    };
    service = new RefreshService(db, new FeedService(), http as any, {} as any, {} as any, workerFactory as any);
    const firstRunId = service.startOrJoin();
    expect(service.status().active).toBe(true);
    expect(service.startOrJoin()).toBe(firstRunId);
    expect(db.db.prepare('SELECT count(*) n FROM items').get()).toEqual({ n: 0 });
    release(response(`<rss><channel>${entry('Shared', 'https://news.example/story')}</channel></rss>`));
    await waitUntil(() => !service.status().active);
    const rows = db.db.prepare('SELECT url,headline FROM items ORDER BY id').all() as Array<{ url: string; headline: string }>;
    expect(rows.map(row => row.url)).toEqual(['https://news.example/story', 'https://news.example/other']);
    expect((db.db.prepare('SELECT count(*) n FROM item_sources').get() as { n: number }).n).toBe(3);
    const savedDown = db.getSource(db.db, ids[2]);
    expect(savedDown.last_error).toBe('Feed could not be refreshed safely');
    expect(savedDown.last_success_at).toBeNull();
    expect(service.status().errors).toEqual([{ source: 'Down', message: 'Feed could not be refreshed safely' }]);
  });

  it('commits the headline before waiting for summary generation', async () => {
    const settings = { dataDir: dir, databasePath: path.join(dir, 'pykasu.sqlite3'), maxEnabledSources: 10, entriesPerFeed: 20, summariesPerRefresh: 10, summariesPerDay: 30, modelMinIntervalSeconds: 65, groqApiKey: 'test', groqZeroDataRetention: true } as any;
    db = new DatabaseService(settings);
    db.createSource(db.db, { name: 'Alpha', feedUrl: 'https://alpha.example/feed', enabled: true, extractArticles: false });
    const http = { fetchText: jest.fn(async () => response(`<rss><channel>${entry('A new report', 'https://news.example/new')}</channel></rss>`)) };
    let finishSummary!: (result: any) => void;
    const summaryDone = new Promise(resolve => { finishSummary = resolve; });
    const summaries = { lastTokenUsage: 9, summaryForItem: jest.fn(async () => summaryDone) };
    const articles = { paragraphs: jest.fn(async () => ({ paragraphs: [{ paragraph_id: 1, text: 'The source reports a decision and explains what the new plan will change.' }], basedOnExcerpt: true })) };
    let service!: RefreshService;
    const workerFactory = (_filename: string, options: any) => {
      const worker: any = new EventEmitter();
      worker.terminate = async () => 0;
      setImmediate(async () => {
        await service.executeRefresh(options.workerData.runId, options.workerData.sources);
        worker.emit('exit', 0);
      });
      return worker;
    };
    service = new RefreshService(db, new FeedService(), http as any, articles as any, summaries as any, workerFactory as any);
    service.startOrJoin();
    await waitUntil(() => summaries.summaryForItem.mock.calls.length === 1);
    const visible = db.db.prepare('SELECT headline,summary_status,summary FROM items').get() as any;
    expect(visible).toEqual({ headline: 'A new report', summary_status: 'pending', summary: null });
    finishSummary({ summary: 'The source reports a decision and its expected effect.', insufficient_information: false, evidence: [{ paragraph_id: 1, quote: 'a decision and explains what the new plan will change' }] });
    await waitUntil(() => !service.status().active);
    const saved = db.db.prepare('SELECT summary,summary_status,supporting_excerpts FROM items').get() as any;
    expect(saved.summary_status).toBe('feed-excerpt');
    expect(saved.summary).toContain('reports a decision');
    expect(JSON.parse(saved.supporting_excerpts)).toHaveLength(1);
    expect(db.db.prepare('SELECT token_usage FROM model_attempts').get()).toEqual({ token_usage: 9 });
  });

  it('does not acknowledge item sequences from the Sources page', () => {
    const settings = { dataDir: dir, databasePath: path.join(dir, 'pykasu.sqlite3'), maxEnabledSources: 10 } as any;
    db = new DatabaseService(settings);
    const source = db.createSource(db.db, { name: 'Alpha', feedUrl: 'https://alpha.example/feed', enabled: true, extractArticles: false });
    db.insertOrUpdateItem(db.db, source, { url: 'https://news.example/new', title: 'Update', publishedAt: nowIso(), excerpt: '', fingerprint: 'v1' });
    db.queryItems(db.db, { window: '24h', acknowledge: false });
    const visit = db.queryItems(db.db, { window: '24h', acknowledge: false });
    expect(visit.items[0].is_new).toBe(true);
  });
});
