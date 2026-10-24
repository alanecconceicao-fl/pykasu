import 'reflect-metadata';
import { parentPort, workerData } from 'node:worker_threads';
import { Settings } from './config';
import { ArticleService } from './article.service';
import { DatabaseService } from './database.service';
import { FeedService } from './feed.service';
import { SafeHttpService } from './http.service';
import { RefreshService } from './refresh.service';
import { SummaryService } from './summary.service';

interface Job {
  runId: number;
  sources: any[];
  settings: Settings;
}

async function main(): Promise<void> {
  const job = workerData as Job;
  const database = new DatabaseService(job.settings, false);
  const http = new SafeHttpService(job.settings);
  const refresh = new RefreshService(database, new FeedService(), http, new ArticleService(http), new SummaryService(job.settings, http));
  try {
    await refresh.executeRefresh(job.runId, job.sources);
    parentPort?.postMessage({ type: 'complete', runId: job.runId });
  } finally {
    database.onModuleDestroy();
  }
}

void main().catch(() => { process.exitCode = 1; });
