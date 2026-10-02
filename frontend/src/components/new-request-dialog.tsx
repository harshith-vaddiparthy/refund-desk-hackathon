import { useState } from 'react'
import { Plus, LoaderCircle } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import type { NewRequest } from '@/types'

export function NewRequestDialog({ busy, onCreate }: { busy: boolean; onCreate: (request: NewRequest) => Promise<boolean> }) {
  const [open, setOpen] = useState(false)
  const [capture, setCapture] = useState('')
  const [message, setMessage] = useState('')
  const [condition, setCondition] = useState('unknown')
  const [date, setDate] = useState(new Date().toISOString().slice(0, 10))
  return <Dialog open={open} onOpenChange={setOpen}><DialogTrigger asChild><Button disabled={busy}><Plus className="size-4" aria-hidden />New request</Button></DialogTrigger>
    <DialogContent className="sm:max-w-lg"><DialogHeader><DialogTitle>New refund request</DialogTitle><DialogDescription>Start with a captured PayPal sandbox payment. Its amount and payment date will be fetched from PayPal.</DialogDescription></DialogHeader>
      <form onSubmit={async event => {
        event.preventDefault()
        if (busy || condition === 'unknown') return
        const success = await onCreate({ capture_id: capture.trim(), customer_message: message.trim(), item_used: condition === 'used', request_date: date })
        if (success) { setOpen(false); setCapture(''); setMessage(''); setCondition('unknown'); setDate(new Date().toISOString().slice(0, 10)) }
      }}>
        <fieldset disabled={busy} className="space-y-5 py-3">
          <div className="space-y-2"><Label htmlFor="capture-id">Sandbox capture ID</Label><Input id="capture-id" value={capture} onChange={event => setCapture(event.target.value)} required pattern="[A-Za-z0-9]+" maxLength={64} autoComplete="off" spellCheck={false} className="font-mono" /><p className="text-xs text-muted-foreground">Use the capture ID, not the checkout order ID.</p></div>
          <div className="space-y-2"><Label htmlFor="customer-message">Customer message</Label><Textarea id="customer-message" value={message} onChange={event => setMessage(event.target.value)} required maxLength={4000} rows={4} placeholder="What is the customer asking for?" /></div>
          <div className="grid gap-5 sm:grid-cols-2"><div className="space-y-2"><Label htmlFor="item-condition">Item condition</Label><Select value={condition} onValueChange={setCondition} disabled={busy}><SelectTrigger id="item-condition" className="w-full"><SelectValue /></SelectTrigger><SelectContent><SelectItem value="unknown">Not confirmed</SelectItem><SelectItem value="unused">Unused · confirmed</SelectItem><SelectItem value="used">Used · confirmed</SelectItem></SelectContent></Select></div><div className="space-y-2"><Label htmlFor="request-date">Received date (UTC)</Label><Input id="request-date" type="date" value={date} onChange={event => setDate(event.target.value)} required max={new Date().toISOString().slice(0, 10)} /></div></div>
          <p className="rounded-md bg-muted px-3 py-2.5 text-xs leading-relaxed text-muted-foreground">The item condition is your statement as the merchant. Confirm it before creating a case. Creating a request does not submit a refund.</p>
        </fieldset>
        <DialogFooter className="mt-3"><Button variant="outline" type="button" onClick={() => setOpen(false)}>Cancel</Button><Button type="submit" disabled={busy || condition === 'unknown' || !capture.trim() || !message.trim()}>{busy && <LoaderCircle className="size-4 animate-spin" aria-hidden />}Fetch payment & create</Button></DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
}
