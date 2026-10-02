import { useState } from 'react'
import { FilePenLine, Plus, LoaderCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { merchantFacts } from '@/lib/refunds'
import type { CaseResolution, NewRequest, RefundCase } from '@/types'

const exampleConversation = `SYNTHETIC EXAMPLE — replace with your own customer conversation.

Customer: I'd like to return this. I bought it recently and expected a refund of $49.

Customer: I haven't used it. I only opened the box to see whether everything was there.

Customer, later: I tried it once for a few minutes, but it didn't suit me. Does that count as used?

Support: Please share the date you first requested the return. We need to check the policy and confirm the item's condition before deciding.`

function MerchantFactsFields({ condition, setCondition, date, setDate, busy, prefix }: {
  condition: string; setCondition: (value: string) => void; date: string; setDate: (value: string) => void; busy: boolean; prefix: string
}) {
  return <div className="grid gap-4 sm:grid-cols-2">
    <div className="space-y-2"><Label htmlFor={`${prefix}-condition`}>Item condition</Label><Select value={condition} onValueChange={setCondition} disabled={busy}><SelectTrigger id={`${prefix}-condition`} className="h-10 w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unknown">Not confirmed</SelectItem><SelectItem value="unused">Unused · merchant confirmed</SelectItem><SelectItem value="used">Used · merchant confirmed</SelectItem></SelectContent></Select></div>
    <div className="space-y-2"><Label htmlFor={`${prefix}-date`}>Request received (UTC)</Label><Input id={`${prefix}-date`} type="date" value={date} onChange={event => setDate(event.target.value)} max={new Date().toISOString().slice(0, 10)} className="h-10" aria-describedby={`${prefix}-date-help`} /><p id={`${prefix}-date-help`} className="text-xs text-muted-foreground">Leave blank if not confirmed.</p></div>
  </div>
}

export function NewRequestDialog({ busy, onCreate }: { busy: boolean; onCreate: (request: NewRequest) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  const [capture, setCapture] = useState('')
  const [message, setMessage] = useState('')
  const [condition, setCondition] = useState('unknown')
  const [date, setDate] = useState('')
  const [failed, setFailed] = useState(false)
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button disabled={busy} className="h-9"><Plus className="size-4" aria-hidden />New request</Button></DialogTrigger>
    <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl"><DialogHeader><DialogTitle>Bring a refund request into focus</DialogTitle><DialogDescription>Add the conversation and a PayPal sandbox capture. AI can identify what needs clarifying while merchant facts are still unknown.</DialogDescription></DialogHeader>
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy) return
        setFailed(false)
        const success = await onCreate({ capture_id: capture.trim(), customer_message: message.trim(), ...merchantFacts(condition, date) })
        if (success) { setOpen(false); setCapture(''); setMessage(''); setCondition('unknown'); setDate('') }
        else setFailed(true)
      }}>
        <fieldset disabled={busy} className="space-y-5 py-3">
          <div className="space-y-2"><Label htmlFor="capture-id">PayPal sandbox capture ID</Label><Input id="capture-id" value={capture} onChange={event => setCapture(event.target.value)} required pattern="[A-Za-z0-9]+" maxLength={64} autoComplete="off" spellCheck={false} className="h-10 font-mono" /><p className="text-xs text-muted-foreground">The server checks the payment and amount directly with PayPal.</p></div>
          <div className="space-y-2"><div className="flex flex-wrap items-center justify-between gap-2"><Label htmlFor="customer-message">Customer conversation</Label><Button type="button" variant="link" size="sm" onClick={() => setMessage(exampleConversation)}>Use synthetic example conversation</Button></div><Textarea id="customer-message" value={message} onChange={event => setMessage(event.target.value)} required maxLength={4000} rows={7} className="max-h-64 min-h-40 text-sm leading-relaxed" placeholder={'Paste the exchange, including who said what.\n\nSeparate messages with a blank line.'} /><div className="flex justify-between gap-3 text-xs text-muted-foreground"><span>Up to 12 paragraphs. Claims remain separate from confirmed facts.</span><span className="shrink-0 tabular-nums">{message.length}/4000</span></div></div>
          <div className="space-y-3 border-t pt-4"><p className="text-sm font-medium">Facts you can confirm <span className="font-normal text-muted-foreground">· optional now</span></p><MerchantFactsFields prefix="new" condition={condition} setCondition={setCondition} date={date} setDate={setDate} busy={busy} /><p className="text-xs leading-relaxed text-muted-foreground">A customer's statement does not confirm physical condition or the received date. You can resolve these after reviewing the evidence.</p></div>
          {failed && <p role="alert" className="text-sm text-destructive">The request could not be created. Check the error notification before trying again.</p>}
        </fieldset>
        <DialogFooter className="mt-3"><Button variant="outline" type="button" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={busy || !capture.trim() || !message.trim()} className="h-10">{busy && <LoaderCircle className="size-4 animate-spin" aria-hidden />}Fetch payment & create request</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}

