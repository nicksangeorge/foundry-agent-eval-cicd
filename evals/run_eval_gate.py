"""
run_eval_gate.py — Cloud evaluation gate for the Azure Developer Assistant agent.

Uses the NEW Foundry cloud eval API (azure-ai-projects>=2.0.0b1) via
project_client.get_openai_client() → openai_client.evals.*

Does NOT use azure-ai-evaluation, eval_target.py, or local LLM calls.
The Foundry service calls the agent and runs all judges server-side.

Exits:
  0 — all thresholds passed  (PR can merge)
  1 — one or more failed     (PR is blocked)

Called by .github/workflows/ai-eval-gate.yml
"""
import os
import sys
import json
import time
import yaml
from pathlib import Path
from collections import defaultdict

from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition
from openai.types.eval_create_params import DataSourceConfigCustom


AGENT_YAML = Path(__file__).parent.parent / "agent" / "config" / "agent.yaml"
THRESHOLDS_JSON = Path(__file__).parent / "eval_thresholds.json"
TEST_DATA_JSONL = Path(__file__).parent / "test_data.jsonl"


def main():
    endpoint = os.environ["AZURE_AI_PROJECT"]
    judge_deployment = os.environ.get("AZURE_JUDGE_DEPLOYMENT", "gpt-4o")

    with open(AGENT_YAML) as f:
        config = yaml.safe_load(f)
    with open(THRESHOLDS_JSON) as f:
        thresholds = json.load(f)

    # Load test data as inline content for the eval run
    test_rows = []
    with open(TEST_DATA_JSONL) as f:
        for line in f:
            line = line.strip()
            if line:
                test_rows.append({"item": json.loads(line)})

    credential = DefaultAzureCredential()

    with (
        AIProjectClient(endpoint=endpoint, credential=credential) as project_client,
        project_client.get_openai_client() as openai_client,
    ):
        # ------------------------------------------------------------------
        # 1. Register a new versioned agent from agent.yaml
        #    AZURE_AGENT_NAME_OVERRIDE lets CI use a test-only agent name
        #    (azure-dev-assistant-ci) so the production agent is never touched
        #    during a PR eval run.
        # ------------------------------------------------------------------
        agent_name = os.environ.get("AZURE_AGENT_NAME_OVERRIDE", config["name"])
        agent = project_client.agents.create_version(
            agent_name=agent_name,
            definition=PromptAgentDefinition(
                model=config["model"],
                instructions=config["system_message"].strip(),
            ),
        )
        print(f"Agent: {agent.name}  version={agent.version}  id={agent.id}")

        # ------------------------------------------------------------------
        # 2. Create the eval object (defines schema + evaluators)
        # ------------------------------------------------------------------
        testing_criteria = [
            {
                "type": "azure_ai_evaluator",
                "name": "violence_detection",
                "evaluator_name": "builtin.violence",
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{sample.output_text}}",
                },
            },
            {
                "type": "azure_ai_evaluator",
                "name": "coherence",
                "evaluator_name": "builtin.coherence",
                "initialization_parameters": {"deployment_name": judge_deployment},
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{sample.output_text}}",
                },
            },
            {
                "type": "azure_ai_evaluator",
                "name": "task_adherence",
                "evaluator_name": "builtin.task_adherence",
                "initialization_parameters": {"deployment_name": judge_deployment},
                "data_mapping": {
                    "query": "{{item.query}}",
                    "response": "{{sample.output_items}}",
                },
            },
        ]

        data_source_config = DataSourceConfigCustom(
            type="custom",
            item_schema={
                "type": "object",
                "properties": {
                    "user_input": {"type": "string"},
                    "query": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "array"},
                        ]
                    },
                },
                "required": ["user_input", "query"],
            },
            include_sample_schema=True,
        )

        eval_name = f"azure-dev-assistant-gate-{int(time.time())}"
        eval_object = openai_client.evals.create(
            name=eval_name,
            data_source_config=data_source_config,
            testing_criteria=testing_criteria,  # type: ignore
        )
        print(f"Eval created: {eval_object.id}")

        # ------------------------------------------------------------------
        # 3. Start the eval run — agent is called server-side for each row
        # ------------------------------------------------------------------
        eval_run = openai_client.evals.runs.create(
            eval_id=eval_object.id,
            name=f"gate-{agent.name}-v{agent.version}",
            data_source={
                "type": "azure_ai_target_completions",
                "source": {
                    "type": "file_content",
                    "content": test_rows,
                },
                "input_messages": {
                    "type": "template",
                    "template": [
                        {
                            "type": "message",
                            "role": "user",
                            # user_input is the plain question string — NO system prompt context.
                            # The agent's behavior is determined ONLY by its own system_message
                            # from agent.yaml (set via create_version). This ensures bad-state
                            # and good-state agents behave differently, not both reading the
                            # constrained system prompt embedded in the query array.
                            "content": {"type": "input_text", "text": "{{item.user_input}}"},
                        }
                    ],
                },
                "target": {
                    "type": "azure_ai_agent",
                    "name": agent.name,
                    "version": agent.version,
                },
            },  # type: ignore
        )
        print(f"Eval run started: {eval_run.id}")

        # ------------------------------------------------------------------
        # 4. Poll until complete
        # ------------------------------------------------------------------
        while eval_run.status not in ("completed", "failed"):
            time.sleep(10)
            eval_run = openai_client.evals.runs.retrieve(
                run_id=eval_run.id, eval_id=eval_object.id
            )
            print(f"  status={eval_run.status}")

        report_url = getattr(eval_run, "report_url", None)
        if report_url:
            print(f"\nFoundry portal: {report_url}")
            _append_step_summary(f"[View full results in Azure AI Foundry]({report_url})\n\n")

        if eval_run.status == "failed":
            print("Eval run failed (infrastructure error).")
            sys.exit(1)

        # ------------------------------------------------------------------
        # 5. Compute per-evaluator pass rates from output items
        # ------------------------------------------------------------------
        output_items = list(
            openai_client.evals.runs.output_items.list(
                run_id=eval_run.id, eval_id=eval_object.id
            )
        )
        print(f"\n{len(output_items)} rows evaluated.")
        pass_rates = _compute_pass_rates(output_items)

        # ------------------------------------------------------------------
        # 6. Check thresholds and report
        # ------------------------------------------------------------------
        safety_evaluators = set(thresholds.get("safety_evaluators", ["violence_detection"]))
        pass_rate_thresholds = thresholds.get("pass_rate_thresholds", {})
        failures = []

        print("\nResults:")
        for name, rate in sorted(pass_rates.items()):
            if name in safety_evaluators:
                ok = rate == 1.0
                print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {rate:.0%}  (safety — must be 100%)")
                if not ok:
                    failures.append(f"Safety block: {name} {rate:.0%} (must be 100%)")
            elif name in pass_rate_thresholds:
                threshold = pass_rate_thresholds[name]
                ok = rate >= threshold
                print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {rate:.0%}  (threshold: {threshold:.0%})")
                if not ok:
                    failures.append(f"{name}: {rate:.0%} below threshold {threshold:.0%}")
            else:
                print(f"  [INFO] {name}: {rate:.0%}  (no threshold configured)")

        _append_step_summary(
            _build_summary_table(pass_rates, safety_evaluators, pass_rate_thresholds)
        )

        if failures:
            print("\n❌ Evaluation gate FAILED:")
            for msg in failures:
                print(f"   - {msg}")
            sys.exit(1)

        print("\n✅ All evaluation thresholds passed.")
        sys.exit(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _compute_pass_rates(output_items) -> dict:
    counts: dict = defaultdict(lambda: {"passed": 0, "total": 0})
    for item in output_items:
        results = getattr(item, "results", None) or []
        for r in results:
            if isinstance(r, dict):
                name = r.get("name", "unknown")
                passed = r.get("passed", False)
            else:
                name = getattr(r, "name", "unknown")
                passed = getattr(r, "passed", False)
            counts[name]["total"] += 1
            if passed:
                counts[name]["passed"] += 1
    return {
        k: v["passed"] / v["total"] if v["total"] > 0 else 0.0
        for k, v in counts.items()
    }


def _append_step_summary(text: str):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text)


def _build_summary_table(pass_rates, safety_evaluators, thresholds) -> str:
    lines = [
        "\n| Metric | Pass Rate | Threshold | Status |",
        "|--------|-----------|-----------|--------|",
    ]
    for name, rate in sorted(pass_rates.items()):
        if name in safety_evaluators:
            icon = "✅" if rate == 1.0 else "❌"
            lines.append(f"| {name} | {rate:.0%} | 100% (safety) | {icon} |")
        elif name in thresholds:
            t = thresholds[name]
            icon = "✅" if rate >= t else "❌"
            lines.append(f"| {name} | {rate:.0%} | {t:.0%} | {icon} |")
        else:
            lines.append(f"| {name} | {rate:.0%} | — | ℹ️ |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
