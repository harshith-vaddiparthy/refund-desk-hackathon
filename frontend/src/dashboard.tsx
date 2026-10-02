import { useEffect, useRef, useState } from 'react'
import { ArrowDownLeft, ArrowUpRight, FileText, History, LayoutDashboard, LoaderCircle, LockKeyhole, ReceiptText, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react'
import { toast } from 'sonner'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Separator } from '@/components/ui/separator'
import { Sidebar, SidebarContent, SidebarFooter, SidebarGroup, SidebarGroupContent, SidebarGroupLabel, SidebarHeader, SidebarInset, SidebarMenu, SidebarMenuButton, SidebarMenuItem, SidebarProvider, SidebarTrigger, useSidebar } from '@/components/ui/sidebar'
import { Skeleton } from '@/components/ui/skeleton'
import { Toaster } from '@/components/ui/sonner'
import { CaseDetail } from '@/components/case-detail'
import { NewRequestDialog } from '@/components/new-request-dialog'
import { ActivityList, RequestTable } from '@/components/request-table'
import { ApiError, approveCase, createCase, getCase, getPolicy, initializeSession, listCases, refreshCase, resolveCase, reviewCase } from '@/lib/api'
import { money, snapshotMatches, totals } from '@/lib/refunds'
import type { ApprovalSnapshot, CaseResolution, NewRequest, Policy, RefundCase, Session } from '@/types'

type Page = 'overview' | 'requests' | 'policy' | 'activity'
const navigation = [
  { id: 'overview' as const, label: 'Overview', icon: LayoutDashboard },
  { id: 'requests' as const, label: 'Refund requests', icon: ReceiptText },
  { id: 'policy' as const, label: 'Policy', icon: FileText },
  { id: 'activity' as const, label: 'Activity', icon: History },
]

function WorkspaceSidebar({ page, onNavigate, busy, count }: { page: Page; onNavigate: (page: Page) => void; busy: boolean; count: number }) {
  const { setOpenMobile } = useSidebar()
  return <Sidebar collapsible="offcanvas"><SidebarHeader className="px-5 py-6"><div className="flex items-center gap-3"><div className="flex size-9 items-center justify-center rounded-xl bg-primary text-primary-foreground"><ArrowDownLeft className="size-5" aria-hidden /></div><div><p className="text-base font-semibold tracking-tight">Refund Desk</p><p className="text-[11px] text-muted-foreground">Merchant workspace</p></div></div></SidebarHeader><SidebarContent><SidebarGroup className="px-3"><SidebarGroupLabel className="px-3 text-[10px] font-medium uppercase tracking-widest">Workspace</SidebarGroupLabel><SidebarGroupContent><SidebarMenu className="gap-1">{navigation.map(item => <SidebarMenuItem key={item.id}><SidebarMenuButton isActive={page === item.id} disabled={busy} onClick={() => { onNavigate(item.id); setOpenMobile(false) }} className="h-10 rounded-lg px-3"><item.icon className="size-4" aria-hidden /><span>{item.label}</span>{item.id === 'requests' && <span className="ml-auto rounded bg-background px-1.5 py-0.5 text-[10px] tabular-nums text-muted-foreground">{count}</span>}</SidebarMenuButton></SidebarMenuItem>)}</SidebarMenu></SidebarGroupContent></SidebarGroup></SidebarContent><SidebarFooter className="gap-3 border-t p-5"><div className="flex items-center gap-2 text-xs font-medium"><ShieldCheck className="size-4 text-emerald-700" aria-hidden />PayPal sandbox only</div><p className="text-[11px] leading-relaxed text-muted-foreground">Review with evidence. Approve deliberately. Verify every result.</p></SidebarFooter></Sidebar>
}

