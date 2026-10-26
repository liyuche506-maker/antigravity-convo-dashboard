---
name: convo-dashboard
description: Open the Conversation & Token Dashboard side panel or answer questions about the user's conversation history, token usage patterns, context window fill, and model quotas.
---

# Conversation & Token Dashboard Skill

When the user asks to view, manage, or analyze their conversations or token usage patterns:

1. **Surface the Side-Pane Pill**: Always include the clickable pill so the user can open the interactive side panel immediately:
   `[Convo & Token Dashboard](sidecar://convo-dashboard/dashboard/)`
2. **Query the Local Dashboard API (Optional)**: If the user also wants a written summary in chat, you can query the Language Server RPCs (`GetAllCascadeTrajectories`, `GetCascadeModelConfigData`, `GetCascadeTrajectoryGeneratorMetadata`) to summarize their heaviest conversations, cache hit rates, and model quotas.
