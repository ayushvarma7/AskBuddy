# API keys and `.env` setup

cuga reads provider credentials from the project environment. For local projects, put them in a `.env` file at the project root before running `uv run cuga start <service>` or your own Python entry point.

## Pick a model settings file

Set `AGENT_SETTING_CONFIG` to the TOML file that matches the provider used by cuga's internal agent nodes (planner, chat, code, final answer, and related model calls). The variable name is singular: `AGENT_SETTING_CONFIG`.

Bundled model settings live in cuga under `src/cuga/configurations/models/`:

- `settings.groq.toml` for Groq.
- `settings.openai.toml` for OpenAI and OpenAI-compatible endpoints such as LiteLLM.
- `settings.azure.toml` for Azure OpenAI.
- `settings.watsonx.toml` for WatsonX.
- `settings.openrouter.toml` for OpenRouter.

Use `MODEL_NAME` to override the model in that settings file. For OpenAI-compatible providers, use `OPENAI_BASE_URL` to point at the provider or gateway endpoint.

## Groq default

This matches cuga's upstream `.env.example`.

```env
GROQ_API_KEY="gsk-your-groq-api-key"
MODEL_NAME="openai/gpt-oss-120b"
AGENT_SETTING_CONFIG="settings.groq.toml"
```

## OpenAI

```env
OPENAI_API_KEY="sk-your-openai-api-key"
MODEL_NAME="gpt-4o"
AGENT_SETTING_CONFIG="settings.openai.toml"
```

## OpenAI-compatible endpoint

Use this shape for LiteLLM, local gateways, or providers exposed through an OpenAI-compatible API.

```env
OPENAI_API_KEY="your-api-key"
OPENAI_BASE_URL="https://api.example.com/v1"
MODEL_NAME="gpt-4o"
AGENT_SETTING_CONFIG="settings.openai.toml"
```

## Azure OpenAI

```env
AZURE_OPENAI_API_KEY="your-azure-api-key"
AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com"
OPENAI_API_VERSION="2024-08-01-preview"
AGENT_SETTING_CONFIG="settings.azure.toml"
```

## WatsonX

Use either `WATSONX_PROJECT_ID` or `WATSONX_SPACE_ID`.

```env
WATSONX_API_KEY="your-watsonx-api-key"
WATSONX_PROJECT_ID="your-project-id"
# WATSONX_SPACE_ID="your-space-id"
WATSONX_URL="https://us-south.ml.cloud.ibm.com"
MODEL_NAME="meta-llama/llama-4-maverick-17b-128e-instruct-fp8"
AGENT_SETTING_CONFIG="settings.watsonx.toml"
```

## OpenRouter

```env
OPENROUTER_API_KEY="sk-or-your-openrouter-key"
OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
MODEL_NAME="openai/gpt-4o"
AGENT_SETTING_CONFIG="settings.openrouter.toml"
```

## Optional services

```env
# Langfuse tracing
LANGFUSE_SECRET_KEY="sk-lf-xxx"
LANGFUSE_PUBLIC_KEY="pk-lf-xxx"
LANGFUSE_HOST="https://us.cloud.langfuse.com"
DYNACONF_ADVANCED_FEATURES__LANGFUSE_TRACING=true

# E2B sandbox
E2B_API_KEY="e2b_xxx"

# Slow/reasoning models
CUGA_LLM_HTTP_TIMEOUT=120
```

Do not commit `.env`. If a project does not already ignore it, add `.env` to `.gitignore`.
