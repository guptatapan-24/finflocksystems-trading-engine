import {create} from 'zustand'
import {persist} from 'zustand/middleware'

export type SidebarMode = 'expanded' | 'compact'
export type Density = 'comfortable' | 'compact'
export type ChartType = 'candlestick' | 'line' | 'area'

export interface ChartPreferences {
  range: string
  interval: string
  chartType: ChartType
  volumeVisible: boolean
  indicatorsVisible: boolean
  percentageScale: boolean
}

interface WorkspacePreferences {
  sidebarMode: SidebarMode
  mobileNavigationOpen: boolean
  density: Density
  rightRailOpen: boolean
  collapsedPanels: Record<string, boolean>
  fullscreenPanelId: string | null
  chartPreferences: Record<string, Partial<ChartPreferences>>
  setSidebarMode: (mode: SidebarMode) => void
  setMobileNavigationOpen: (open: boolean) => void
  setDensity: (density: Density) => void
  setRightRailOpen: (open: boolean) => void
  setPanelCollapsed: (key: string, collapsed: boolean) => void
  setFullscreenPanelId: (id: string | null) => void
  setChartPreferences: (id: string, preferences: Partial<ChartPreferences>) => void
  resetWorkspace: () => void
}

const defaults = {
  sidebarMode: 'expanded' as SidebarMode,
  mobileNavigationOpen: false,
  density: 'compact' as Density,
  rightRailOpen: true,
  collapsedPanels: {} as Record<string, boolean>,
  fullscreenPanelId: null as string | null,
  chartPreferences: {} as Record<string, Partial<ChartPreferences>>,
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

export const useWorkspacePreferences = create<WorkspacePreferences>()(persist((set) => ({
  ...defaults,
  setSidebarMode: (sidebarMode) => set({sidebarMode}),
  setMobileNavigationOpen: (mobileNavigationOpen) => set({mobileNavigationOpen}),
  setDensity: (density) => set({density}),
  setRightRailOpen: (rightRailOpen) => set({rightRailOpen}),
  setPanelCollapsed: (key, collapsed) => set((state) => ({collapsedPanels: {...state.collapsedPanels, [key]: collapsed}})),
  setFullscreenPanelId: (fullscreenPanelId) => set({fullscreenPanelId}),
  setChartPreferences: (id, preferences) => set((state) => ({
    chartPreferences: {...state.chartPreferences, [id]: {...state.chartPreferences[id], ...preferences}},
  })),
  resetWorkspace: () => set(defaults),
}), {
  name: 'finflock-workspace-v1',
  version: 1,
  partialize: (state) => ({
    sidebarMode: state.sidebarMode,
    density: state.density,
    rightRailOpen: state.rightRailOpen,
    collapsedPanels: state.collapsedPanels,
    chartPreferences: state.chartPreferences,
  }),
  merge: (persisted, current) => {
    const saved = isRecord(persisted) ? persisted : {}
    return {
      ...current,
      sidebarMode: saved.sidebarMode === 'compact' ? 'compact' : 'expanded',
      density: saved.density === 'comfortable' ? 'comfortable' : 'compact',
      rightRailOpen: typeof saved.rightRailOpen === 'boolean' ? saved.rightRailOpen : true,
      collapsedPanels: isRecord(saved.collapsedPanels) ? saved.collapsedPanels as Record<string, boolean> : {},
      chartPreferences: isRecord(saved.chartPreferences) ? saved.chartPreferences as Record<string, Partial<ChartPreferences>> : {},
      mobileNavigationOpen: false,
      fullscreenPanelId: null,
    }
  },
}))
