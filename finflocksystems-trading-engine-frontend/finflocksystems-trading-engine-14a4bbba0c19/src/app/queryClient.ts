import {QueryClient} from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      gcTime: 5 * 60_000,
      retry: (failures, error) => {
        const status = typeof error === 'object' && error !== null && 'status' in error ? Number(error.status) : 0
        return status >= 400 && status < 500 ? false : failures < 2
      },
      refetchOnWindowFocus: true,
    },
    mutations: {retry: false},
  },
})

