# Antigravity Conversation & Token Dashboard (`convo-dashboard`)

A native **Google Antigravity 2.0** plugin with a built-in side-panel UI (`sidecar`) for managing multiple conversations across workspaces and visualizing token usage patterns, context window fill, and live model quotas.

## Features

- **Multi-Conversation Manager**
  - Filter conversations by workspace/project, search by title or model, and toggle subagent trajectories.
  - Sort by **Last Active**, **Total Tokens**, **Fresh + Output Tokens**, **Context Window Fill %**, or **Step Count**.
  - **One-Click Switch (`Open ↗`)**: Jump directly to any conversation from the side panel.
  - **Turn-by-Turn Inspector**: Expand any conversation to inspect its context window growth (`estimatedTokensUsed / maxContextTokens`), peak context, average time-to-first-token (TTFT), child subagents, and per-step token breakdown.
- **Token Usage Patterns**
  - **Daily Token Burn Chart**: Stacked bar chart breaking down **Fresh Input**, **Cache Read**, **Output**, and **Thinking** tokens per day (with an **Exclude Cache Reads** toggle).
  - **By Model & By Workspace**: Token share, LLM call count, and cache hit efficiency (`%`) per model and per workspace.
- **Live Model Quotas**
  - Real-time remaining quota bars and reset countdowns (`Resets in Xh Ym`) across all configured models.

## Installation

1. Clone this repository into your Antigravity plugins directory:

   ```bash
   mkdir -p ~/.gemini/config/plugins
   git clone https://github.com/liyuche506-maker/antigravity-convo-dashboard.git ~/.gemini/config/plugins/convo-dashboard
   ```

2. Enable the plugin and its sidecar in Antigravity (via the **Plugins** settings page, or ask the Antigravity agent to enable `convo-dashboard` and `convo-dashboard/dashboard`).
3. Open the panel from the **Plugins** tab in the left sidebar, or click the `[Convo & Token Dashboard](sidecar://convo-dashboard/dashboard/)` pill in chat.

## Structure

```text
convo-dashboard/
├── plugin.json                      # Plugin manifest
├── assets/
│   └── logo.svg                     # Plugin icon
├── skills/
│   └── convo-dashboard/
│       └── SKILL.md                 # Skill to surface the side-pane pill in chat
└── sidecars/
    └── dashboard/
        ├── sidecar.json             # Sidecar UI config (AUX_PANE + SIDEBAR)
        ├── server.py                # Zero-dependency Python 3 backend
        └── index.html               # Theme-aware dashboard frontend
```