export function ResolveRequestDialog({ record, busy, disabled, onResolve }: { record: RefundCase; busy: boolean; disabled: boolean; onResolve: (resolution: CaseResolution) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button variant="outline" disabled={busy || disabled} className="h-9"><FilePenLine className="size-4" aria-hidden />Resolve & update facts</Button></DialogTrigger><DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl"><DialogHeader><DialogTitle>Resolve the request's open questions</DialogTitle><DialogDescription>Confirm what you know and explain the change. Saving creates a new case revision. Its evidence must be analyzed again before approval.</DialogDescription></DialogHeader>{open && <ResolutionForm key={record.case_version} record={record} busy={busy} onResolve={onResolve} onClose={() => setOpen(false)} />}</DialogContent></Dialog>
}

function ResolutionForm({ record, busy, onResolve, onClose }: { record: RefundCase; busy: boolean; onResolve: (resolution: CaseResolution) => Promise<boolean>; onClose: () => void }) {
  const [message, setMessage] = useState(record.customer_message)
  const [condition, setCondition] = useState(record.item_used === false ? 'unused' : record.item_used === true ? 'used' : 'unknown')
  const [date, setDate] = useState(record.request_date || '')
  const [note, setNote] = useState('')
  const [failed, setFailed] = useState(false)
  return <form onSubmit={async event => {
    event.preventDefault()
    if (busy || !note.trim()) return
    setFailed(false)
    const saved = await onResolve({ expected_version: record.case_version, customer_message: message.trim(), ...merchantFacts(condition, date), resolution_note: note.trim() })
    if (saved) onClose(); else setFailed(true)
  }}><fieldset disabled={busy} className="space-y-5 py-3">
    <MerchantFactsFields prefix="resolve" condition={condition} setCondition={setCondition} date={date} setDate={setDate} busy={busy} />
    <div className="space-y-2"><Label htmlFor="resolution-message">Conversation, including any new replies</Label><Textarea id="resolution-message" value={message} onChange={event => setMessage(event.target.value)} required maxLength={4000} rows={6} className="max-h-64 min-h-36 text-sm leading-relaxed" /><p className="text-xs text-muted-foreground">Keep who said what. Separate messages with a blank line; up to 12 paragraphs.</p></div>
    <div className="space-y-2"><Label htmlFor="resolution-note">What did you confirm or correct?</Label><Textarea id="resolution-note" value={note} onChange={event => setNote(event.target.value)} required maxLength={2000} rows={3} placeholder="Explain how you confirmed these facts and how the open question was resolved." /><p className="text-xs text-muted-foreground">Your explanation is saved with this revision. Payment facts remain read-only.</p></div>
    {failed && <p role="alert" className="text-sm text-destructive">Changes were not confirmed. Close this dialog and reload the saved request before another update.</p>}
  </fieldset><DialogFooter className="mt-3"><Button type="button" variant="outline" onClick={onClose}>Cancel</Button><Button type="submit" disabled={busy || !note.trim() || !message.trim()} className="h-10">{busy && <LoaderCircle className="size-4 animate-spin" aria-hidden />}Save revision</Button></DialogFooter></form>
}
