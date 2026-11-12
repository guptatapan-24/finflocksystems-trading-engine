import {API_BASE_URL} from '../../api/client'

export type DownloadFormat = 'pdf' | 'html' | 'markdown'

async function failureMessage(response: Response) {
  try {
    const body = await response.json() as {error?: {message?: string}}
    if (body.error?.message) return body.error.message
  } catch { /* not a JSON error body */ }
  return `Download failed (${response.status})`
}

/** Fetches a report from the server and saves it under the server's file name. Throws with the server's message on failure. */
export async function downloadReport(path: string, format: DownloadFormat, fallbackName: string) {
  const separator = path.includes('?') ? '&' : '?'
  const response = await fetch(`${API_BASE_URL}/${path}${separator}format=${format}`, {credentials: 'include'})
  if (!response.ok) throw new Error(await failureMessage(response))
  const named = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') ?? '')?.[1]
  const extension = format === 'markdown' ? 'md' : format
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url
  link.download = named ?? `${fallbackName}.${extension}`
  link.click()
  URL.revokeObjectURL(url)
}
