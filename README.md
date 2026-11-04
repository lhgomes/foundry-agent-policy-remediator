# Foundry Agent Policy Remediator

Scheduled Azure Function (Python v2) that queries **Azure Policy component compliance** across a subscription's Foundry projects and disables agents with a **currently serving noncompliant version**, then notifies the owner/governance mailbox.

> **Status: reference implementation, not production validated.** Starts in dry-run mode. Verify the API response fields, component timestamps, permissions, and endpoint routing against your tenant before enabling enforcement. Disabling an agent interrupts traffic.

## Behavior

1. Query component compliance for the configured subscription (all returned pages).
2. Select `NonCompliant` agent-version components matching any policy in `POLICY_DEFINITION_IDS`. Ignore toolbox components. Violations from multiple policies are combined per project and agent, so each agent is processed only once per run.
3. Reject stale/undated component findings (default: 120 minutes).
4. Resolve each finding's project from `resourceId`, fetch the agent from that project's endpoint, and resolve versions receiving traffic, including `@latest`. Group by project and agent name so identically named agents in different projects remain independent.
5. Skip historical-only violations and unknown routing (fail closed).
6. In dry run, log proposed actions only. Otherwise call the documented `POST /agents/{name}:disable?api-version=v1` endpoint and email the owner plus governance recipients.

The function disables the **whole agent endpoint**, not an individual version. It does not remove toolboxes or automatically re-enable agents. Agent owner is resolved only from `metadata.ownerEmail` / `metadata.ownerUPN` on the agent or latest version. If no owner exists, governance recipients are required.

## Configuration

Copy `local.settings.sample.json` to `local.settings.json` and update values. Never commit credentials or `local.settings.json`.

| Setting | Required | Meaning |
|---|---|---|
| `SUBSCRIPTION_ID` | Yes | Subscription GUID. Scans all Foundry projects with matching component findings in that subscription. |
| `POLICY_DEFINITION_IDS` | Yes | Non-empty JSON array encoded as a string, containing full policy definition resource IDs. No default or fallback is used. |
| `TIMER_SCHEDULE` | Yes | Azure Functions NCRONTAB; sample every 15 minutes. |
| `DRY_RUN` | No | Defaults to `true`. Set `false` only after validation. |
| `MAX_COMPONENT_AGE_MINUTES` | No | Maximum age of component finding (120 by default). |
| `REQUIRE_SERVING_VERSION` | No | Defaults `true`; avoid disabling for historical-only violations. |
| `SENDER_MAILBOX` | When `DRY_RUN=false` | Mailbox from which Graph sends email. Missing or blank values fail validation before querying Azure or disabling agents. |
| `GOVERNANCE_RECIPIENTS` | Recommended | Comma-separated fallback addresses. |
| `REQUEST_TIMEOUT_SECONDS` | No | Default 30. |

No project resource ID or policy assignment ID environment setting is required. Project resource IDs come from each component finding. Findings with missing, invalid, or out-of-subscription project resource IDs are logged and skipped.

### Configure multiple policies

Set `POLICY_DEFINITION_IDS` to a JSON array of full policy definition resource IDs. Azure Function application settings and `local.settings.json` values are strings, so encode the array as a string inside `Values`:

```json
{
  "POLICY_DEFINITION_IDS": "[\"/providers/Microsoft.Authorization/policyDefinitions/e7352ad2-6c86-4a07-9573-47f67b557ba8\", \"/subscriptions/<subscription-id>/providers/Microsoft.Authorization/policyDefinitions/<custom-policy-name>\"]"
}
```

Replace the custom-policy placeholders with a real definition resource ID. In the Azure portal's application setting value, enter the array directly, without the outer JSON string escaping:

```json
["/providers/Microsoft.Authorization/policyDefinitions/e7352ad2-6c86-4a07-9573-47f67b557ba8", "/subscriptions/<subscription-id>/providers/Microsoft.Authorization/policyDefinitions/<custom-policy-name>"]
```

Built-in, subscription-scoped, and management-group-scoped definition resource IDs are supported. Matching is case-insensitive; do not use assignment IDs, initiative IDs, or bare policy names/GUIDs. The configured list is the sole source of policies. A missing setting, empty list, malformed JSON, or invalid definition ID fails configuration validation before querying Azure.

The approved-tools built-in definition in the sample settings is an explicit example, not a code default. Configure at least one valid definition resource ID. Validation checks ID format, not whether the definition exists in Azure. Only policies reporting agent-version **component** findings can trigger this function; resource-level findings or other component types are not processed. Review each policy's suitability for whole-agent disabling before adding it. Multiple assignments of a selected definition are included; assignment-specific filtering is not implemented.

Restart the local Function host after changing the list and validate with `DRY_RUN=true`.

## Authentication and permissions

Enable a managed identity on the Function App. Grant only the permissions required to:

