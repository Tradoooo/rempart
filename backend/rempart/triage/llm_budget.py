"""Plafond budgétaire du modèle de triage (prolonge EF-BUD-04 au modèle).

Le triage appelle un modèle payant : Bedrock dans l'environnement déployé,
InferHub en local. Sans garde, une flotte qui trie beaucoup facture beaucoup.
Ce module relève les jetons **mesurés** par Strands
(`common/usage.py::accumulated_usage`) et ferme le robinet quand le plafond
du mois est atteint : `triage/llm.build_model_client` renvoie alors ``None``,
et le triage déterministe prend le relais. Le service n'est jamais interrompu ;
seule la prose du modèle s'arrête.

Deux plafonds, en variables d'environnement :

- ``REMPART_LLM_MONTHLY_USD_BUDGET`` — dollars par mois, chiffrés avec les
  prix configurés (`review/cost.py`). Sans prix configurés, le coût n'est pas
  chiffrable : ce plafond ne se déclenche pas (on n'invente pas un prix) ;
- ``REMPART_LLM_MONTHLY_TOKEN_BUDGET`` — jetons par mois, toujours mesurables.

Sans plafond configuré, rien n'est bloqué : la démo hors ligne ne coûte rien.

Le relevé vit en mémoire par conteneur Lambda (comme la clé SSM) : il remet
le compteur à zéro à chaque mois calendaire, car le plafond est mensuel — un
redémarrage de conteneur peut réouvrir le robinet, mais AWS Budgets reste le
filet indépendant côté facture.
"""

from __future__ import annotations

import logging
import os

from rempart.common.timeutil import utcnow
from rempart.review.cost import estimate_cost

LOGGER = logging.getLogger("rempart.triage.llm_budget")

MONTHLY_USD_ENV = "REMPART_LLM_MONTHLY_USD_BUDGET"
MONTHLY_TOKEN_ENV = "REMPART_LLM_MONTHLY_TOKEN_BUDGET"

#: Relevé du mois courant, en mémoire par conteneur.
_LEDGER: dict[str, object] = {"month": "", "tokens": {}}


def _month_key() -> str:
    """Mois calendaire courant, ``AAAA-MM``."""
    return utcnow().strftime("%Y-%m")


def _env_number(name: str) -> float | None:
    """Valeur positive du plafond, ou ``None`` (absent ou invalide = pas de plafond)."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def record_usage(usage: dict[str, int]) -> None:
    """Accumule les jetons du mois : appelé après chaque tri par le modèle."""
    if not usage:
        return
    month = _month_key()
    if _LEDGER["month"] != month:
        _LEDGER["month"] = month
        _LEDGER["tokens"] = {}
    ledger = _LEDGER["tokens"]
    assert isinstance(ledger, dict)
    for key, value in usage.items():
        if isinstance(value, int):
            ledger[key] = ledger.get(key, 0) + value


def month_usage() -> dict[str, int]:
    """Relevé du mois courant (``{}`` si rien n'a encore été consommé)."""
    return dict(_LEDGER["tokens"]) if isinstance(_LEDGER["tokens"], dict) else {}


def month_usd() -> float | None:
    """Coût du mois en dollars, ou ``None`` si les prix ne sont pas configurés."""
    cost = estimate_cost(month_usage())
    return cost.usd


def month_tokens() -> int:
    """Total de jetons consommés ce mois (entrées + sorties + cache)."""
    return sum(month_usage().values())


def budget_exceeded() -> bool:
    """Vrai si un plafond configuré est atteint (voir les deux variables plus haut)."""
    token_budget = _env_number(MONTHLY_TOKEN_ENV)
    if token_budget is not None and month_tokens() >= token_budget:
        LOGGER.warning(
            "plafond de jetons LLM atteint ce mois (%d >= %d) : repli déterministe",
            month_tokens(),
            token_budget,
        )
        return True
    usd_budget = _env_number(MONTHLY_USD_ENV)
    if usd_budget is not None:
        usd = month_usd()
        if usd is not None and usd >= usd_budget:
            LOGGER.warning(
                "plafond de coût LLM atteint ce mois (%.4f >= %.4f $) : repli déterministe",
                usd,
                usd_budget,
            )
            return True
    return False


def reset() -> None:
    """Vide le relevé (tests uniquement)."""
    _LEDGER["month"] = ""
    _LEDGER["tokens"] = {}


__all__ = [
    "MONTHLY_TOKEN_ENV",
    "MONTHLY_USD_ENV",
    "budget_exceeded",
    "month_tokens",
    "month_usage",
    "month_usd",
    "record_usage",
    "reset",
]
