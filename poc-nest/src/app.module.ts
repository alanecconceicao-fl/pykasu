import { Module } from '@nestjs/common';
import { AppController } from './app.controller';
import { ArticleService } from './article.service';
import { DatabaseService } from './database.service';
import { FeedService } from './feed.service';
import { SafeHttpService } from './http.service';
import { RefreshService } from './refresh.service';
import { SummaryService } from './summary.service';
import { loadSettings, SETTINGS } from './config';
import { REFRESH_WORKER_FACTORY } from './config';
import { Worker } from 'node:worker_threads';

@Module({
  controllers: [AppController],
  providers: [
    { provide: SETTINGS, useFactory: loadSettings },
    { provide: REFRESH_WORKER_FACTORY, useValue: (filename: string, options: any) => new Worker(filename, options) },
    { provide: DatabaseService, useFactory: (settings: ReturnType<typeof loadSettings>) => new DatabaseService(settings), inject: [SETTINGS] },
    { provide: SafeHttpService, useFactory: (settings: ReturnType<typeof loadSettings>) => new SafeHttpService(settings), inject: [SETTINGS] },
    FeedService,
    ArticleService,
    { provide: SummaryService, useFactory: (settings: ReturnType<typeof loadSettings>, http: SafeHttpService) => new SummaryService(settings, http), inject: [SETTINGS, SafeHttpService] },
    RefreshService
  ]
})
export class AppModule {}
