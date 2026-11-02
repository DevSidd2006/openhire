# Bring Your Own Key (BYOK)

BYOK settings are per account. Recruiters may use their selected credentials
for their existing AI operations. Candidate credentials are available in the
same Profile → API Keys section, but are resolved only when the authenticated
candidate starts a practice interview against a practice job they created.
Candidate keys are not attached to real interview sessions, resume processing,
evaluation, scoring, reports, or recruiter actions. Candidate practice
interview usage is attributed to that candidate's selected credential.

The profile API Keys card supports an encrypted, recruiter-owned LLM credential.
Configure `BYOK_ENCRYPTION_KEY` as a Fernet key to enable the feature; generate
one with:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep the value stable across restarts and backups. Losing or rotating it without
re-encrypting stored credential ciphertext makes saved keys unreadable. When the
setting is empty, the BYOK API stays disabled and provider resolution continues
to use the existing platform configuration.

Recruiters can save multiple credentials, including multiple keys from one
provider. Each credential has a user-defined name and provider model. Keys are
encrypted at rest and APIs return only a masked suffix. Each recruiter chooses
BYOK or platform mode, a default credential and per-key model, and whether
failed calls may try another saved key or platform credentials. These settings
are scoped to that recruiter. Usage is tracked per credential and provider;
provider-reported token totals are used where available, with estimates for
adapters that do not report usage.

Supported key providers are OpenAI, Gemini, Anthropic, Groq, NVIDIA NIM, and
OpenRouter. Credential save/test performs a live provider request; the test
suite mocks provider responses and never reports a mock as a live key check.

For existing PostgreSQL deployments, apply
`repositories/postgres/migrations/20261102_byok_multi_credentials.sql` once
after the previous BYOK schema has been created. It assigns an ID and default
name to each existing row, preserving its encrypted key and user association,
then creates the per-user settings and usage tables. New databases can use
`repositories/postgres/schema.sql` directly.

Use the profile page's **API Keys** section to create/test credentials, edit a
credential name or model, set its default, configure fallback behavior, or
switch that recruiter to platform keys. Deleting a credential removes only
that credential; its aggregate usage history remains available to its owner.
