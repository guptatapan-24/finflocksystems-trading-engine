import {queryOptions} from '@tanstack/react-query'
import {mutationOptions, request, withQuery} from '../../api/client'
import type {EpisodeFilters, IntelligenceEpisode, IntelligenceEpisodeDetail, IntelligenceHealth, IntelligenceReview, EpisodeLive, OpenEpisodeRow, TradeReport, IntelligenceLesson, LearningEventRow, PerformanceRow, CalibrationRow, LearningDigest, PreTradeAdvisoryData, WeeklyReviewRow, CounterfactualSweep, DailyReport, SessionRow, ExperimentRow, ChangePointRow} from './types'

// Kept separate from the trading queries so an intelligence failure can never affect them.
export const intelligenceQueries = {
  health: (enabled = true) => queryOptions({
    queryKey: ['intelligence', 'health'],
    queryFn: () => request<IntelligenceHealth>('intelligence/health/'),
    enabled,
    retry: false,
    refetchInterval: 30_000,
  }),
  episodes: (filters: EpisodeFilters = {}) => queryOptions({
    queryKey: ['intelligence', 'episodes', filters],
    queryFn: () => request<IntelligenceEpisode[]>(withQuery('intelligence/episodes/', {status: filters.status, currency: filters.currency, limit: filters.limit ?? 100})),
    retry: false,
    refetchInterval: 20_000,
  }),
  review: (episodeId?: number | null) => queryOptions({
    queryKey: ['intelligence', 'review', episodeId ?? 'none'],
    queryFn: () => request<IntelligenceReview>(`intelligence/episodes/${episodeId}/review/`),
    enabled: Boolean(episodeId),
    retry: false,
  }),
  openEpisodes: () => queryOptions({
    queryKey: ['intelligence', 'open-episodes'],
    queryFn: () => request<OpenEpisodeRow[]>('intelligence/live/open-episodes/'),
    retry: false,
    refetchInterval: 15_000,
  }),
  live: (episodeId?: number | null) => queryOptions({
    queryKey: ['intelligence', 'live', episodeId ?? 'none'],
    queryFn: () => request<EpisodeLive>(`intelligence/episodes/${episodeId}/live/`),
    enabled: Boolean(episodeId),
    retry: false,
    refetchInterval: 15_000,
  }),
  report: (episodeId?: number | null) => queryOptions({
    queryKey: ['intelligence', 'report', episodeId ?? 'none'],
    queryFn: () => request<TradeReport>(`intelligence/episodes/${episodeId}/report/`),
    enabled: Boolean(episodeId),
    retry: false,
  }),
  lessons: (status = '') => queryOptions({
    queryKey: ['intelligence', 'lessons', status],
    queryFn: () => request<IntelligenceLesson[]>(withQuery('intelligence/lessons/', {status, limit: 100})),
    retry: false,
    refetchInterval: 60_000,
  }),
  lesson: (id?: number | null) => queryOptions({
    queryKey: ['intelligence', 'lesson', id ?? 'none'],
    queryFn: () => request<IntelligenceLesson>(`intelligence/lessons/${id}/`),
    enabled: Boolean(id),
    retry: false,
  }),
  learningFeed: () => queryOptions({
    queryKey: ['intelligence', 'feed'],
    queryFn: () => request<LearningEventRow[]>('intelligence/learning-feed/?limit=200'),
    retry: false,
    refetchInterval: 60_000,
  }),
  performance: (currency = '') => queryOptions({
    queryKey: ['intelligence', 'performance', currency],
    queryFn: () => request<PerformanceRow[]>(withQuery('intelligence/performance/', {currency})),
    retry: false,
    refetchInterval: 60_000,
  }),
  calibration: () => queryOptions({
    queryKey: ['intelligence', 'calibration'],
    queryFn: () => request<CalibrationRow[]>('intelligence/calibration/'),
    retry: false,
    refetchInterval: 60_000,
  }),
  digest: (days = 7) => queryOptions({
    queryKey: ['intelligence', 'digest', days],
    queryFn: () => request<LearningDigest>(withQuery('intelligence/digest/', {days})),
    retry: false,
  }),
  advisoryPreview: (strategyKey: string, parameters: Record<string, unknown>, instrument = '') => queryOptions({
    queryKey: ['intelligence', 'advisory-preview', strategyKey, parameters, instrument],
    queryFn: () => request<PreTradeAdvisoryData>('intelligence/advisories/preview/', mutationOptions('POST', {strategy_key: strategyKey, parameters, instrument})),
    enabled: Boolean(strategyKey),
    retry: false,
    staleTime: 60_000,
  }),
  weeklyReviews: () => queryOptions({
    queryKey: ['intelligence', 'weekly'],
    queryFn: () => request<WeeklyReviewRow[]>('intelligence/reviews/?scope=WEEKLY&limit=50'),
    retry: false,
    refetchInterval: 120_000,
  }),
  optionsSweep: (strategyKey: string) => queryOptions({
    queryKey: ['intelligence', 'options-sweep', strategyKey],
    queryFn: () => request<CounterfactualSweep>(withQuery('intelligence/counterfactuals/options/', {strategy_key: strategyKey})),
    enabled: Boolean(strategyKey),
    retry: false,
  }),
  experiments: (status = '') => queryOptions({
    queryKey: ['intelligence', 'experiments', status],
    queryFn: () => request<ExperimentRow[]>(withQuery('intelligence/experiments/', {status})),
    retry: false,
    refetchInterval: 60_000,
  }),
  changePoints: () => queryOptions({
    queryKey: ['intelligence', 'changepoints'],
    queryFn: () => request<ChangePointRow[]>('intelligence/changepoints/'),
    retry: false,
    refetchInterval: 120_000,
  }),
  dailyReport: (date = '', zone = '') => queryOptions({
    queryKey: ['intelligence', 'daily-report', date, zone],
    queryFn: () => request<DailyReport>(withQuery('intelligence/reports/daily/', {date, zone})),
    retry: false,
    refetchInterval: 120_000,
  }),
  sessions: () => queryOptions({
    queryKey: ['intelligence', 'sessions'],
    queryFn: () => request<SessionRow[]>('intelligence/sessions/'),
    retry: false,
    refetchInterval: 120_000,
  }),
  episode: (id?: number | null) => queryOptions({
    queryKey: ['intelligence', 'episode', id ?? 'none'],
    queryFn: () => request<IntelligenceEpisodeDetail>(`intelligence/episodes/${id}/`),
    enabled: Boolean(id),
    retry: false,
  }),
}
