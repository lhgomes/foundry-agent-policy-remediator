"""Scheduled, fail-closed Foundry agent remediation from Policy Insights component states."""
from __future__ import annotations

import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

POLICY_DEFINITION_RE = re.compile(
    r"(?:/subscriptions/[^/\s]+|/providers/microsoft\.management/managementgroups/[^/\s]+)?"
    r"/providers/microsoft\.authorization/policydefinitions/[^/\s]+", re.I
)
POLICY_API = "2024-10-01"
FOUNDRY_API = "v1"
PROJECT_RE = re.compile(r"^/subscriptions/([^/]+)/resourcegroups/([^/]+)/providers/microsoft\.cognitiveservices/accounts/([^/]+)/projects/([^/]+)$", re.I)
COMPONENT_RE = re.compile(r"(?:^|/)accounts/([^/]+)/projects/([^/]+)/agents/([^/]+)/versions/([^/]+)$", re.I)


@dataclass(frozen=True)
class Settings:
    subscription_id: str
    sender_mailbox: str
    governance_recipients: tuple[str, ...]
    policy_definition_ids: tuple[str, ...]
    dry_run: bool = True
    max_age_minutes: int = 120
    require_serving_version: bool = True
    timeout: int = 30

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", self.subscription_id, re.I):
            raise ValueError("SUBSCRIPTION_ID must be a subscription GUID")
        if not self.policy_definition_ids or any(
            not isinstance(definition, str) or not POLICY_DEFINITION_RE.fullmatch(definition.strip())
            for definition in self.policy_definition_ids
        ):
            raise ValueError("POLICY_DEFINITION_IDS must contain at least one full policy definition resource ID")
        if not self.dry_run and not self.sender_mailbox.strip():
            raise ValueError("SENDER_MAILBOX is required when DRY_RUN=false; no agents will be disabled")

    @classmethod
    def from_env(cls):
        configured_definitions = os.getenv("POLICY_DEFINITION_IDS")
        if configured_definitions is None:
            raise ValueError("POLICY_DEFINITION_IDS is required")
        try:
            definitions = json.loads(configured_definitions)
        except json.JSONDecodeError as exc:
            raise ValueError("POLICY_DEFINITION_IDS must be a JSON array of policy definition resource IDs") from exc
        if not isinstance(definitions, list):
            raise ValueError("POLICY_DEFINITION_IDS must be a JSON array of policy definition resource IDs")
        return cls(
            subscription_id=os.environ["SUBSCRIPTION_ID"].strip(),
            sender_mailbox=os.getenv("SENDER_MAILBOX", "").strip(),
            governance_recipients=tuple(x.strip() for x in os.getenv("GOVERNANCE_RECIPIENTS", "").split(",") if x.strip()),
            dry_run=os.getenv("DRY_RUN", "true").lower() != "false",
            max_age_minutes=int(os.getenv("MAX_COMPONENT_AGE_MINUTES", "120")),
            require_serving_version=os.getenv("REQUIRE_SERVING_VERSION", "true").lower() != "false",
            timeout=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "30")),
            policy_definition_ids=tuple(definitions),
        )


def parse_component(component_id: str, account: str, project: str):
    match = COMPONENT_RE.search(component_id)
    if not match:
        return None
    a, p, agent, version = match.groups()
    if a.lower() != account.lower() or p.lower() != project.lower():
        return None
    return agent, version


def parse_timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def serving_versions(agent):
    """Return serving version IDs; None means routing could not be interpreted safely."""
    endpoint = agent.get("agent_endpoint") or {}
    selector = endpoint.get("version_selector") or {}
    rules = selector.get("version_selection_rules")
    if not isinstance(rules, list) or not rules:
        return None
    latest = ((agent.get("versions") or {}).get("latest") or {}).get("version")
    result = set()
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "FixedRatio":
            return None
        try:
            percentage = float(rule.get("traffic_percentage", 0))
        except (ValueError, TypeError):
            return None
        if percentage <= 0:
            continue
        version = rule.get("agent_version")
        if version == "@latest":
            version = latest
        if not version:
            return None
        result.add(str(version))
    return result or None


