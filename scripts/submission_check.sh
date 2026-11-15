#!/usr/bin/env bash
# submission_check.sh — vérification one-shot de la démo publique Rempart.
#
# Rejouable par n'importe qui, sans secret, sans compte AWS :
#   bash scripts/submission_check.sh
#
# Contrôles :
#   1. https://tradoooo.github.io/rempart/ répond 200 ;
#   2. les routes profondes (/fleet /evidence /audit /triage) servent le SPA
#      (200, ou 404 avec le HTML du bundle — normal sur GitHub Pages) ;
#   3. le bundle JS téléchargé contient les 5 pages de gouvernance ;
#   4. docs/img/*.png existantes sont listées ;
#   5. le dépôt est PRIVÉ (règlement : dépôt privé partagé au jury) ;
#   6. aucun espace réservé d'identifiants ne reste dans les docs ;
#   7. le schéma d'architecture PNG (formulaire Devpost) est présent ;
#   8. l'issue du premier test Continuum (#36) est FERMÉE ;
#   9. imprime le bloc « à coller sur Devpost ».
#
# Les contrôles 5 à 8 échouent tant que la soumission n'est pas prête :
# c'est voulu — le script doit rester rouge jusqu'au bout.
#
# Code de sortie : 0 si tout est conforme, 1 sinon.

set -uo pipefail

BASE="https://tradoooo.github.io/rempart"
REPO_SLUG="Tradoooo/rempart"
REPO_URL="https://github.com/${REPO_SLUG}"
CONTINUUM_ISSUE=36   # « Premier test Continuum réel » — bloquant soumission
ARCH_PNG="docs/img/architecture.png"
PLACEHOLDER_RE='JUDGE_USER|JUDGE_PASSWORD|ANALYST_USER|ANALYST_PASSWORD|<repo-url>'
PLACEHOLDER_FILES=(README.md docs/*.md scripts/*.sh)
CURL_TIMEOUT=30
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

BUNDLE_STRINGS=(
  "Evidence Dossier"
  "Audit Journal"
  "Industrialization"
  "Forecast"
  "Contract Correction Policy"
)
DEEP_ROUTES=(fleet evidence audit triage)

fail=0

msg()  { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }

# fetch <url> <outfile> -> http_code (000 en cas d'échec réseau)
fetch() {
  local code
  code="$(curl -sS -o "$2" -w '%{http_code}' --max-time "${CURL_TIMEOUT}" "$1" 2>/dev/null)" \
    || code="000"
  printf '%s' "${code:-000}"
}

# is_bundle_html <file> : vrai si le HTML contient le script du bundle SPA
is_bundle_html() {
  grep -q 'src="/rempart/assets/' "$1" && grep -q 'id="root"' "$1"
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

step "1. Page d'accueil"
code="$(fetch "${BASE}/" "$tmp/root.html")"
if [ "$code" = "200" ]; then
  msg "   OK   ${BASE}/ -> 200"
else
  msg "   KO   ${BASE}/ -> ${code} (attendu 200)"
  fail=1
fi

step "2. Routes profondes (SPA sur GitHub Pages)"
for route in "${DEEP_ROUTES[@]}"; do
  code="$(fetch "${BASE}/${route}" "$tmp/route.html")"
  if [ "$code" = "200" ]; then
    msg "   OK   /${route} -> 200"
  elif [ "$code" = "404" ] && is_bundle_html "$tmp/route.html"; then
    msg "   OK   /${route} -> 404 (sert le HTML du bundle : normal sur GitHub Pages)"
  else
    msg "   KO   /${route} -> ${code} (pas le HTML du bundle)"
    fail=1
  fi
done

step "3. Contenu du bundle"
bundle_path="$(grep -oE 'src="/rempart/assets/[^"]+\.js"' "$tmp/root.html" | head -1 | sed -E 's/^src="|"$//g')"
if [ -z "$bundle_path" ]; then
  msg "   KO   impossible de trouver le script du bundle dans index.html"
  fail=1
else
  bundle_url="https://tradoooo.github.io${bundle_path}"
  code="$(fetch "${bundle_url}" "$tmp/bundle.js")"
  if [ "$code" != "200" ]; then
    msg "   KO   téléchargement du bundle -> ${code} (${bundle_url})"
    fail=1
  else
    msg "   OK   bundle téléchargé (${bundle_path}, $(wc -c < "$tmp/bundle.js") octets)"
    for string in "${BUNDLE_STRINGS[@]}"; do
      if grep -qF "$string" "$tmp/bundle.js"; then
        msg "   OK   « ${string} » présent dans le bundle"
      else
        msg "   KO   « ${string} » absent du bundle"
        fail=1
      fi
    done
  fi
fi

step "4. Captures docs/img"
count=0
for png in "$REPO_ROOT"/docs/img/*.png; do
  [ -e "$png" ] || continue
  count=$((count + 1))
  msg "   docs/img/$(basename "$png")"
done
msg "   -> ${count} capture(s) PNG listée(s)."

# --------------------------------------------------------------------- #
# Barrières de soumission : le script DOUT échouer tant que l'une manque.
# --------------------------------------------------------------------- #

# gh_api <endpoint> : sortie de `gh api`, ou chaîne vide si gh est absent.
gh_api() {
  if command -v gh >/dev/null 2>&1; then
    gh api "$1" 2>/dev/null || true
  fi
}

step "5. Dépôt privé (règlement : partagé avec testing@devpost.com et aws-msp@amazon.com)"
visibility="$(gh_api "repos/${REPO_SLUG}" | grep -o '"private":[a-z]*' | head -1 || true)"
if [ -z "${visibility}" ]; then
  # Sans gh (ou sans droit de lecture) : l'API publique répond 404 pour un
  # dépôt privé — c'est le signal attendu une fois le basculement fait.
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "${CURL_TIMEOUT}" \
    "https://api.github.com/repos/${REPO_SLUG}")"
  if [ "$code" = "404" ]; then
    msg "   OK   ${REPO_URL} n'est plus accessible publiquement (privé ou injoignable sans jeton)"
  else
    msg "   KO   impossible de lire la visibilité (API -> ${code}) : vérifier à la main"
    fail=1
  fi
elif echo "$visibility" | grep -q '"private":true'; then
  msg "   OK   ${REPO_URL} est privé"
else
  msg "   KO   ${REPO_URL} est encore PUBLIC — bascule avant la soumission (voir submission-checklist.md, élément 4)"
  fail=1
fi

step "6. Aucun espace réservé d'identifiants dans les docs"
placeholders="$(grep -rInE "${PLACEHOLDER_RE}" "${PLACEHOLDER_FILES[@]}" \
  --exclude=submission_check.sh 2>/dev/null || true)"
if [ -z "$placeholders" ]; then
  msg "   OK   aucun JUDGE_*/ANALYST_*/<repo-url> restant dans README.md, docs/ et scripts/"
else
  msg "   KO   espaces réservés restants :"
  printf '%s\n' "$placeholders" | sed 's/^/        /'
  fail=1
fi

step "7. Schéma d'architecture PNG (formulaire Devpost)"
size="$(wc -c < "${REPO_ROOT}/${ARCH_PNG}" 2>/dev/null | tr -d '[:space:]')"
if [ -n "$size" ] && [ "$size" -gt 0 ]; then
  msg "   OK   ${ARCH_PNG} (${size} octets)"
else
  msg "   KO   ${ARCH_PNG} absent ou vide — bash scripts/export_architecture_png.sh"
  fail=1
fi

step "8. Premier test Continuum (issue #${CONTINUUM_ISSUE}) terminé"
issue_state="$(gh_api "repos/${REPO_SLUG}/issues/${CONTINUUM_ISSUE}" \
  | grep -o '"state":"[a-z]*"' | head -1 | cut -d'"' -f4 || true)"
if [ -z "${issue_state}" ]; then
  # Sans gh : sur un dépôt encore public, l'API anonyme donne l'état ; sur un
  # dépôt privé elle répond 404 et l'état doit être vérifié à la main.
  issue_state="$(curl -s --max-time "${CURL_TIMEOUT}" \
    "https://api.github.com/repos/${REPO_SLUG}/issues/${CONTINUUM_ISSUE}" \
    | grep -o '"state":"[a-z]*"' | head -1 | cut -d'"' -f4 || true)"
