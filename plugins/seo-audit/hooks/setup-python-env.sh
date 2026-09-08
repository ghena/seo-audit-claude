#!/bin/bash
# Installa le dipendenze Python del plugin seo-audit in un venv persistente
# sotto CLAUDE_PLUGIN_DATA (sopravvive agli aggiornamenti del plugin).
# Reinstalla solo se requirements.txt e' cambiato rispetto all'ultima
# installazione (confronto con diff, stesso pattern del pattern ufficiale
# per le dipendenze node_modules).
#
# Non usa CLAUDE_ENV_FILE per esportare variabili (bug noto: non
# funziona in modo affidabile per hook distribuiti via plugin). Il path
# del venv e' invece deterministico e ricostruibile da SKILL.md:
# ${CLAUDE_PLUGIN_DATA}/venv/bin/python3

set -uo pipefail

REQ_FILE="${CLAUDE_PLUGIN_ROOT}/scripts/requirements.txt"
DATA_DIR="${CLAUDE_PLUGIN_DATA}"
STORED_REQ="${DATA_DIR}/requirements.txt"
VENV_DIR="${DATA_DIR}/venv"

mkdir -p "$DATA_DIR"

if [ ! -f "$REQ_FILE" ]; then
  exit 0
fi

if diff -q "$REQ_FILE" "$STORED_REQ" >/dev/null 2>&1 && [ -x "$VENV_DIR/bin/python3" ]; then
  # Nessuna modifica ai requirements e venv gia' presente: niente da fare
  exit 0
fi

PYTHON_BIN="$(command -v python3 || command -v python || true)"
if [ -z "$PYTHON_BIN" ]; then
  echo "[seo-audit] python3 non trovato: impossibile preparare il venv, il setup andra' fatto manualmente" >&2
  exit 0
fi

"$PYTHON_BIN" -m venv "$VENV_DIR" 2>/dev/null || true

if [ -x "$VENV_DIR/bin/pip" ] && "$VENV_DIR/bin/pip" install -q -r "$REQ_FILE" 2>/dev/null; then
  cp "$REQ_FILE" "$STORED_REQ"
else
  echo "[seo-audit] Installazione dipendenze Python fallita, verra' ritentata alla prossima sessione" >&2
  rm -f "$STORED_REQ"
fi

exit 0
