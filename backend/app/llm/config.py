"""Loads and validates config/models.yaml."""

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"google", "openai", "anthropic", "xai"}


class ProviderConfig(BaseModel):
    api_key_env: str


class ModelEntry(BaseModel):
    provider: str
    model: str
    params: dict = Field(default_factory=dict)
    timeout_s: float = 30
    # If the call hasn't answered (or, when streaming, sent its first text) after this many
    # seconds, send the same request again and use whichever answers first. Cuts the slow tail.
    hedge_after_s: float | None = None

    @property
    def label(self) -> str:
        return f"{self.provider}:{self.model}"


class CatalogModel(ModelEntry):
    """A model the user can pick in the UI."""

    id: str
    name: str
    description: str = ""


class Policy(BaseModel):
    retries_per_model: int = 1
    # A model that answered "quota exhausted" (HTTP 429) is skipped for this long, so every
    # call doesn't pay for the same refusal before falling back.
    quota_cooldown_s: float = 120


class AgentModelConfig(BaseModel):
    chain: str


class ModelsConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    chains: dict[str, list[ModelEntry]]
    policy: Policy = Policy()
    agents: dict[str, AgentModelConfig]
    catalog: list[CatalogModel] = Field(default_factory=list)

    def catalog_model(self, model_id: str) -> CatalogModel | None:
        return next((m for m in self.catalog if m.id == model_id), None)

    def chain_for(self, agent: str) -> list[ModelEntry]:
        """Model chain for an agent, minus providers that have no API key set."""
        agent_cfg = self.agents.get(agent) or self.agents.get("general")
        if agent_cfg is None:
            raise KeyError(f"No model config for agent '{agent}' and no 'general' fallback")
        entries = self.chains[agent_cfg.chain]
        return [e for e in entries if self.provider_available(e.provider)]

    def provider_available(self, provider: str) -> bool:
        cfg = self.providers.get(provider)
        return bool(cfg and os.environ.get(cfg.api_key_env))


def load_models_config(path: Path) -> ModelsConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = ModelsConfig.model_validate(raw)

    for name, entries in cfg.chains.items():
        for e in entries:
            if e.provider not in SUPPORTED_PROVIDERS:
                raise ValueError(f"chain '{name}': unsupported provider '{e.provider}'")
            if e.provider not in cfg.providers:
                raise ValueError(f"chain '{name}': provider '{e.provider}' missing from providers")
    ids = [m.id for m in cfg.catalog]
    if len(ids) != len(set(ids)):
        raise ValueError("catalog: model ids must be unique")
    for m in cfg.catalog:
        if m.provider not in cfg.providers:
            raise ValueError(f"catalog '{m.id}': provider '{m.provider}' missing from providers")
    for agent, a in cfg.agents.items():
        if a.chain not in cfg.chains:
            raise ValueError(f"agent '{agent}' references unknown chain '{a.chain}'")

    missing = [p for p in cfg.providers if not cfg.provider_available(p)]
    if missing:
        log.warning("No API key set for providers %s; they will be skipped in fallback chains", missing)
    return cfg
