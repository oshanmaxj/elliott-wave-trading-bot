import assert from 'node:assert/strict'
import test from 'node:test'

import { EMPTY_VALUE, formatDisplayValue } from './format.js'

test('missing and invalid timestamps render an em dash', () => {
  assert.equal(formatDisplayValue(null), EMPTY_VALUE)
  assert.equal(formatDisplayValue(undefined), EMPTY_VALUE)
  assert.equal(formatDisplayValue('not-a-dateTvalue'), EMPTY_VALUE)
})

test('valid timestamps and ordinary values are preserved safely', () => {
  assert.notEqual(formatDisplayValue('2025-01-01T00:00:00Z'), EMPTY_VALUE)
  assert.equal(formatDisplayValue('INFO'), 'INFO')
  assert.equal(formatDisplayValue(0), 0)
})
