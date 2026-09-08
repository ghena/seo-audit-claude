# claude-plugins

Marketplace interno di plugin per Claude Code.

## Come pubblicarlo sul vostro GitLab CE

Dalla cartella estratta di questo archivio:

```bash
cd claude-plugins
git init
git add .
git commit -m "Initial commit: seo-audit plugin"
git branch -M main
git remote add origin https://gitlab.tuodominio/gruppo/claude-plugins.git
git push -u origin main
```

Sostituisci l'URL del remote con quello del progetto GitLab CE che hai creato
(vuoto, senza README iniziale, per evitare conflitti sul primo push).

## Come lo installano i colleghi

Chi ha accesso al repo (via SSH key in `ssh-agent` o credential helper HTTPS
già configurato, come per qualsiasi altro repo privato) esegue:

```
/plugin marketplace add https://gitlab.tuodominio/gruppo/claude-plugins.git
/plugin install seo-audit@giancarlo-tools
```

Da quel momento il comando `/seo-audit:audit [url]` è disponibile nelle loro
sessioni di Claude Code.

## Come aggiornarlo

Basta modificare i file sotto `plugins/seo-audit/` e fare commit + push. Non
serve incrementare manualmente nessuna versione: essendo un source relativo
(`./plugins/seo-audit`) dentro un repo git, Claude Code rileva l'aggiornamento
dal nuovo commit quando gli utenti lanciano:

```
/plugin marketplace update
```

o quando l'auto-update in background li aggiorna (dipende dalle impostazioni).

## Come aggiungere altri plugin in futuro

1. Crea una nuova cartella sotto `plugins/<nome-plugin>/` con la sua struttura
   (`.claude-plugin/plugin.json` + `SKILL.md`/`commands/`/altro).
2. Aggiungi una voce all'array `plugins` in `.claude-plugin/marketplace.json`:
   ```json
   { "name": "<nome-plugin>", "source": "./plugins/<nome-plugin>" }
   ```
3. Commit + push. Non serve creare un nuovo marketplace per ogni plugin.
