import type {QueryClient} from '@tanstack/react-query'
import type {StrategyInstance} from '../../api/types'

export function canEnable(strategy: StrategyInstance) {
  return (!strategy.enabled || strategy.state === 'BLOCKED') && strategy.conid !== null && !['ERROR', 'ACTIVATING'].includes(strategy.state)
}

export function canPause(strategy: StrategyInstance) {
  return strategy.enabled && strategy.state !== 'PAUSED'
}

export function canFlatten(strategy: StrategyInstance) {
  return strategy.current_target !== null && Number(strategy.current_target) !== 0
}

export async function refreshAfterStrategyDeletion(queryClient: QueryClient, strategyId: number) {
  queryClient.removeQueries({queryKey: ['strategy-instance', strategyId]})
  queryClient.removeQueries({queryKey: ['strategy-timeline', strategyId]})
  queryClient.removeQueries({queryKey: ['strategy-chart', strategyId]})
  await Promise.all([
    queryClient.invalidateQueries({queryKey: ['strategy-instances']}),
    queryClient.invalidateQueries({queryKey: ['dashboard']}),
    queryClient.invalidateQueries({queryKey: ['allocation-policies']}),
    queryClient.invalidateQueries({queryKey: ['allocation-runs']}),
    queryClient.invalidateQueries({queryKey: ['rebalance-runs']}),
    queryClient.invalidateQueries({queryKey: ['streaming']}),
    queryClient.invalidateQueries({queryKey: ['portfolio-universe']}),
    queryClient.invalidateQueries({queryKey: ['optimization-runs']}),
    queryClient.invalidateQueries({queryKey: ['positions']}),
    queryClient.invalidateQueries({queryKey: ['portfolio-series']}),
    queryClient.invalidateQueries({queryKey: ['audit']}),
  ])
}
