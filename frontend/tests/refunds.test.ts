import assert from 'node:assert/strict'
import test from 'node:test'
import { canApprove, money, snapshotMatches, totals } from '../src/lib/refunds.ts'
import type { RefundCase } from '../src/types.ts'

function record(state = 'review_ready', provenance = 'paypal_sandbox', amount = 3900): RefundCase {
  return {
    id: 'synthetic-case', capture_id: 'SYNTHETICCAPTURE', state, created_at: '', case_version: '', customer_message: 'Test fixture only',
    item_used: false, request_date: '', purchase_date: '', amount_minor: amount, currency: 'USD', transaction_provenance: provenance,
    transaction_verified_by_service: provenance === 'paypal_sandbox', physical_facts_provenance: 'merchant_supplied', capture_read_at: '',
    policy: { version: 'test', clauses: {} }, latest_review: { id: 'review', created_at: '', status: 'REVIEW_READY', review_hash: 'a'.repeat(64),
      accepted_recommendation: 'refund', rationale: '', citations: [], missing_information: [], errors: [], model_calls: 0,
      completed_model_responses: 0, runtime_provenance: 'injected_test_generator', error: null }, reviews: [],
    operation: state === 'review_ready' ? null : { id: 'op', state, refund_id: 'SYNTHETICREFUND', provider_status: 'COMPLETED', last_error: null,
      verification_provenance: provenance, approval: { approval_kind: 'test_operator', approved_by: 'test', approved_at: '',
        snapshot: { amount_minor: amount, currency: 'USD', review_hash: 'a'.repeat(64) } }, events: [] },
    events: [], can_review: false, can_approve: true, approval_kind: 'test_operator', approved_by: 'test', error: null,
  }
}

test('dashboard money excludes pending, uncertain, unsubmitted and injected evidence', () => {
  const actual = totals([record('verified'), record('pending'), record('uncertain'), record(), record('verified', 'injected_test_transport', 7000)])
  assert.equal(actual.total, 5)
  assert.equal(actual.verified, 1)
  assert.equal(actual.refundedMinor, 3900)
  assert.equal(totals([]).refundedMinor, 0)
})

test('money and approval reject unsafe integer values', () => {
  const unsafe = Number.MAX_SAFE_INTEGER + 1
  assert.equal(money(unsafe), 'Amount unavailable')
  assert.equal(canApprove(record('review_ready', 'paypal_sandbox', unsafe)), false)
  assert.ok(Number.isNaN(totals([record('verified', 'paypal_sandbox', unsafe)]).refundedMinor))
  assert.ok(Number.isNaN(totals([record('verified', 'paypal_sandbox', Number.MAX_SAFE_INTEGER), record('verified')]).refundedMinor))
})

test('approval snapshot binds capture, amount, currency and exact review', () => {
  const current = record()
  const snapshot = { caseId: current.id, captureId: current.capture_id, amountMinor: 3900, currency: 'USD', reviewHash: 'a'.repeat(64) }
  assert.equal(snapshotMatches(snapshot, current), true)
  for (const changed of [{ ...snapshot, amountMinor: 1 }, { ...snapshot, captureId: 'OTHER' }, { ...snapshot, currency: 'EUR' }, { ...snapshot, reviewHash: 'b'.repeat(64) }]) {
    assert.equal(snapshotMatches(changed, current), false)
  }
  assert.equal(snapshotMatches(snapshot, { ...current, can_approve: false }), false)
})

test('no submit control is allowed after a payment has been claimed', () => {
  assert.equal(canApprove(record('prepared')), true)
  for (const state of ['submitting', 'pending', 'uncertain', 'verified', 'failed']) assert.equal(canApprove(record(state)), false)
})
