# Prana VMC — Integrazione per Home Assistant

Integrazione **custom** per Home Assistant per controllare i recuperatori **Prana** tramite **API locale su Wi-Fi** (senza cloud).

> ✅ Questo progetto è basato sull’integrazione originale di dCode91:  
> https://github.com/dCode91/ha-prana-local-wifi  
> Grazie a dCode91 per il lavoro iniziale 🙏

---

## Compatibilità / Test

✅ **Testata su dispositivi Prana serie ERP con firmware 49**.  
Altri modelli o versioni firmware potrebbero esporre campi diversi tramite API locale (sensori/flag), quindi alcune entità potrebbero variare.

---

## Requisiti

- Recuperatore Prana con **Wi-Fi** 
- **Serie ERP con firmware 49** (ambiente di test di riferimento)
- Home Assistant nella stessa rete locale del dispositivo
- (Consigliato) Home Assistant Core **2024.2+** per supporto `icons.json` (icone preset)

---

## Funzionalità

### ✅ Controllo completo
- **Entità Climate** (card nativa “climate” in Lovelace)
  - Modalità: `off` / `fan_only`
  - Velocità ventilazione: `off` / `1..6`
  - Preset: `manual`, `auto`, `auto_plus`, `night`, `boost`, `winter`
  - Spegnimento reale: il tasto **OFF** spegne davvero la VMC
- **Ventole (controllo velocità)**
  - Supply (immissione)
  - Extract (estrazione)
  - Bounded/Recuperator (sincronizzate)
- **Modalità (switch)**
  - Bound Mode, Heater, Winter, Auto, Auto+, Night, Boost
- **Luminosità display** (0..6)

### ✅ Sensori (in base al modello)
- Temperature inside/outside (e secondarie se presenti)
- Umidità, CO₂, VOC, pressione aria
- Velocità correnti ventole

### ✅ Migliorie rispetto alla base
Questa versione include:
- Entità **Climate** completa per usare la card nativa
- Logiche preset coerenti (night/auto/auto+ e boost)
- Sincronizzazione UI più stabile (anti “rimbalzo” valori dopo i comandi)
- Supporto icone preset tramite `icons.json`

---

## Installazione

### Opzione A — HACS (se il repo è aggiunto come custom repository)
1. HACS → **Integrations**
2. Menu (⋮) → **Custom repositories**
3. Aggiungi l’URL del repository (tipo **Integration**)
4. Installa **Prana VMC**
5. **Riavvia Home Assistant**

### Opzione B — Manuale
1. Copia la cartella `prana_vmc` in:
   `config/custom_components/prana_vmc/`
2. Riavvia Home Assistant

---

## Configurazione

### Discovery automatico (se disponibile)
Se mDNS/Zeroconf è supportato dalla rete/router, il dispositivo comparirà in:
**Impostazioni → Dispositivi e servizi**

### Configurazione manuale
1. **Impostazioni → Dispositivi e servizi**
2. **Aggiungi integrazione**
3. Cerca **Prana VMC**
4. Inserisci **IP** del dispositivo
5. Imposta (opzionale) un nome
6. Conferma

---

## Entità 
> Le entità dipendono dal modello/firmware. Sui Prana **serie ERP firmware 49** nella mia installazione vengono create queste.

### Climate
- **Climate* — entità principale per la card Climate

### Switch (modalità)
- **Auto Mode**
- **Auto+ Mode**
- **Boost Mode**
- **Bound Mode**
- **Night Mode**
- **Winter Mode**
- **Heater**

### Number (controlli)
> Queste sono le entità di controllo (slider) che inviano comandi alla VMC.
- **Display Brightness**
- **Supply Fan Speed**
- **Extract Fan Speed**
- **Recuperator Speed**

### Sensor (letture)
> Queste sono le letture reali (stato) riportate dal dispositivo.
- **Air Pressure**
- **Humidity**
- **Inside Temperature**
- **Outside Temperature 2**
- **Supply Speed**
- **Extract Speed**
- **Bounded Speed**

---

##  Icone preset (icons.json)
Questa integrazione include: onfig/custom_components/prana_vmc/icons.json
per mostrare icone diverse sui preset della climate (luna per night, razzo per boost, ecc.).
Richiede Home Assistant Core 2024.2+.


##  Troubleshooting
> Non compare in discovery
Verifica che HA e Prana siano nella stessa rete
Alcuni router bloccano mDNS: usa la configurazione manuale via IP
> Valori non si aggiornano / entità unavailable
Verifica con ping che l’IP sia raggiungibile
Riavvia VMC e Home Assistant
Controlla i log
Abilita log di debug (opzionale)

> In configuration.yaml:
logger:
  default: warning
  logs:
    custom_components.prana_vmc: debug
Riavvia e controlla: Impostazioni → Sistema → Log

##  Crediti
Progetto originale di riferimento: dCode91/ha-prana-local-wifi
https://github.com/dCode91/ha-prana-local-wifi

##  Licenza
Questo progetto è rilasciato sotto licenza MIT - consultare il file LICENSE per i dettagli.
