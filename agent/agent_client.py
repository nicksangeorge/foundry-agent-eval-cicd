"""
agent_client.py — Creates a new versioned Foundry Agent from agent.yaml.

Uses create_version() which registers a new version of the named agent each time.
The cloud eval target always resolves to the latest version by agent name.

Usage:
    python agent/agent_client.py
"""
import os
import yaml
from pathlib import Path
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition

CONFIG_PATH = Path(__file__).parent / "config" / "agent.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def get_project_client() -> AIProjectClient:
    return AIProjectClient(
        endpoint=os.environ["AZURE_AI_PROJECT"],
        credential=DefaultAzureCredential(),
    )


def create_agent_version(client: AIProjectClient, config: dict):
    """Create a new versioned agent definition from config. Returns the agent object."""
    agent = client.agents.create_version(
        agent_name=config["name"],
        definition=PromptAgentDefinition(
            model=config["model"],
            instructions=config["system_message"].strip(),
        ),
    )
    print(f"Created agent '{agent.name}' version {agent.version} (id: {agent.id})")
    return agent


if __name__ == "__main__":
    config = load_config()
    client = get_project_client()
    agent = create_agent_version(client, config)
    print(f"AGENT_NAME={agent.name}")
    print(f"AGENT_VERSION={agent.version}")
