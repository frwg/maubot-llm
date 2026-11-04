# maubot-llm

Ein Maubot-Plugin für interne Matrix-Chats mit OpenAI-kompatiblen Chat-Completions-APIs. Die Modell-API wird vom Maubot-Prozess aus angesprochen; für den Betrieb sind keine zusätzlichen Cloud-Dienste erforderlich.

## Kompatibilität und Funktionsumfang

- Getestete Laufzeit: Python 3.12 und Maubot 0.6.0.
- `basic_openai`: OpenAI-kompatible `POST /v1/chat/completions`- und `GET /v1/models`-Endpunkte, konfigurierbare Basis-URL, optionaler Bearer-API-Key und Modellparameter.
- `openai`: dieselbe API mit `https://api.openai.com` als Standard-Basis-URL.
- `anthropic`: Messages API `POST /v1/messages`; die Modellerkennung ist nicht implementiert.
- Nachrichtenkontexte werden nach Raum, Thread und (in Direktnachrichten) Matrix-Absender getrennt. Anzahl und Ablaufzeit sind begrenzt/konfigurierbar.
- Antworten erfolgen asynchron mit einer globalen Parallelitätsbegrenzung. Streaming wird nicht verwendet; Antworten werden bei Bedarf in mehrere Matrix-Nachrichten geteilt.
- Maubots `MessageEvent.reply()` wird für Markdown-Antworten mit deaktiviertem HTML-Parsing genutzt. Das ist keine Garantie für identische Markdown-Darstellung in allen Matrix-Clients.

Die konkrete Firmen-API ist noch nicht verfügbar. Automatische Integrationstests verwenden ausschließlich einen lokalen Mock-HTTP-Server. Server-spezifische Erweiterungen, abweichende Authentifizierungsverfahren und Tokenizer-basierte Tokenbudgets sind daher nicht abgedeckt.

## Installation

