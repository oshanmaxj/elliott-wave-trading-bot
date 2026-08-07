import test from 'node:test'
import assert from 'node:assert/strict'
import {applyCsrf,readCookie} from './api.js'

test('authenticated mutations read and send the current CSRF cookie',()=>{
  const source='other=1; wavescope_csrf=current-token; theme=dark'
  assert.equal(readCookie('wavescope_csrf',source),'current-token')
  const config=applyCsrf({method:'post',headers:{}},source)
  assert.equal(config.headers['X-CSRF-Token'],'current-token')
})

test('safe GET requests do not receive a CSRF header',()=>{
  const config=applyCsrf({method:'get',headers:{}},'wavescope_csrf=current-token')
  assert.equal(config.headers['X-CSRF-Token'],undefined)
})
