# Refund Desk

A local merchant desk with an official shadcn dashboard for reviewing refund
requests against a fixed, versioned policy, approving an exact refund and
independently checking its PayPal result. Scope: one merchant, USD, full refunds
and **PayPal sandbox only**.

The default interface is React + TypeScript + Vite, built with shadcn CLI
**4.21.1** and **18 generated components**. The
[component manifest](frontend/shadcn-manifest.json) records their origin and
source hashes. The dashboard includes Overview, searchable/filterable Cases,
Policy without needing a first case, and review/approval/audit detail.

**The shadcn dashboard is verified through real sandbox refunds.** Four
separate USD 39.00 refunds are verified across the retained proof runs, with
actual local AI and test-operator approval. The latest used a fresh public
checkout and empty workspace, including the approval checkbox and independent
PayPal readback.
These observations do not establish personal merchant approval, production
readiness, public deployment or competition submission.

## Requirements and source

- Linux or macOS with Python **3.11+**, Git and a browser. Linux/Python 3.12 is
  tested; a native macOS application run has not yet been verified.
- **Node.js 22.22.2 and npm** to build the dashboard. No Node server is needed
  after the build; Python serves the compiled UI and its API from one origin.
- Ollama serving the installed, pinned `qwen3:4b` model at
  `http://127.0.0.1:11434`. The model needs several GB of disk and memory.
- Your own free PayPal Developer sandbox app, its sandbox Business merchant and
  a separate sandbox Personal buyer account.

There are no pip packages, Docker, paid hosting or cloud-model accounts required.
Local compute still consumes machine resources. The application source, license, tests and build instructions are in this
repository.

```bash
git clone https://github.com/harshith-vaddiparthy/refund-desk-hackathon.git
cd refund-desk-hackathon
python3 --version
node --version
npm --version
```

Install missing tools yourself before running the app. On Ubuntu 24.04 or
Debian 12 (or newer), the usual Python/Git setup is:

```bash
sudo apt-get update
sudo apt-get install -y python3 git
```

