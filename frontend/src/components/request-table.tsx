import { useState } from 'react'
import { ArrowUpRight, Search, ReceiptText } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { isAttention, label, money, timestamp } from '@/lib/refunds'
import type { RefundCase } from '@/types'

export function StatusBadge({ state }: { state: string }) {
  const color = state === 'verified' ? 'border-emerald-200 bg-emerald-50 text-emerald-800' : ['uncertain', 'failed'].includes(state)
    ? 'border-amber-200 bg-amber-50 text-amber-900' : state === 'review_ready' ? 'border-blue-200 bg-blue-50 text-blue-800' : 'bg-muted text-muted-foreground'
  return <Badge variant="outline" className={`whitespace-nowrap font-medium ${color}`}>{label(state)}</Badge>
}

export function RequestTable({ records, busy, onOpen, searchable = true }: { records: RefundCase[]; busy: boolean; onOpen: (record: RefundCase) => void; searchable?: boolean }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const visible = records.filter(record => `${record.capture_id} ${record.customer_message}`.toLowerCase().includes(query.toLowerCase())
    && (filter === 'all' || (filter === 'review' && record.can_review) || (filter === 'ready' && record.can_approve)
      || (filter === 'attention' && isAttention(record)) || (filter === 'verified' && record.operation?.state === 'verified')))
  return <>
    {searchable && <div className="flex flex-col gap-3 border-b px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
      <div className="relative w-full sm:max-w-sm"><Search className="pointer-events-none absolute top-2.5 left-3 size-4 text-muted-foreground" aria-hidden /><Input aria-label="Search refund requests" className="h-9 pl-9" placeholder="Search capture ID or customer message" value={query} onChange={event => setQuery(event.target.value)} /></div>
      <Select value={filter} onValueChange={setFilter}><SelectTrigger className="w-full sm:w-48" aria-label="Filter requests by status"><SelectValue /></SelectTrigger><SelectContent>
        <SelectItem value="all">All statuses</SelectItem><SelectItem value="review">Needs assessment</SelectItem><SelectItem value="ready">Ready for approval</SelectItem><SelectItem value="attention">Needs attention</SelectItem><SelectItem value="verified">Refund verified</SelectItem>
      </SelectContent></Select>
    </div>}
    <Table><TableHeader><TableRow><TableHead className="pl-5">Request</TableHead><TableHead>Amount</TableHead><TableHead>Status</TableHead><TableHead className="hidden lg:table-cell">Received</TableHead><TableHead className="pr-5 text-right"><span className="sr-only">Open request</span></TableHead></TableRow></TableHeader>
      <TableBody>{visible.map(record => <TableRow key={record.id}>
        <TableCell className="max-w-56 py-4 pl-5"><div className="font-mono text-xs font-medium">{record.capture_id}</div><div className="mt-1 max-w-60 truncate text-xs text-muted-foreground">{record.customer_message}</div></TableCell>
        <TableCell className="font-medium tabular-nums">{money(record.amount_minor, record.currency)}<span className="mt-1 block text-[10px] font-normal text-muted-foreground">USD · sandbox</span></TableCell>
        <TableCell><StatusBadge state={record.state} />{record.transaction_provenance !== 'paypal_sandbox' && <span className="mt-1 block text-[10px] text-muted-foreground">Synthetic evidence</span>}</TableCell>
        <TableCell className="hidden text-xs text-muted-foreground lg:table-cell">{record.request_date}<span className="sr-only"> UTC</span></TableCell>
        <TableCell className="pr-5 text-right"><Button variant="ghost" size="sm" onClick={() => onOpen(record)} disabled={busy} aria-label={`Open request for capture ${record.capture_id}`}>Open<ArrowUpRight className="size-3.5" aria-hidden /></Button></TableCell>
      </TableRow>)}</TableBody>
    </Table>
    {!visible.length && <div className="flex flex-col items-center px-6 py-14 text-center"><ReceiptText className="mb-3 size-7 text-muted-foreground/60" aria-hidden /><h3 className="text-sm font-medium">{records.length ? 'No matching requests' : 'No refund requests yet'}</h3><p className="mt-1 max-w-sm text-sm text-muted-foreground">{records.length ? 'Try another search or status filter.' : 'Create a request from a captured PayPal sandbox payment to begin.'}</p></div>}
    <div className="flex items-center justify-between border-t px-5 py-3 text-xs text-muted-foreground"><span>{visible.length} of {records.length} requests</span><span>Saved case records</span></div>
  </>
}

export function ActivityList({ records, limit }: { records: RefundCase[]; limit?: number }) {
  const events = records.flatMap(record => record.events.map((event, index) => ({ ...event, captureId: record.capture_id, key: `${record.id}-${index}` })))
    .sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
  const visible = limit ? events.slice(0, limit) : events
  if (!visible.length) return <p className="px-5 py-10 text-center text-sm text-muted-foreground">Activity appears as requests are created, assessed, and resolved.</p>
  return <ol className="divide-y">{visible.map(event => <li key={event.key} className="flex gap-3 px-5 py-4"><span className="mt-1.5 size-2 shrink-0 rounded-full bg-primary/60" aria-hidden /><div className="min-w-0 flex-1"><p className="text-sm font-medium">{label(event.kind)}</p><p className="mt-1 truncate font-mono text-[11px] text-muted-foreground">{event.captureId}</p>{event.to_state && <p className="mt-1 text-xs text-muted-foreground">{event.from_state ? `${label(event.from_state)} → ` : ''}{label(event.to_state)}</p>}</div><time className="shrink-0 text-right text-[11px] text-muted-foreground" dateTime={event.occurred_at}>{timestamp(event.occurred_at)}</time></li>)}</ol>
}
