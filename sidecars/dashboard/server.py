#!/usr/bin/env python3
"""Backend server for the Convo & Token Dashboard sidecar."""

import datetime
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = Path.home()
APP_DATA_DIR = Path(
    os.environ.get("ANTIGRAVITY_APP_DATA_DIR", str(HOME / ".gemini" / "antigravity"))
)

PRELOAD_CANDIDATES = [
    APP_DATA_DIR / "lib" / "node_modules" / "sidecar_sdk" / "preload.js",
    HOME / ".gemini" / "config" / "node_modules" / "sidecar_sdk" / "preload.js",
]

KNOWN_MODEL_LABELS = {
    "MODEL_PLACEHOLDER_M318": "Gemini 3.8 Flash (High)",
    "MODEL_PLACEHOLDER_M319": "Gemini 3.8 Flash (Medium)",
    "MODEL_PLACEHOLDER_M320": "Gemini 3.8 Flash (Low)",
    "MODEL_PLACEHOLDER_M400": "Claude Opus 5.5 (Low)",
    "MODEL_PLACEHOLDER_M401": "Claude Opus 5.5 (Medium)",
    "MODEL_PLACEHOLDER_M402": "Claude Opus 5.5 (High)",
    "MODEL_PLACEHOLDER_M403": "Claude Sonnet 5.5 (Low)",
    "MODEL_PLACEHOLDER_M404": "Claude Sonnet 5.5 (Medium)",
    "MODEL_PLACEHOLDER_M405": "Claude Sonnet 5.5 (High)",
    "MODEL_PLACEHOLDER_M418": "Gemini 4 Argon (Medium)",
    "MODEL_OPENAI_GPT_OSS_120B_MEDIUM": "GPT-OSS 120B (Medium)",
}

# In-memory cache keyed by cascadeId -> (stepCount, lastModifiedTime, parsed_metrics)
_METADATA_CACHE = {}
_CACHE_LOCK = threading.Lock()


def load_preload_js() -> str:
    for p in PRELOAD_CANDIDATES:
        if p.exists():
            return p.read_text(encoding="utf-8", errors="replace")
    return "// preload.js not found\nwindow.sidecar = window.sidecar || {};\n"


def ls_rpc(method: str, payload: dict, timeout: float = 15.0) -> dict:
    ls_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
    csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
    if not ls_addr:
        raise RuntimeError("ANTIGRAVITY_LS_ADDRESS is not set")
    url = f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": csrf,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        return json.loads(raw) if raw.strip() else {}


def run_agentapi(args: list, project_id: str = "") -> dict:
    env = dict(os.environ)
    if project_id:
        env["ANTIGRAVITY_PROJECT_ID"] = project_id
    exe = os.environ.get("ANTIGRAVITY_AGENTAPI_EXE")
    cmd = [exe, "agentapi", *args] if exe else ["agentapi", *args]
    out = subprocess.check_output(cmd, env=env, text=True, timeout=15)
    return json.loads(out) if out.strip() else {}


def parse_duration_ms(val: str) -> float:
    if not val or not isinstance(val, str):
        return 0.0
    try:
        if val.endswith("ms"):
            return float(val[:-2])
        if val.endswith("s"):
            return float(val[:-1]) * 1000.0
    except ValueError:
        pass
    return 0.0


def parse_iso_date(ts: str) -> str:
    if not ts or not isinstance(ts, str):
        return ""
    return ts[:10]


def read_step_dates(cascade_id: str) -> dict:
    """Map step_index -> YYYY-MM-DD (and ISO timestamp) from transcript.jsonl if available."""
    step_dates = {}
    tpath = APP_DATA_DIR / "brain" / cascade_id / ".system_generated" / "logs" / "transcript.jsonl"
    if not tpath.exists():
        return step_dates
    try:
        with tpath.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                si = obj.get("step_index")
                ca = obj.get("created_at")
                if si is not None and ca:
                    step_dates[int(si)] = ca
    except Exception:
        pass
    return step_dates


