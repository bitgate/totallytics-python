// Regenerates tests/fixtures/conformance.json from the JS SDK, the reference implementation.
// Usage: npx tsx scripts/generate_fixtures.mts ../totallytics-js > tests/fixtures/conformance.json
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const root = process.argv[2]
if (!root) throw new Error('pass the path to a totallytics-js checkout')

const load = (file: string) => import(pathToFileURL(path.resolve(root, file)).href)
const { Totallytics } = await load('src/core/client.ts')
const { bucket } = await load('src/core/histogram.ts')
const { VERSION } = await load('src/version.ts')

const KEY = `tt_${'ab'.repeat(24)}`
const MINUTE = Date.UTC(2026, 8, 26, 12, 0, 0)

type Entry = Record<string, unknown>
type Respond = (payload: { metrics: unknown[]; errors: unknown[] }) => number

// JSON carries neither NaN/Infinity nor Error objects, so we tag them
function encode(value: unknown): unknown {
  if (typeof value === 'number' && !Number.isFinite(value)) return { $number: String(value) }
  if (value instanceof Error) return { $error: { name: value.name, message: value.message } }
  return value
}

function encodeEntry(entry: Entry): Entry {
  return Object.fromEntries(Object.entries(entry).map(([key, value]) => [key, encode(value)]))
}

async function scenario(name: string, options: Entry, entries: Entry[], respond: Respond = () => 202) {
  const batches: unknown[] = []
  globalThis.fetch = (async (_url: string, init: { body: string }) => {
    const payload = JSON.parse(init.body)
    const status = respond(payload)
    if (status < 300) batches.push({ metrics: payload.metrics, errors: payload.errors })
    return new Response('{}', { status })
  }) as typeof fetch

  const client = new Totallytics({ apiKey: KEY, ...options })
  for (const entry of entries) client.record(entry)
  await client.flush()
  return { name, options, entries: entries.map(encodeEntry), batches }
}

function nextUp(value: number, direction: 1 | -1): number {
  const view = new DataView(new ArrayBuffer(8))
  view.setFloat64(0, value)
  view.setBigUint64(0, view.getBigUint64(0) + BigInt(direction))
  return view.getFloat64(0)
}

function bucketCases(): unknown[] {
  const values = [0, -1, 1, 1.0000000000000002, 0.9999999999999999, 5e-324, 1e308, NaN, Infinity, -Infinity]
  for (let i = 0; i <= 252; i++) {
    const edge = Math.pow(1.08, i)
    values.push(edge, nextUp(edge, 1), nextUp(edge, -1), edge * 1.0000001, edge * 0.9999999)
  }
  for (let ms = 0.5; ms < 2_000_000; ms = ms * 1.0137 + 0.0071) values.push(ms)
  return values.map((ms) => [encode(ms), bucket(ms)])
}

const base: Entry = {
  method: 'GET',
  path: '/users/42',
  route: '/users/:id',
  status: 200,
  durationMs: 12.5,
  startedAt: MINUTE + 1_000,
  userAgent: 'curl/8.4',
  consumer: 'acme',
}

const keying = [
  base,
  { ...base, durationMs: 250.25 },
  { ...base, method: 'post' },
  { ...base, method: 'Patch' },
  { ...base, method: 'straße' },
  { ...base, method: undefined },
  { ...base, method: '' },
  { ...base, route: '/users/:id/posts' },
  { ...base, route: '' },
  { ...base, route: undefined, path: '/users/43' },
  { ...base, status: 201 },
  { ...base, status: 200.0 },
  { ...base, userAgent: 'Mozilla/5.0' },
  { ...base, userAgent: undefined },
  { ...base, userAgent: '' },
  { ...base, consumer: 'globex' },
  { ...base, consumer: undefined },
  { ...base, consumer: '' },
  { ...base, consumer: 0 },
  { ...base, startedAt: MINUTE + 59_999 },
  { ...base, startedAt: MINUTE + 59_999.9 },
  { ...base, startedAt: MINUTE + 60_000 },
  { ...base, startedAt: MINUTE - 1 },
]

const paths = [
  'https://api.example.com/v1/items?limit=5#top',
  'http://localhost:8080',
  'HTTP://Example.COM/Upper?x',
  'ftp://host/file',
  'mailto:someone@example.com',
  '/search?q=a/b',
  '/fragment#x?y',
  '?only=query',
  '#only-fragment',
  '',
  '//double//slash',
  '/café/ünïcode',
  '/emoji/😀',
  'relative/path',
].map((path, index) => ({ method: 'GET', path, status: 404, durationMs: index + 1, startedAt: MINUTE }))
paths.push({ method: 'GET', path: '/x', route: '/keeps?query=1', status: 404, durationMs: 1, startedAt: MINUTE })

