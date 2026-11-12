import {intelligenceQueries} from '../src/features/intelligence/queries'

test('health query targets the staff-only endpoint, does not retry, and respects the enabled flag', async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ok: true, data: {enabled: false, rows: {}}, error: null, meta: {}}), {status: 200}))
  vi.stubGlobal('fetch', fetchMock)
  const options = intelligenceQueries.health()
  expect(options.retry).toBe(false)
  expect(intelligenceQueries.health(false).enabled).toBe(false)
  const data = await (options.queryFn as unknown as () => Promise<{enabled: boolean}>)()
  expect(data.enabled).toBe(false)
  expect(String(fetchMock.mock.calls[0][0])).toContain('/intelligence/health')
  vi.unstubAllGlobals()
})
