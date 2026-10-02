export type PublicError = string | { code?: string; message?: string; error_name?: string }
export type Policy = { version: string; clauses: Record<string, string> }
export type AuditEvent = { kind: string; occurred_at: string; from_state?: string; to_state?: string; details?: Record<string, unknown> }
export type Review = {
  id: string; created_at: string; finished_at?: string; status: string; review_hash: string | null
  accepted_recommendation: string | null; rationale: string | null
  citations: { id: string; quote: string }[]; missing_information: string[]; errors: PublicError[]
  model_calls: number; completed_model_responses: number; runtime_provenance: string | null; error: PublicError | null
}
export type PaymentOperation = {
  id: string; state: string; refund_id: string | null; provider_status: string | null
  last_error: PublicError | null; verification_provenance?: string | null; verified_at?: string | null
  approval: { approval_kind: string; approved_by: string; approved_at: string; snapshot: { amount_minor: number; currency: string; review_hash: string } }
  events: AuditEvent[]
}
export type RefundCase = {
  id: string; capture_id: string; state: string; created_at: string; case_version: string
  customer_message: string; item_used: boolean | null; request_date: string; purchase_date: string
  amount_minor: number; currency: string; transaction_provenance: string; transaction_verified_by_service: boolean
  physical_facts_provenance: string; capture_read_at: string; policy: Policy
  latest_review: Review | null; reviews: Review[]; operation: PaymentOperation | null; events: AuditEvent[]
  can_review: boolean; can_approve: boolean; approval_kind: string; approved_by: string; error: PublicError | null
}
export type Session = { environment: 'sandbox'; approval_mode: 'human_ui' | 'test_operator' }
export type NewRequest = { capture_id: string; customer_message: string; item_used: boolean; request_date: string }
export type ApprovalSnapshot = { caseId: string; captureId: string; amountMinor: number; currency: string; reviewHash: string }
