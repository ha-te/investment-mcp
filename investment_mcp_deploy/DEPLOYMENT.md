# Investment MCP — private GitHub + cloud deployment (staging scaffold)

## What is implemented
- FastMCP tools: database status, local CSV ingest, SEC company facts, J-Quants daily sync, stored price lookup.
- Docker image and streamable HTTP entrypoint.
- CI syntax/compile check.
- Git ignores DB files and secrets. API key must be set in cloud host secret manager as `JQUANTS_API_KEY`.

## NOT YET DEPLOYED / REQUIRED
1. Create a **private** GitHub repository; push only source files in this directory. Do not upload a database or `.env`.
2. Select a cloud host with persistent volume for `INVESTMENT_DB` (e.g. `/data/investment.sqlite`). Initial DB must be migrated/imported separately; the image contains no DB.
3. Configure a real OAuth-capable authentication gateway for remote MCP access. The Docker process refuses to start unless `INVESTMENT_TRUSTED_AUTH_PROXY=true`, and that flag alone is **not** authentication.
4. Restrict dangerous tools (`import_prices_csv_file`, `jquants_sync_prices`) to authenticated, authorized users; ideally split into read-only public interface and private maintenance endpoint before exposing remotely.
5. Supply `JQUANTS_API_KEY` through the cloud host secret manager, never GitHub source, chat, or log.
6. Deploy HTTPS, register the MCP URL in ChatGPT if your plan/workspace supports custom connectors, and test authorization.

## Known limits
- J-Quants Free plan does not include recent data; API live connectivity and response schema are not yet validated.
- The DB must have the expected investment schema and master ticker rows; the existing connector aborts a day with unmapped codes.
- SEC needs a real `INVESTMENT_USER_AGENT` with contact email.
- No public URL, cloud account, OAuth provider, or cloud disk has been provisioned by this bundle.
