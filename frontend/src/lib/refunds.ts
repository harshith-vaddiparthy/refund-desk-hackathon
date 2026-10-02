import type { ApprovalSnapshot, PublicError, RefundCase } from '../types.ts'

const labels: Record<string, string> = {
  case_open: 'Not assessed', reviewing: 'Assessing', review_ready: 'Review ready', review_incomplete: 'Incomplete',
  REVIEW_READY: 'Review ready', REVIEW_INCOMPLETE: 'Incomplete', prepared: 'Approved · not submitted',
  submitting: 'Submitting', pending: 'Awaiting verification', uncertain: 'Outcome uncertain', verified: 'Refund verified', failed: 'Refund failed',
  refund: 'Refund recommended', decline: 'Decline recommended', request_information: 'More information needed',
  case_created: 'Request created', review_completed: 'Assessment completed', review_started: 'Assessment started',
  approved: 'Approval recorded', submission_claimed: 'Submission started', submission_received: 'PayPal receipt recorded', refund_verified: 'Refund independently verified', refund_readback: 'Refund status checked',
}
export const label = (value: string) => labels[value] || value.replaceAll('_', ' ')
export const money = (value: number, currency = 'USD') => Number.isSafeInteger(value) && currency === 'USD'
  ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value / 100) : 'Amount unavailable'
export const timestamp = (value?: string | null) => value && !Number.isNaN(Date.parse(value))
  ? new Date(value).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }) : 'Not recorded'
export const errorText = (error: PublicError) => typeof error === 'string' ? error : error.message || error.error_name || error.code || 'The operation is incomplete.'
export const isRealVerified = (record: RefundCase) => record.operation?.state === 'verified' && record.operation.verification_provenance === 'paypal_sandbox'
export const isAttention = (record: RefundCase) => ['uncertain', 'pending', 'failed', 'review_incomplete'].includes(record.state)
export function totals(records: RefundCase[]) {
  const verified = records.filter(isRealVerified)
  const amounts = verified.map(record => record.operation!.approval.snapshot)
  const amountMinor = amounts.every(amount => Number.isSafeInteger(amount.amount_minor) && amount.amount_minor >= 0 && amount.currency === 'USD')
    ? amounts.reduce((sum, amount) => sum + amount.amount_minor, 0) : NaN
  return { total: records.length, toReview: records.filter(record => record.can_review).length, attention: records.filter(isAttention).length,
    verified: verified.length, refundedMinor: Number.isSafeInteger(amountMinor) ? amountMinor : NaN }
}
export function canApprove(record: RefundCase) {
  return record.can_approve && (!record.operation || record.operation.state === 'prepared')
    && Number.isSafeInteger(record.amount_minor) && record.amount_minor > 0 && record.currency === 'USD'
    && /^[a-f0-9]{64}$/.test(record.latest_review?.review_hash || '')
}
export function snapshotMatches(snapshot: ApprovalSnapshot, record: RefundCase) {
  return canApprove(record) && snapshot.caseId === record.id && snapshot.captureId === record.capture_id
    && snapshot.amountMinor === record.amount_minor && snapshot.currency === record.currency && snapshot.reviewHash === record.latest_review?.review_hash
}
