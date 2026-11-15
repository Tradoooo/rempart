"""Choix du fournisseur du triage : Bedrock en déployé, repli local, plafond.

Ces tests n'appellent JAMAIS un modèle (aucun réseau, aucun AWS, aucun coût) :
le client Bedrock est un **faux** (`FakeBedrockModel`), comme les autres doubles
de test. Ce qui est vérifié ici :

1. la sélection du fournisseur (`REMPART_LLM_PROVIDER`) ;
2. le **modèle** retenu et la région Passed au client ;
3. l'**inchangement des garde-fous** : la matrice gagne, un Critical/High exige
   une validation humaine — quel que soit le fournisseur ;
4. le **plafond budgétaire** : atteint, le modèle n'est plus construit et le
   triage déterministe prend le relais (le service n'est jamais interrompu).
"""

from __future__ import annotations

from typing import Any

import pytest

from rempart.common.models import Application, Finding
from rempart.triage import llm as llm_module
from rempart.triage import llm_budget
from rempart.triage.agent import run_triage
from rempart.triage.llm import (
    DEFAULT_BEDROCK_MODEL_IDS,
    DEFAULT_PROVIDER,
    PROVIDER_BEDROCK,
    PROVIDER_ENV,
    PROVIDER_OPENAI_COMPAT,
    build_model_client,
    provider,
)
from tests.triage.test_agent import _ScriptedStrandsModel

MODEL_ID = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"


class FakeBedrockModel:
    """Faux `BedrockModel` : enregistre ce qu'on lui passe, n'appelle rien."""

    instances: list[FakeBedrockModel] = []

    def __init__(self, *, model_id: str, region_name: str | None = None, **_: Any) -> None:
        self.model_id = model_id
        self.region_name = region_name
        FakeBedrockModel.instances.append(self)

    def get_config(self) -> dict[str, Any]:
        return {"model_id": self.model_id}


@pytest.fixture(autouse=True)
def _env_propre(monkeypatch):
    """Chaque test part d'un environnement sans fournisseur et sans budget."""
    for name in (
        PROVIDER_ENV,
        llm_module.BEDROCK_MODEL_IDS_ENV,
        "REMPART_LLM_BASE_URL",
        "LLM_BASE_URL",
        "REMPART_LLM_API_KEY",
        "LLM_API_KEY",
        llm_module.SSM_PARAM_ENV,
        llm_budget.MONTHLY_TOKEN_ENV,
        llm_budget.MONTHLY_USD_ENV,
        "REMPART_LLM_USD_PER_M_INPUT",
        "REMPART_LLM_USD_PER_M_OUTPUT",
    ):
        monkeypatch.delenv(name, raising=False)
    llm_budget.reset()
    FakeBedrockModel.instances.clear()
    yield
    llm_budget.reset()
    FakeBedrockModel.instances.clear()


@pytest.fixture
def _bedrock_faux(monkeypatch):
    """Le faux Bedrock remplace le vrai modèle Strands."""
    strands_bedrock = pytest.importorskip("strands.models.bedrock")
    monkeypatch.setattr(strands_bedrock, "BedrockModel", FakeBedrockModel)
    return FakeBedrockModel


# ---------------------------------------------------------------------- #
# 1. Sélection du fournisseur
# ---------------------------------------------------------------------- #


def test_sans_variable_le_fournisseur_par_defaut_est_le_repli_local(monkeypatch):
    """Hors CDK, rien n'est posé : on ne suppose jamais Bedrock par accident."""
    assert provider() == DEFAULT_PROVIDER == PROVIDER_OPENAI_COMPAT


def test_la_variable_selectionne_bedrock(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "bedrock")
    assert provider() == PROVIDER_BEDROCK


def test_un_fournisseur_inconnu_retombe_sur_le_defaut(monkeypatch):
    """Une faute de frappe ne doit pas fabriquer un client qui n'existe pas."""
    monkeypatch.setenv(PROVIDER_ENV, "bedrocks")
    assert provider() == DEFAULT_PROVIDER


