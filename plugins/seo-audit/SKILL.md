---
name: audit
description: Esegue un audit SEO completo di un URL (crawling on-page, confronto con sitemap.xml, benchmark Lighthouse mobile/desktop) e genera report CSV + HTML. Invocalo con /seo-audit:audit [url].
argument-hint: "[url]"
disable-model-invocation: true
---

# SEO Audit

Comando interattivo che lancia `scripts/seo_audit.py`: crawler SEO in Python che
analizza un sito, estrae metriche on-page pagina per pagina, confronta gli URL
crawlati con `sitemap.xml`, ed esegue benchmark Lighthouse (mobile + desktop) su
un campione di pagine. Produce un report CSV dettagliato e una dashboard HTML.

Questo comando si invoca solo manualmente con `/seo-audit:audit`, non viene attivato
automaticamente da Claude.

## Setup (gestito automaticamente)

Le dipendenze sono installate automaticamente e non richiedono setup manuale:

- **Lighthouse**: dichiarato come dipendenza npm reale del plugin
  (`package.json` + lockfile) — Claude Code lo installa in automatico,
  isolato in `${CLAUDE_PLUGIN_ROOT}/node_modules/`, ogni volta che il plugin
  viene installato o aggiornato. Non serve `npm install -g` e non c'è rischio
  di raccogliere per errore un binario Windows via l'interop di WSL, perché
  non si tocca il PATH globale.
- **Dipendenze Python** (`scripts/requirements.txt`): installate da un hook
  `SessionStart` in un venv persistente sotto `${CLAUDE_PLUGIN_DATA}/venv`,
  al primo avvio di ogni sessione (reinstalla solo se `requirements.txt` è
  cambiato).

Se per qualche motivo l'installazione automatica non è andata a buon fine
(rete ristretta, ambiente atipico), puoi eseguire manualmente come fallback:

```bash
pip install -r ${CLAUDE_PLUGIN_ROOT}/scripts/requirements.txt
```

e, se serve, procedere con l'audit usando `--lighthouse-sample 0` (vedi
sotto), avvisando l'utente che i benchmark Lighthouse sono stati saltati.

## Flusso della conversazione

Segui questi passi in ordine. Fai **una sola domanda compatta** che copra tutti
i parametri necessari — non un'interrogazione a raffica su ogni singolo flag.

1. **URL target**
   - Se `$ARGUMENTS` contiene già un URL, usalo come URL di partenza e non
     chiederlo di nuovo.
   - Altrimenti chiedi qual è l'URL da auditare (es. `https://example.com`).

2. **Parametri principali + default**, in un unico messaggio dopo aver
   ricevuto l'URL (o insieme alla richiesta dell'URL se manca anche quello).
   Presenta i default e chiedi conferma o modifiche, così l'utente può
   rispondere anche solo "vanno bene i default":

   | Parametro | Default | Flag script |
   |---|---|---|
   | Profondità di scansione | 4 | `--max-depth` |
   | Concorrenza (worker paralleli) | 2 | `--concurrency` |
   | Max pagine da crawlare | 500 | `--max-pages` |
   | Richieste al secondo max | 1.0 | `--max-rps` |
   | Campione URL per Lighthouse | 5 (0 = disabilita) | `--lighthouse-sample` |
   | Confronto con sitemap.xml | attivo | assenza di `--no-sitemap` |

   Esempio di come porre la domanda:

   > Faccio l'audit di `<url>`. Uso questi default salvo indicazioni diverse:
   > profondità 4, concorrenza 2, max 500 pagine, 1 richiesta/sec, campione
   > Lighthouse di 5 pagine, sitemap inclusa. Vanno bene o vuoi modificarne
   > qualcuno?

   Se l'utente conferma o non risponde su un parametro specifico, usa il
   default. Non insistere con altre domande oltre a questa, a meno che la
   risposta dell'utente non lasci un dubbio genuino su un valore necessario
   (es. URL ambiguo o mancante).

3. **Sito di terzi vs sito gestito dall'utente**: se l'utente chiede
   esplicitamente concorrenza o `--max-rps` più aggressivi (es. concorrenza >
   4, `--max-rps` > 2, o `--max-rps 0`), verifica in una riga che il sito sia
   effettivamente suo o che abbia il permesso di scansionarlo con quei
   parametri, prima di eseguire. Non serve chiederlo per i default (2/1.0),
   che sono già conservativi.

## Esecuzione

**Non scrivere mai i report nella directory di lavoro corrente "nuda".** Lo
script salva i file relativi alla cwd in cui gira, quindi senza una cartella
dedicata rischi di sporcare la repo del progetto aperto in quel momento o di
sovrascrivere un audit precedente sullo stesso sito. Crea sempre prima una
sottocartella dedicata sotto `seo-audit-reports/`, nominata con dominio e data
(`<dominio-senza-schema>_<YYYY-MM-DD>`), poi passa i percorsi di output dentro
quella cartella:

