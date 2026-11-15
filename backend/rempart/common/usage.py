"""Tokens réellement consommés par un agent Strands (mesurés, jamais estimés).

Pourquoi ce module est séparé : la lecture de l'usage était dans
`review/agent.py`, qui l'utilisait pour chiffrer une revue. Le triage a besoin
du même relevé pour tenir un **plafond budgétaire** : deux consommateurs,
une seule implémentation.

`accumulated_usage` est un `TypedDict` chez Strands : on le lit donc comme un
dictionnaire, et non par attributs — un `getattr` sur un dict renvoie
systématiquement `None`, ce qui ferait disparaître silencieusement les
tokens mesurés.
"""

from __future__ import annotations

from typing import Any

#: Clés de jetons relevées chez Strands.
USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "totalTokens",
    "cacheReadInputTokens",
    "cacheWriteInputTokens",
)


def accumulated_usage(agent: Any) -> dict[str, int]:
    """Tokens cumulés d'une invocation, tels que Strands les a comptabilisés.

    ``{}`` quand l'agent n'expose pas de métriques : un coût inconnu est
    rendu comme absent, jamais comme zéro (zéro voudrait dire « gratuit », ce
    qui serait faux).
    """
    metrics = getattr(agent, "event_loop_metrics", None)
    usage = getattr(metrics, "accumulated_usage", None)
    if not isinstance(usage, dict):
        return {}
    return {
        key: value
        for key in USAGE_KEYS
        if isinstance((value := usage.get(key)), int)
    }


__all__ = ["USAGE_KEYS", "accumulated_usage"]