def get_convo_token_data(cascade_id: str, step_count: int, last_modified: str, fallback_date: str) -> dict:
    with _CACHE_LOCK:
        cached = _METADATA_CACHE.get(cascade_id)
        if cached and cached[0] == step_count and cached[1] == last_modified:
            return cached[2]

    step_timestamps = read_step_dates(cascade_id)
    try:
        resp = ls_rpc("GetCascadeTrajectoryGeneratorMetadata", {"cascadeId": cascade_id}, timeout=12.0)
    except Exception:
        resp = {}

    gen_list = resp.get("generatorMetadata") or []
    input_tokens = 0
    cache_read_tokens = 0
    output_tokens = 0
    thinking_tokens = 0
    call_count = 0
    models_map = {}  # model_enum -> dict
    daily_map = {}   # YYYY-MM-DD -> dict
    latest_context_used = 0
    max_context_tokens = 0
    peak_context_used = 0
    ttft_sum_ms = 0.0
    ttft_count = 0
    turns = []

    for idx, item in enumerate(gen_list):
        cm = item.get("chatModel") or {}
        usage = cm.get("usage") or {}
        model_enum = usage.get("model") or cm.get("model") or "UNKNOWN"
        inp = int(usage.get("inputTokens") or 0)
        cache_r = int(usage.get("cacheReadTokens") or 0)
        out = int(usage.get("outputTokens") or 0)
        think = int(usage.get("thinkingOutputTokens") or 0)

        input_tokens += inp
        cache_read_tokens += cache_r
        output_tokens += out
        thinking_tokens += think
        call_count += 1

        # Context window metadata
        csm = cm.get("chatStartMetadata") or {}
        cwm = csm.get("contextWindowMetadata") or {}
        est_ctx = int(cwm.get("estimatedTokensUsed") or 0)
        max_ctx = int(cwm.get("maxContextTokens") or 0)
        if est_ctx > 0:
            latest_context_used = est_ctx
            if est_ctx > peak_context_used:
                peak_context_used = est_ctx
        if max_ctx > 0:
            max_context_tokens = max_ctx

        ttft_ms = parse_duration_ms(cm.get("timeToFirstToken") or "")
        stream_ms = parse_duration_ms(cm.get("streamingDuration") or "")
        if ttft_ms > 0:
            ttft_sum_ms += ttft_ms
            ttft_count += 1

        step_indices = item.get("stepIndices") or []
        first_step = step_indices[0] if step_indices else idx
        step_ts = ""
        for si in step_indices:
            if si in step_timestamps:
                step_ts = step_timestamps[si]
                break
        day = parse_iso_date(step_ts) or fallback_date or "Unknown"

        m_entry = models_map.setdefault(
            model_enum,
            {"inputTokens": 0, "cacheReadTokens": 0, "outputTokens": 0, "thinkingTokens": 0, "calls": 0},
        )
        m_entry["inputTokens"] += inp
        m_entry["cacheReadTokens"] += cache_r
        m_entry["outputTokens"] += out
        m_entry["thinkingTokens"] += think
        m_entry["calls"] += 1

        d_entry = daily_map.setdefault(
            day,
            {"inputTokens": 0, "cacheReadTokens": 0, "outputTokens": 0, "thinkingTokens": 0, "calls": 0},
        )
        d_entry["inputTokens"] += inp
        d_entry["cacheReadTokens"] += cache_r
        d_entry["outputTokens"] += out
        d_entry["thinkingTokens"] += think
        d_entry["calls"] += 1

        turns.append(
            {
                "turn": idx + 1,
                "step": first_step,
                "timestamp": step_ts,
                "model": model_enum,
                "responseModel": cm.get("responseModel") or "",
                "inputTokens": inp,
                "cacheReadTokens": cache_r,
                "outputTokens": out,
                "thinkingTokens": think,
                "contextUsed": est_ctx,
                "maxContext": max_ctx,
                "ttftMs": round(ttft_ms),
                "streamMs": round(stream_ms),
            }
        )

    result = {
        "inputTokens": input_tokens,
        "cacheReadTokens": cache_read_tokens,
        "outputTokens": output_tokens,
        "thinkingTokens": thinking_tokens,
        "totalTokens": input_tokens + cache_read_tokens + output_tokens + thinking_tokens,
        "freshAndOutputTokens": input_tokens + output_tokens + thinking_tokens,
        "callCount": call_count,
        "latestContextUsed": latest_context_used,
        "peakContextUsed": peak_context_used,
        "maxContextTokens": max_context_tokens,
        "avgTtftMs": round(ttft_sum_ms / ttft_count) if ttft_count else 0,
        "modelsMap": models_map,
        "dailyMap": daily_map,
        "turns": turns,
    }
    with _CACHE_LOCK:
        _METADATA_CACHE[cascade_id] = (step_count, last_modified, result)
    return result