1. Maubot 0.6.0 mit Python 3.12 betreiben. Der Bot braucht Netzwerkzugriff auf den Matrix-Homeserver und den konfigurierten LLM-Endpunkt.
2. Einen dedizierten Matrix-Bot-Benutzer über den Homeserver-Administrator bzw. den dort freigegebenen Registrierungsweg anlegen. Kein persönliches Mitarbeiterkonto verwenden.
3. Einen frischen Client für dieses Konto in Maubot anlegen. Entweder mit `mbc auth --update-client` (Homeserver in der Maubot-Konfiguration eintragen und `mbc login` ausführen) oder über **Clients** im Maubot-Webinterface mit Homeserver, Bot-MXID, Access-Token und Device-ID. Kein Element-Token wiederverwenden. Details: [Maubot Basic usage](https://docs.mau.fi/maubot/usage/basic.html) und [`mbc auth`](https://docs.mau.fi/maubot/usage/cli/auth.html).
4. Für E2EE müssen Maubot und der Client zusätzlich gemäß der [offiziellen Maubot-E2EE-Anleitung](https://docs.mau.fi/maubot/usage/encryption.html) mit Crypto-Abhängigkeiten und einem frischen, E2EE-fähigen Device eingerichtet werden.
5. Plugin bauen und hochladen:

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install --upgrade pip
   .venv/bin/python -m pip install -r requirements-test.txt
   .venv/bin/mbc build
   ```

   Im Maubot-Webinterface die erzeugte `.mbp`-Datei hochladen, einen Plugin-Client auswählen und eine Plugin-Instanz anlegen. Die Plugin-Konfiguration anschließend im Instanz-Editor anpassen.

6. Den Bot in einen Element-Raum einladen und mit `!llm info` die aktive Konfiguration prüfen.

## LLM konfigurieren

`base-config.yaml` ist eine sichere Beispielkonfiguration. Ein interner OpenAI-kompatibler Backend-Eintrag sieht beispielsweise so aus:

```yaml
default_backend: company
backends:
  company:
    type: basic_openai
    base_url: https://llm.example.invalid/api
    api_key: REPLACE_WITH_SECRET
    default_model: internal-model
    models:
      - internal-model
    default_system_prompt: You are a helpful internal assistant.
    timeout: 120
    temperature: 0.7
    max_tokens: 2048
```

`base_url` ist der API-Präfix, an den das Plugin `/v1/chat/completions` beziehungsweise `/v1/models` anhängt. Die Adresse muss vom Maubot-Container aus erreichbar sein. Interne HTTPS-Zertifikate müssen im Trust Store des Maubot-Prozesses liegen; die TLS-Prüfung wird nicht abgeschaltet. HTTP-URLs sind für vertrauenswürdige lokale Netze möglich.

Die Basis-URL kann nur in der administrativen Maubot-Instanzkonfiguration geändert werden, niemals durch Chat-Nachrichten. HTTP-Weiterleitungen werden nicht verfolgt. Zugangsdaten gehören in den geschützten Maubot-Konfigurationsspeicher bzw. das unternehmensinterne Secret-Management, nicht in Git, Images, Logs oder Chatbefehle. Maubot interpoliert `${...}` in dieser YAML nicht automatisch.

Beim Typ `openai` ist `base_url` optional und standardmäßig `https://api.openai.com`. `anthropic` verwendet die Messages API und `max_tokens`; für dieses Backend Modellnamen in `models` explizit freigeben, da `GET /v1/models` nicht unterstützt wird. Andere OpenAI-kompatible API-Abweichungen sind nicht getestet.

## Zugriff, Räume und Befehle

- `allowlist: []` sperrt die Nutzung für alle normalen Benutzer. Nur explizit eingetragene Matrix-IDs dürfen chatten. `allowlist: false` öffnet den Bot für alle; dies nur bewusst aktivieren.
- `admin_users` bestimmt globale Bot-Administratoren. Zusätzlich können Matrix-Raummitglieder mit Power-Level mindestens 50 die raumbezogenen Einstellungen verwalten.
- Gruppenräume reagieren standardmäßig auf eine Matrix-Erwähnung des Bots oder den Befehl `!llm`. `!llm mode all` aktiviert Antworten auf alle Textnachrichten des Raums; `!llm mode mention` setzt den Standard zurück. Direktnachrichten werden anhand der Mitgliedschaft erkannt; bei abweichenden Raumkonstellationen können Raum-IDs unter `direct_rooms` eingetragen werden.
- Matrix stellt kein universelles Kennzeichen für Bot-Konten bereit. Andere Bots können über ihre MXIDs unter `ignored_senders` explizit ausgeschlossen werden.
- `!llm info`: aktive Backend-/Modell-/Prompt-/Modus-Einstellungen anzeigen (keine API-Schlüssel).
- `!llm clear`: eigenen Kontext in einer Direktnachricht löschen. In einem Gruppenraum können nur Raum- oder Bot-Administratoren den aktuellen Kontext löschen.
- `!llm clear-room`: sämtliche Kontexte des Raums löschen (Raum- oder Bot-Administratoren).
- `!llm backend KEY`, `!llm model NAME`, `!llm system TEXT` und `!llm mode mention|all`: raumbezogene Einstellungen ändern. Ein Modell muss unter `models` freigegeben oder über die Backend-Modellerkennung verfügbar sein.

Kontexte in Gruppenräumen sind innerhalb eines Raums bzw. Threads gemeinsam. Administratoren sollten keine Nutzer mit unterschiedlichen Berechtigungs- oder Vertraulichkeitsstufen in denselben Chat-Kontext aufnehmen. Direktnachrichten sind zusätzlich nach Absender getrennt.

## Datenbank, Updates und Betrieb

Die Plugin-Datenbank wird über Maubots UpgradeTable migriert. Version 2 ergänzt Kontextsichten, Zeitstempel, Sequenzzähler, Raummodus und Event-Deduplizierung. Bestehende Räume und Nachrichten bleiben erhalten und werden dem bisherigen Raumkontext zugeordnet. Vor Plugin-Updates trotzdem das Maubot-Datenbank-Volume sichern und die automatische Migration zunächst in einer Testinstanz prüfen.

Maubot führt die Datenbankpflege durch. In Docker/OpenShift sicherstellen, dass das Maubot-Datenbankverzeichnis persistent gespeichert wird. Der Maubot-Pod benötigt nur ausgehende Verbindungen zu Matrix und zum LLM; interne TLS-Zertifikate, DNS, NetworkPolicies und Secret-Zugriff müssen dort passend eingerichtet sein. Vor Updates Datenbank und Maubot-Konfiguration sichern; nach erfolgreichem Update Backup-Aufbewahrung gemäß Unternehmensrichtlinie behandeln.

## Entwicklung und Qualitätssicherung

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -B -m pytest -q
.venv/bin/ruff check .
.venv/bin/pip-audit
.venv/bin/mbc build
```

CI führt Tests, Ruff, `pip-audit` und den `.mbp`-Build aus. Ein echter Homeserver-/Element-Test sowie Tests gegen die Firmen-API benötigen eine separate, zugelassene Testumgebung und sind nicht Bestandteil der lokalen Mock-Tests.

## Bekannte Einschränkungen

- Kein Tokenizer-basiertes Kontextbudget; der Nachrichtenverlauf wird durch Anzahl und Inaktivitätszeit begrenzt.
- Keine Streaming-Ausgabe und keine periodische Aktualisierung der Schreibanzeige während einer Generierung.
- Die Berechtigung zum Ändern von Raumeinstellungen basiert auf dem Matrix-Power-Level ab 50 oder `admin_users`; eine abweichend konfigurierte unternehmensweite Admin-Rolle wird nicht automatisch erkannt.
- Das Verhalten von E2EE, Geräteverifizierung und Crypto-State nach Neustarts hängt von der installierten Maubot-/Crypto-Konfiguration ab und muss separat in einer echten Testinstanz validiert werden.
- Antworten sind vom konfigurierten Modell und dessen Kontextfenster abhängig. Providerfehler werden nutzerverständlich gemeldet; Rate-Limits werden nicht automatisch wiederholt.
