# Accord: private dashboard deployment

Prepared 25 October 2026. Nothing is published by these instructions or the packaging script.
Manal will authenticate in her own browser, not through this shared Codex account.

## Prepare now

Run `.\.venv\Scripts\python.exe scripts\prepare_hosting.py` in the source project.
The resulting `.local/deployment/<id>` folder contains only allowlisted service code,
compiled React assets, configuration, this guide and an inactive scheduler template.
Upload that folder's contents, not the source workspace, to a new private PERSONAL
GitHub repository when ready. Keep app/ and bank/ as sibling folders. Include the
hidden .python-version files in both folders. Never upload credential files.

## Private credentials, at the final setup stage

Use your own browser and your own GitHub, Vercel and Neon accounts. Do not connect
any of them as plugins to this shared OpenAI account. Git ignore and an external
folder prevent accidental packaging; neither isolates secrets from tools running
as the same Windows user. For strict separation, perform credential setup under
a separate private Windows user or on your own device without this shared Codex
session. Do not open secret-bearing files or terminals in this chat.

Create a dedicated Neon Free project, PostgreSQL 17, AWS Singapore if available.
Use its direct (pooling OFF) owner connection for provisioning. In a private
PowerShell terminal with this source and Python dependencies available, run:

```powershell
.\.venv\Scripts\python.exe scripts\setup_hosted.py --private-dir "$env:LOCALAPPDATA\AccordPrivate"
```

Paste the connection string only into the hidden prompt. The script creates two
databases, restricted runtime roles, schema and fresh synthetic data. It writes
config.json into the selected directory. The Neon provisioning path is not yet
provider-tested. If it fails, share only the sanitized error type.
If you already ran the old setup, retain its credentials; do not recreate roles
or delete the database to bypass a conflict. Arrange a private migration first.

## Final dashboard sequence

1. In your own GitHub browser session, create a private personal repository and
   upload the prepared folder contents. Do not connect GitHub to Codex.
2. In Vercel, choose your Hobby workspace, Add New > Project, connect your GitHub
   account directly to Vercel and grant access only to this repository.
3. Import the repository as accord-bank. Set Root Directory to bank, framework
   FastAPI; keep framework build/output defaults. Set function region to Singapore
   (sin1), matching Neon, in project settings before final runtime verification.
4. Enter production environment variables privately: BANK_DSN from bank_dsn in
   config.json, BANK_KEY from bank_key. Deploy and retain the production URL.
   Bank requests use a service key; Vercel deployment protection must also allow
   server-to-server access to the production URL. Never remove bank API auth.
5. Import the same repository again as accord-demo, Root Directory app, framework
   FastAPI. Set production variables using the mapping below, then deploy. Set
   the matching function region. Use the stable production URL for sharing.
6. Check deployment protection allows visitors to open the app production URL
   without a Vercel login. Test in a private browser window.
7. Configure the hourly worker only after checking GitHub's included Actions
   allowance and disabling paid overage. Create .github/workflows/accord-worker.yml
   from scheduler/accord-worker.yml on the default branch. Set repository Actions
   secrets ACCORD_BASE_URL to the app HTTPS origin and ACCORD_WORKER_SECRET to
   cron_secret. Run the workflow manually once and inspect its success status.
   The app also includes a daily Vercel cron backup.

| App environment name | Private config value |
|---|---|
| APP_DSN | app_dsn |
| JWT_SECRET | jwt_secret |
| BANK_URL | Bank production HTTPS origin plus /bank/v1 |
| BANK_KEY | bank_key |
| CRON_SECRET | cron_secret |
| DEMO_PASSWORD | demo_password |
| PUBLIC_DEMO | 1 |

Never deploy owner DSNs, passwords mapping, or the entire config file. Environment
variables belong on the server, never in React or any VITE_ variable.

## Acceptance before sharing

Verify both role logins, submission, approval, bank-confirmed payment and matched
reconciliation. Open a dispute on another milestone and verify approval is blocked.
Verify unauthenticated bank and worker requests are rejected. Verify worker manual
dispatch succeeds and the scheduled workflow is enabled. Do not advance production
time to demonstrate seven days; that policy is verified by isolated local tests.

This is a shared synthetic demo, not real escrow. Visitors modify the same demo
records. Governed dispute rework is implemented; public role shortcuts remain limited to client and freelancer. Operations/checker credentials must be handled separately. Free quotas can interrupt
service; automatic release becomes eligible after 168 hours and executes on the
next successful worker invocation, possibly later. No exact-time SLA is promised.

Use Vercel Hobby, Neon Free, included GitHub Actions and free vercel.app URLs.
No paid trial, custom domain or paid upgrade is needed for this plan.

References: https://vercel.com/docs/git ; https://vercel.com/docs/plans/hobby ;
https://neon.com/pricing ; https://vercel.com/docs/cron-jobs/usage-and-pricing
