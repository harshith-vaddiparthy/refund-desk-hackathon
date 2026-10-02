# Refund Desk

A merchant evidence desk for PayPal sandbox refund decisions. Paste a messy
customer conversation, inspect source-backed claims and policy quotations,
resolve missing or conflicting facts, review an unsent response draft, then
explicitly approve an exact refund and independently verify its result.

The AI works on the conversation before every merchant fact is known. It
selects sourced findings, identifies disagreements and asks concrete questions,
assigning each question to the customer or merchant. The application composes
unsent replies from customer-owned questions and saved payment state; it does
not ask the model to write financial-status prose. Customer statements remain
claims; item condition and request date require merchant confirmation. PayPal supplies the payment facts. The payment rule stays narrow:
one merchant, unused items requested within 30 days, full USD refunds and
**sandbox only**.

**The v2 conversation-to-refund workflow is verified in PayPal sandbox.** It
completed two immutable case revisions, two actual Groq reviews and one exact
USD39 refund with independent readback. This is the fifth distinct USD39
sandbox refund in the project, including four earlier v1 proofs.

[Demo and submitted entry](https://devpost.com/software/refund-desk). V2
publication and the updated video/entry are still pending; that page currently
contains the submitted v1 presentation. The verified v2 behavior and its
remaining evaluation limits are documented below and in the
[inspectable evidence pack](benchmarks/v2/README.md).

## Requirements and source

- Linux or macOS, Python **3.11+**, Git and a browser. Linux/Python 3.12 is tested;
  a native macOS backend run has not been established.
- **Node.js 22.22.2 and npm** to build the React/TypeScript/Vite/shadcn interface.
  Python serves the compiled UI and API together; no Node server runs afterward.
- Your own free PayPal Developer sandbox app, Business merchant and separate
  Personal buyer account.
- One explicitly selected AI route: **Groq Free / openai/gpt-oss-120b**, or the
  installed local **Ollama qwen3:4b** route. Local remains the CLI default, but
  its richer v2 pilot failed semantic checks; it is not an equivalent validated
  alternative to the Groq route used in the v2 rehearsal.

No paid account, paid hosting, Docker or pip packages are required. Groq requires
an account and a private API key; its Free quota can reject requests. The app
never upgrades a plan, purchases capacity or switches providers automatically.
Local computation still consumes machine resources.

The public source location is:

```bash
git clone https://github.com/harshith-vaddiparthy/refund-desk-hackathon.git
cd refund-desk-hackathon
python3 --version
node --version
npm --version
```

Until v2 is released, public main remains the submitted v1. The v2 instructions
below apply to this isolated revision; cloning public main is not evidence that
v2 has been published.

On Ubuntu 24.04/Debian 12 or newer, install missing Python/Git as a user setup step:

```bash
sudo apt-get update
sudo apt-get install -y python3 git
```

On macOS, install Python 3.12 from [python.org](https://www.python.org/downloads/macos/)
and Git using `xcode-select --install` if needed. Install Node.js 22.22.2 for your
OS/architecture from the [official downloads](https://nodejs.org/download/release/v22.22.2/).

## 1. Build the shadcn dashboard

From the repository root, install the locked frontend dependencies and build:

```bash
cd frontend
npm ci
npm run build
cd ..
```

Complete this **before starting `python3 app.py`**. The CLI defaults to
`frontend/dist` and fails with a build instruction if that directory is missing
or unsafe; no frontend flag or legacy fallback is needed. `frontend/dist` and
`frontend/node_modules` are ignored by Git and recreated locally. After a later
frontend rebuild, restart the Python app and open its newly written private
launch link. There is no separate Vite/Node runtime server to keep running.

## 2. Configure your sandbox app privately

Sign in to the [PayPal Developer dashboard](https://developer.paypal.com/dashboard/).
Use its **Sandbox** environment to create/select a Business merchant, a Personal
buyer and a REST app attached to that merchant. Obtain the app's client ID and
secret. Buyer approval below uses the Personal sandbox account.

Keep the credential file outside the repository:

```bash
mkdir -p "$HOME/.config/refund-desk"
chmod 700 "$HOME/.config/refund-desk"
umask 077
touch "$HOME/.config/refund-desk/sandbox-app.json"
chmod 600 "$HOME/.config/refund-desk/sandbox-app.json"
```

Open that file in your own editor and enter this structure with your values:

```json
{
  "environment": "sandbox",
  "client_id": "YOUR_SANDBOX_APP_CLIENT_ID",
  "client_secret": "YOUR_SANDBOX_APP_CLIENT_SECRET"
}
```

The application rejects credential files readable by other users. Never put keys,
raw API receipts or browser-session files in public GitHub, screenshots or a demo
recording. The browser never receives the PayPal client secret.

## 3. Choose the AI runtime explicitly

### Groq Free — hosted v2 route

Sign in to the [Groq console](https://console.groq.com/), confirm that your account
is on **Free**, and create a dedicated API key. No billing upgrade is needed.
If you set an expiry, keep the key valid for your intended test/judging period.
This project uses only `openai/gpt-oss-120b` at Groq's fixed API endpoint, with
`medium` reasoning effort, temperature 0 and a 2500-token completion limit.
Those settings are configured by the application; the browser cannot change
them or choose another provider. Hidden reasoning text is not requested or
displayed, although the provider reports numeric reasoning-token usage.

Run this in an interactive terminal to write the key privately without placing
it in shell history. It refuses to overwrite an existing file:

```bash
python3 - <<'PYKEY'
from pathlib import Path
import getpass, json, os
os.umask(0o077)
directory = Path.home() / '.config/refund-desk'
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
directory.chmod(0o700)
path = directory / 'groq-v2.json'
record = {'provider': 'groq', 'model': 'openai/gpt-oss-120b',
          'verified_plan': 'Free', 'api_key': getpass.getpass('Groq API key: ')}
with path.open('x') as output:
    json.dump(record, output)
    output.write('\n')
print('Private configuration saved to', path)
PYKEY
```

`verified_plan` records your console check; it is not a live billing attestation
by the app. Keep the key outside the repository and never include it in a
screenshot, conversation or submission. The client rejects unsafe file
permissions, an unsupported model/provider and an expired `expires_date` when
that optional field is present.

**Data disclosure:** selecting Groq sends the supplied conversation paragraphs,
policy, merchant confirmations/resolution note and payment summary (currency,
amount, purchase date and known prior-refund total) to Groq. The request does not
inject the configured merchant email, PayPal capture ID or credentials. Personal
information pasted into the conversation is still part of that conversation;
use explicitly synthetic or suitably anonymized material for the demo. The UI
shows hosted-provider disclosure before analysis. Draft replies are not sent.

### Local Ollama — experimental for the richer v2 task

The local route makes no hosted inference request and needs no cloud account.
However, the richer conversation pilot produced incorrect conflicts and date
reasoning. Do not present it as a validated drop-in route for v2 merely because
it completed the simpler v1 examples.

Install Ollama from its [official download page](https://ollama.com/download).
Its macOS app starts the local server. On Linux, follow the official installation
instructions; if no server is running, use `ollama serve` in another terminal.
Download the model yourself:

```bash
ollama pull qwen3:4b
```

The application requires this exact digest at `http://127.0.0.1:11434`:

```text
359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7
```

The recorded local runtime is Ollama 0.16.2. The tag can change upstream; a
different digest is not a verified substitute. The app does not pull/unload
models, change Ollama or interrupt another workload. It refuses inference when
that shared runtime is busy.

## 4. Create and approve one sandbox purchase

The helper creates a USD 39.00 order. Choose a **new** state directory and use the
email of the sandbox Business account attached to your app:

```bash
python3 sandbox_capture.py create \
  --credentials "$HOME/.config/refund-desk/sandbox-app.json" \
  --state-dir "$HOME/.local/share/refund-desk/purchase-one" \
  --merchant-email 'YOUR_SANDBOX_BUSINESS_EMAIL'
```

It prints the location of `approval-url.txt`. Open its saved URL in your browser,
then approve with your sandbox **Personal buyer**:

```bash
python3 - <<'PYOPEN'
from pathlib import Path
import webbrowser
path = Path.home() / '.local/share/refund-desk/purchase-one/approval-url.txt'
webbrowser.open(path.read_text().strip())
PYOPEN
```

The helper's default return URL is
`http://127.0.0.1:8749/checkout-return`. When the app is already running, this
read-only page explains the next capture step. On a first setup the app may be
started after capture, so the browser return can temporarily be unavailable.
That page does not capture or refund anything; the next command independently
checks PayPal's buyer-approval state.

After buyer approval, use the **same** state directory:

```bash
python3 sandbox_capture.py capture \
  --credentials "$HOME/.config/refund-desk/sandbox-app.json" \
  --state-dir "$HOME/.local/share/refund-desk/purchase-one"
```

Success prints `capture_id`, `merchant_id`, `merchant_email`, USD 39.00 and
`stage: capture_verified`. Use those returned identifiers below. The helper
checks the order, amount and merchant before capturing, then independently reads
the capture back. Interrupted/uncertain payment phases do not automatically
repeat POST requests; retain their private `purchase.json` for reconciliation.

## 5. Start the merchant app

Use the merchant ID/email returned by the independently verified capture. For
the explicit Groq route:

```bash
python3 app.py \
  --client-file "$HOME/.config/refund-desk/sandbox-app.json" \
  --merchant-id 'MERCHANT_ID_FROM_CAPTURE' \
  --merchant-email 'MERCHANT_EMAIL_FROM_CAPTURE' \
  --data-dir "$HOME/.local/share/refund-desk/v2-app" \
  --port 8749 \
  --ai-provider groq \
  --groq-client-file "$HOME/.config/refund-desk/groq-v2.json"
```

For the experimental local route, use the same command with `--ai-provider local`
and omit `--groq-client-file`. Omitting both flags also selects local; it does not
choose Groq based on a key's presence. Groq without its file, or a Groq file
without explicit Groq selection, is rejected. There is no provider selection or
secret-setting endpoint in the browser.

Use a separate v2 data directory while validating this revision. Existing v1
snapshots and finished reviews remain readable; pre-approval changes create
append-only revisions. Do not point a development process at the submitted
demo's working database.

The server binds to loopback and saves its private launch link locally:

```bash
python3 - <<'PYOPEN'
from pathlib import Path
import json, webbrowser
path = Path.home() / '.local/share/refund-desk/v2-app/browser-session.json'
webbrowser.open(json.loads(path.read_text())['url'])
PYOPEN
```

Open the newly written link after each restart. The bare URL does not grant
access, and the browser never receives provider keys or their file paths. If
8749 is occupied, choose another port and use it in the helper's `--return-url`
for the next purchase. Restart Python after rebuilding frontend assets.

In the application:

1. Open a request with its capture ID and a conversation. Separate source
   paragraphs with blank lines; use speaker labels in the supplied text. Leave
   item use or request date **unknown** when unconfirmed. A case does not
   silently become unused or receive today's date.
2. Check the configured provider/disclosure, then Analyze. Inspect the selected
   full source paragraphs, claim kinds, policy quotations, conflicts and concrete
   questions and who should answer them. Exact quotations establish source
   references, not semantic truth.
3. Use Resolve to correct the conversation or confirm merchant facts, with an
   explanation. The new immutable revision invalidates the old analysis. Run a
   new analysis explicitly; unchanged completed work is not automatically rerun.
4. Review the explicitly unsent reply composed by the application. Customer-owned
   AI questions may be included; merchant-only checks stay internal. Neutral
   pending-decision text and financial-status text come from application rules
   and saved payment state. Copy/edit the reply if useful; the app never emails
   or messages the customer. No model-written financial-status field is used.
5. Only a current, complete refund recommendation with valid evidence, no
   unresolved issues and eligible merchant-confirmed facts can reach approval.
   Review the exact USD amount and check the confirmation. The server re-fetches
   PayPal facts before the durable claim and refund POST.
6. Inspect the independent result and audit. `pending`/`uncertain` are not
   `verified`. Verification requires a separate matching PayPal refund GET.

`REVIEW_NEEDS_INFORMATION` is useful completed analysis requiring resolution;
invalid/truncated/timed-out output is `REVIEW_INCOMPLETE`. Neither enables
payment. A supported outside-window decline remains non-payable even if it also
mentions an issue. Source and merchant-fact edits are locked once any payment
operation exists, including prepared or uncertain operations.

For automated QA, add `--test-operator 'Automated sandbox QA'`. This trusted
server setting records `test_operator`, not `human_ui`. The browser cannot choose
approval provenance or submit an amount. Saved reviews retain their original
runtime labels when the startup provider changes; a new approval then requires
explicit analysis under the newly selected route. An already prepared approval
can resume against its original supported report and exact immutable snapshot.

## Verification and evidence

Run the offline checks without real PayPal/Groq credentials or an Ollama server:

```bash
python3 -m unittest discover -v
cd frontend
npm run lint
npm test
npm run build
cd ..
```

The verified integration passed **125 Python tests and 10 frontend tests**,
plus frontend lint/build checks. Coverage includes nullable intake, source
validation, immutable revisions, stale approvals, provider-specific completion
proof, secret-safe configuration, concurrency and independent refund readback.
An HTTP cleanup race was reproduced against the old implementation and fixed:
response emission follows mutation-lock release. Tests do not establish model
semantic accuracy.

### What the actual v2 rehearsal established

The [sandbox workflow proof](benchmarks/v2/sandbox-workflow.json) records capture
`4CP15886LP9883220` and refund `0DY277976X017934B`. The workflow used a synthetic
customer conversation and synthetic merchant observations, with actual Groq and
PayPal sandbox calls through the browser interface.

- The first review took **4.297 seconds** and returned
  `REVIEW_NEEDS_INFORMATION`. It identified contradictory item-condition
  statements and a missing request date, then supplied customer questions.
  The application composed the unsent reply from those questions. No payment
  operation existed at that point.
- The operator supplied an explicit synthetic resolution: the selected unit
  was unused, the used pair belonged to another order, USD39 was confirmed for
  this capture, and the request date was 2 October 2026. This created revision
  two and invalidated the first analysis.
- A fresh **4.790-second** review returned a refund recommendation with no
  unresolved issues. Test-operator approval bound its exact saved review hash,
  case/policy versions, capture and USD39 amount.
- One approval, one operation and one durable submission claim were recorded.
  Independent verification GETs both returned HTTP200: the refund was COMPLETED
  USD39 and the associated capture was REFUNDED. There was no automatic
  payment retry.

The first review also quoted a customer statement of USD49, but **did not
create an amount-conflict issue or ask for a receipt**. This is a condition/date
resolution demonstration, not proof that the model caught every discrepancy.
The merchant's synthetic resolution supplied the amount clarification. Customer
statements did not automatically become verified merchant facts.

The v2 workspace held one case, two revisions and two reviews. The original v1
demo workspace remains two cases / USD78. This was authorized automated
`test_operator` QA, not a real merchant/customer pilot or production money.

### Current model evaluation and limitations

The [latest reserve-eight evaluation](benchmarks/v2/reserve8-latest/summary.json)
used eight previously unseen synthetic cases selected before their outcomes.
All eight returned complete HTTP200 responses; **seven of eight produced an
accepted next action matching the frozen label**. One guard-rejected result is
retained. This does not mean seven wholly correct analyses or establish a
general accuracy rate.

The run had 19/19 exact source-quotation pairs and 14/14 exact policy pairs.
Semantic defects remained: some questions were routed to the customer when
merchant verification was needed; policy meaning was stretched; a linked-record
limitation was overstated; and a recommendation was worded as an already
completed decline. A customer-supplied date still requires merchant confirmation.
Currency-equivalence wording also remained imperfect in a reused development
case; this evaluation did not establish that it was solved.

Observed median request latency was 3.130 seconds and maximum was 4.816 seconds,
excluding 102.443 seconds of quota pacing. Eight observations are not a general
latency guarantee. The evidence pack retains exact inputs, separately frozen
labels, credential-free requests, final answers and case-level notes. Review
labels/notes were prepared by assistants, not blinded external merchant raters.

The [earlier 24-case candidate](benchmarks/v2/earlier24-failed/summary.json) and
[failed core-12 candidate](benchmarks/v2/core12-failed/summary.json) remain
separate failures. Their data, prompts and metrics differ; do not pool them with
the latest eight or replace their failures with later runs. Reused development
examples are separately labelled and excluded from unseen-case metrics.

V2's updated public source, final video and entry update remain release steps;
they are not established by this successful sandbox workflow.

The four existing proofs below establish **v1**, not v2's new AI behavior:

- [API proof](benchmarks/sandbox-proof.json): USD39 refund, 49.088s local review,
  independent readback and controlled same-request-ID replay.
- [Earlier browser proof](benchmarks/browser-proof.json): separate refund with a
  46.471s local review through the original HTML interface.
- [Shadcn proof](benchmarks/shadcn-proof.json): separate refund with a 46.078s
  local review, checked approval and one submission claim.
- [Fresh-public-checkout proof](benchmarks/judge-rehearsal-proof.json): new empty
  workspace, 49.733s local review and refund `8T744493UL845153Y`; one case,
  review, approval, operation and claim. Existing accounts/tools/model were
  reused; first-time installation and native macOS backend execution were not
  demonstrated.

Those four v1 proofs use separate stores where noted; the original demo
workspace remains two cases / USD78. Together with the v2 rehearsal, the project
has five distinct USD39 sandbox refunds. All recorded approvals were
test-operator actions with sandbox money.
The unchanged [original AI benchmark](benchmarks/first-results.json) remains
**3/4**, including the missing-facts answer that incorrectly chose decline.
Earlier 85-second timeout evidence remains preserved.

## Boundaries

- Fixed unused-item/30-day policy, one merchant, USD and full sandbox refunds.
  No arbitrary policy engine, discretionary exception, production payment,
  partial refund or multi-merchant platform is implied.
- Conversation claims never replace PayPal facts or become confirmed physical
  facts automatically. Source IDs and full quotes are checked together; this
  does not prove the model classified or interpreted them correctly.
- Model-output parsing may accept recursively identical duplicate keys while
  retaining raw output and warnings. Conflicting duplicates and non-finite
  values fail. API, credential and payment parsers remain strict.
- Intake allows at most 4000 characters and 12 paragraphs. The combined prompt
  budget is 16000 bytes for Groq and 6000 for the experimental local route;
  policy/metadata and UTF-8 size also count. Inputs are never silently truncated.
  Shorten excessive text explicitly.
- Groq has a 60-second parent deadline; local v2 inference has 180 seconds.
  Stopping a client does not prove server-side generation stopped. There is no
  automatic provider retry, paid upgrade, cloud fallback or model download.
- One stable request ID and atomic claim are saved before a refund POST.
  Unknown outcomes are not automatically replayed. Refresh is readback-only;
  an unknown refund ID requires explicit reconciliation.
- A successful POST is not verification. PayPal completion is not bank
  settlement. The observed v1 replay does not establish an undocumented
  request-ID retention guarantee.

The source is MIT licensed; see [third-party notices](THIRD_PARTY_NOTICES.md).
Raw receipts, credentials and browser-session files stay outside public source.
