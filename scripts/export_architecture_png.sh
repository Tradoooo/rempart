#!/usr/bin/env bash
# export_architecture_png.sh — régénère docs/img/architecture.png.
#
# Le schéma d'architecture du README est écrit en Mermaid : la version PNG
# sert au formulaire Devpost (qui n'accepte pas le Mermaid). Une seule source
# de vérité : le README.
#
# Prérequis : Node.js + `npx` (mermaid-cli downloaded à la volée) et Chrome
# pour le rendu headless.
#
#   bash scripts/export_architecture_png.sh
#
# Code de sortie : 0 si le PNG est écrit, 1 sinon.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${REPO_ROOT}/docs/img/architecture.png"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

MMDFILE="${WORK}/architecture.mmd"

# 1. Extraire le premier bloc mermaid du README.
awk '/^```mermaid$/{flag=1;next}/^```$/{if(flag){exit}}flag' \
  "${REPO_ROOT}/README.md" > "${MMDFILE}"

if [ ! -s "${MMDFILE}" ]; then
  echo "KO   aucun diagramme mermaid trouvé dans README.md"
  exit 1
fi

# 2. Rendre en PNG (fond blanc, échelle 2 pour la lisibilité dans le formulaire).
if ! npx --yes @mermaid-js/mermaid-cli \
      -i "${MMDFILE}" -o "${OUT}" -b white -s 2; then
  echo "KO   rendu mermaid-cli impossible"
  exit 1
fi

if [ ! -s "${OUT}" ]; then
  echo "KO   ${OUT} vide"
  exit 1
fi

echo "OK   ${OUT} ($(wc -c < "${OUT}") octets)"
