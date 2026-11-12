import {useEffect, useMemo} from 'react'
import {useQuery} from '@tanstack/react-query'
import {queries} from '../api/queries'
import {usePreferencesStore} from './preferences'

export function useSelection() {
  const sessionsQuery = useQuery(queries.brokerSessions())
  const portfoliosQuery = useQuery(queries.portfolios())
  const selectedSessionId = usePreferencesStore((state) => state.selectedSessionId)
  const selectedAccountId = usePreferencesStore((state) => state.selectedAccountId)
  const selectedPortfolioId = usePreferencesStore((state) => state.selectedPortfolioId)
  const setSelectedSession = usePreferencesStore((state) => state.setSelectedSession)
  const setSelectedAccount = usePreferencesStore((state) => state.setSelectedAccount)
  const setSelectedPortfolio = usePreferencesStore((state) => state.setSelectedPortfolio)

  const sessions = sessionsQuery.data || []
  const session = sessions.find((item) => item.id === selectedSessionId) || sessions[0] || null
  const sessionAccountsQuery = useQuery(queries.brokerSessionAccounts(session?.id))
  const legacyAccountsQuery = useQuery({...queries.accounts(), enabled: !session && !sessionsQuery.isLoading})
  const allPortfolios = portfoliosQuery.data || []
  const portfolios = useMemo(() => session
    ? allPortfolios.filter((portfolio) => portfolio.gateway_session_id === session.id)
    : allPortfolios.filter((portfolio) => !portfolio.gateway_session_id), [allPortfolios, session])
  const eligibleAccountIds = new Set(portfolios.map((portfolio) => portfolio.account_id).filter((value): value is number => Boolean(value)))
  const allAccounts = session ? sessionAccountsQuery.data || [] : legacyAccountsQuery.data || []
  const accounts = session ? allAccounts.filter((account) => eligibleAccountIds.has(account.id)) : allAccounts

  useEffect(() => {
    if (session && session.id !== selectedSessionId) setSelectedSession(session.id)
    if (!session && selectedSessionId) setSelectedSession(null)
    const nextAccount = accounts.find((item) => item.id === selectedAccountId) || accounts[0]
    if ((nextAccount?.id ?? null) !== selectedAccountId) setSelectedAccount(nextAccount?.id ?? null)
    const eligible = nextAccount ? portfolios.filter((portfolio) => portfolio.account_id === nextAccount.id) : portfolios
    if ((!selectedPortfolioId || !eligible.some((portfolio) => portfolio.id === selectedPortfolioId))) {
      setSelectedPortfolio(eligible[0]?.id ?? null, nextAccount?.id ?? null)
    }
  }, [accounts, portfolios, selectedAccountId, selectedPortfolioId, selectedSessionId, session, setSelectedAccount, setSelectedPortfolio, setSelectedSession])

  const account = accounts.find((item) => item.id === selectedAccountId) || accounts[0] || null
  const portfolio = portfolios.find((item) => item.id === selectedPortfolioId) || portfolios[0] || null

  return {
    sessions,
    session,
    accounts,
    allAccounts,
    allPortfolios,
    portfolios,
    account,
    portfolio,
    selectedSessionId: session?.id ?? null,
    selectedAccountId: account?.id ?? null,
    selectedPortfolioId: portfolio?.id ?? null,
    setSelectedSession,
    setSelectedAccount,
    setSelectedPortfolio,
    loading: sessionsQuery.isLoading || sessionAccountsQuery.isLoading || legacyAccountsQuery.isLoading || portfoliosQuery.isLoading,
    error: sessionsQuery.error || sessionAccountsQuery.error || legacyAccountsQuery.error || portfoliosQuery.error,
  }
}
