import { mkdtempSync, rmSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { DatabaseService } from '../src/database.service';
import { FeedService } from '../src/feed.service';
import { SafeHttpService } from '../src/http.service';
import { RefreshService } from '../src/refresh.service';
import { SummaryService } from '../src/summary.service';
import { ArticleService } from '../src/article.service';

describe('refresh worker thread', () => {
  it('runs the refresh job in a separate worker and persists completion', async () => {
    const dataDir = mkdtempSync(path.join(os.tmpdir(), 'pykasu-worker-'));
    const settings = { dataDir, databasePath: path.join(dataDir, 'pykasu.sqlite3'), bind: '127.0.0.1', port: 8001, groqApiKey: undefined, groqZeroDataRetention: false, maxEnabledSources: 10, entriesPerFeed: 20, summariesPerRefresh: 10, summariesPerDay: 30, modelMinIntervalSeconds: 65, responseSizeLimit: 2 * 1024 * 1024, requestDeadlineMs: 1000, maxRedirects: 3, promptCharacterLimit: 16000, modelCompletionLimit: 2048 } as any;
    const database = new DatabaseService(settings);
    const http = new SafeHttpService(settings);
    const service = new RefreshService(database, new FeedService(), http, new ArticleService(http), new SummaryService(settings, http));
    try {
      const runId = service.startOrJoin();
      expect(runId).not.toBeNull();
      const deadline = Date.now() + 5000;
      while (service.status().active && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 10));
      expect(service.status().active).toBe(false);
      const run = database.db.prepare('SELECT status,errors_json FROM refresh_runs WHERE id=?').get(runId) as { status: string; errors_json: string };
      if (run.status !== 'completed') throw new Error(JSON.stringify(run));
      expect(run).toMatchObject({ status: 'completed' });
    } finally {
      await service.onModuleDestroy();
      database.onModuleDestroy();
      rmSync(dataDir, { recursive: true, force: true });
    }
  });
});