```bash
mkdir -p seo-audit-reports/<dominio>_<YYYY-MM-DD>

# Usa il Python del venv gestito dal plugin se presente, altrimenti fallback
PY="${CLAUDE_PLUGIN_DATA}/venv/bin/python3"
[ -x "$PY" ] || PY="python3"

"$PY" ${CLAUDE_PLUGIN_ROOT}/scripts/seo_audit.py <url> \
  --max-depth <profondita> \
  --concurrency <concorrenza> \
  --max-pages <max_pages> \
  --max-rps <max_rps> \
  --lighthouse-sample <lighthouse_sample> \
  --lighthouse-bin  "${CLAUDE_PLUGIN_ROOT}/node_modules/.bin/lighthouse" \
  --output          seo-audit-reports/<dominio>_<YYYY-MM-DD>/seo_report.csv \
  --html            seo-audit-reports/<dominio>_<YYYY-MM-DD>/seo_report.html \
  --sitemap-output  seo-audit-reports/<dominio>_<YYYY-MM-DD>/sitemap_report.csv \
  --progress-file   seo-audit-reports/<dominio>_<YYYY-MM-DD>/.progress.json
```

Ometti `--lighthouse-bin` solo se `${CLAUDE_PLUGIN_ROOT}/node_modules/.bin/lighthouse`
non esiste per qualche motivo (installazione automatica delle dipendenze npm
fallita): in quel caso lo script ricade comunque sulla ricerca in `PATH`
(escludendo i mount Windows di WSL) descritta più sotto.

Dove `<dominio>` è l'host dell'URL senza schema né `www.` (es. `example.com`
per `https://www.example.com`) e `<YYYY-MM-DD>` è la data odierna. Se esiste
già una cartella con lo stesso nome (stesso dominio, stesso giorno), aggiungi
un suffisso incrementale (`_2`, `_3`, ...) invece di sovrascriverla, a meno
che l'utente non chieda esplicitamente di sovrascrivere l'audit precedente.

Ometti `--sitemap-output` solo se hai usato `--no-sitemap` (in quel caso il
file di confronto sitemap non viene generato). Aggiungi `--no-sitemap` solo
se l'utente ha chiesto di disabilitare il confronto con la sitemap.

### Avanzamento durante l'esecuzione

L'audit può richiedere diversi minuti (il crawl con `--max-rps` conservativo,
più fino a `2 × lighthouse_sample` esecuzioni di Lighthouse da ~30-60s
l'una). Non c'è una vera barra di progresso grafica in Claude Code: il modo
corretto di dare visibilità sull'avanzamento è eseguire lo script in
background e riportare periodicamente lo stato in chat leggendo il file
`--progress-file`, che lo script aggiorna continuamente con un JSON tipo:

```json
{"phase": "crawl", "current": 145, "total": 500, "percent": 29.0, "updated_at": "...", "last_url": "...", "depth": 3}
```

Le fasi possibili nel campo `phase` sono, in ordine: `crawl`, `sitemap`,
`lighthouse_mobile`, `lighthouse_desktop`, `report`, `done`.

Procedi così:

1. Lancia il comando Python con `run_in_background: true`.
2. Ogni 20-30 secondi circa, leggi il contenuto di `.progress.json` (o
   controlla l'output accumulato del processo in background) e riporta
   all'utente un aggiornamento breve in chat, una riga, es.:
   > 🔄 Crawl: 145/500 pagine (29%), profondità 3
   oppure, in fase Lighthouse:
   > 🔄 Lighthouse mobile: 3/5 pagine testate
3. Continua a controllare finché `phase` non diventa `"done"`, poi passa
   alla sezione "Dopo l'esecuzione".
4. Non generare un aggiornamento per ogni singola pagina crawlata: sarebbe
   rumoroso. Un check ogni 20-30 secondi (o ogni volta che l'utente chiede
   "a che punto sei?") è sufficiente.

## Output prodotti

Tutti e tre i file finiscono in `seo-audit-reports/<dominio>_<YYYY-MM-DD>/`,
mai nella cwd nuda (vedi sezione Esecuzione):

- `seo_report.csv` — una riga per URL crawlato, con: status HTTP, tempo di
  risposta e TTFB, title/lunghezza, meta description/lunghezza, canonical
  (self/esterno), hreflang, H1 e conteggio, meta robots, Open Graph, link
  interni/esterni, immagini senza `alt`, presenza e tipi di dati strutturati
  JSON-LD, sequenza e violazioni della gerarchia H1-H6, locale rilevata,
  presenza in sitemap, **profondità di scansione**.
- `sitemap_report.csv` — URL presenti in sitemap ma non crawlati, e viceversa
  (pagine orfane o sitemap disallineate). Assente se è stato usato
  `--no-sitemap`.
- `seo_report.html` — dashboard con KPI aggregati, tabelle dei problemi
  principali e benchmark Lighthouse (Performance, Accessibility, Best
  Practices, SEO, LCP, FCP, TBT, CLS) per mobile e desktop.

## Dopo l'esecuzione

1. Leggi `seo_report.csv` (e `sitemap_report.csv` se presente) dalla cartella
   di output per riassumere in chat i problemi principali **prima** di
   rimandare l'utente al file HTML: title o meta description mancanti/troppo
   corte/lunghe, H1 mancanti o multipli, canonical assenti o errati, immagini
   senza `alt`, violazioni della gerarchia heading, URL in sitemap ma non
   raggiungibili dal crawl (o viceversa), pagine con status non 200.
2. Se sono stati generati benchmark Lighthouse, evidenzia le pagine con
   punteggi bassi (Performance/Accessibility/SEO) o Core Web Vitals fuori
   soglia (LCP, TBT, CLS).
3. **Servi automaticamente l'HTML via browser** (vedi sezione dedicata subito
   sotto) e dai all'utente il link diretto — non limitarti a indicare il
   percorso del file. Fallo sempre, non solo se l'ambiente sembra essere
   Docker: funziona ovunque e toglie ambiguità.
