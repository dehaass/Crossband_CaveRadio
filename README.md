# Crossband Cave Radio Relay
Very early stages but functional. Almost completely 'vibe-coded'. 

This repository implements a simple radio relay bridge between Fldigi-based digital message data traffic and the APRS network. The goal is to let messages received from a QDX-style cave radio path be decoded and forwarded over APRS, while also allowing APRS traffic to be sent out via the same radio path.

In practical terms, the project listens for RadioMSG traffic from Fldigi, parses the incoming packets, logs them, and routes them into APRS messages over a KISS TCP connection. It also exposes a small Flask dashboard for monitoring health and recent traffic.

## Project goal

The project is designed for a low-power, underground or cave radio environment where operators may:

- receive messages from a radio modem through Fldigi using the THOR4 modem,
- convert those messages into APRS traffic,
- transmit APRS messages to the radio network,
- monitor relay status, health, and recent activity from a browser.

This is mainly an experimental amateur-radio relay system rather than a polished, general-purpose messaging platform.

## Current capabilities

At the moment, the repo includes the following working pieces:

- Crossband relay logic in `Crossband_Relay.py`
  - connects to Fldigi on `127.0.0.1:7362`
  - reads raw RX data from the Fldigi XML-RPC interface
  - parses RadioMSG messages
  - converts received RadioMSG traffic into APRS packets
  - monitors Fldigi/APRS health and restarts failed connections when needed
  - stores traffic in `messages.jsonl`

- APRS support in `aprs_relay.py`
  - builds AX.25/APRS UI frames
  - sends APRS messages over a KISS TCP listener
  - decodes incoming APRS packets and recognizes addressed messages and ACK/REJ frames

- RadioMSG helpers in `QDX_Fldigi_coms.py`
  - formats transmit buffers for Fldigi
  - sends RadioMSG-style payloads over Fldigi

- Web dashboard in `CaveRelay_Website/app.py`
  - serves a small Flask UI at the root page
  - exposes `/api/messages` for recent traffic logs
  - exposes `/api/health` for current relay health
  - supports sending messages to APRS and Fldigi from the web UI/API

- Traffic logging in `messages.jsonl`
  - appends relay events as JSON Lines entries
  - useful for debugging and message archival

## Repository layout

- `Crossband_Relay.py` — main crossband relay implementation
- `aprs_relay.py` — APRS KISS sender/receiver utility
- `QDX_Fldigi_coms.py` — RadioMSG formatting and Fldigi transmit helpers
- `radiomsg.py` — RadioMSG parser and checksum logic
- `test_radiomsg_tx.py` — example script for transmitting a RadioMSG test message
- `CaveRelay_Website/` — Flask app for monitoring and sending traffic
- `messages.jsonl` — traffic log file created while the relay runs

## Requirements

The project expects the following external components to be available:

- Python 3.10+ recommended
- Fldigi installed and running locally
- Access to a radio modem or sound card interface for Fldigi
- Dire Wolf (or another KISS-compatible APRS TNC) listening on TCP port 8001
- A working `pyFldigi` Python interface available to the project
- A radio configuration that supports the THOR4 modem in Fldigi

### Required runtime services

The relay is configured for these addresses:

- Fldigi XML-RPC host: `127.0.0.1:7362`
- APRS KISS host: `127.0.0.1:8001`
- Web dashboard: `127.0.0.1:5000` by default

The code is hard-coded to use these values in several places, so the local services should match this layout unless you edit the constants.

## Installation and setup

### 1. Create and activate a virtual environment

From the repository root:

```bash
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
```

On macOS/Linux:

```bash
source .venv/bin/activate
```

### 2. Install Python dependencies

The repo includes a website dependency file at `CaveRelay_Website/requirements.txt`:

```bash
pip install -r CaveRelay_Website/requirements.txt
```

This currently installs Flask. If your environment requires additional Python packages for the radio stack or `pyFldigi`, install them as needed.

### 3. Ensure `pyFldigi` is available

The project adds a local `pyFldigi` directory to `sys.path` if it exists beside the repo, but the repo does not appear to include that library directly. You will need either:

- an installed `pyFldigi` package in the same Python environment, or
- a local checkout named `pyFldigi` adjacent to the repo root that matches the expected import structure.

If necessary, install or configure the library before running the relay.

### 4. Start Fldigi

Configure Fldigi as follows:

- listen on `127.0.0.1:7362` for XML-RPC/command access
- use the `THOR4` modem mode
- leave the local radio path connected to the sound card or radio hardware you intend to use

The relay code checks the modem and will attempt to switch Fldigi to `THOR4` automatically if needed.

### 5. Start Dire Wolf or another APRS KISS TNC

Configure Dire Wolf to listen on a KISS TCP port. The project expects a KISS listener at `127.0.0.1:8001`.

Typical APRS/Dire Wolf configuration should expose the KISS interface to the local machine and use the callsign configured by the script (`VE6LF` in the relay code).

### 6. Run the relay

To start the main relay loop:

```bash
python Crossband_Relay.py
```

This starts the APRS service and enters a loop that polls Fldigi for RadioMSG traffic.

### 7. Run the web dashboard

To launch the monitoring interface:

```bash
python CaveRelay_Website/app.py
```

Then open:

```text
http://127.0.0.1:5000/
```

The dashboard provides access to:

- recent traffic
- current health status
- APRS send form
- Fldigi send form

## Example usage

### Send a direct APRS message

```bash
python aprs_relay.py --source VE6LF --destination UNIT7 --message "Relay test" --message-id 001
```

### Send a RadioMSG test message through Fldigi

```bash
python test_radiomsg_tx.py
```

This script is intended to test Fldigi transmit behavior from the local machine.

## Operational notes

- The application writes relay traffic into `messages.jsonl`.
- The code includes health-monitoring logic and automatic connection restarts for both APRS and Fldigi.
- This repo is aimed at a local experimental radio network and is not a turnkey public service.
- If the radio environment or Fldigi/Dire Wolf setup changes, you may need to adjust the hard-coded addresses, callsigns, and modem names in the Python files.

## Recommended next steps

1. Verify Fldigi is reachable on `127.0.0.1:7362`.
2. Verify Dire Wolf/KISS is reachable on `127.0.0.1:8001`.
3. Start the relay in a terminal and watch for log output.
4. Use the web dashboard to confirm health and message flow.
5. Adjust callsigns, ports, and modem configuration to fit your actual hardware setup.

## License

This repository does not currently include an explicit license file. Check the repository state and your local project policy before distributing or publishing modified versions.
