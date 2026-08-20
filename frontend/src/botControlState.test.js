import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const source = readFileSync(new URL('./pages/ExecutionPages.jsx', import.meta.url), 'utf8')

test('dashboard exposes an explicit canonical resume action', () => {
  assert.match(source, /act\('resume'\)/)
  assert.match(source, />Resume New Entries</)
  assert.match(source, /x\.pause_new_entries&&x\.status==='running'/)
})

test('control actions immediately apply and then refresh canonical backend state', () => {
  assert.match(source, /const response=await api\.post\(`\/bot\/\$\{action\}`\)/)
  assert.match(source, /s\.setData\(response\.data\)/)
  assert.match(source, /Promise\.all\(\[s\.load\(\),a\.load\(\)\]\)/)
})