4. Menziona comunque anche il percorso dei file CSV (utili per ulteriori
   elaborazioni, es. import in un foglio di calcolo).
5. Se il crawl si è fermato per `--max-pages`, `--max-depth` o `robots.txt`,
   segnalalo esplicitamente: il report potrebbe non coprire l'intero sito.

### Servire l'HTML via browser (automatico, sempre)

Il file HTML vive nel filesystem di dove gira il comando (spesso un
container Docker/devcontainer): un percorso di file da solo spesso non è
apribile direttamente dal browser sull'host. Risolvi servendo l'intera
cartella `seo-audit-reports/` con un web server HTTP minimale, così ottieni
sempre un link cliccabile invece di un path.

Un solo server basta per tutti gli audit della sessione (non uno per
report): prima controlla se è già in ascolto, e avvialo solo se serve.

```bash
PORT=8787

# Se non risponde nulla su quella porta, avvia il server (altrimenti riusa
# quello già attivo da un audit precedente nella stessa sessione)
curl -s -o /dev/null "http://localhost:${PORT}/" || \
  (cd seo-audit-reports && nohup python3 -m http.server "${PORT}" --bind 0.0.0.0 \
    > /tmp/seo-audit-http-server.log 2>&1 &)

sleep 1
```

Lancialo con `run_in_background: true` se il tool bash lo supporta, così
resta attivo dopo la fine del comando. Poi presenta all'utente il link
diretto al report appena generato:

```
http://localhost:8787/<dominio>_<YYYY-MM-DD>/seo_report.html
```

**Se la porta 8787 è già occupata da un processo non tuo** (raro, ma
possibile), prova la porta successiva (8788, 8789, ...) finché `curl`
non fallisce a connettersi, e usa quella sia per l'avvio del server sia
nel link.

**Perché funziona anche su Docker**: il server è in ascolto su `0.0.0.0`
(non solo `localhost` dentro al container), quindi è raggiungibile
dall'host se la porta è mappata. Nella maggior parte dei setup:
- **VS Code Dev Containers**: la porta in ascolto viene rilevata
  automaticamente e VS Code propone di aprirla nel browser (tab "Ports"),
  senza bisogno di configurare nulla.
- **Docker Compose / `docker run` manuale**: serve che la porta sia già
  mappata verso l'host (es. `ports: ["8787:8787"]` nel `docker-compose.yml`,
  o `-p 8787:8787` su `docker run`). Se il link non si apre, è quasi sempre
  questo il motivo: dillo esplicitamente all'utente invece di lasciarlo
  indovinare, e suggerisci di aggiungere il mapping e ricreare il container.
- **Nessun Docker** (Claude Code gira direttamente sull'host): il link
  funziona comunque identico, senza nulla da configurare.

## Note tecniche

- La profondità 0 è la home/URL di partenza; profondità 4 significa che
  vengono seguiti fino a 4 "salti" di link interni da lì.
- Lighthouse gira in modalità headless via Chromium; la prima esecuzione può
  richiedere qualche secondo in più per l'installazione di Chromium.
- Il throttling delle richieste HTTP è globale: anche con `--concurrency` >
  1, il totale rispetta comunque `--max-rps`.
- Per siti molto grandi, riduci `--lighthouse-sample` (o portalo a 0) o la
  profondità per velocizzare.
- Lighthouse ora è una dipendenza npm dichiarata del plugin (non più
  globale): il binario risolto ha sempre priorità su qualsiasi installazione
  di sistema, il che evita anche il problema, riscontrato su WSL, di
  raccogliere per errore un binario Windows esposto via interop.