def build_dashboard_payload() -> dict:
    # 1. Model configs & quotas
    model_labels = dict(KNOWN_MODEL_LABELS)
    quotas = []
    try:
        mc_resp = ls_rpc("GetCascadeModelConfigData", {})
        for cfg in mc_resp.get("clientModelConfigs") or []:
            label = cfg.get("label") or ""
            m_enum = (cfg.get("modelOrAlias") or {}).get("model") or ""
            if m_enum and label:
                model_labels[m_enum] = label
            q_info = cfg.get("quotaInfo") or {}
            if label:
                quotas.append(
                    {
                        "model": m_enum,
                        "label": label,
                        "remainingFraction": q_info.get("remainingFraction"),
                        "resetTime": q_info.get("resetTime") or "",
                        "tagTitle": cfg.get("tagTitle") or "",
                    }
                )
    except Exception:
        pass

    # 2. All conversation summaries
    try:
        all_resp = ls_rpc("GetAllCascadeTrajectories", {})
    except Exception as e:
        return {"error": f"Failed to query Language Server: {e}"}

    summaries = all_resp.get("trajectorySummaries") or {}

    conversations = []
    global_daily = {}   # YYYY-MM-DD -> stats
    global_models = {}  # model_label -> stats
    global_workspaces = {}  # workspace_name -> stats

    totals = {
        "conversationCount": 0,
        "rootConversationCount": 0,
        "subagentCount": 0,
        "inputTokens": 0,
        "cacheReadTokens": 0,
        "outputTokens": 0,
        "thinkingTokens": 0,
        "totalTokens": 0,
        "callCount": 0,
        "stepCount": 0,
    }

    for cid, info in summaries.items():
        step_count = int(info.get("stepCount") or 0)
        last_mod = info.get("lastModifiedTime") or ""
        created = info.get("createdTime") or ""
        fallback_date = parse_iso_date(last_mod) or parse_iso_date(created)

        t_meta = info.get("trajectoryMetadata") or {}
        root_cid = t_meta.get("rootConversationId") or cid
        ann = info.get("annotations") or {}
        title = ann.get("title") or info.get("summary") or ""
        is_subagent = (root_cid != cid) or (not title and step_count < 80 and "subtrajectory" in str(info).lower())
        if not title:
            title = f"Subagent ({cid[:8]})" if root_cid != cid else f"Conversation {cid[:8]}"

        workspaces = info.get("workspaces") or t_meta.get("workspaces") or []
        ws_uri = ""
        branch = ""
        if workspaces:
            ws_uri = workspaces[0].get("workspaceFolderAbsoluteUri") or ""
            branch = workspaces[0].get("branchName") or ""
        if not ws_uri:
            uris = t_meta.get("workspaceUris") or []
            if uris:
                ws_uri = uris[0]

        ws_name = urllib.parse.unquote(ws_uri.rstrip("/").split("/")[-1]) if ws_uri else "Global / Scratch"

        tok = get_convo_token_data(cid, step_count, last_mod, fallback_date)

        # Resolve human-readable model labels for this conversation
        convo_models = []
        for m_enum, m_stats in tok["modelsMap"].items():
            m_label = model_labels.get(m_enum, m_enum)
            convo_models.append(
                {
                    "model": m_enum,
                    "label": m_label,
                    **m_stats,
                }
            )
            gm = global_models.setdefault(
                m_label,
                {
                    "label": m_label,
                    "model": m_enum,
                    "inputTokens": 0,
                    "cacheReadTokens": 0,
                    "outputTokens": 0,
                    "thinkingTokens": 0,
                    "totalTokens": 0,
                    "calls": 0,
                    "conversations": 0,
                },
            )
            gm["inputTokens"] += m_stats["inputTokens"]
            gm["cacheReadTokens"] += m_stats["cacheReadTokens"]
            gm["outputTokens"] += m_stats["outputTokens"]
            gm["thinkingTokens"] += m_stats["thinkingTokens"]
            gm["totalTokens"] += (
                m_stats["inputTokens"]
                + m_stats["cacheReadTokens"]
                + m_stats["outputTokens"]
                + m_stats["thinkingTokens"]
            )
            gm["calls"] += m_stats["calls"]
            gm["conversations"] += 1

        convo_models.sort(key=lambda x: x["calls"], reverse=True)

        for day, d_stats in tok["dailyMap"].items():
            gd = global_daily.setdefault(
                day,
                {
                    "date": day,
                    "inputTokens": 0,
                    "cacheReadTokens": 0,
                    "outputTokens": 0,
                    "thinkingTokens": 0,
                    "totalTokens": 0,
                    "calls": 0,
                    "conversations": 0,
                },
            )
            gd["inputTokens"] += d_stats["inputTokens"]
            gd["cacheReadTokens"] += d_stats["cacheReadTokens"]
            gd["outputTokens"] += d_stats["outputTokens"]
            gd["thinkingTokens"] += d_stats["thinkingTokens"]
            gd["totalTokens"] += (
                d_stats["inputTokens"]
                + d_stats["cacheReadTokens"]
                + d_stats["outputTokens"]
                + d_stats["thinkingTokens"]
            )
            gd["calls"] += d_stats["calls"]
            gd["conversations"] += 1

        gw = global_workspaces.setdefault(
            ws_name,
            {
                "workspace": ws_name,
                "conversations": 0,
                "inputTokens": 0,
                "cacheReadTokens": 0,
                "outputTokens": 0,
                "thinkingTokens": 0,
                "totalTokens": 0,
            },
        )
        gw["conversations"] += 1
        gw["inputTokens"] += tok["inputTokens"]
        gw["cacheReadTokens"] += tok["cacheReadTokens"]
        gw["outputTokens"] += tok["outputTokens"]
        gw["thinkingTokens"] += tok["thinkingTokens"]
        gw["totalTokens"] += tok["totalTokens"]

        max_ctx = tok["maxContextTokens"] or 200000
        ctx_pct = round((tok["latestContextUsed"] / max_ctx) * 100, 1) if tok["latestContextUsed"] else 0.0

        labeled_turns = []
        for t in tok["turns"]:
            labeled_turns.append({**t, "modelLabel": model_labels.get(t["model"], t["model"])})

        conversations.append(
            {
                "cascadeId": cid,
                "rootConversationId": root_cid,
                "isSubagent": root_cid != cid,
                "title": title,
                "workspaceName": ws_name,
                "workspaceUri": ws_uri,
                "branchName": branch,
                "projectId": t_meta.get("projectId") or "",
                "status": (info.get("status") or "").replace("CASCADE_RUN_STATUS_", ""),
                "stepCount": step_count,
                "createdTime": created,
                "lastModifiedTime": last_mod,
                "lastUserViewTime": ann.get("lastUserViewTime") or "",
                "inputTokens": tok["inputTokens"],
                "cacheReadTokens": tok["cacheReadTokens"],
                "outputTokens": tok["outputTokens"],
                "thinkingTokens": tok["thinkingTokens"],
                "totalTokens": tok["totalTokens"],
                "freshAndOutputTokens": tok["freshAndOutputTokens"],
                "callCount": tok["callCount"],
                "latestContextUsed": tok["latestContextUsed"],
                "peakContextUsed": tok["peakContextUsed"],
                "maxContextTokens": tok["maxContextTokens"],
                "contextFillPct": ctx_pct,
                "avgTtftMs": tok["avgTtftMs"],
                "models": convo_models,
                "primaryModel": convo_models[0]["label"] if convo_models else "—",
                "turns": labeled_turns,
            }
        )

        totals["conversationCount"] += 1
        if root_cid != cid:
            totals["subagentCount"] += 1
        else:
            totals["rootConversationCount"] += 1
        totals["inputTokens"] += tok["inputTokens"]
        totals["cacheReadTokens"] += tok["cacheReadTokens"]
        totals["outputTokens"] += tok["outputTokens"]
        totals["thinkingTokens"] += tok["thinkingTokens"]
        totals["totalTokens"] += tok["totalTokens"]
        totals["callCount"] += tok["callCount"]
        totals["stepCount"] += step_count

    conversations.sort(key=lambda c: c["lastModifiedTime"] or "", reverse=True)
    daily_series = sorted(global_daily.values(), key=lambda d: d["date"])
    model_series = sorted(global_models.values(), key=lambda m: m["totalTokens"], reverse=True)
    workspace_series = sorted(global_workspaces.values(), key=lambda w: w["totalTokens"], reverse=True)

    prompt_total = totals["inputTokens"] + totals["cacheReadTokens"]
    totals["cacheHitPct"] = round((totals["cacheReadTokens"] / prompt_total) * 100, 1) if prompt_total else 0.0

    return {
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "currentConversationId": os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
        "totals": totals,
        "quotas": quotas,
        "dailyUsage": daily_series,
        "modelUsage": model_series,
        "workspaceUsage": workspace_series,
        "conversations": conversations,
    }


class SidecarHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[convo-dashboard] {fmt % args}\n")

    def _validate_token(self, parsed_url) -> bool:
        if parsed_url.path == "/preload.js":
            return True
        expected = os.environ.get("ANTIGRAVITY_SIDECAR_UI_TOKEN", "")
        if not expected:
            return True
        header_tok = self.headers.get("x-sidecar-token") or self.headers.get("X-Sidecar-Token")
        qs = urllib.parse.parse_qs(parsed_url.query)
        query_tok = (qs.get("token") or [""])[0]
        return header_tok == expected or query_tok == expected

    def _send_json(self, data: dict, status: int = 200):
        raw = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _read_body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._validate_token(parsed):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        if parsed.path == "/preload.js":
            js = load_preload_js().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(js)))
            self.end_headers()
            self.wfile.write(js)
            return

        if parsed.path in ("/", "/index.html", "/auxpane"):
            html_path = HERE / "index.html"
            body = html_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/dashboard":
            try:
                payload = build_dashboard_payload()
                self._send_json(payload, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if not self._validate_token(parsed):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"Unauthorized")
            return

        body = self._read_body_json()

        if parsed.path == "/api/dashboard":
            if body.get("forceRefresh"):
                with _CACHE_LOCK:
                    _METADATA_CACHE.clear()
            try:
                payload = build_dashboard_payload()
                self._send_json(payload, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/api/focus-conversation":
            cid = body.get("cascadeId") or ""
            if not cid:
                self._send_json({"error": "cascadeId required"}, 400)
                return
            try:
                ls_rpc("SmartFocusConversation", {"cascadeId": cid})
                self._send_json({"ok": True, "cascadeId": cid})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 200)
            return

        if parsed.path == "/_sidecar/get-conversation-metadata":
            cid = body.get("conversationId") or ""
            try:
                res = run_agentapi(["get-conversation-metadata", cid]) if cid else {}
                self._send_json(res, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 200)
            return

        if parsed.path == "/_sidecar/send-message":
            try:
                args = ["send-message"]
                if body.get("title"):
                    args.extend(["--title", body["title"]])
                args.extend([body.get("conversationId", ""), body.get("message", "")])
                res = run_agentapi(args, project_id=body.get("projectId", ""))
                self._send_json(res, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if parsed.path == "/_sidecar/new-conversation":
            try:
                model = body.get("model") or "flash"
                args = ["new-conversation", "--model", model]
                if body.get("title"):
                    args.extend(["--title", body["title"]])
                args.append(body.get("message", ""))
                res = run_agentapi(args, project_id=body.get("projectId", ""))
                self._send_json(res, 200)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)


def main():
    port = int(os.environ.get("ANTIGRAVITY_SIDECAR_WEB_PORT", "0"))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), SidecarHandler)
    actual_port = server.server_address[1]
    print(f"[convo-dashboard] listening on 127.0.0.1:{actual_port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
