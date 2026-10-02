import type { AiRuntime, ApprovalSnapshot, PublicError, RefundCase, Review } from '../types.ts'

const labels: Record<string, string> = {
  case_open: 'Not assessed', reviewing: 'Assessing', review_ready: 'Review ready', review_incomplete: 'Incomplete',
  needs_information: 'Needs information', REVIEW_NEEDS_INFORMATION: 'Needs information',
  REVIEW_READY: 'Review ready', REVIEW_INCOMPLETE: 'Incomplete', prepared: 'Approved · not submitted',
  submitting: 'Submitting', pending: 'Awaiting verification', uncertain: 'Outcome uncertain', verified: 'Refund verified', failed: 'Refund failed',
  refund: 'Refund recommended', decline: 'Decline recommended', request_information: 'More information needed',
  case_created: 'Request created', review_completed: 'Assessment completed', review_started: 'Assessment started',
  case_resolved: 'Case information updated', case_revised: 'Case information updated',
  item_used: 'Item condition', request_date: 'Request received date', item_condition: 'Item condition',
  amount: 'Amount mentioned', timeline: 'Timeline', intent: 'Customer intent', promise: 'Promise', policy: 'Policy', other: 'Other detail',
  approved: 'Approval recorded', submission_claimed: 'Submission started', submission_received: 'PayPal receipt recorded', refund_verified: 'Refund independently verified', refund_readback: 'Refund status checked',
}
export const label = (value: string) => labels[value] || value.replaceAll('_', ' ')
export const money = (value: number, currency = 'USD') => Number.isSafeInteger(value) && currency === 'USD'
  ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value / 100) : 'Amount unavailable'
export const timestamp = (value?: string | null) => value && !Number.isNaN(Date.parse(value))
  ? new Date(value).toLocaleString(undefined, { year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }) : 'Not recorded'
export const errorText = (error: PublicError) => typeof error === 'string' ? error : error.message || error.error_name || error.code || 'The operation is incomplete.'
export const isRealVerified = (record: RefundCase) => record.operation?.state === 'verified' && record.operation.verification_provenance === 'paypal_sandbox'
export const isAttention = (record: RefundCase) => ['uncertain', 'pending', 'failed', 'review_incomplete', 'needs_information'].includes(record.state)
export const merchantFacts = (condition: string, date: string) => ({
  item_used: condition === 'unused' ? false : condition === 'used' ? true : null,
  request_date: date || null,
})
export const conditionLabel = (used: boolean | null) => used === false ? 'Unused · confirmed' : used === true ? 'Used · confirmed' : 'Not confirmed'
export function customerResponseDraft(record: RefundCase): string | null {
  const operation = record.operation
  if (!operation) {
    const evidence = record.latest_review?.evidence
    return evidence?.validation_status === 'valid' && evidence.reply_draft_source === 'application_template'
      ? evidence.reply_draft : null
  }
  const approved = operation.approval.snapshot
  if (isRealVerified(record) && operation.provider_status === 'COMPLETED' && operation.refund_id
      && Number.isSafeInteger(approved.amount_minor) && approved.amount_minor > 0 && approved.currency === 'USD') {
    return `PayPal sandbox confirmed a completed refund of ${money(approved.amount_minor, approved.currency)} ${approved.currency}. Refund reference: ${operation.refund_id}.`
  }
  if (operation.state === 'verified' && operation.verification_provenance !== 'paypal_sandbox') {
    return 'This is a synthetic test result. No PayPal refund was made.'
  }
  if (operation.state === 'prepared') {
    return 'A sandbox refund was approved, but no refund submission is recorded yet.'
  }
  return 'The sandbox refund outcome has not been verified. A completed refund cannot be confirmed yet.'
}
export function analysisRuntimeInfo(runtime?: AiRuntime) {
  if (runtime?.provider === 'groq' && runtime.location === 'hosted') return {
    label: `Hosted · Groq · ${runtime.model}`,
    disclosure: 'Analyzing sends this conversation, payment summary, and saved policy to Groq.',
  }
  if (runtime?.provider === 'ollama' && runtime.location === 'local') return {
    label: `Local · Ollama · ${runtime.model}`, disclosure: 'Analysis runs on the local Ollama model.',
  }
  if (runtime?.provider === 'test' && runtime.location === 'test') return {
    label: `Synthetic test · ${runtime.model}`, disclosure: 'This runtime returns test responses, not live model evidence.',
  }
  return { label: 'AI provider unavailable', disclosure: 'Reload the workspace to confirm where analysis will run.' }
}
export function reviewRuntimeLabel(review: Review) {
  const provenance = review.runtime_provenance
  const provider = provenance === 'local_ollama' ? 'Local · Ollama' : provenance === 'hosted_groq' ? 'Hosted · Groq'
    : provenance === 'injected_test_generator' ? 'Synthetic test response' : 'Runtime not recorded'
  return review.model?.model ? `${provider} · ${review.model.model}` : provider
}
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
    && typeof record.item_used === 'boolean' && Boolean(record.request_date)
    && record.latest_review?.status === 'REVIEW_READY' && record.latest_review.accepted_recommendation === 'refund'
    && Number.isSafeInteger(record.amount_minor) && record.amount_minor > 0 && record.currency === 'USD'
    && /^[a-f0-9]{64}$/.test(record.latest_review?.review_hash || '')
}
export function snapshotMatches(snapshot: ApprovalSnapshot, record: RefundCase) {
  return canApprove(record) && snapshot.caseId === record.id && snapshot.captureId === record.capture_id
    && snapshot.caseVersion === record.case_version
    && snapshot.amountMinor === record.amount_minor && snapshot.currency === record.currency && snapshot.reviewHash === record.latest_review?.review_hash
}
