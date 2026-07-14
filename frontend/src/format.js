export const EMPTY_VALUE = '\u2014'

export function formatDisplayValue(value) {
  if (value === null || value === undefined || value === '') return EMPTY_VALUE
  if (typeof value !== 'string' || !value.includes('T')) return value

  const timestamp = new Date(value)
  return Number.isNaN(timestamp.getTime()) ? EMPTY_VALUE : timestamp.toLocaleString()
}
