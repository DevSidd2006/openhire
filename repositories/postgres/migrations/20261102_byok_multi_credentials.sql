BEGIN;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS credential_id text;
UPDATE user_llm_credentials SET credential_id = gen_random_uuid()::text WHERE credential_id IS NULL;
ALTER TABLE user_llm_credentials ALTER COLUMN credential_id SET NOT NULL;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS name text NOT NULL DEFAULT 'My API key';
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS health_status text NOT NULL DEFAULT 'not_tested';
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS auth_status text NOT NULL DEFAULT 'unknown';
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS last_tested_at timestamptz;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS last_successful_test_at timestamptz;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS health_error_code text;
ALTER TABLE user_llm_credentials DROP CONSTRAINT IF EXISTS user_llm_credentials_pkey;
ALTER TABLE user_llm_credentials ADD CONSTRAINT user_llm_credentials_pkey PRIMARY KEY (credential_id);
ALTER TABLE user_llm_credentials DROP CONSTRAINT IF EXISTS user_llm_credentials_provider_check;
ALTER TABLE user_llm_credentials ADD CONSTRAINT user_llm_credentials_provider_check CHECK (provider IN ('openai','gemini','anthropic','groq','nvidia_nim','openrouter'));
CREATE INDEX IF NOT EXISTS idx_user_llm_credentials_user ON user_llm_credentials(user_id);
CREATE TABLE IF NOT EXISTS user_llm_settings (
 user_id text PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
 mode text NOT NULL DEFAULT 'byok' CHECK (mode IN ('byok','platform')),
 default_credential_id text,
 provider text CHECK (provider IS NULL OR provider IN ('openai','gemini','anthropic','groq','nvidia_nim','openrouter')),
 fallback_to_other_keys boolean NOT NULL DEFAULT true,
 fallback_to_platform boolean NOT NULL DEFAULT true,
 model text,
 updated_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE user_llm_settings ADD COLUMN IF NOT EXISTS provider text;
ALTER TABLE user_llm_settings ALTER COLUMN mode SET DEFAULT 'platform';
CREATE TABLE IF NOT EXISTS user_llm_usage (
 user_id text NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
 credential_id text NOT NULL,
 provider text NOT NULL,
 requests bigint NOT NULL DEFAULT 0,
 successes bigint NOT NULL DEFAULT 0,
 failures bigint NOT NULL DEFAULT 0,
 input_tokens bigint NOT NULL DEFAULT 0,
 output_tokens bigint NOT NULL DEFAULT 0,
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (user_id, credential_id, provider)
);
COMMIT;