const clipped = [
  { path: `/${'a'.repeat(600)}`, userAgent: 'u'.repeat(600), consumer: 'c'.repeat(200), error: 'e'.repeat(1500) },
  { path: `/${'a'.repeat(510)}😀😀`, userAgent: `${'u'.repeat(511)}😀`, consumer: `${'c'.repeat(127)}😀x`, error: `${'e'.repeat(999)}😀` },
  { path: `/${'a'.repeat(509)}😀😀`, userAgent: `${'u'.repeat(510)}😀`, consumer: `${'c'.repeat(126)}😀x`, error: `${'e'.repeat(998)}😀` },
  { path: '/r', route: `/${'r'.repeat(600)}` },
].map((entry, index) => ({ method: 'GET', status: 500, durationMs: 3, startedAt: MINUTE + index, ...entry }))

const invalidStatuses = [99, 600, 200.5, NaN, Infinity, -Infinity, -200, 'abc', null, true, 0, 100, 599]
const invalidDurations = [-5, 0, NaN, Infinity, -Infinity, 1e-9, '12', null, 1e-300, 5e-324, 86_400_000.5, 1e12]
const invalid = [
  ...invalidStatuses.map((status, index) => ({ method: 'GET', path: `/s/${index}`, status, durationMs: 5, startedAt: MINUTE })),
  ...invalidDurations.map((durationMs, index) => ({ method: 'GET', path: `/d/${index}`, status: 200, durationMs, startedAt: MINUTE })),
]

const errors = Array.from({ length: 90 }, (_, index) => {
  const server = index % 3 !== 0
  return {
    method: index % 4 ? 'GET' : 'DELETE',
    path: `/e/${index}?secret=1`,
    route: '/e/:id',
    status: server ? 500 + (index % 4) : 400 + (index % 30),
    durationMs: index * 1.5 + 0.25,
    startedAt: MINUTE + index * 1_000,
    error: index % 5 === 0 ? new TypeError(`bad ${index}`) : index % 7 === 0 ? new RangeError(`far ${index}`) : `boom ${index}`,
    userAgent: index % 2 ? 'ua' : undefined,
    consumer: index % 7 ? `c${index % 3}` : undefined,
  }
})

const roundingDurations = [
  0.0005, 0.0015, 0.0025, 0.0035, 1.0005, 1.0015, 2.0045, 8.0005, 1.2345, 1.2355, 10.0005, 100.0005,
  1000.0005, 12345.6785, 0.1235, 0.3335, 2.675, 1.4995, 4.0004999, 7.77777, 0.1 + 0.2, 1 / 3, 2 / 3, 1e-4,
  5e-4, 9.9995, 99.9995, 0.9995, 3.14159265, 2.71828182, 1.1115, 6.0005, 16.0005, 32.0005, 64.0005,
]
const rounding = [
  ...roundingDurations.map((durationMs, index) => ({
    method: 'GET',
    path: `/round/${index}`,
    status: 503,
    durationMs,
    startedAt: MINUTE,
  })),
  ...Array.from({ length: 300 }, (_, index) => ({
    method: 'GET',
    path: '/sum',
    status: 200,
    durationMs: 0.1 + (index % 7) * 0.0001,
    startedAt: MINUTE,
  })),
]

const batching = Array.from({ length: 7 }, (_, index) => ({
  method: 'GET',
  path: `/b/${index}`,
  route: `/batch/${index}`,
  status: index % 2 ? 200 : 500,
  durationMs: 10 + index,
  startedAt: MINUTE,
  error: `batch ${index}`,
}))

const split = Array.from({ length: 7 }, (_, index) => ({
  method: 'GET',
  path: `/split/${index}`,
  route: `/split/${index}`,
  status: index < 5 ? 502 : 200,
  durationMs: 20 + index,
  startedAt: MINUTE,
  error: `split ${index}`,
}))

const scenarios = [
  await scenario('keying', {}, keying),
  await scenario('paths', {}, paths),
  await scenario('clipping', {}, clipped),
  await scenario('invalid input', {}, invalid),
  await scenario('error samples', {}, errors),
  await scenario('rounding', {}, rounding),
  await scenario('batching', { maxBatchRows: 3 }, batching),
  await scenario('no error samples', { errorSamples: false }, errors),
  await scenario('413 split', {}, split, (payload) => (payload.metrics.length + payload.errors.length > 3 ? 413 : 202)),
]

process.stdout.write(`${JSON.stringify({ source: `totallytics-js ${VERSION}`, buckets: bucketCases(), scenarios })}\n`)
