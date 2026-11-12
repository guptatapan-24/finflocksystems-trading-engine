import {create} from 'zustand'
import {persist} from 'zustand/middleware'

interface PreferencesState {
  selectedSessionId: string | null
  selectedAccountId: number | null
  selectedPortfolioId: number | null
  setSelectedSession: (id: string | null) => void
  setSelectedAccount: (id: number | null) => void
  setSelectedPortfolio: (id: number | null, accountId?: number | null) => void
}

export const usePreferencesStore = create<PreferencesState>()(persist((set) => ({
  selectedSessionId: null,
  selectedAccountId: null,
  selectedPortfolioId: null,
  setSelectedSession: (selectedSessionId) => set({selectedSessionId}),
  setSelectedAccount: (selectedAccountId) => set({selectedAccountId}),
  setSelectedPortfolio: (selectedPortfolioId, selectedAccountId) => set((state) => ({
    selectedPortfolioId,
    selectedAccountId: selectedAccountId === undefined ? state.selectedAccountId : selectedAccountId,
  })),
}), {
  name: 'finflock-broker-selection',
  partialize: (state) => ({
    selectedSessionId: state.selectedSessionId,
    selectedAccountId: state.selectedAccountId,
    selectedPortfolioId: state.selectedPortfolioId,
  }),
}))