export default function Dashboard() {
  const [session, setSession] = useState<Session | null>(null)
  const [records, setRecords] = useState<RefundCase[]>([])
  const [policy, setPolicy] = useState<Policy | null>(null)
  const [selected, setSelected] = useState<RefundCase | null>(null)
  const [page, setPage] = useState<Page>('requests')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [reviewStartedAt, setReviewStartedAt] = useState<number | null>(null)
  const [blocked, setBlocked] = useState<Set<string>>(new Set())
  const busyRef = useRef(false)
  const contentRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let mounted = true
    initializeSession().then(async next => {
      const [cases, currentPolicy] = await Promise.all([listCases(), getPolicy()])
      if (mounted) { setSession(next); setRecords(cases.cases); setPolicy(currentPolicy.policy) }
    }).catch(reason => { if (mounted) setError(reason instanceof Error ? reason.message : 'The local session could not be opened.') })
      .finally(() => { if (mounted) setLoading(false) })
    return () => { mounted = false }
  }, [])

  useEffect(() => { contentRef.current?.focus() }, [page, selected?.id])

  function replaceRecord(record: RefundCase) {
    setRecords(current => current.some(item => item.id === record.id) ? current.map(item => item.id === record.id ? record : item) : [record, ...current])
    setSelected(current => current?.id === record.id ? record : current)
  }

  async function perform(message: string, work: () => Promise<void>, uncertainCase?: string) {
    if (busyRef.current) return false
    busyRef.current = true; setBusy(message); setError('')
    try { await work(); return true }
    catch (reason) {
      const message = reason instanceof Error ? reason.message : 'The operation could not be completed.'
      if (uncertainCase) setBlocked(current => new Set(current).add(uncertainCase))
      if (reason instanceof ApiError && reason.status === 401) { setSession(null); setRecords([]); setSelected(null) }
      setError(message); toast.error(message)
      return false
    } finally { busyRef.current = false; setBusy('') }
  }

  function openRecord(record: RefundCase) {
    void perform('Loading the saved request…', async () => {
      const result = await getCase(record.id); replaceRecord(result.case); setSelected(result.case); setPage('requests')
      setBlocked(current => { const next = new Set(current); next.delete(record.id); return next })
    })
  }

  function reload() {
    void perform('Reloading saved workspace records…', async () => {
      const [result, currentPolicy, currentSession] = await Promise.all([listCases(), getPolicy(), initializeSession()]); setRecords(result.cases); setPolicy(currentPolicy.policy); setSession(currentSession)
      if (selected) {
        const refreshed = await getCase(selected.id); setSelected(refreshed.case); replaceRecord(refreshed.case)
        setBlocked(current => { const next = new Set(current); next.delete(selected.id); return next })
      }
    })
  }

  async function newRequest(request: NewRequest) {
    return perform('Fetching the sandbox payment and creating a review request…', async () => {
      const result = await createCase(request); replaceRecord(result.case); setSelected(result.case); setPage('requests'); toast.success('Refund request created. No refund was submitted.')
    })
  }

  async function approve(snapshot: ApprovalSnapshot) {
    if (!selected || !snapshotMatches(snapshot, selected) || blocked.has(snapshot.caseId)) {
      setError('This approval no longer matches the saved request. Reload it before continuing.'); return
    }
    await perform('Recording approval and submitting one sandbox refund. Do not repeat this action…', async () => {
      const result = await approveCase(snapshot.caseId, snapshot.reviewHash); replaceRecord(result.case)
      toast.info('Refund operation recorded. Inspect the saved result and status verification.')
    }, snapshot.caseId)
  }

  async function analyze() {
    if (!selected || busyRef.current) return
    if (!session?.ai_runtime) { setError('Reload the workspace to confirm the AI provider before analysis.'); return }
    setReviewStartedAt(Date.now())
    try {
      await perform('Analyzing the saved conversation…', async () => {
        const result = await reviewCase(selected.id); replaceRecord(result.case)
        const status = result.case.latest_review?.status
        toast.info(status === 'REVIEW_NEEDS_INFORMATION' ? 'Analysis complete. Resolve the open questions to continue.' : status === 'REVIEW_READY' ? 'Analysis saved. Review the evidence and recommendation.' : 'Analysis incomplete. Inspect the validation result.')
      }, selected.id)
    } finally { setReviewStartedAt(null) }
  }

  async function resolve(resolution: CaseResolution) {
    if (!selected || blocked.has(selected.id)) return false
    return perform('Saving the updated case revision…', async () => {
      const result = await resolveCase(selected.id, resolution); replaceRecord(result.case)
      toast.success(result.case.case_version === resolution.expected_version ? 'No changes to the saved revision.' : 'Revision saved. Analyze the updated conversation to continue.')
    }, selected.id)
  }

  const metrics = totals(records)
  const pageTitle = navigation.find(item => item.id === page)!.label
  return <SidebarProvider><a href="#dashboard-content" className="sr-only z-50 rounded bg-primary px-4 py-2 text-primary-foreground focus:not-sr-only focus:fixed focus:top-3 focus:left-3">Skip to workspace</a><WorkspaceSidebar page={page} onNavigate={next => { setPage(next); setSelected(null) }} busy={Boolean(busy)} count={records.length} /><SidebarInset className="min-w-0 bg-[#f7f9fb]">
    <header className="flex min-h-16 shrink-0 items-center justify-between gap-3 border-b bg-background px-4 md:px-7"><div className="flex min-w-0 items-center gap-3"><SidebarTrigger aria-label="Toggle navigation" /><Separator orientation="vertical" className="h-4!" /><span className="truncate text-sm text-muted-foreground">Workspace<span className="mx-2 text-border">/</span><span className="font-medium text-foreground">{pageTitle}</span></span></div><div className="flex shrink-0 items-center gap-2"><Badge variant="outline" className="border-emerald-200 bg-emerald-50 text-emerald-800">Sandbox</Badge>{session?.approval_mode === 'test_operator' && <Badge variant="outline" className="border-amber-200 bg-amber-50 text-amber-900">Test operator</Badge>}<span className="hidden text-xs text-muted-foreground lg:block">Private session</span></div></header>
    <div id="dashboard-content" ref={contentRef} tabIndex={-1} className="mx-auto w-full max-w-[1500px] space-y-6 p-4 outline-none md:p-7 lg:p-9" aria-busy={Boolean(busy) || loading}>
      {busy && reviewStartedAt === null && <Alert className="border-blue-200 bg-blue-50 text-blue-950"><LoaderCircle className="animate-spin" /><AlertTitle>Operation in progress</AlertTitle><AlertDescription className="text-blue-900" role="status">{busy}</AlertDescription></Alert>}
      {error && session && <Alert variant="destructive"><TriangleAlert /><AlertTitle>Action needs attention</AlertTitle><AlertDescription>{error}</AlertDescription></Alert>}
      {loading ? <div className="space-y-6"><Skeleton className="h-9 w-52" /><div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{[0, 1, 2, 3].map(item => <Skeleton key={item} className="h-32 rounded-xl" />)}</div><Skeleton className="h-80 rounded-xl" /></div> : !session ? <Card className="mx-auto mt-14 max-w-lg"><CardHeader><LockKeyhole className="mb-3 size-8 text-muted-foreground" aria-hidden /><CardTitle>Open your private session</CardTitle><CardDescription>The server saves a private launch URL in <code className="text-xs">browser-session.json</code> inside your data directory.</CardDescription></CardHeader><CardContent><p className="text-sm leading-relaxed text-muted-foreground">Open the latest link after a server restart. It grants access to this local sandbox workspace.</p>{error && <Alert variant="destructive" className="mt-5"><AlertDescription>{error}</AlertDescription></Alert>}</CardContent></Card> : <>
        {selected && page === 'requests' ? <CaseDetail key={`${selected.id}-${selected.case_version}`} record={selected} session={session} busy={Boolean(busy)} blocked={blocked.has(selected.id)} reviewStartedAt={reviewStartedAt} onBack={() => setSelected(null)} onReload={reload}
          onReview={() => { void analyze() }} onResolve={resolve}
          onApprove={approve} onRefresh={() => { void perform('Reading the existing refund from PayPal sandbox. No new refund is submitted…', async () => { const result = await refreshCase(selected.id); replaceRecord(result.case); toast.info('Status check finished. Inspect the saved refund outcome.') }) }} /> : <>
          <div className="flex flex-col justify-between gap-4 sm:flex-row sm:items-center"><div><h1 className="text-2xl font-semibold tracking-tight">{page === 'overview' ? 'Refund overview' : pageTitle}</h1><p className="mt-1.5 text-sm text-muted-foreground">{page === 'overview' ? 'A clear view of your requests, decisions, and verified results.' : page === 'requests' ? 'Turn customer conversations into sourced decisions and clear next steps.' : page === 'policy' ? 'The active server policy and its exact decision clauses.' : 'An append-only record of what happened across this workspace.'}</p></div><div className="flex items-center gap-2"><Button variant="outline" size="icon" aria-label="Reload workspace records" onClick={reload} disabled={Boolean(busy)}><RefreshCw className="size-4" aria-hidden /></Button>{['overview', 'requests'].includes(page) && <NewRequestDialog busy={Boolean(busy)} onCreate={newRequest} />}</div></div>
          {page === 'overview' && <>
            <dl className="grid divide-y rounded-xl border bg-background sm:grid-cols-2 sm:divide-y-0 xl:grid-cols-4">{[
              { title: 'Needs analysis', value: String(metrics.toReview), description: 'Unanalyzed or incomplete requests' },
              { title: 'Needs information', value: String(records.filter(record => record.state === 'needs_information').length), description: 'Questions ready for merchant resolution' },
              { title: 'Ready for approval', value: String(records.filter(record => record.can_approve).length), description: 'Review evidence before deciding' },
              { title: 'Verified refunds', value: money(metrics.refundedMinor), description: `${metrics.verified} independently verified sandbox operations` },
            ].map(metric => <div key={metric.title} className="px-5 py-5"><dt className="text-xs font-medium text-muted-foreground">{metric.title}</dt><dd className="mt-2 text-xl font-semibold tabular-nums">{metric.value}</dd><p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{metric.description}</p></div>)}</dl>
            <div className="grid items-start gap-6 2xl:grid-cols-[minmax(0,1fr)_340px]"><Card className="min-w-0 gap-0 overflow-hidden py-0"><CardHeader className="flex flex-row items-center justify-between border-b px-5 py-5"><div><CardTitle>Recent requests</CardTitle><CardDescription className="mt-1">Review the latest saved cases.</CardDescription></div><Button variant="ghost" size="sm" onClick={() => setPage('requests')} disabled={Boolean(busy)}>View all<ArrowUpRight className="size-3.5" aria-hidden /></Button></CardHeader><RequestTable records={records.slice(0, 6)} busy={Boolean(busy)} onOpen={openRecord} searchable={false} /></Card><Card className="gap-0 overflow-hidden py-0"><CardHeader className="border-b px-5 py-5"><CardTitle>Recent activity</CardTitle><CardDescription>Actual saved events.</CardDescription></CardHeader><ActivityList records={records} limit={4} /></Card></div>
            <p className="flex items-start gap-2 text-xs leading-relaxed text-muted-foreground"><ShieldCheck className="size-4 shrink-0" aria-hidden />Refund totals include only independently verified PayPal sandbox operations in this workspace. Pending operations and synthetic test evidence are excluded.</p>
          </>}
          {page === 'requests' && <Card className="gap-0 overflow-hidden py-0"><RequestTable records={records} busy={Boolean(busy)} onOpen={openRecord} /></Card>}
          {page === 'policy' && policy && <div className="grid items-start gap-6 xl:grid-cols-[minmax(0,1fr)_300px]"><Card><CardHeader><div className="flex flex-wrap items-center gap-2"><CardTitle>Refund policy</CardTitle><Badge variant="secondary">Active server policy</Badge></div><CardDescription className="break-all font-mono text-xs">{policy.version}</CardDescription></CardHeader><CardContent className="divide-y">{Object.entries(policy.clauses).map(([id, quote]) => <div key={id} className="flex gap-4 py-5 first:pt-0"><Badge variant="outline" className="mt-0.5 h-fit shrink-0 font-mono text-xs">{id}</Badge><p className="text-sm leading-7">{quote}</p></div>)}</CardContent></Card><Card><CardHeader><ShieldCheck className="mb-1 size-5 text-primary" aria-hidden /><CardTitle>Every decision keeps its evidence</CardTitle></CardHeader><CardContent><p className="text-sm leading-relaxed text-muted-foreground">Each request retains its policy snapshot. AI citations must match the saved clause exactly. The merchant still approves any refund.</p><Separator className="my-4" /><p className="text-xs leading-relaxed text-muted-foreground">This page is read-only. Changing a policy cannot rewrite the history of an approved request.</p></CardContent></Card></div>}
          {page === 'activity' && <Card className="gap-0 overflow-hidden py-0"><CardHeader className="border-b py-5"><CardTitle>Workspace activity</CardTitle><CardDescription>Request creation, AI assessments, approval attribution, and refund verification.</CardDescription></CardHeader><ActivityList records={records} /></Card>}
        </>}
      </>}
    </div>
  </SidebarInset><Toaster theme="light" richColors closeButton position="bottom-right" /></SidebarProvider>
}
