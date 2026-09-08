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

## Setup (una tantum per ambiente)

Se non è già stato fatto in questo ambiente/progetto, installa le dipendenze
prima di lanciare il primo audit:

```bash
pip install -r ${CLAUDE_PLUGIN_ROOT}/scripts/requirements.txt
npm install -g lighthouse   # richiede Node.js + npm
```

Se `npm install -g lighthouse` fallisce o Node.js non è disponibile, procedi
comunque con l'audit usando `lighthouse_sample=0` (vedi sotto) e avvisa
l'utente che i benchmark Lighthouse sono stati saltati.

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

python ${CLAUDE_PLUGIN_ROOT}/scripts/seo_audit.py <url> \
  --max-depth <profondita> \
  --concurrency <concorrenza> \
  --max-pages <max_pages> \
  --max-rps <max_rps> \
  --lighthouse-sample <lighthouse_sample> \
  --output          seo-audit-reports/<dominio>_<YYYY-MM-DD>/seo_report.csv \
  --html            seo-audit-reports/<dominio>_<YYYY-MM-DD>/seo_report.html \
  --sitemap-output  seo-audit-reports/<dominio>_<YYYY-MM-DD>/sitemap_report.csv
```

Dove `<dominio>` è l'host dell'URL senza schema né `www.` (es. `example.com`
per `https://www.example.com`) e `<YYYY-MM-DD>` è la data odierna. Se esiste
già una cartella con lo stesso nome (stesso dominio, stesso giorno), aggiungi
un suffisso incrementale (`_2`, `_3`, ...) invece di sovrascriverla, a meno
che l'utente non chieda esplicitamente di sovrascrivere l'audit precedente.

Ometti `--sitemap-output` solo se hai usato `--no-sitemap` (in quel caso il
file di confronto sitemap non viene generato). Aggiungi `--no-sitemap` solo
se l'utente ha chiesto di disabilitare il confronto con la sitemap.

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
3. Presenta all'utente sia il CSV che l'HTML come file scaricabili,
   specificando il percorso della cartella di output usata.
4. Se il crawl si è fermato per `--max-pages`, `--max-depth` o `robots.txt`,
   segnalalo esplicitamente: il report potrebbe non coprire l'intero sito.

## Note tecniche

- La profondità 0 è la home/URL di partenza; profondità 4 significa che
  vengono seguiti fino a 4 "salti" di link interni da lì.
- Lighthouse gira in modalità headless via Chromium; la prima esecuzione può
  richiedere qualche secondo in più per l'installazione di Chromium.
- Il throttling delle richieste HTTP è globale: anche con `--concurrency` >
  1, il totale rispetta comunque `--max-rps`.
- Per siti molto grandi, riduci `--lighthouse-sample` (o portalo a 0) o la
  profondità per velocizzare.