def test_le_client_bedrock_utilise_le_modele_choisi(monkeypatch, _bedrock_faux):
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    monkeypatch.setenv(llm_module.BEDROCK_MODEL_IDS_ENV, MODEL_ID)
    monkeypatch.setenv("REMPART_REGION", "eu-north-1")

    client = build_model_client()

    assert client is not None
    assert FakeBedrockModel.instances[0].model_id == MODEL_ID
    assert FakeBedrockModel.instances[0].region_name == "eu-north-1"


def test_plusieurs_modeles_bedrock_donnent_une_chaine_de_repli(monkeypatch, _bedrock_faux):
    """Si le premier modèle échoue, le suivant est essayé : le triage n'est pas perdu."""
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    monkeypatch.setenv(
        llm_module.BEDROCK_MODEL_IDS_ENV, f"{MODEL_ID},eu.anthropic.claude-haiku-4-5-v1:0"
    )

    client = build_model_client()

    assert client is not None
    assert len(FakeBedrockModel.instances) == 2


def test_sans_modele_bedrock_aucun_client_n_est_construit(monkeypatch, _bedrock_faux):
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    monkeypatch.setenv(llm_module.BEDROCK_MODEL_IDS_ENV, "")
    assert build_model_client() is None


def test_le_modele_bedrock_par_defaut_est_une_liste_non_vide():
    """La valeur par défaut doit être un identifiant, pas une chaîne vide."""
    assert DEFAULT_BEDROCK_MODEL_IDS
    assert all(model.strip() for model in DEFAULT_BEDROCK_MODEL_IDS)


def test_un_client_injecte_prime_sur_le_fournisseur(monkeypatch, _bedrock_faux):
    sentinel = object()
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    assert build_model_client(sentinel) is sentinel
    assert FakeBedrockModel.instances == []


# ---------------------------------------------------------------------- #
# 2. Garde-fous inchangés, quel que soit le fournisseur
# ---------------------------------------------------------------------- #


def _app() -> Application:
    return Application(
        app_id="app-1", tenant_id="tenant-a", name="ShopLite",
        criticality="high", internet_exposed=True, data_types=["personal"],
        environment="staging", agent_space_id="space-1",
        domains=["shop.example.com"], repositories=["org/shoplite"],
        team="checkout", ticket_channel="github", context={},
        created_at="2026-11-10T10:00:00Z",
    )


def _finding(severity: str = "Critical") -> Finding:
    return Finding(
        finding_id="f-1", app_id="app-1", tenant_id="tenant-a", run_id="run-1",
        fingerprint="fp-1", title="Faille test", risk_type="IDOR",
        severity=severity, confidence="High", priority="P2", status="new",
        due_at=None, ticket_url=None, remediation_pr_url=None,
        evidence={"entry_point": "/orders/{id}", "parameter": "id"},
        cvss={}, created_at="2026-11-10T10:00:00Z", updated_at="2026-11-10T10:00:00Z",
        closed_at=None, closed_by_run_id=None,
    )


def test_avec_bedrock_la_matrice_gagne_et_le_critical_exige_un_humain(monkeypatch):
    """Le modèle tente de passer un Critical en P4 « faux positif » : la matrice gagne."""
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    payload = {
        "findingId": "f-1", "duplicateOf": None, "isRegression": False,
        "proposedStatus": "LIKELY_FALSE_POSITIVE", "priority": "P4",
        "plainSummary": "Rien de grave.", "technicalSummary": "Dismissed.",
        "references": ["CWE-89"], "justification": "Model tried to downgrade.",
        "needsHumanValidation": False,
    }

    proposals = run_triage(
        findings=[_finding("Critical")],
        app=_app(),
        known_findings=[],
        past_decisions=[],
        model_client=_ScriptedStrandsModel(payload),
    )

    assert len(proposals) == 1
    assert proposals[0].priority == "P1", "la matrice doit gagner sur le modèle"
    assert proposals[0].proposed_status == "TO_VERIFY"
    assert proposals[0].needs_human_validation is True