class Governance:
    def __init__(self, settings, credential, session=None):
        self.settings = settings
        self.credential = credential
        self.session = session or requests.Session()

    def project_match(self, resource_id):
        match = PROJECT_RE.fullmatch(resource_id)
        if not match or match[1].lower() != self.settings.subscription_id.lower():
            raise ValueError("Resource ID must identify a Foundry project in the configured subscription")
        return match

    def project_endpoint(self, resource_id):
        match = self.project_match(resource_id)
        return f"https://{match[3]}.services.ai.azure.com/api/projects/{quote(match[4], safe='')}"

    def call(self, method, url, scope, payload=None, expected=(200,)):
        token = self.credential.get_token(scope).token
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        response = self.session.request(method, url, headers=headers, json=payload, timeout=self.settings.timeout)
        if response.status_code not in expected:
            request_id = response.headers.get("apim-request-id") or response.headers.get("x-ms-request-id") or "unknown"
            raise RuntimeError(f"{method} returned HTTP {response.status_code}; request ID {request_id}: {response.text[:600]}")
        return response.json() if response.content else {}

    def components(self):
        url = (f"https://management.azure.com/subscriptions/{self.settings.subscription_id}"
               f"/providers/Microsoft.PolicyInsights/componentPolicyStates/latest/queryResults?api-version={POLICY_API}")
        # Query all pages. Filter locally because some component results omit optional fields.
        items = []
        while url:
            page = self.call("POST", url, "https://management.azure.com/.default", payload={})
            items.extend(page.get("value", []))
            url = page.get("@odata.nextLink") or page.get("nextLink")
        return items

    def agent(self, project_resource_id, name):
        url = f"{self.project_endpoint(project_resource_id)}/agents/{quote(name, safe='')}?api-version={FOUNDRY_API}"
        return self.call("GET", url, "https://ai.azure.com/.default")

    def disable(self, project_resource_id, name):
        url = f"{self.project_endpoint(project_resource_id)}/agents/{quote(name, safe='')}:disable?api-version={FOUNDRY_API}"
        self.call("POST", url, "https://ai.azure.com/.default", expected=(200, 202, 204))

    def notify(self, project_resource_id, agent_name, versions, owner, action):
        recipients = sorted(set(([owner] if owner else []) + list(self.settings.governance_recipients)))
        if not recipients:
            raise RuntimeError("No agent owner or governance recipient available")
        if not self.settings.sender_mailbox:
            raise RuntimeError("SENDER_MAILBOX is required for notifications")
        body = (f"<p>Foundry agent <b>{html.escape(agent_name)}</b> in project "
                f"<b>{html.escape(project_resource_id)}</b>: {html.escape(action)}.</p>"
                f"<p>Noncompliant serving versions: {html.escape(', '.join(sorted(versions)))}</p>"
                "<p>Review the configured Azure Policies, remediate the agent, "
                "and request re-enablement from the governance team.</p>")
        payload = {"message": {"subject": f"Foundry policy: {agent_name} - {action}",
                               "body": {"contentType": "HTML", "content": body},
                               "toRecipients": [{"emailAddress": {"address": r}} for r in recipients]},
                   "saveToSentItems": True}
        url = f"https://graph.microsoft.com/v1.0/users/{quote(self.settings.sender_mailbox, safe='@.')}/sendMail"
        self.call("POST", url, "https://graph.microsoft.com/.default", payload=payload, expected=(202,))

    @staticmethod
    def owner(agent):
        latest = ((agent.get("versions") or {}).get("latest") or {})
        for metadata in (agent.get("metadata") or {}, latest.get("metadata") or {}):
            for key in ("ownerEmail", "ownerUPN"):
                candidate = metadata.get(key)
                if isinstance(candidate, str) and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate):
                    return candidate
        return None

    def run(self):
        result = {"components": 0, "agents": 0, "disabled": 0, "dry_run": 0, "skipped": 0, "errors": 0}
        now = datetime.now(timezone.utc)
        policy_definitions = {definition.strip().lower() for definition in self.settings.policy_definition_ids}
        grouped = {}
        for row in self.components():
            if str(row.get("complianceState", "")).lower() != "noncompliant":
                continue
            definition = row.get("policyDefinitionId")
            if not isinstance(definition, str) or definition.lower() not in policy_definitions:
                # Fail closed rather than disabling from an unrelated policy.
                continue
            resource_id = str(row.get("resourceId") or "").rstrip("/")
            try:
                project = self.project_match(resource_id)
            except ValueError:
                logging.warning("Skipping component with invalid/out-of-scope project resourceId: %s", resource_id)
                result["skipped"] += 1
                continue
            parsed = parse_component(str(row.get("componentId") or ""), project[3], project[4])
            if not parsed:
                continue  # Toolbox or unrelated component; never disable it.
            result["components"] += 1
            agent_name, version = parsed
            timestamp = parse_timestamp(row.get("timestamp"))
            if timestamp is None or now - timestamp > timedelta(minutes=self.settings.max_age_minutes) or timestamp > now + timedelta(minutes=5):
                logging.warning("Skipping stale/invalid component state: %s", row.get("componentId"))
                result["skipped"] += 1
                continue
            grouped.setdefault((resource_id.lower(), agent_name), set()).add(version)

        for (project_resource_id, name), violating_versions in grouped.items():
            result["agents"] += 1
            try:
                agent = self.agent(project_resource_id, name)
                if str(agent.get("state", "")).lower() == "disabled" or str(agent.get("configuration_state", "")).lower() == "disabled":
                    result["skipped"] += 1
                    continue
                serving = serving_versions(agent)
                if self.settings.require_serving_version and (serving is None or not (serving & violating_versions)):
                    logging.warning("Skipping %s in %s: noncompliant versions %s, serving versions %s", name, project_resource_id, violating_versions, serving)
                    result["skipped"] += 1
                    continue
                impacted = violating_versions & serving if serving else violating_versions
                owner = self.owner(agent)
                if not owner and not self.settings.governance_recipients:
                    raise RuntimeError("Owner metadata missing and no governance fallback configured")
                if self.settings.dry_run:
                    logging.warning("DRY RUN: would disable %s in %s due to versions %s", name, project_resource_id, sorted(impacted))
                    result["dry_run"] += 1
                    # Dry run intentionally does not send email to avoid recurring notifications.
                    continue
                self.disable(project_resource_id, name)
                result["disabled"] += 1
                try:
                    self.notify(project_resource_id, name, impacted, owner, "disabled for policy noncompliance")
                except Exception:
                    logging.exception("Agent %s in %s disabled, but notification failed", name, project_resource_id)
                    result["errors"] += 1
            except Exception:
                logging.exception("Failed processing agent %s in %s", name, project_resource_id)
                result["errors"] += 1
        return result