On macOS, install Python 3.12 from [python.org](https://www.python.org/downloads/macos/).
If Git is missing, run `xcode-select --install` and complete Apple's installer.
Install Node.js 22.22.2 for your operating system and architecture from the
[official release downloads](https://nodejs.org/download/release/v22.22.2/).

Install Ollama from [its official download page](https://ollama.com/download).
The macOS app starts its local server. On Linux, follow the official installation
instructions. If no Ollama server is running, use a separate terminal:

```bash
ollama serve
```

Download the model explicitly as a user setup step:

```bash
ollama pull qwen3:4b
```

The app never downloads models or changes the runtime. It checks this full digest
and stops if the installed model differs or the shared runtime is busy:

```text
359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7
```

The tag may change upstream; a different digest is not a verified replacement.
The retained benchmark used Ollama 0.16.2. Do not unload another active workload
merely to start a review.

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

## 3. Create and approve one sandbox purchase

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

## 4. Start the merchant app

```bash
python3 app.py \
  --client-file "$HOME/.config/refund-desk/sandbox-app.json" \
  --merchant-id 'MERCHANT_ID_FROM_CAPTURE' \
  --merchant-email 'MERCHANT_EMAIL_FROM_CAPTURE' \
  --data-dir "$HOME/.local/share/refund-desk/app" \
  --port 8749
```

The server binds to loopback. It saves the private launch URL to
`$HOME/.local/share/refund-desk/app/browser-session.json`. Open it without copying
the session token into terminal output:

```bash
python3 - <<'PYOPEN'
from pathlib import Path
import json
import webbrowser
path = Path.home() / '.local/share/refund-desk/app/browser-session.json'
webbrowser.open(json.loads(path.read_text())['url'])
PYOPEN
```

On restart, open the newly written launch URL. Session credentials stay in that
browser tab's session storage; opening the bare app URL does not grant access.
If 8749 is occupied, select another `--port`; use the same port in the helper's
`--return-url` when creating a later purchase.

In the app:

1. Create a case using the returned capture ID and the customer request. Confirm
   whether the item was used; physical facts are explicitly merchant supplied.
   For a sandbox exercise, identify invented facts as test assumptions.
2. Run the local review. Inspect its recommendation, rationale, exact policy
   citations and missing information. Case dates use UTC calendar dates.
3. For a complete eligible refund recommendation, review the displayed amount
   and give explicit approval. The service fetches the capture again and checks
   merchant, amount, review hash and policy before claiming the operation.
4. Inspect the refund and audit timeline. `pending` is not `verified`;
   verification requires a separate matching refund GET.

For automated QA, add `--test-operator 'YOUR QA LABEL'` to the app command.
This is a trusted server setting and records `test_operator`, not `human_ui`.
The browser cannot select approval provenance or submit an amount.

## Verification and retained evidence

Run the offline suites without PayPal credentials or an Ollama server (frontend
dependencies must already be installed):

```bash
python3 -m unittest discover -v
cd frontend
npm run lint
npm test
npm run build
cd ..
```

At this checkpoint **77 Python tests and 6 frontend tests passed**. They cover
guarded assessments, request boundaries, durable approvals, concurrent claims,
stale reviews, uncertain responses, independent readback, authenticated routes,
frontend money handling and API behavior. Frontend lint/type/build checks also
pass. CI is configured to run these checks from the lockfile.

The curated [sandbox proof](benchmarks/sandbox-proof.json) records the real
capture/refund, a fresh local AI review completed in **49.088 seconds**, exact
USD 39.00 readback, and controlled same-request-ID replay returning the same
refund. Its approval was performed by a test operator. The separate
[browser proof](benchmarks/browser-proof.json) records the initial interface
working through Ego on the Mac mini, with a fresh 46.471-second AI review and a
second independently verified refund.

The [shadcn proof](benchmarks/shadcn-proof.json) records the new dashboard's
completed workflow: a **46.078-second** local review, initially disabled approval
until the confirmation checkbox was checked, exact USD 39.00 test-operator
approval, and independently verified refund `00K11021V5076505G` for capture
`35045355PG785913J`. There was one submission claim; a second claim was refused.
At that checkpoint the dashboard held two browser cases and showed two verified
refunds totaling USD 78.00. The first standalone API proof used a separate store.
Raw credentials and API receipts are excluded from source control.

The [fresh-checkout rehearsal](benchmarks/judge-rehearsal-proof.json) used an
anonymous public Git clone, a clean frontend build and an empty private data
directory. It completed a new sandbox purchase, a **49.733-second** local review,
explicit test-operator approval and independently verified refund
`8T744493UL845153Y`. The ledger has one case, review, approval, operation and
submission claim. Existing private sandbox accounts and the installed pinned
model were reused; first-time account/tool installation and native macOS backend
execution are not claimed.

The compiled shadcn dashboard was checked in Ego: startup, Overview, Policy,
search/filters, a 390px mobile layout without horizontal overflow, blocking an
unknown item-use value, and Escape closing the form and returning focus to New
request. No CSP or JavaScript errors were observed in that check. Fresh
refund review, approval and readback also completed through this interface.

The unchanged [first AI benchmark](benchmarks/first-results.json) remains **3/4**:
the missing-facts answer selected `decline` while asking for more information.
Exact quotations did not guarantee the right recommendation. The guarded flow
now stops incomplete input before inference and preserves rejected model answers.
An earlier integrated request timed out after 85 seconds; a later synthetic
review completed in 64.924 seconds with four requested CPU threads. These
observations do not establish general reliability or latency.

Optional assessment-only commands make no payment calls:

```bash
python3 review.py --example missing_facts --output-dir evidence/review-missing
python3 review.py --example eligible --output-dir evidence/review-eligible
```

The second command performs real local inference. To reproduce the separate
four-case benchmark, use another new output directory:

```bash
python3 benchmarks/local_ai.py --output-dir evidence/local-ai-one
```

## Boundaries

- One merchant, USD, eligible completed captures and full refunds only. Used
  items, requests outside the fixed 30-day policy, incomplete/declined reviews
  and changed approvals cannot proceed to payment.
- The model recommends; it never approves a payment. Merchant identity and
  captured amount come from an independent PayPal GET, not browser fields.
- SQLite persists approval and operation association before the refund POST.
  Duplicate clicks and restart reuse the operation. A POST receipt alone is
  never completion proof.
- `uncertain` means the earlier request might have taken effect. No automatic
  payment POST retry occurs. Refresh is readback-only; an unknown refund ID
  requires explicit reconciliation. Keep the data directory and operation IDs.
- The client sends an explicit exact amount, verified in the sandbox proof.
  The refund endpoint's request-ID retention duration remains unspecified in
  the captured reference; the observed replay is not an unlimited guarantee.
- Local inference has a 90-second parent deadline, but stopping its client does
  not prove server-side generation stopped immediately. No cloud fallback or
  automatic model download occurs.
- PayPal-reported completion does not establish bank settlement. Production
  payments, public multi-user hosting, partial refunds, customer messaging and
  store integrations are outside this version.

The retained benchmark and proof summaries above document the observed behavior.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for upstream component and
font licenses.
