"""Multica API & CLI client implementations."""

import abc
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from integrations.multica.models import (
    MulticaComment,
    MulticaIssue,
    MulticaIssueStatus,
    utc_iso_now,
)


class BaseMulticaClient(abc.ABC):
    """Abstract interface for communicating with Multica."""

    @abc.abstractmethod
    async def list_issues(
        self, status: Optional[str] = None, assignee: Optional[str] = None
    ) -> List[MulticaIssue]:
        pass

    @abc.abstractmethod
    async def get_issue(self, issue_id: str) -> Optional[MulticaIssue]:
        pass

    @abc.abstractmethod
    async def create_issue(self, issue: MulticaIssue) -> MulticaIssue:
        pass

    @abc.abstractmethod
    async def update_issue_status(
        self,
        issue_id: str,
        status: MulticaIssueStatus,
        branch_name: Optional[str] = None,
    ) -> MulticaIssue:
        pass

    @abc.abstractmethod
    async def claim_issue(self, issue_id: str, assignee: str) -> MulticaIssue:
        pass

    @abc.abstractmethod
    async def post_comment(
        self, issue_id: str, body: str, author: str = "AgentCooperation"
    ) -> MulticaComment:
        pass

    @abc.abstractmethod
    async def list_comments(self, issue_id: str) -> List[MulticaComment]:
        pass


class MemoryMulticaClient(BaseMulticaClient):
    """In-memory or file-backed Multica client for isolated testing and offline operation."""

    def __init__(self, persistence_file: Optional[Path] = None):
        self._persistence_file = persistence_file
        self._issues: Dict[str, MulticaIssue] = {}
        self._comments: Dict[str, List[MulticaComment]] = {}
        self._load()

    def _load(self) -> None:
        if self._persistence_file and self._persistence_file.exists():
            try:
                data = json.loads(self._persistence_file.read_text(encoding="utf-8"))
                for issue_dict in data.get("issues", []):
                    issue = MulticaIssue.from_dict(issue_dict)
                    self._issues[issue.id] = issue
                for comment_dict in data.get("comments", []):
                    comment = MulticaComment.from_dict(comment_dict)
                    self._comments.setdefault(comment.issue_id, []).append(comment)
            except Exception:
                pass

    def _persist(self) -> None:
        if self._persistence_file:
            self._persistence_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "issues": [issue.to_dict() for issue in self._issues.values()],
                "comments": [
                    c.to_dict()
                    for comments_list in self._comments.values()
                    for c in comments_list
                ],
            }
            self._persistence_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    async def list_issues(
        self, status: Optional[str] = None, assignee: Optional[str] = None
    ) -> List[MulticaIssue]:
        results = list(self._issues.values())
        if status:
            results = [i for i in results if i.status == status]
        if assignee:
            results = [i for i in results if i.assignee == assignee]
        return results

    async def get_issue(self, issue_id: str) -> Optional[MulticaIssue]:
        return self._issues.get(issue_id)

    async def create_issue(self, issue: MulticaIssue) -> MulticaIssue:
        self._issues[issue.id] = issue
        self._persist()
        return issue

    async def update_issue_status(
        self,
        issue_id: str,
        status: MulticaIssueStatus,
        branch_name: Optional[str] = None,
    ) -> MulticaIssue:
        if issue_id not in self._issues:
            raise KeyError(f"Issue {issue_id} not found")
        issue = self._issues[issue_id]
        issue.status = status
        issue.updated_at = utc_iso_now()
        if branch_name:
            issue.branch_name = branch_name
        self._persist()
        return issue

    async def claim_issue(self, issue_id: str, assignee: str) -> MulticaIssue:
        if issue_id not in self._issues:
            raise KeyError(f"Issue {issue_id} not found")
        issue = self._issues[issue_id]
        issue.assignee = assignee
        issue.status = "in_progress"
        issue.updated_at = utc_iso_now()
        self._persist()
        return issue

    async def post_comment(
        self, issue_id: str, body: str, author: str = "AgentCooperation"
    ) -> MulticaComment:
        if issue_id not in self._issues:
            raise KeyError(f"Issue {issue_id} not found")
        comment = MulticaComment(
            id=f"cmt-{len(self._comments.get(issue_id, [])) + 1}",
            issue_id=issue_id,
            author=author,
            body=body,
            created_at=utc_iso_now(),
        )
        self._comments.setdefault(issue_id, []).append(comment)
        self._persist()
        return comment

    async def list_comments(self, issue_id: str) -> List[MulticaComment]:
        return list(self._comments.get(issue_id, []))