- Query Azure Policy Insights component policy states at subscription scope.
- Read and disable agents on each target Foundry project's data plane.
- Send Graph mail as the designated mailbox (`Mail.Send` application permission with appropriate Exchange application access restriction).

**The exact least-privilege Foundry data actions for `:disable` have not been confirmed.** Validate roles in the target environment. Do not assign broad roles by default.

For local testing, `DefaultAzureCredential` can use an authenticated developer identity. The sample requires Azure Functions Core Tools and Azurite or an Azure Storage connection for timer scheduling.

## Configure email notifications

Email is sent through Microsoft Graph, not SMTP. No SMTP server or mailbox password is required.

### Sender and recipients

Set these values in `local.settings.json` for local runs, or in Function App application settings when deployed:

```json
{
  "SENDER_MAILBOX": "governance@yourcompany.com",
  "GOVERNANCE_RECIPIENTS": "recipient@yourcompany.com",
  "DRY_RUN": "true"
}
```

In `local.settings.json`, place these entries inside the existing `Values` object; do not replace the rest of the configuration.

- `SENDER_MAILBOX` must identify an existing Exchange Online mailbox. Use its user principal name or Microsoft Entra user object ID.
- `GOVERNANCE_RECIPIENTS` accepts comma-separated addresses. Notifications go to these recipients plus the agent owner, when `metadata.ownerEmail` or `metadata.ownerUPN` is available.
- Restart the local Function host after changing settings so the new values are loaded.

### Managed identity authentication (recommended)

Use the Azure Function App's **system-assigned managed identity**. No separate app registration, client secret, or application credential environment variables are required.

1. In the Azure portal, open **Function App > Identity > System assigned**, set **Status** to **On**, and save. Record the identity's principal (object) ID.
2. Grant this identity the required Azure Policy Insights query access at subscription scope and Foundry agent permissions on each target project. The code uses the same credential for Azure Resource Manager, Foundry, and Microsoft Graph.
3. Have an authorized administrator assign the Microsoft Graph **`Mail.Send` application role** to the managed identity's service principal, identified by its principal ID. This is a Graph app-role assignment granting application permission, not an Azure subscription RBAC role; assigning Azure Contributor or Reader does not grant mail-sending permission.
4. Configure `SENDER_MAILBOX` and `GOVERNANCE_RECIPIENTS` in the Function App's application settings. Keep `DRY_RUN=true` during setup.
5. Remove any previously configured client-secret authentication settings so they do not take precedence over managed identity. The existing `DefaultAzureCredential` automatically uses the managed identity when running in Azure; no code change is required.

Application `Mail.Send` grants broad sending access by default. Have an Exchange administrator restrict access to the intended sender mailbox using appropriate Exchange application access controls. Validate that the selected mailbox is allowed before enabling enforcement.

### Local testing limitations

The Azure Function App's managed identity is not directly available to `func start` on a local PC or WSL. Use `az login` for local dry-run testing of policy queries and agent reads with your developer identity. An ordinary Azure CLI login does not automatically grant Microsoft Graph permission to send mail.

Validate managed-identity email delivery from an Azure-hosted environment using the configured identity and mailbox, separately from the enforcement function. Do not introduce client secrets to reproduce managed identity locally.

### Validate safely before enforcement

**Keep `DRY_RUN=true` while configuring email. Dry runs neither disable agents nor send emails.**

The current implementation sends notifications only **after disabling an agent**; it has no email-only test mode. Do not set `DRY_RUN=false` just to test email, because that also enables subscription-wide agent enforcement. Test Graph email delivery separately with the intended identity and mailbox before enabling enforcement.

Enforcement configuration rejects a missing or blank `SENDER_MAILBOX` before querying Azure or disabling any agents. This validates only that a sender is configured, not mailbox existence or Graph permissions. Validate email delivery separately first: a notification failure after disable leaves the agent disabled and marks the run as failed. Already-disabled agents are skipped on subsequent runs, so rerunning does not automatically retry their failed notifications. Microsoft Graph returning `202 Accepted` means the email request was accepted, not that delivery is guaranteed; investigate Exchange delivery, quarantine, and message tracing if mail does not arrive.

See [Microsoft Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0) for permission and response details.

## Run locally

### 1. Install prerequisites

