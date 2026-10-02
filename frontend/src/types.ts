export type PublicError = string | { code?: string; message?: string; error_name?: string }
export type Policy = { version: string; clauses: Record<string, string> }
export type AuditEvent = { kind: string; occurred_at: string; from_state?: string; to_state?: string; details?: Record<string, unknown> }
export type ConversationSource = { id: string; text: string }
export type EvidenceClaim = { kind: string; source_id: string; quote: string }
export type EvidenceIssue = { kind: 'missing' | 'conflict'; field: string; source_ids: string[]; detail: string; question: string; owner?: 'customer' | 'merchant' }
export type EvidenceBrief = {
  summary: string; claims: EvidenceClaim[]; issues: EvidenceIssue[]; reply_draft: string
  reply_draft_source?: string
  validation_status?: 'valid' | 'invalid'; errors?: PublicError[]
  source_references_validated?: boolean; semantic_truth_verified?: false
}
export type Review = {
  id: string; created_at: string; finished_at?: string; status: string; review_hash: string | null
  accepted_recommendation: string | null; rationale: string | null
  citations: { id: string; quote: string }[]; missing_information: string[]; errors: PublicError[]
  model_calls: number; completed_model_responses: number; runtime_provenance: string | null; error: PublicError | null
  evidence?: EvidenceBrief | null
  case_version?: string | null
  model?: { model?: string; digest?: string } | null
}
export type PaymentOperation = {
  id: string; state: string; refund_id: string | null; provider_status: string | null
  last_error: PublicError | null; verification_provenance?: string | null; verified_at?: string | null
  approval: { approval_kind: string; approved_by: string; approved_at: string; snapshot: { amount_minor: number; currency: string; review_hash: string } }
  events: AuditEvent[]
}
export type RefundCase = {
  id: string; capture_id: string; state: string; created_at: string; case_version: string
  customer_message: string; item_used: boolean | null; request_date: string | null; purchase_date: string
  amount_minor: number; currency: string; transaction_provenance: string; transaction_verified_by_service: boolean
  physical_facts_provenance: string; capture_read_at: string; policy: Policy
  latest_review: Review | null; reviews: Review[]; operation: PaymentOperation | null; events: AuditEvent[]
  can_review: boolean; can_approve: boolean; approval_kind: string; approved_by: string; error: PublicError | null
  sources?: ConversationSource[]; resolution_note?: string | null; can_resolve?: boolean
  revision_history?: { case_version: string; created_at: string; resolution_note: string | null }[]
}
export type AiRuntime = { provider: 'ollama' | 'groq' | 'test'; model: string; location: 'local' | 'hosted' | 'test' }
export type Session = { environment: 'sandbox'; approval_mode: 'human_ui' | 'test_operator'; ai_runtime?: AiRuntime }
export type NewRequest = { capture_id: string; customer_message: string; item_used: boolean | null; request_date: string | null }
export type CaseResolution = { expected_version: string; customer_message: string; item_used: boolean | null; request_date: string | null; resolution_note: string }
export type ApprovalSnapshot = { caseId: string; caseVersion: string; captureId: string; amountMinor: number; currency: string; reviewHash: string }