class CliMulticaClient(BaseMulticaClient):
    """Client that communicates with Multica using the official local `multica` CLI binary."""

    def __init__(self, cli_bin: str = "multica"):
        self.cli_bin = cli_bin
        if not shutil.which(cli_bin):
            raise FileNotFoundError(f"Multica CLI binary '{cli_bin}' is not installed or not in PATH")

    def _run_cli(self, args: List[str]) -> str:
        proc = subprocess.run(
            [self.cli_bin] + args,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Multica CLI command failed: {proc.stderr or proc.stdout}")
        return proc.stdout

    async def list_issues(
        self, status: Optional[str] = None, assignee: Optional[str] = None
    ) -> List[MulticaIssue]:
        args = ["issue", "list", "--json"]
        if status:
            args.extend(["--status", status])
        if assignee:
            args.extend(["--assignee", assignee])
        out = self._run_cli(args)
        data = json.loads(out)
        return [MulticaIssue.from_dict(item) for item in data]

    async def get_issue(self, issue_id: str) -> Optional[MulticaIssue]:
        args = ["issue", "get", issue_id, "--json"]
        try:
            out = self._run_cli(args)
            return MulticaIssue.from_dict(json.loads(out))
        except Exception:
            return None

    async def create_issue(self, issue: MulticaIssue) -> MulticaIssue:
        args = ["issue", "create", "--title", issue.title, "--description", issue.description, "--json"]
        if issue.priority:
            args.extend(["--priority", issue.priority])
        out = self._run_cli(args)
        return MulticaIssue.from_dict(json.loads(out))

    async def update_issue_status(
        self,
        issue_id: str,
        status: MulticaIssueStatus,
        branch_name: Optional[str] = None,
    ) -> MulticaIssue:
        args = ["issue", "update", issue_id, "--status", status, "--json"]
        if branch_name:
            args.extend(["--branch", branch_name])
        out = self._run_cli(args)
        return MulticaIssue.from_dict(json.loads(out))

    async def claim_issue(self, issue_id: str, assignee: str) -> MulticaIssue:
        args = ["issue", "claim", issue_id, "--assignee", assignee, "--json"]
        out = self._run_cli(args)
        return MulticaIssue.from_dict(json.loads(out))

    async def post_comment(
        self, issue_id: str, body: str, author: str = "AgentCooperation"
    ) -> MulticaComment:
        args = ["issue", "comment", issue_id, "--body", body, "--author", author, "--json"]
        out = self._run_cli(args)
        return MulticaComment.from_dict(json.loads(out))

    async def list_comments(self, issue_id: str) -> List[MulticaComment]:
        args = ["issue", "comments", issue_id, "--json"]
        out = self._run_cli(args)
        return [MulticaComment.from_dict(item) for item in json.loads(out)]


class HttpMulticaClient(BaseMulticaClient):
    """Client for connecting directly to a self-hosted Multica Server REST API."""

    def __init__(self, base_url: str = "http://localhost:8080", token: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.token = token or os.environ.get("MULTICA_API_TOKEN", "")

    def _request(self, method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        data_bytes = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "AgentCooperation-Multica-Client",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = urllib.request.Request(url, data=data_bytes, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                content = resp.read().decode("utf-8")
                return json.loads(content) if content else {}
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Multica API error {error.code} {error.reason}: {body}") from error

    async def list_issues(
        self, status: Optional[str] = None, assignee: Optional[str] = None
    ) -> List[MulticaIssue]:
        params = {}
        if status:
            params["status"] = status
        if assignee:
            params["assignee"] = assignee
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        data = self._request("GET", f"/api/v1/issues{query}")
        items = data if isinstance(data, list) else data.get("items", [])
        return [MulticaIssue.from_dict(item) for item in items]

    async def get_issue(self, issue_id: str) -> Optional[MulticaIssue]:
        try:
            data = self._request("GET", f"/api/v1/issues/{issue_id}")
            return MulticaIssue.from_dict(data)
        except Exception:
            return None

    async def create_issue(self, issue: MulticaIssue) -> MulticaIssue:
        data = self._request("POST", "/api/v1/issues", issue.to_dict())
        return MulticaIssue.from_dict(data)

    async def update_issue_status(
        self,
        issue_id: str,
        status: MulticaIssueStatus,
        branch_name: Optional[str] = None,
    ) -> MulticaIssue:
        payload: Dict[str, Any] = {"status": status}
        if branch_name:
            payload["branch_name"] = branch_name
        data = self._request("PATCH", f"/api/v1/issues/{issue_id}", payload)
        return MulticaIssue.from_dict(data)

    async def claim_issue(self, issue_id: str, assignee: str) -> MulticaIssue:
        payload = {"assignee": assignee, "status": "in_progress"}
        data = self._request("POST", f"/api/v1/issues/{issue_id}/claim", payload)
        return MulticaIssue.from_dict(data)

    async def post_comment(
        self, issue_id: str, body: str, author: str = "AgentCooperation"
    ) -> MulticaComment:
        payload = {"body": body, "author": author}
        data = self._request("POST", f"/api/v1/issues/{issue_id}/comments", payload)
        return MulticaComment.from_dict(data)

    async def list_comments(self, issue_id: str) -> List[MulticaComment]:
        data = self._request("GET", f"/api/v1/issues/{issue_id}/comments")
        items = data if isinstance(data, list) else data.get("items", [])
        return [MulticaComment.from_dict(item) for item in items]
