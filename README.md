# Pollbook Clients

Client-side implementations of the two Raspberry Pi check-in kiosks described
in the pollbook paper: the voter-facing **untrusted client** and the
poll-worker-supervised **trusted client**. This repo replaces the older,
scattered copies of this code (previous prototype iterations lived in the
`Untrusted_Client` repo and under `HardwareSourceCodes-/Trusted_Devices/RaspberryPi`);
those earlier versions are superseded by what's here.

## Layout

```
untrusted_client/
  untrustedclient_ui_2Id.py   — voter-facing kiosk
  config.ini.example
trusted_client/
  trustedclient.py            — poll-worker-supervised kiosk
  config.ini.example
```

## Components

**`untrusted_client/untrustedclient_ui_2Id.py`** — the voter-facing,
untrusted kiosk. Presents a Tkinter UI with two check-in paths:

- **ID card mode**: captures a photo of the voter's ID with the Pi camera,
  runs it through an OpenCV preprocessing pipeline (rotation, resize,
  denoise, adaptive threshold) and Tesseract OCR, extracts name and ID
  number, and asks the voter to confirm before continuing.
- **Manual entry mode**: lets the voter type their name and ID number
  directly, for cases where the card can't be read.

Either path sends a signed validation request to the ID service (image or
text variant), verifies the signed response, forwards a signed check-in
request to the check-in server, and — on approval — writes the returned
ticket to an RFID tag via a PN532 reader for the voter to carry to the
trusted client.

**`trusted_client/trustedclient.py`** — the poll-worker-supervised, trusted
kiosk. Reads the RFID ticket written by the untrusted client, prompts for
the voter's PIN, sends a signed request to the check-in server, and on
approval displays the voter's name and secret/voting-access token.

Both clients communicate with the backend over mTLS (a local CA plus
per-client certificates, matching the shared PKI used across the pollbook
project) and sign/verify every request and response with RSA-2048 +
PKCS1v15/SHA-256.

## Setup

1. Generate/obtain this client's certificate and key pair (signed by the
   project's shared CA — see the main pollbook-server repo's
   `generate_keys.sh`) and the CA cert. Place them under `certs/` (or any
   path you reference from `config.ini`).
2. Copy the matching example config to `config.ini` in that client's folder:
   - `cp untrusted_client/config.ini.example untrusted_client/config.ini`
   - `cp trusted_client/config.ini.example trusted_client/config.ini`
   Then fill in the real hostnames/ports and key paths for your deployment.
   `config.ini` is gitignored, so device-specific values never get
   committed.
3. Install dependencies (Raspberry Pi OS, Python 3):
   ```
   pip install cryptography opencv-python-headless pytesseract pillow numpy \
               picamera2 adafruit-circuitpython-pn532 RPi.GPIO
   sudo apt install tesseract-ocr
   ```
4. Run, from inside each folder:
   ```
   cd untrusted_client && python3 untrustedclient_ui_2Id.py   # untrusted-kiosk Pi
   cd trusted_client && python3 trustedclient.py              # trusted-kiosk Pi
   ```

## Notes on data handled by these scripts

- Captured/processed ID photos (`captured_id.jpg`, `processed_id_debug.jpg`,
  `processed_voter_id_client.jpg`) and the per-run latency logs
  (`checkin_latency_log.csv`, `trusted_checkin_latency_log.csv`) are written
  to the working directory at runtime and are gitignored — they are not
  part of this repository and should not be committed.
- No real voter data is used anywhere in this codebase; all identity
  documents and registration data used during development and evaluation
  were synthetic, generated for testing only.
