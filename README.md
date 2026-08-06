# Accredia Downloader

Downloader regionale dei certificati pubblicati nella banca dati Accredia.

Il programma utilizza Playwright per mantenere una sessione browser validata,
BeautifulSoup per interpretare `div.ppsearch > table` e SQLite come staging
temporaneo. Al termine produce un solo `certificati.json` per regione.

Il CAPTCHA non viene risolto né aggirato automaticamente. Quando la sessione
non è valida, l'utente deve completare manualmente la verifica nel browser.

Prima di effettuare estrazioni massive è responsabilità dell'utilizzatore
verificare di essere autorizzato e rispettare condizioni d'uso, limiti tecnici
e frequenza delle richieste del servizio.

## Flusso di lavoro

Per una regione il downloader:

1. apre la pagina filtrata con
   `PPSEARCH_COMPANY_SEARCH_MASK_REGIONE`;
2. legge il totale indicato da `Risultati:`;
3. calcola le pagine considerando 20 tabelle per pagina;
4. modifica esclusivamente il parametro zero-based `page`;
5. interpreta le tabelle figlie dirette di `div.ppsearch`;
6. registra ogni pagina in una transazione SQLite;
7. accorpa soltanto le tabelle completamente identiche;
8. ricontrolla il totale al termine;
9. genera atomicamente il JSON regionale;
10. aggiorna l'indice e rimuove lo staging SQLite.

Una pagina incompleta non viene registrata. Il precedente JSON regionale non
viene sostituito finché il nuovo snapshot non supera tutti i controlli.

## Installazione

### Windows PowerShell

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Per usare Firefox:

```powershell
python -m playwright install firefox
```

Per usare Chromium:

```powershell
python -m playwright install chromium
```

### macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Google Chrome è il browser predefinito. Sono disponibili anche `msedge`,
`chromium` e `firefox` tramite `--browser-channel`.

Ogni motore deve utilizzare il proprio profilo persistente. Le directory dei
profili sono escluse da Git perché possono contenere cookie e sessioni.

## Verifica manuale della sessione

Con Chrome:

```powershell
python accredia_scraper.py --check-session --region Abruzzo
```

Con Firefox e un profilo dedicato:

```powershell
python accredia_scraper.py --check-session `
  --region Abruzzo `
  --browser-channel firefox `
  --profile-dir .accredia-firefox-profile
```

Se viene aperta la maschera di ricerca:

1. completare manualmente il CAPTCHA;
2. lasciare vuoti i filtri;
3. cliccare `Cerca`;
4. attendere i risultati;
5. confermare nel terminale.

Dopo la conferma il programma ricarica automaticamente l'URL con il filtro
regionale configurato.

## Download di una regione

Con Chrome:

```powershell
python accredia_scraper.py --region Abruzzo
```

Con Firefox:

```powershell
python accredia_scraper.py `
  --region Abruzzo `
  --browser-channel firefox `
  --profile-dir .accredia-firefox-profile
```

Per i nomi contenenti spazi o apostrofi usare le virgolette:

```powershell
python accredia_scraper.py --region "Valle d'Aosta"
```

Il nome inviato ad Accredia conserva la forma originale. La directory viene
normalizzata per essere portabile su Windows:

```text
Abruzzo               -> abruzzo
Emilia-Romagna        -> emilia-romagna
Valle d'Aosta         -> valle-d-aosta
Trentino-Alto Adige   -> trentino-alto-adige
```

## Download automatico di tutte le regioni

Per completare automaticamente tutte le regioni usando una sola sessione
Firefox:

```powershell
python accredia_scraper.py `
  --all-regions `
  --browser-channel firefox `
  --profile-dir .accredia-firefox-profile
```

Senza opzioni aggiuntive il ciclo scarica soltanto le regioni mancanti o con
stato locale incompleto. Una regione viene saltata esclusivamente quando sono
presenti e coerenti tutti e tre i file:

```text
certificati.json
state/records-index.jsonl
state/last-run.json
```

Gli eventuali `staging.sqlite` incompleti continuano a essere gestiti dalla
singola regione. Se compatibili, il download riprende dalle pagine già salvate.

Per aggiornare completamente anche gli snapshot esistenti:

```powershell
python accredia_scraper.py `
  --all-regions `
  --refresh-existing `
  --browser-channel firefox `
  --profile-dir .accredia-firefox-profile
```

Per aggiornare soltanto le regioni il cui ultimo completamento risale ad almeno
7 giorni prima:

```powershell
python accredia_scraper.py `
  --all-regions `
  --refresh-after-days 7 `
  --browser-channel firefox `
  --profile-dir .accredia-firefox-profile
