# Fiche 2026-11-15 — scan secrets avant passage en privé

> Contexte : décision du 15/11/2026 de repasser `Tradoooo/rempart` en
> **privé** avant la soumission (élément 4 de la checklist). Avant le
> changement de visibilité, un balayage de tout l'arbre de travail a été fait.
> **Rien n'a été supprimé** ; l'historique git conserve tout ce qui est listé
> ci-dessous.

Commandes utilisées (rejouables sur n'importe quel clone) :

```bash
git grep -nIE "(AKIA|ASIA)[0-9A-Z]{16}"                 # clés d'accès AWS
git grep -nIiE "(secret|api[_-]?key|password|passwd|token)[ ]*[:=]" -- ':!*.md'
git grep -nI "[0-9]{12}"                                # identifiants de compte AWS
git grep -nIiE "external[_-]?id.{0,30}(=|:).{0,40}[0-9a-f-]{20,}"
```

## Résultats

| Cible | Trouvé ? | Détail |
|---|---|---|
| Clés d'accès AWS (`AKIA`/`ASIA`) | **non** | zéro occurrence dans l'arbre. |
| Mots de passe / jetons / clés API en dur | **non** | toutes les occurrences lisent une variable d'environnement (`os.environ`, `secrets.` GitHub Actions) ou un nom de paramètre SSM, jamais une valeur. |
| `sts:ExternalId` en valeur | **non** | le template client le prend en paramètre CloudFormation ; aucune valeur commise. |
| Identifiants de compte AWS réels | **oui — 1** | **`710332178565`** (compte de déploiement eu-north-1), ~20 occurrences : `docs/deployment-eu-north-1.md`, `docs/portal-deployment.md`, `scripts/deploy_api.sh` (défaut `ACCOUNT`), commentaires « INCIDENT RÉEL » dans `iac/`, `backend/` (tests et un docstring de `tenant.py`). Sensibilité **faible** (un ID de compte n'est pas un secret), mais il identifie le compte. **Laissé tel quel**, y compris dans l'historique : décision explicite du 15/11, à revoir si le propriétaire préfère le masquer (réécriture d'historique = force-push, hors périmètre de cette session). |
| Données client | **non** | le jeu de démonstration est figé et synthétique (`demo/seed/demo-dataset.json`, généré par `scripts/seed_demo.py`) ; aucune donnée réelle de client. |
| URLs publiques | — | CloudFront `d1o2xnfrq8xomh.cloudfront.net` et API `qm6dmdkgdg.execute-api.eu-north-1.amazonaws.com` sont **volontairement** publics (démo jury), pas des fuites. |

## Portée et limites

- Le balayage couvre **l'arbre de travail** (commit courant). L'**historique**
  n'a pas été réécrit ni scanné commit par commit (`git log -S` sur chaque
  motif) : tout ce qui précède était déjà public, donc le passage en privé
  n'aggrave rien ; une revue d'historique complète reste possible avant la
  soumission si le propriétaire le souhaite.
- Les espaces réservés d'identifiants de démo (`JUDGE_USER`, `ANALYST_PASSWORD`,
  `<repo-url>`) sont traités séparément — voir
  [`../judging-test-instructions.md`](../judging-test-instructions.md) et
  l'élément 3 de la checklist : les vrais identifiants vivront dans le coffre
  (Bitwarden), jamais dans le dépôt.
