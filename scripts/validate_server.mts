// Feeds SDK request bodies through the server's own normalizeBatch and fails on any rejected row.
// Usage: npx tsx scripts/validate_server.mts <totallytics server checkout> payloads.json
import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const MAX_BODY_BYTES = 4 * 1024 * 1024

const [root, file] = process.argv.slice(2)
if (!root || !file) throw new Error('usage: validate_server.mts <server checkout> <payloads.json>')

const ingest = pathToFileURL(path.resolve(root, 'src/api-analytics/ingest.ts')).href
const { normalizeBatch } = await import(ingest)
const bodies: string[] = JSON.parse(fs.readFileSync(file, 'utf8'))

const now = Date.now()
let metrics = 0
let errors = 0
let oversized = 0
let failures = 0

for (const body of bodies) {
  const bytes = Buffer.byteLength(body)
  if (bytes > MAX_BODY_BYTES) oversized++

  const payload = JSON.parse(body)
  const result = normalizeBatch(payload, now)
  const clean =
    !('error' in result) &&
    result.rejected === 0 &&
    result.acceptedMetrics === payload.metrics.length &&
    result.acceptedErrors === payload.errors.length

  if (!clean) {
    failures++
    console.error(`batch ${payload.batch_id} (${bytes} bytes): ${JSON.stringify(result).slice(0, 400)}`)
    continue
  }
  metrics += result.acceptedMetrics
  errors += result.acceptedErrors
}

console.log(
  `${bodies.length} batches: ${metrics} metric rows and ${errors} error rows accepted, ` +
    `${failures} batches with rejections, ${oversized} over 4 MiB (the SDK splits those on 413)`,
)
process.exit(failures ? 1 : 0)
