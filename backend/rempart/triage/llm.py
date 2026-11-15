"""Client LLM du triage — Strands branché sur Amazon Bedrock ou un endpoint OpenAI-compatible.

Pourquoi ce module existe : le triage de Rempart savait déjà utiliser un
vrai modèle Strands (`triage/agent.py::_run_strands_agent`), mais rien ne le
construisait : en production, l'agent tombait donc toujours sur le repli
déterministe. C'est ce qui rendait la démonstration « pas assez réelle » : les
propositions venaient d'un règleur, pas d'un modèle.

Ici, on construit le client Strands à partir de l'environnement. Deux
fournisseurs, choisis par ``REMPART_LLM_PROVIDER`` :

- ``bedrock`` — `strands.models.bedrock.BedrockModel`, la valeur par défaut
  dans l'environnement **déployé** (le CDK pose la variable) : pas de clé à
  gérer, le rôle IAM de la Lambda porte `bedrock:InvokeModel` sur les ARN de
  modèles choisis, pas plus. La liste se règle avec
  ``REMPART_BEDROCK_MODEL_IDS`` (séparés par des virgules) ;
- ``openai_compat`` — endpoint compatible OpenAI (le repli **local**,
  InferHub par défaut) : ``REMPART_LLM_BASE_URL``/``LLM_BASE_URL``,
  ``REMPART_LLM_API_KEY``/``LLM_API_KEY`` (secret : paramètre SSM
  SecureString en production, fichier local en développement),
  ``REMPART_LLM_MODEL``/``REMPART_LLM_MODELS``.

Sans configuration, on renvoie ``None`` : le triage déterministe reste le
repli, et la démo hors ligne ne coûte donc **rien** (aucun appel LLM, aucune
facturation). Le plafond budgétaire (`triage/llm_budget.py`) ferme le robinet
de la même façon quand le mois dépasse le budget configuré.

Les garde-fous déterministes (`triage/guardrails.py`) s'appliquent TOUJOURS
après le modèle, quel que soit le fournisseur : priorité recalculée par la
matrice, EF-TRI-05 (un Critical ou un High ne peut pas être écarté seul comme
faux positif), détection de doublon et de régression. Un modèle qui propose
n'importe quoi ne peut donc pas faire passer une faute critique en P4.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from strands.models import Model

from . import llm_budget

LOGGER = logging.getLogger("rempart.triage.llm")

DEFAULT_BASE_URL = "https://api.inferhub.dev/v1"
#: Modèles essayés dans l'ordre. Le routeur d'InferHub bascule ses fournisseurs
#: (quota, cooldown, prix) : un seul modèle rend le triage instable, une chaîne
#: de modèles le rend robuste. Mesuré le 12/11/2026 : `combo/cheap` répond en
#: ~2 s, mais peut être saturé ; `ali/glm-5.2` répond en ~2 s ; au-delà, les
#: fournisseurs sont hors budget de l'utilisateur (HTTP 402 « no_provider_under_bid »).
DEFAULT_MODELS = ("combo/cheap", "ali/glm-5.2")

PROVIDER_ENV = "REMPART_LLM_PROVIDER"
PROVIDER_BEDROCK = "bedrock"
PROVIDER_OPENAI_COMPAT = "openai_compat"
#: Sans variable, le repli local est InferHub ; c'est le CDK qui pose
#: ``REMPART_LLM_PROVIDER=bedrock`` dans les Lambdas déployées.
DEFAULT_PROVIDER = PROVIDER_OPENAI_COMPAT

BEDROCK_MODEL_IDS_ENV = "REMPART_BEDROCK_MODEL_IDS"
#: Profil d'inférence européen de Claude Sonnet 4.5 — à **confirmer** dans le
#: catalogue Bedrock de la région déployée (eu-north-1) avant toute
#: facturation ; surchargeable par ``REMPART_BEDROCK_MODEL_IDS``.
DEFAULT_BEDROCK_MODEL_IDS = ("eu.anthropic.claude-sonnet-4-5-20250929-v1:0",)

#: Nom du paramètre SSM SecureString qui porte la clé (jamais la valeur).
SSM_PARAM_ENV = "REMPART_LLM_API_KEY_SSM_PARAM"

#: Une seule lecture SSM par conteneur Lambda : le secret ne change pas, et
#: chaque appel coûterait une requête réseau au milieu du triage.
_SSM_CACHE: dict[str, str | None] = {}


class _FallbackModel(Model):
    """Modèle Strands qui essaie plusieurs modèles OpenAI-compatibles.

    L'agent de triage ne doit jamais tomber en panne parce qu'un fournisseur est
    saturé : on essaie le suivant. Si TOUS échouent, l'appel remonte et le
    triage déterministe prend le relais (voir `triage/agent.py`).
    """

    def __init__(self, models: list[Any]) -> None:
        super().__init__()
        self._models = list(models)
        #: Le modèle qui a réellement répondu (traçabilité), sinon ``None``.
        self.last_used: Any = None

    def update_config(self, **model_config: Any) -> None:
        """Applique la configuration à tous les modèles de la chaîne."""
        for model in self._models:
            try:
                model.update_config(**model_config)
            except Exception:  # pragma: no cover - un modèle peut refuser
                continue

    def get_config(self) -> Any:
        """Configuration du premier modèle de la chaîne."""
        return self._models[0].get_config()

    async def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        """Sortie structurée : déléguée au premier modèle de la chaîne."""
        return await self._models[0].structured_output(*args, **kwargs)

    async def stream(
        self,
        messages: Any,
        tool_specs: Any = None,
        system_prompt: Any = None,
        **kwargs: Any,
    ) -> Any:
        """Diffuse les événements du premier modèle qui répond."""
        last_error: Exception | None = None
        for model in self._models:
            try:
                async for event in model.stream(
                    messages, tool_specs=tool_specs, system_prompt=system_prompt, **kwargs
                ):
                    yield event
                # Le modèle qui a réellement répondu est celui qu'on trace :
                # `get_config()` ne renvoie que le premier de la chaîne.
                self.last_used = model
                return
            except Exception as exc:  # noqa: BLE001 — on essaie le suivant
                last_error = exc
        raise last_error if last_error else RuntimeError("aucun modèle disponible")


def strands_available() -> bool:
    """Vrai si `strands-agents` est importable (le layer Lambda l'embarque)."""
    try:
        import strands  # noqa: F401

        return True
    except ImportError:
        return False


def is_real_model(model_client: Any) -> bool:
    """Vrai si ``model_client`` est un vrai modèle Strands, pas un faux de test.

    Déplacé ici depuis `triage/agent.py` (où il était privé) parce que la revue
    de code a la même exigence : un faux client ne doit jamais faire croire
    qu'un modèle a travaillé. C'est la différence entre « la revue vient d'un
    agent » et « la revue est vide parce qu'aucun modèle n'était branché ».
    """
    if model_client is None:
        return False
    try:
        from strands.models.model import Model

        return isinstance(model_client, Model)
    except ImportError:
        return False


def model_label(model_client: Any) -> str | None:
    """Identifiant du modèle en usage, pour la trace d'audit (jamais la clé).

    Avec une chaîne de repli (`_FallbackModel`), on rend le modèle qui a
    **réellement répondu**, pas le premier de la liste : sinon la preuve dirait
    « combo/cheap a trié » alors que c'est `ali/glm-5.2` qui a répondu.
    """
    used = getattr(model_client, "last_used", None)
    for client in [used, model_client] if used is not None else [model_client]:
        label = _config_label(client)
        if label:
            return label
    return None


def _config_label(model_client: Any) -> str | None:
    """`model`/`model_id` de la configuration d'un modèle Strands."""
    try:
        config = model_client.get_config()
    except Exception:  # noqa: BLE001 — un label absent ne doit pas casser la revue
        return None
    if isinstance(config, dict):
        return str(config.get("model") or config.get("model_id") or "") or None
    return str(config) if config else None


def llm_configured() -> bool:
    """Vrai si un endpoint et une clé sont disponibles."""
    return bool(base_url() and api_key())


def base_url() -> str | None:
    """Endpoint compatible OpenAI du modèle (public, jamais un secret)."""
    return os.environ.get("REMPART_LLM_BASE_URL") or os.environ.get("LLM_BASE_URL")


def api_key() -> str | None:
    """Clé du modèle : environnement d'abord, puis paramètre SSM si configuré."""
    direct = os.environ.get("REMPART_LLM_API_KEY") or os.environ.get("LLM_API_KEY")
    if direct:
        return direct
    name = os.environ.get(SSM_PARAM_ENV)
    return api_key_from_ssm(name) if name else None


def api_key_from_ssm(name: str) -> str | None:
    """Valeur déchiffrée d'un paramètre SecureString, ou ``None`` (jamais d'exception).

    Pourquoi ce détour : CloudFormation **refuse** la référence dynamique
    ``resolve:ssm-secure`` dans ``AWS::Lambda::Function/Properties/Environment/
    Variables`` — mesuré le 13/11/2026, le changeset échoue avec « SSM Secure
    reference is not supported in ». Le vrai modèle n'aurait donc jamais pu
    être déployé. La Lambda lit le paramètre elle-même, une fois par conteneur.
    """
    if name in _SSM_CACHE:
        return _SSM_CACHE[name]
    try:
        client = _ssm_client()
        value = client.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except Exception as exc:  # noqa: BLE001 — une clé absente ne doit pas casser le triage
        LOGGER.warning("lecture SSM de %s impossible : %s", name, type(exc).__name__)
        value = None
    _SSM_CACHE[name] = value or None
    return _SSM_CACHE[name]


def _ssm_client() -> Any:
    """Client SSM du compte courant — point d'injection pour les tests."""
    import boto3

    return boto3.client("ssm")


def model_ids() -> tuple[str, ...]:
    """Modèles à essayer, dans l'ordre (``REMPART_LLM_MODELS``, séparés par des virgules)."""
    raw = os.environ.get("REMPART_LLM_MODELS")
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    single = os.environ.get("REMPART_LLM_MODEL")
    if single:
        return (single.strip(),)
    return DEFAULT_MODELS


def provider() -> str:
    """Fournisseur choisi : ``bedrock`` ou ``openai_compat`` (défaut local)."""
    raw = (os.environ.get(PROVIDER_ENV) or "").strip().lower()
    return raw if raw in (PROVIDER_BEDROCK, PROVIDER_OPENAI_COMPAT) else DEFAULT_PROVIDER


def bedrock_model_ids() -> tuple[str, ...]:
    """Identifiants de modèles Bedrock essayés dans l'ordre.

    Variable **absente** : la liste par défaut. Variable **posée** (même vide) :
    c'est elle qui fait foi — la poser à vide désactive Bedrock sans toucher au
    code.
    """
    raw = os.environ.get(BEDROCK_MODEL_IDS_ENV)
    if raw is None:
        return DEFAULT_BEDROCK_MODEL_IDS
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def bedrock_region() -> str | None:
    """Région du client Bedrock : la région courante de la Lambda."""
    return (
        os.environ.get("REMPART_REGION")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )


def bedrock_configured() -> bool:
    """Vrai si au moins un identifiant de modèle Bedrock est disponible."""
    return bool(bedrock_model_ids())


def build_model_client(model_client: Any = None) -> Any:
    """Construit le client Strands du triage, ou ``None`` si non configuré.

    ``model_client`` permet d'injecter un client (tests) sans toucher à
    l'environnement. Sinon, le fournisseur est choisi par
    ``REMPART_LLM_PROVIDER`` ; dans les deux cas, une chaîne de modèles est
    construite : le premier qui répond est utilisé, les suivants servent de
    repli. Le plafond budgétaire (`triage/llm_budget.py`) est vérifié **avant**
    toute construction : budget épuisé = ``None`` = triage déterministe.
    """
    if model_client is not None:
        return model_client
    if llm_budget.budget_exceeded():
        return None
    if provider() == PROVIDER_BEDROCK:
        return _build_bedrock_client()
    return _build_openai_client()


def _build_bedrock_client() -> Any:
    """Chaîne de `BedrockModel` Strands : droits IAM par ARN, pas de clé à gérer."""
    ids = bedrock_model_ids()
    if not ids:
        LOGGER.warning("provider=bedrock mais aucun modèle (REMPART_BEDROCK_MODEL_IDS)")
        return None
    try:
        from strands.models.bedrock import BedrockModel
    except ImportError as exc:  # pragma: no cover - dépendance optionnelle
        LOGGER.warning("client Bedrock indisponible (strands) : %s", exc)
        return None
    region = bedrock_region()
    models = []
    for model_id in ids:
        try:
            models.append(BedrockModel(model_id=model_id, region_name=region))
        except Exception as exc:  # noqa: BLE001 - une région absente ne casse pas le triage
            LOGGER.warning("modèle Bedrock %s non constructible : %s", model_id, exc)
    if not models:
        return None
    return models[0] if len(models) == 1 else _FallbackModel(models)


def _build_openai_client() -> Any:
    """Chaîne de modèles OpenAI-compatibles (repli local : InferHub)."""
    if not llm_configured():
        return None
    base = base_url()
    key = api_key()
    if not base or not key:
        return None
    try:
        from strands.models.openai import OpenAIModel
    except ImportError as exc:  # pragma: no cover - dépendance optionnelle
        LOGGER.warning("client du modèle indisponible (openai/strands) : %s", exc)
        return None
    models = []
    for model_id in model_ids():
        try:
            models.append(
                OpenAIModel(
                    client_args={"base_url": base, "api_key": key},
                    model_id=model_id,
                )
            )
        except Exception:  # pragma: no cover - configuration invalide
            continue
    if not models:
        LOGGER.warning("aucun modèle constructible parmi %s", model_ids())
        return None
    if len(models) == 1:
        return models[0]
    return _FallbackModel(models)


__all__ = [
    "BEDROCK_MODEL_IDS_ENV",
    "DEFAULT_BEDROCK_MODEL_IDS",
    "DEFAULT_BASE_URL",
    "DEFAULT_MODELS",
    "DEFAULT_PROVIDER",
    "PROVIDER_BEDROCK",
    "PROVIDER_ENV",
    "PROVIDER_OPENAI_COMPAT",
    "SSM_PARAM_ENV",
    "api_key",
    "api_key_from_ssm",
    "base_url",
    "bedrock_configured",
    "bedrock_model_ids",
    "bedrock_region",
    "build_model_client",
    "is_real_model",
    "llm_configured",
    "model_ids",
    "model_label",
    "provider",
    "strands_available",
]