- Python with `venv` support, using a version supported by Azure Functions.
- [Azure Functions Core Tools v4](https://learn.microsoft.com/en-us/azure/azure-functions/functions-run-local), which provides `func`.
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli), which provides `az`.
- [Node.js and npm](https://nodejs.org/en/download), which provide `npm` and `npx` for running the Azurite storage emulator.

The Python virtual environment does not install these command-line tools. If `azurite: command not found` appears, use the `npx` command in step 3 or install Azurite globally as shown there.

If `func: command not found` appears, install Core Tools v4 using npm:

```bash
npm install -g azure-functions-core-tools@4
func --version
```

The version should start with `4.`. If global npm installation fails with a permissions error, use the Linux package-manager installation in the Core Tools link above, or run the Function without a global installation:

```bash
npx --yes --package azure-functions-core-tools@4 func start
```

Use this `npx` command in place of `func start` in step 4, after configuring local settings and starting Azurite.

### 2. Configure Python and local settings

From the project directory:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
if [ ! -e local.settings.json ]; then
  cp local.settings.sample.json local.settings.json
fi
```

Copy the sample only if `local.settings.json` does not already exist; do not overwrite an existing configuration.

Edit `local.settings.json`:

- Set `SUBSCRIPTION_ID` to your subscription GUID.
- Set `POLICY_DEFINITION_IDS` to the policy definition resource IDs you intend to enforce, using the JSON-array string format described above. The sample includes one example; the setting is required and has no code default.
- Keep `DRY_RUN` set to `"true"` for initial validation.
- Set `GOVERNANCE_RECIPIENTS` to your email address (or comma-separated addresses). This is required as a fallback when an agent has no owner metadata, including in dry runs.
- Leave `SENDER_MAILBOX` empty during dry runs; it is required when enforcement sends notifications.
- Keep `AzureWebJobsStorage` set to `"UseDevelopmentStorage=true"` when using Azurite.

Authenticate with an identity that has Policy Insights query access at subscription scope and permission to read agents in each target project:

```bash
az login
az account set --subscription "<your-subscription-guid>"
```

### 3. Start Azurite

In a separate terminal, run:

```bash
npx --yes --package azurite azurite --location /tmp/foundry-remediator-azurite
```

This downloads Azurite if needed without requiring a global installation. Leave this terminal running while the Function runs.

Alternatively, install Azurite globally once, then start it:

```bash
npm install -g azurite
azurite --location /tmp/foundry-remediator-azurite
```

If you use an Azure Storage account instead, replace `AzureWebJobsStorage` with its connection string and skip Azurite. Never commit that connection string.

### 4. Start the Function

Back in the project terminal with the Python virtual environment activated:

```bash
func start
```

The sample timer runs every 15 minutes, at minutes 00, 15, 30, and 45. Startup does not explicitly trigger a run, but a past-due scheduled invocation can run immediately after the host starts. Keep `DRY_RUN=true` during setup.

### 5. Trigger an immediate run

While `func start` is running, use another terminal:

```bash
curl -i -X POST \
  http://localhost:7071/admin/functions/enforce_foundry_policy \
  -H "Content-Type: application/json" \
  -d '{"input":""}'
```

If the host starts on a different port, use the port shown in its output. Check the Function terminal for the governance run summary and any errors.

With `DRY_RUN=true`, the Function queries real Azure policy findings and reads agents, but **does not disable agents or send emails**. No matching findings may produce a summary with zero agents. Keep dry run enabled until you have validated the subscription-wide scope and permissions.

Stop the Function and Azurite with `Ctrl+C` in their respective terminals when finished.

### Unit tests

Unit tests do not require Azure login or Azurite:

```bash
python -m unittest discover -s tests -v
```

## Deploy

Create a Python Azure Function App with managed identity and application settings matching the sample. Deploy using Azure Functions Core Tools or your CI/CD pipeline. Validate `DRY_RUN=true` logs before setting `DRY_RUN=false`. Enable Application Insights alerts for function failures and repeated skipped findings.

## Safety and known boundaries

- **Validation is limited, not production certification.** Local Azure CLI-authenticated runs have exercised policy queries, agent reads, and agent disabling. Managed-identity execution and successful email delivery have not been verified. Validate response fields, pagination, permissions, and routing in your own tenant.
- Component compliance is asynchronous; an agent with no reported component findings will not be processed, including newly created agents awaiting evaluation. The function ignores findings older than `MAX_COMPONENT_AGE_MINUTES` and does not inspect the live tool list. A recently remediated agent could still have a fresh stale finding; consider an additional live policy check before production enforcement.
- When endpoint routing cannot be understood, it skips the agent. The only recognized routing rule is `FixedRatio`.
- If multiple policy assignments use the same configured definition, they are not distinguished without an assignment filter.
- If email fails **after** disable, the function reports an error; the agent remains disabled and should be investigated.
- Dry run intentionally does not send emails on every timer execution.
- Scope is **one subscription**. Only projects represented in the policy component findings are processed; no separate project inventory is required. Notifications and logs identify the project resource ID.

## References

- [Azure Policy Component Policy States REST API](https://learn.microsoft.com/en-us/rest/api/policyinsights/component-policy-states/list-query-results-for-subscription?view=rest-policyinsights-2024-10-01)
- [Manage Foundry agents, including disable](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/manage-hosted-agent)
- [Foundry Agents REST API](https://learn.microsoft.com/en-us/rest/api/aifoundry/project/agents?view=foundry-classic)
- [Microsoft Graph sendMail](https://learn.microsoft.com/en-us/graph/api/user-sendmail)