```

Il ciclo mantiene aperto un unico browser. Un errore nei dati di una regione
viene riportato nel riepilogo e il programma prova quella successiva. Se la
sessione CAPTCHA o il browser non sono più disponibili, il ciclo si interrompe
per evitare una sequenza di errori identici.

## Parametri principali

```text
--region REGION
--all-regions
--refresh-existing
--refresh-after-days GIORNI
--delay SECONDI
--timeout SECONDI
--retries NUMERO
--browser-channel chrome|msedge|chromium|firefox
--profile-dir PERCORSO
--headless
--check-session
--output-root PERCORSO
```

Il ritardo predefinito è 2 secondi e non può essere inferiore a 1 secondo.

`--retries` indica i nuovi tentativi di rete per ciascuna richiesta e ha valore
predefinito `10000`. In caso di timeout, disconnessione o errore HTTP temporaneo,
il programma applica un'attesa crescente di 1, 2, 4, 8, 16 e infine 30 secondi.
Da quel momento l'attesa rimane limitata a 30 secondi. È sempre possibile
interrompere con `Ctrl+C`: le pagine già completate restano nello staging.

I tentativi relativi ai file locali sono volutamente separati e limitati a 3,
per evitare che un file bloccato su Windows lasci il processo in attesa per ore.

## Struttura dei dati

```text
documenti/accredia/
└── regioni/
    ├── abruzzo/
    │   ├── certificati.json
    │   └── state/
    │       ├── records-index.jsonl
    │       ├── last-run.json
    │       └── staging.sqlite
    ├── basilicata/
    │   └── ...
    └── veneto/
        └── ...
```

`staging.sqlite` esiste soltanto durante un'esecuzione incompleta. Dopo il
completamento viene eliminato insieme agli eventuali file `-wal` e `-shm`.

`records-index.jsonl` è un indice compatto che permette di confrontare gli
aggiornamenti senza caricare in memoria tutti gli HTML del JSON regionale.

`last-run.json` contiene il riepilogo dell'ultima esecuzione completata.

## Ripartenza e consistenza

Ogni pagina viene salvata in una singola transazione SQLite. Se il processo si
interrompe, le pagine completate nello stesso giorno possono essere riprese.

Lo staging viene ricreato dalla pagina 1 quando:

- appartiene a un giorno precedente;
- il totale dei risultati è cambiato;
- la regione è differente;
- la versione dello schema record è cambiata.

Prima della pubblicazione il totale viene letto nuovamente. Se è diverso dal
totale iniziale, il JSON non viene sostituito e la regione dovrà essere
riscaricata dalla pagina 1.

## Identificatori e duplicati

Sono presenti tre identificatori SHA-256.

### `certificate_id`

Identifica il certificato di base usando:

```text
organismo + numero certificato + partita IVA/CF + norma
```

### `entity_id`

Identifica certificato, emissione e sede:

```text
certificate_id + data emissione + tipo sede + indirizzo sede
```

### `record_id`

Identifica la versione esatta della tabella:

```text
SHA-256(entity_id + content_hash)
```

`content_hash` comprende l'intera tabella `raw_html`. Una variazione di testo o
punteggiatura, compresa una virgola, genera quindi un record distinto.

Due tabelle con lo stesso `record_id` vengono accorpate. Il JSON conserva il
numero di copie osservate in `source_occurrences`. Pagina e posizione non
partecipano agli identificatori, quindi uno spostamento tra pagine non crea un
duplicato.

## Formato di `certificati.json`

```json
{
  "schema_version": 1,
  "record_schema_version": 3,
  "region": "Abruzzo",
  "run_id": "20260806T120000+0200",
  "started_at": "2026-08-06T12:00:00+02:00",
  "completed_at": "2026-08-06T13:30:00+02:00",
  "total_results_reported": 15234,
  "total_pages": 762,
  "tables_processed": 15234,
  "unique_records": 14980,
  "duplicates_collapsed": 254,
  "records": [
    {
      "schema_version": 3,
      "record_id": "...",
      "entity_id": "...",
      "certificate_id": "...",
      "certificate_number": "C2024-00722",
      "issued_on": "2024-01-01",
      "status": "in corso di validità",
      "accreditation_body": {},
      "company": {},
      "scope": "...",
      "standard": "UNI EN ISO 9001:2015",
      "accreditation_scheme": "SGQ",
      "sectors": [],
      "updated_on": "2026-08-03",
      "source": {
        "url": "...&page=0",
        "url_page": 0,
        "page": 1,
        "position": 1,
        "scraped_at": "2026-08-06T12:00:00+02:00"
      },
      "source_occurrences": 2,
      "raw_text": "...",
      "raw_html": "<table>...</table>",
      "content_hash": "..."
    }
  ]
}
```

Il file viene prodotto in streaming e sostituito con `Path.replace()`, quindi
non è necessario caricare l'intera regione in memoria.

## Aggiornamenti

Il riepilogo distingue:

- `new`: nuova `entity_id`;
- `updated`: stessa `entity_id`, contenuto differente;
- `unchanged`: stesso `record_id`;
- `missing`: `entity_id` non più presente nello snapshot regionale.

Il JSON finale contiene lo snapshot corrente. L'indice conserva soltanto gli
hash necessari ai confronti successivi.

La ricerca pubblica consente un filtro per data di rilascio, ma non per data di
ultimo aggiornamento. Per riconoscere in modo affidabile modifiche, sospensioni
e rimozioni, una regione selezionata per l'aggiornamento viene quindi riletta
completamente. `--refresh-after-days` riduce il lavoro scegliendo quali regioni
aggiornare, senza trasformare il controllo in un confronto parziale insicuro.

## Test

```powershell
python -m unittest discover -s tests -v
```

I test non effettuano richieste ad Accredia. Verificano parser, identificatori,
deduplicazione, transazioni SQLite, esportazione atomica e controllo del totale.

## File locali esclusi da Git

Non devono essere committati:

- profili Playwright;
- ambienti virtuali;
- cookie e file `.env`;
- `documenti/accredia/`;
- database SQLite e JSON scaricati.
