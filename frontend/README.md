# Refund Desk dashboard

React + TypeScript + Vite dashboard built with the official shadcn CLI 4.21.1,
Radix primitives, and the nova preset. The CLI is pinned in devDependencies;
package-lock.json fixes the installed dependency graph. shadcn-manifest.json
records the commands, configuration, and generated component hashes.

## Build and check

```sh
npm ci
npm run lint
npm test
npm run build
```

The production output is `frontend/dist`. Serve it with the existing Python
Refund Desk server using `--frontend-dir frontend/dist`, alongside the private
sandbox client and merchant configuration described in the project README.
Vite preview alone does not provide the refund API. No API proxy or external
fonts/assets are required by the supported production run path.

Open the private launch URL saved by the Python server. The frontend uses the
existing fragment bootstrap, origin-scoped sessionStorage, session header,
and CSRF header. It handles a fresh launch fragment after a server restart.
Tokens are not logged or embedded in the build.

Overview, Refund Requests, Policy, and Activity use authenticated server data.
Verified refund totals include only independently verified PayPal sandbox
operations in that workspace. Synthetic transport evidence and pending,
uncertain, or unsubmitted requests cannot inflate the total.

Approval requires a frozen capture/amount/currency/review-hash snapshot and an
explicit checked confirmation. The browser sends only review_hash and
confirmed:true. It never retries a refund automatically. Closing an in-flight
dialog does not cancel the operation; the persistent request view retains its
status. Test-operator attribution is visible throughout the workspace.

All components under src/components/ui were downloaded with the official CLI.
The use-mobile hook was adapted to use React useSyncExternalStore for current
lint compatibility. The light palette is configured through shadcn CSS tokens.
No sample transactions, charts, or fabricated activity are shipped.

The compiled page requires script-src self and local font assets. Radix modal
and Sonner behavior uses inline styles/style elements; the Python server's
compiled-frontend CSP permits styles while keeping scripts strict. Keep the
server on loopback; the complete local-run path is the intended judging route.