def test_avec_bedrock_un_high_exige_une_validation_humaine(monkeypatch):
    """EF-TRI-05 : un High n'est jamais tranché seul, même s'il est « confirmé »."""
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    payload = {
        "findingId": "f-1", "duplicateOf": None, "isRegression": False,
        "proposedStatus": "CONFIRMED", "priority": "P2",
        "plainSummary": "Résumé.", "technicalSummary": "Détail.",
        "references": [], "justification": "Confirmé par le modèle.",
        "needsHumanValidation": False,
    }

    proposals = run_triage(
        findings=[_finding("High")],
        app=_app(),
        known_findings=[],
        past_decisions=[],
        model_client=_ScriptedStrandsModel(payload),
    )

    assert proposals[0].needs_human_validation is True
    assert proposals[0].source == "model", "la provenance du verdict doit survivre"


# ---------------------------------------------------------------------- #
# 3. Plafond budgétaire du modèle
# ---------------------------------------------------------------------- #


def test_sans_plafond_le_modele_est_construit(monkeypatch, _bedrock_faux):
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    assert llm_budget.budget_exceeded() is False
    assert build_model_client() is not None


def test_le_plafond_de_jetons_bloque_le_modele(monkeypatch, _bedrock_faux):
    """Plafond atteint : plus de modèle, le triage déterministe prend le relais."""
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    monkeypatch.setenv(llm_budget.MONTHLY_TOKEN_ENV, "1000")
    llm_budget.record_usage({"inputTokens": 900, "outputTokens": 200})

    assert llm_budget.budget_exceeded() is True
    assert build_model_client() is None


def test_sous_le_plafond_le_modele_est_construit(monkeypatch, _bedrock_faux):
    monkeypatch.setenv(PROVIDER_ENV, PROVIDER_BEDROCK)
    monkeypatch.setenv(llm_budget.MONTHLY_TOKEN_ENV, "1000")
    llm_budget.record_usage({"inputTokens": 10, "outputTokens": 20})

    assert llm_budget.budget_exceeded() is False
    assert build_model_client() is not None


def test_le_plafond_en_dollars_ne_declenche_pas_sans_prix_configures(monkeypatch):
    """Sans prix configurés, aucun coût n'est inventé : le plafond ne mord pas."""
    monkeypatch.setenv(llm_budget.MONTHLY_USD_ENV, "1")
    llm_budget.record_usage({"inputTokens": 1_000_000, "outputTokens": 1_000_000})

    assert llm_budget.month_usd() is None
    assert llm_budget.budget_exceeded() is False


def test_le_plafond_en_dollars_declenche_avec_des_prix_configures(monkeypatch):
    monkeypatch.setenv(llm_budget.MONTHLY_USD_ENV, "1")
    monkeypatch.setenv("REMPART_LLM_USD_PER_M_INPUT", "3")
    monkeypatch.setenv("REMPART_LLM_USD_PER_M_OUTPUT", "15")
    llm_budget.record_usage({"inputTokens": 1_000_000, "outputTokens": 1_000_000})

    assert llm_budget.month_usd() is not None
    assert llm_budget.month_usd() >= 1
    assert llm_budget.budget_exceeded() is True


def test_le_releve_du_mois_compte_les_jetons_reels(monkeypatch):
    """Seuls les jetons mesurés comptent : pas d'estimation."""
    llm_budget.record_usage({"inputTokens": 10, "outputTokens": 5})
    llm_budget.record_usage({"inputTokens": 7, "outputTokens": 1})

    assert llm_budget.month_tokens() == 23
    assert llm_budget.month_usage()["inputTokens"] == 17
