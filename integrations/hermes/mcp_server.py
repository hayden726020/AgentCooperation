"""Stdio JSON-RPC MCP Server exposing AgentCooperation capabilities."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure workspace root is in sys.path
_root = Path(__file__).resolve().parent.parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from integrations.hermes.bridge import HermesCooperationBridge
from integrations.hermes.models import HermesApprovalRequest, HermesSubmitRequest

logger = logging.getLogger("agent_cooperation.mcp")

BRIDGE: Optional[HermesCooperationBridge] = None


def get_bridge() -> HermesCooperationBridge:
    global BRIDGE
    if BRIDGE is None:
        BRIDGE = HermesCooperationBridge()
    return BRIDGE


TOOLS = [
    {
        "name": "coop_submit_task",
        "description": "Submit a software engineering task to the AgentCooperation workflow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string", "description": "Task requirement specification"},
                "repo_root": {"type": "string", "description": "Target repository path"},
                "model": {"type": "string", "enum": ["glm", "deepseek"], "description": "Flash model choice"},
                "title": {"type": "string", "description": "Task title"},
                "mock_mode": {"type": "boolean", "description": "Run in simulation mode"},
                "idempotency_key": {"type": "string", "description": "Persistent deduplication key"},
            },
            "required": ["requirement"],
        },
    },
    {
        "name": "coop_query_task",
        "description": "Query task state, test logs, review verdict, and artifacts.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Unique task ID"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "coop_list_tasks",
        "description": "List recent tasks managed by AgentCooperation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max tasks to list"},
                "status_filter": {"type": "string", "description": "State filter"},
            },
        },
    },
    {
        "name": "coop_get_pending_approvals",
        "description": "List P0 tasks waiting for approval and queued Codex supplemental reviews.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "coop_approve_task",
        "description": "Approve or reject a task in WAITING_APPROVAL state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "approved": {"type": "boolean", "description": "True to approve, False to reject"},
                "notes": {"type": "string", "description": "Optional approval notes"},
            },
            "required": ["task_id", "approved"],
        },
    },
    {
        "name": "coop_check_budget",
        "description": "Check current monthly spending, reservations, and quota.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "coop_run_dev_plan",
        "description": "Directly invoke Primary Dev Agent (GLM/DeepSeek Flash) to generate a task plan with acceptance tests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string", "description": "Task requirement to plan"},
                "model": {"type": "string", "enum": ["glm", "deepseek"], "description": "Model engine choice"},
                "repo_root": {"type": "string", "description": "Target repo path"},
                "mock_mode": {"type": "boolean", "description": "Mock simulation mode"},
            },
            "required": ["requirement"],
        },
    },
    {
        "name": "coop_run_review",
        "description": "Directly invoke Independent Reviewer (Antigravity/Gemini/Codex CLI) on code and test evidence.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worktree_path": {"type": "string", "description": "Path to worktree directory"},
                "requirement": {"type": "string", "description": "Original requirement"},
                "git_diff": {"type": "string", "description": "Git diff string"},
                "test_summary": {"type": "string", "description": "Test execution output"},
                "mock_mode": {"type": "boolean", "description": "Mock simulation mode"},
            },
            "required": ["worktree_path", "requirement"],
        },
    },
    {
        "name": "coop_run_expert_diagnose",
        "description": "Directly invoke Codex Expert to diagnose an issue or failure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issue_context": {"type": "string", "description": "Description of issue, tests, diff"},
                "worktree_path": {"type": "string", "description": "Worktree path"},
                "mock_mode": {"type": "boolean", "description": "Mock simulation mode"},
            },
            "required": ["issue_context", "worktree_path"],
        },
    },
    {
        "name": "coop_run_gemini_closed_loop",
        "description": "Execute end-to-end Gemini development with GLM or DeepSeek independent review in an isolated worktree.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "requirement": {"type": "string", "description": "Task requirement specification"},
                "repo_root": {"type": "string", "description": "Target repository path"},
                "reviewer": {"type": "string", "enum": ["glm", "deepseek"], "description": "Independent reviewer choice"},
                "title": {"type": "string", "description": "Task title"},
                "mock_mode": {"type": "boolean", "description": "Run in simulation mode"},
                "custom_test_cmd": {"type": "string", "description": "Custom test command override"},
                "max_repair_attempts": {"type": "integer", "description": "Maximum repair attempts (default 3)"},
            },
            "required": ["requirement"],
        },
    },
]


async def handle_tool_call(name: str, arguments: Dict[str, Any]) -> Any:
    bridge = get_bridge()
    if name == "coop_run_gemini_closed_loop":
        res = await bridge.run_gemini_closed_loop(
            requirement=str(arguments.get("requirement", "")),
            repo_root=arguments.get("repo_root"),
            title=arguments.get("title"),
            reviewer_choice=arguments.get("reviewer", "glm"),
            mock_mode=bool(arguments.get("mock_mode", False)),
            custom_test_cmd=arguments.get("custom_test_cmd"),
            max_repair_attempts=arguments.get("max_repair_attempts"),
        )
        return res.model_dump()

    elif name == "coop_submit_task":
        req = HermesSubmitRequest(
            requirement=arguments.get("requirement", ""),
            repo_root=arguments.get("repo_root"),
            chosen_model=arguments.get("model"),
            title=arguments.get("title"),
            mock_mode=arguments.get("mock_mode", False),
            idempotency_key=arguments.get("idempotency_key"),
        )
        res = await bridge.submit_task(req)
        return res.model_dump()

    elif name == "coop_query_task":
        task_id = arguments.get("task_id", "")
        res = bridge.query_task(task_id)
        return res.model_dump() if res else {"error": f"Task '{task_id}' not found"}

    elif name == "coop_list_tasks":
        limit = int(arguments.get("limit", 20))
        res = bridge.list_tasks(limit=limit, status_filter=arguments.get("status_filter"))
        return [t.model_dump() for t in res]

    elif name == "coop_get_pending_approvals":
        return bridge.get_pending_approvals()

    elif name == "coop_approve_task":
        req = HermesApprovalRequest(
            task_id=arguments.get("task_id", ""),
            approved=bool(arguments.get("approved", False)),
            notes=arguments.get("notes"),
        )
        res = await bridge.approve_task(req)
        return res.model_dump()

    elif name == "coop_check_budget":
        return bridge.check_budget_status().model_dump()

    elif name == "coop_run_dev_plan":
        res = await bridge.run_role_dev_plan(
            requirement=arguments.get("requirement", ""),
            repo_root=arguments.get("repo_root"),
            chosen_model=arguments.get("model"),
            mock_mode=bool(arguments.get("mock_mode", False)),
        )
        return res.model_dump()

    elif name == "coop_run_review":
        res = await bridge.run_role_reviewer(
            worktree_path=arguments.get("worktree_path", ""),
            requirement=arguments.get("requirement", ""),
            git_diff=arguments.get("git_diff", ""),
            test_summary=arguments.get("test_summary", ""),
            mock_mode=bool(arguments.get("mock_mode", False)),
        )
        return res.model_dump()

    elif name == "coop_run_expert_diagnose":
        res = await bridge.run_role_expert(
            issue_context=arguments.get("issue_context", ""),
            worktree_path=arguments.get("worktree_path", ""),
            mock_mode=bool(arguments.get("mock_mode", False)),
        )
        return res.model_dump()

    raise ValueError(f"Unknown tool: {name}")


async def run_mcp_stdio():
    """Cross-platform stdio JSON-RPC MCP server loop."""
    writer = sys.stdout

    while True:
        line = await asyncio.to_thread(sys.stdin.readline)
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        try:
            req = json.loads(text)
        except Exception:
            continue

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        response: Dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id}

        try:
            if method == "initialize":
                response["result"] = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "agent-cooperation", "version": "1.0.0"},
                }
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                response["result"] = {"tools": TOOLS}
            elif method == "tools/call":
                tool_name = params.get("name")
                arguments = params.get("arguments", {})
                tool_result = await handle_tool_call(tool_name, arguments)
                response["result"] = {
                    "content": [{"type": "text", "text": json.dumps(tool_result, ensure_ascii=False)}]
                }
            else:
                response["error"] = {"code": -32601, "message": f"Method not found: {method}"}
        except Exception as exc:
            response["error"] = {"code": -32000, "message": str(exc)}

        writer.write(json.dumps(response, ensure_ascii=False) + "\n")
        writer.flush()


if __name__ == "__main__":
    asyncio.run(run_mcp_stdio())
