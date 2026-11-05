# Accord
https://accord-app-eta.vercel.app/

A semester-project prototype for milestone-based freelancer payments.

Accord helps clients and freelancers agree on work, reserve a project budget, review submissions and track payments through a simulated bank.

**All funds and bank accounts are synthetic. No real payments are processed.**

## How it works

1. A client creates a project with milestones, amounts and completion conditions.
2. Both participants accept the agreement.
3. The client reserves the full project budget.
4. The freelancer submits completed work.
5. The client approves payment, requests revisions or opens a dispute.
6. The simulated bank processes the payment.
7. Reconciliation checks that the application and bank records match.

Submitted work has a seven-day review period. Automatic release is subject to dispute and control checks.

## Features

- Client and freelancer registration and sign-in
- Agreements with up to ten milestones
- Explicit sandbox setup with one-time test funds
- Full-budget reservation
- Work submission, approval and revision requests
- Dispute handling and automatic-release safeguards
- Independent simulated bank service
- Double-entry ledger and payment reconciliation
- Duplicate-request protection and recovery handling
- Operations dashboard and separate approval for sensitive actions
- Audit records and a rule-based exception assistant

## Technology

- **Frontend:** React and TypeScript
- **Backend:** Python and FastAPI
- **Database:** PostgreSQL
- **Hosting:** Vercel and Neon
- **Background processing:** Durable worker with scheduled execution

## Repository structure

This repository contains the deployment package:

- `app/` — Application API and compiled frontend
- `bank/` — Independent simulated bank service
- `scheduler/` — Background-worker scheduling template

The complete development source, tests and academic documentation are maintained separately.

## Project purpose

Accord demonstrates enterprise-system design, transaction integrity, failure recovery, reconciliation and controlled financial operations.

The focus is keeping payment records correct and traceable when requests are repeated, responses are delayed or transactions require investigation.

## Limitations

This is an educational prototype. It does not provide real escrow, banking, identity verification or production financial services.

Email verification, password recovery and real payment-provider integration are outside the current implementation.

## Security

Deployment credentials are configured privately through environment variables. Passwords, tokens, connection strings and private configuration files must never be committed to this repository.

Use synthetic information when testing the application.
