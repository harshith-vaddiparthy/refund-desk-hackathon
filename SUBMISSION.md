# Refund Desk — project story

[Narrated demo](https://www.youtube.com/watch?v=Iv_8JoJnSHs) · [Devpost entry](https://devpost.com/software/refund-desk)

## Inspiration

“Factory-sealed” in one message. “I tried them yesterday” in the next. A merchant cannot approve a refund until that contradiction is resolved.

Refund Desk brings the conversation, verified payment facts and policy into one workspace. AI does the reading work; a merchant remains responsible for confirming facts and approving money movement.

## What it does

Start with a PayPal capture and a messy customer conversation. The AI identifies attributed claims, highlights conflicting or missing information, quotes the policy and assigns concrete questions to the customer or merchant. Clicking a claim opens its exact source.

The app turns customer-owned questions into an editable, explicitly unsent follow-up. Merchant checks stay internal. A customer statement does not automatically become a verified fact.

After clarification, the merchant records corrected evidence and confirmed facts. That creates an immutable revision and invalidates the old analysis. A fresh review must pass before approval becomes available. The approval names the exact capture, amount and current review; the app then submits one refund and independently checks PayPal's result. A timeout never causes an automatic second refund.

## How we built it

The workspace uses React, TypeScript, Vite and 18 components generated with the official shadcn CLI. A Python server provides the authenticated API and serves the dashboard; SQLite stores revisions, reviews, approvals and durable payment operations.

PayPal OAuth, Orders v2 and Payments v2 provide the purchase, capture, refund and separate verification. The demonstrated AI route uses Groq Free with openai/gpt-oss-120b. The interface discloses the conversation, policy and limited payment summary sent for analysis. There is no silent provider fallback or paid-plan upgrade. An experimental local Ollama route is also retained, with its failed richer-task evaluation clearly documented.

## Challenges we ran into

Exact quotations do not guarantee sound reasoning. Our evaluation exposed policy overreach, poorly assigned questions and premature payment language. We kept the failed candidates and their results.

We removed free-form model-written payment updates. AI now supplies findings and questions; the application composes the unsent response and renders payment status from the saved, independently verified result. Calendar-day calculations and the full USD refund amount also come from application/payment facts.

Payment uncertainty needed separate treatment: a successful POST is insufficient, and an interrupted request may already have taken effect. One durable operation and a matching independent readback keep those outcomes distinct.

## Accomplishments that we're proud of

The demo records an actual two-review workflow. The first AI review found contradictory item-condition statements and a missing request date. It blocked payment and supplied two customer questions. A synthetic clarification and test-operator confirmation created a new case revision. The second AI review recommended a refund, followed by explicit approval and an independently verified USD39 PayPal sandbox refund.

The two model requests completed in 4.297 and 4.790 seconds. PayPal returned refund 0DY277976X017934B as COMPLETED, and the capture as REFUNDED. The ledger contains one approval, one operation and one submission claim. This is the project's fifth distinct USD39 sandbox refund, with earlier proofs retained separately.

The customer conversation and merchant observations are synthetic; the model and PayPal sandbox requests are actual. No real money moved. The first review quoted the customer's USD49 mention but did not flag an amount discrepancy; the demonstration establishes the condition/date workflow, not perfect detection.

The implementation passes 125 Python tests and 10 frontend tests, plus lint/build checks. Public MIT source includes complete local setup and an inspectable evaluation pack.

## What we learned

On eight previously unseen synthetic cases, all eight model requests completed and seven produced an accepted next action matching the frozen label. These were not seven wholly correct analyses: question ownership and policy interpretation still had defects. All 19 source references and 14 policy references matched exactly; that establishes attribution, not semantic truth. Labels and review were assistant-prepared, not an independent merchant study.

## What's next

Test with merchants and measure actual reading, typing and decision effort. Improve question routing and policy interpretation using independent cases before expanding beyond one merchant, full USD sandbox refunds and the unused-item/30-day demonstration policy. We do not yet claim adoption, measured time savings or production readiness.

## Try it

The public repository contains complete build, account, AI-runtime and sandbox setup instructions. Linux/Python3.12 is verified; a native macOS backend run is not established. Judges use their own free PayPal sandbox and Groq Free accounts. No paid hosting or shared API secret is required. The video edits navigation/typing time, preserves the two visible model waits and shows test-operator actions.