fi
if [ "${issue_state}" = "closed" ]; then
  msg "   OK   issue #${CONTINUUM_ISSUE} fermée : le test réel est prouvé (preuves dans docs/preuves/)"
elif [ -z "${issue_state}" ]; then
  msg "   KO   état de l'issue #${CONTINUUM_ISSUE} illisible (dépôt privé sans gh ?) : vérifier à la main"
  fail=1
else
  msg "   KO   issue #${CONTINUUM_ISSUE} encore ${issue_state} : sans le test du 23/11, l'appel réel à un service d'IA AWS n'est pas prouvé"
  fail=1
fi

step "9. Bloc à coller sur Devpost"
cat <<'EOF'
--- DÉBUT bloc Devpost (à copier-coller) ---

Lien du projet : https://tradoooo.github.io/rempart/

Rempart transforme le test d'intrusion annuel en abonnement mensuel piloté par
un MSP : à chaque release, un test d'intrusion continu (AWS Continuum) s'exécute
dans le compte du client et bloque la mise en production d'une faille grave,
jusqu'à ce qu'un re-test prouve sa correction.

Chiffres : 914 tests · 30/30 preuves · 12 défauts corrigés

--- FIN bloc Devpost ---
EOF

step "Résultat"
if [ "$fail" -ne 0 ]; then
  msg "ÉCHEC : un ou plusieurs contrôles ci-dessus ont échoué."
  exit 1
fi
msg "Tous les contrôles sont conformes."
exit 0