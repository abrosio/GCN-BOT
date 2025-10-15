# GCN → Telegram Bot

Bot Telegram che riceve in tempo reale avvisi dal **NASA GCN** (Gamma-ray Coordinates Network) –
inclusi *Swift/Fermi GRB*, *IGWN GW alerts* e *GCN Circulars* – e li inoltra su Telegram con
immagini/skymap quando disponibili. ATTENZIONE il file python deve rimanere in esecuzione su 
un computer che fungerà da server, altrimenti il bot non funzionerà.

> **Nota:** questo repository contiene codice Python che usa `gcn_kafka` per la sottoscrizione ai topic
> GCN e le API Bot di Telegram per l’invio dei messaggi.

---

## ✨ Funzionalità

- Ricezione e inoltro automatico di:
  - 🌊 **IGWN/LIGO-Virgo GW alerts** (JSON, preliminari filtrati di default)
  - 🛰️ **Swift-BAT GUANO / Fermi-GBM** (solo GRB) – testo e coordinate con grafica
  - 📝 **GCN Circulars** (poller periodico)
- Filtri per-sorgente per ogni utente: GW / Swift-Fermi / Circulars
- Comandi inline / tastiere interattive (menu, impostazioni, filtri, stato)
- Skymap HEALPix se disponibile (via `healpy`); alternativa Aitoff da RA/Dec oppure “card” testuale
- **Test rapido**: invia l’ultima GCN Circular (`/testriceviultimagcn`)
- Blocco a istanza singola per evitare conflitti
- Salvataggi locali (JSON) per visti/filtri/ultimo circular

---

## 🧱 Requisiti

- Python **3.10+**
- Token BOT Telegram
- Credenziali **GCN Kafka** (client id/secret)
- Librerie Python:

```bash
pip install -U requests gcn-kafka numpy matplotlib pillow astropy
# Opzionale per skymap HEALPix:
pip install healpy
```

> Su alcuni sistemi `healpy` richiede `libcfitsio`/`cfitsio` e toolchain C/Fortran.
> Se non installabile, il bot funzionerà comunque (userà il fallback grafico).

---

## ⚙️ Configurazione

Il codice di esempio include le credenziali **hardcoded**. Per sicurezza, si consiglia di
passarle come **variabili d’ambiente** e leggere i valori nel codice.

### Variabili d’ambiente suggerite

```bash
export TELEGRAM_BOT_TOKEN="123456789:ABCDEF..."
export ADMIN_CHAT_ID="123456789"
export GCN_CLIENT_ID="..."
export GCN_CLIENT_SECRET="..."
```

### Dove mettere le credenziali nel codice

Nel file `main.py` sono presenti le costanti:

```python
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "<INSERISCI_TOKEN>")
ADMIN_CHAT_ID = int(os.getenv("ADMIN_CHAT_ID", "0"))
CLIENT_ID = os.getenv("GCN_CLIENT_ID", "<INSERISCI_CLIENT_ID>")
CLIENT_SECRET = os.getenv("GCN_CLIENT_SECRET", "<INSERISCI_CLIENT_SECRET>")
```

Se utilizzi la versione con credenziali già hardcoded, **sostituisci** i placeholder con i tuoi dati.

---

## ▶️ Avvio rapido (bare metal)

Clona il repository e avvia:

```bash
git clone https://github.com/<tuo-utente>/<tuo-repo-gcn-telegram>.git
cd <tuo-repo-gcn-telegram>

# (opzionale) crea e attiva un venv
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

pip install -r requirements.txt  # se presente
# oppure installa manualmente i pacchetti (vedi sopra)

# esporta le variabili (consigliato)
export TELEGRAM_BOT_TOKEN="..."
export ADMIN_CHAT_ID="123456789"
export GCN_CLIENT_ID="..."
export GCN_CLIENT_SECRET="..."

python main.py
```

Se tutto è ok, vedrai in console:
```
✅ GCN BOT avviato. In ascolto…
[GCN] Subscribed to topics:
  - igwn.gwalert
  - gcn.notices.swift.bat.guano
  - gcn.classic.text.FERMI_GBM_ALERT
  ...
[GCN] Circulars poller attivo.
```

Apri la chat del bot su Telegram e invia `/start`.

---

## 🕹️ Comandi principali

- `/start` – messaggio di benvenuto e attivazione ricezione
- `/menu` – menu principale
- `/impostazioni` – azioni principali (attiva/disattiva, filtri, stato)
- `/testriceviultimagcn` – **mostra l’ultima GCN Circular** (test rapido)
- `/filtri` – pannello on/off: GW / Swift-Fermi / Circulars
- `/status` – riepilogo stato e filtri correnti
- `/help` – guida rapida
- `/contattaautore` – contatti

> Le stesse azioni sono disponibili via **pulsanti inline** (tastiera Telegram).

---

## 🧩 Struttura del codice (overview)

- **Kafka consumer**: sottoscrizione ai topic GCN (GW / Swift-Fermi / Fermi classic)
- **Parsers**: normalizzano i dati (caption + meta) e tentano di estrarre RA/Dec
- **Immagini**: priorità a quicklook/preview; altrimenti HEALPix → `healpy`; fallback Aitoff o card
- **Circulars poller**: controlla nuova *GCN Circular* e la inoltra (se filtrata ON)
- **Telegram UI**: long-polling, inline keyboards, persistenza filtri/stato per chat

---

## 🧪 Test rapido (ultima Circular)

Il comando `/testriceviultimagcn` interroga la pagina delle circular e restituisce l’ultima pubblicata,
senza dipendere dal consumer Kafka. Utile per verificare che il bot risponda correttamente in chat.

---

## 🛟 Troubleshooting

- **Nessun messaggio in arrivo**  
  - Verifica credenziali GCN (client id/secret) e connettività verso `gcn.nasa.gov`.
  - Assicurati che *non* ci sia un’altra istanza del bot in esecuzione (lock TCP occupato).
  - Controlla che i **filtri** non stiano bloccando la categoria di tuo interesse.
- **Skymap non mostrata**  
  - Probabilmente `healpy` non è installato o non è leggibile il FITS. Il bot invierà comunque un grafico alternativo.
- **Errori 409 Telegram**  
  - Il codice disattiva esplicitamente il webhook (`deleteWebhook`). Se usi webhooks, rimuovi il long-polling.
- **Windows + healpy**  
  - Installare `healpy` su Windows può essere complesso; valuta WSL/conda, oppure accetta il fallback grafico.

---

## 🔒 Privacy e sicurezza

- Evita di committare **token** o **segreti** nel repository.
- Preferisci variabili d’ambiente o un file `.env` *non versionato*.
- I file JSON locali (`subscribers.json`, ecc.) contengono ID chat: gestiscili con attenzione.

---

## 📝 Licenza

Distribuito con licenza **MIT**. Vedi `LICENSE`.

---

## 🙌 Crediti

- **NASA GCN** – https://gcn.nasa.gov/
- **gcn_kafka** – client Python per GCN Kafka
- **Telegram Bot API** – https://core.telegram.org/bots/api
