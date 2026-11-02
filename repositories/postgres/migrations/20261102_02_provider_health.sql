BEGIN;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS health_status text NOT NULL DEFAULT 'not_tested';
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS auth_status text NOT NULL DEFAULT 'unknown';
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS last_tested_at timestamptz;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS last_successful_test_at timestamptz;
ALTER TABLE user_llm_credentials ADD COLUMN IF NOT EXISTS health_error_code text;
CREATE TABLE IF NOT EXISTS platform_llm_provider_health (
    provider text PRIMARY KEY CHECK (provider IN ('openai','gemini','anthropic','groq','nvidia_nim','openrouter')),
    model text, status text NOT NULL, auth_status text NOT NULL,
    last_tested_at timestamptz, last_successful_test_at timestamptz,
    error_code text, message text, updated_at timestamptz NOT NULL DEFAULT now()
);
COMMIT;
