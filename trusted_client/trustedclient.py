import asyncio
import json
import logging
import ssl
import configparser
import base64
import time
import os
import csv
from datetime import datetime
from asyncio import run_coroutine_threadsafe
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import RPi.GPIO as GPIO
import tkinter as tk
from tkinter import simpledialog, messagebox
import threading
import board
import busio
from adafruit_pn532.i2c import PN532_I2C
import adafruit_pn532.adafruit_pn532 as pn532_mod

GPIO.setwarnings(False)


class TrustedPollbookClient:
    def __init__(self, config, loop):
        self.logger = logging.getLogger("trusted_client")
        self.checkin_writer = None
        self.checkin_reader = None
        self.token = None
        self.loop = loop
        self.client_id = int(config["Basic"]["client_id"])
        self.checkin_server_host = config["Basic"]["checkin_service_host"]
        self.checkin_server_port = int(config["Basic"]["checkin_service_port"])

        i2c = busio.I2C(board.SCL, board.SDA)
        self.pn532 = PN532_I2C(i2c, debug=False)
        self.pn532.SAM_configuration()

        self.client_cert = config["Security"]["local_cert"]
        self.client_key = config["Security"]["private_key"]
        self.ca_cert = config["Security"]["ca_cert"]

        with open(self.client_key, "rb") as key_file:
            self.private_key_signer = load_pem_private_key(
                key_file.read(), password=None)

        self.checkin_start_time = None

    def create_ssl_context(self):
        ssl_context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=self.ca_cert
        )
        ssl_context.load_cert_chain(
            certfile=self.client_cert, keyfile=self.client_key)
        return ssl_context

    async def connect(self):
        ssl_context = self.create_ssl_context()
        self.checkin_reader, self.checkin_writer = await asyncio.open_connection(
            self.checkin_server_host, self.checkin_server_port, ssl=ssl_context
        )
        self.logger.info(
            "Connected to server at {}:{}".format(
                self.checkin_server_host, self.checkin_server_port
            )
        )

    async def read_rfid_with_retry(self):
        while True:
            token = await self.read_rfid()
            if token:
                return token

            try_again = messagebox.askretrycancel(
                "RFID Card Not Detected",
                "No valid card detected or read failed.\n\n"
                "• Make sure you’re using the correct RFID card (Mifare Classic)\n"
                "• Hold it flat on the reader for a moment\n\n"
                "Would you like to try again?",
            )
            if not try_again:
                return None

    async def read_rfid(self):
        try:
            print("Place your RFID card on the reader...")

            uid = None
            for _ in range(30):
                uid = self.pn532.read_passive_target(timeout=0.1)
                if uid is not None:
                    break
            if uid is None:
                self.logger.info("No card detected in this attempt.")
                return None

            print("Found card with UID:", [hex(i) for i in uid])

            block_number = 4
            key = b"\xFF\xFF\xFF\xFF\xFF\xFF"

            if not self.pn532.mifare_classic_authenticate_block(
                uid, block_number, pn532_mod.MIFARE_CMD_AUTH_A, key
            ):
                self.logger.info(
                    "Auth failed on block 4 (wrong card type/key?).")
                return None

            block = self.pn532.mifare_classic_read_block(block_number)
            if not block or len(block) != 16:
                self.logger.info("Could not read a full 16-byte block 4.")
                return None

            self.token = block.hex()
            print(f"[TRUSTED] Ticket READ (hex): {self.token}")

            if len(self.token) != 32 or not all(
                c in "0123456789abcdef" for c in self.token
            ):
                self.logger.info("Ticket read has invalid format.")
                return None

            self.logger.info(
                "RFID read successfully. Token: {} (Length: {})".format(
                    self.token, len(self.token)
                )
            )
            return self.token

        except Exception as e:
            self.logger.error("Error reading RFID: {}".format(str(e)))
            return None

    def record_latency(self, approved):
        if self.checkin_start_time is None:
            return
        try:
            end_time = time.time()
            latency_ms = int((end_time - self.checkin_start_time) * 1000)
            timestamp_iso = datetime.utcnow().isoformat()

            log_file = "trusted_checkin_latency_log.csv"
            file_exists = os.path.exists(log_file)

            with open(log_file, "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(
                        ["timestamp_iso", "approved", "latency_ms"])
                writer.writerow(
                    [timestamp_iso, 1 if approved else 0, latency_ms])

            self.logger.info(
                "Recorded trusted latency: %d ms (approved=%s)",
                latency_ms,
                approved,
            )
        except Exception as e:
            self.logger.error("Error writing latency log: %s", e)

    async def send_to_server(self, pin):
        try:
            self.checkin_start_time = time.time()
            self.logger.info("Preparing request with PIN: {}".format(pin))

            timestamp = int(time.time() * 1000)

            request_body = {
                "client_id": self.client_id,
                "pin": int(pin),
                "ticket": self.token.lower(),
                "timestamp": timestamp,
            }

            request_body_str = json.dumps(
                request_body, separators=(",", ":")
            ).encode()
            self.logger.debug(
                "Serialized body for signing: {}".format(
                    request_body_str.decode())
            )

            signature = self.private_key_signer.sign(
                request_body_str, padding.PKCS1v15(), hashes.SHA256()
            )
            signature_base64 = base64.b64encode(signature).decode("utf-8")

            request = {
                "body": request_body,
                "signature": signature_base64,
            }
            request_str = json.dumps(request, separators=(",", ":"))
            self.logger.debug(
                "Constructed JSON request: {}".format(request_str))

            framed = "{}\n{}\n".format(len(request_str), request_str).encode()
            self.checkin_writer.write(framed)
            await self.checkin_writer.drain()
            self.logger.info("Request sent to server.")

            self.logger.info("Waiting for server response...")
            await self.start_message_read()
        except Exception as e:
            self.logger.error(
                "Error communicating with server: {}".format(str(e)))
            self.record_latency(False)

    async def start_message_read(self):
        approved = False
        try:
            self.logger.info("Starting to read server response...")
            response_message = await self.checkin_reader.readuntil(b"\n")
            self.logger.debug(
                "Raw response message: {}".format(response_message)
            )

            response_json = json.loads(response_message.decode().strip())
            self.logger.debug(
                "Decoded JSON response: {}".format(response_json)
            )

            response_body = response_json.get("body", {})
            approved = response_body.get("approved", False)

            if approved:
                secret = response_body.get("secret", "")
                voter_name = "{} {} {}".format(
                    response_body.get("first_name", ""),
                    response_body.get("middle_name", ""),
                    response_body.get("last_name", ""),
                ).strip()
                print(
                    "Check-in successful! Welcome, {}. Secret: {}".format(
                        voter_name, secret
                    )
                )
                self.logger.info(
                    "Check-in successful. Secret: {}".format(secret)
                )

                try:
                    print("Secret successfully written to the RFID card.")
                    self.logger.info(
                        "Secret successfully written to the RFID card."
                    )
                except Exception as e:
                    self.logger.error(
                        "Error writing secret to RFID card: {}".format(str(e))
                    )
                    print("Error: Could not write secret to the RFID card.")
                finally:
                    GPIO.cleanup()

                self.show_message(
                    "Check-in Completed",
                    "Welcome, {}. You can now proceed to vote.".format(
                        voter_name),
                )

            else:
                reason = response_body.get("reason", "No reason provided.")
                print("Check-in failed: {}".format(reason))
                self.logger.info("Check-in failed. Reason: {}".format(reason))

                self.show_message("Check-in Failed", reason)

            self.record_latency(approved)

        except asyncio.IncompleteReadError as e:
            self.logger.error("Error reading response: {}".format(str(e)))
            print("Error: Received incomplete response from server.")
            self.record_latency(False)
        except json.JSONDecodeError as e:
            self.logger.error(
                "Error parsing response as JSON: {}".format(str(e))
            )
            print("Error: Received invalid JSON response from server.")
            self.record_latency(False)
        except Exception as e:
            self.logger.error(
                "Unexpected error reading server response: {}".format(str(e))
            )
            print(
                "Error: An unexpected error occurred while reading the server response."
            )
            self.record_latency(False)

    def show_message(self, title, message):
        messagebox.showinfo(title, message)


def configure_logging(level):
    logging.basicConfig(level=getattr(logging, level.upper(), logging.DEBUG))


async def process_workflow(client):
    try:
        pin = simpledialog.askstring(
            "Trusted Client – Enter PIN",
            "Please enter the voter PIN:",
            show="*",
        )
        if not (pin and pin.isdigit()):
            tk.messagebox.showerror("Invalid Input", "PIN must be a number.")
            return

        token = await client.read_rfid_with_retry()
        if not token:
            tk.messagebox.showinfo(
                "Cancelled", "No card was provided. Operation cancelled."
            )
            return

        await client.send_to_server(pin)

    except Exception as e:
        logging.error("Error in process_workflow: %s", e)
        tk.messagebox.showerror("Error", f"An error occurred: {e}")


def main():
    print("Starting Trusted Client...")
    config = configparser.ConfigParser()
    config.read("config.ini")

    configure_logging(config["Basic"]["log_level"])

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    client = TrustedPollbookClient(config, loop)

    try:
        loop.run_until_complete(client.connect())
        print("Connected to the server. Initializing workflow...")
        loop.run_until_complete(process_workflow(client))
    except Exception as e:
        print(f"An error occurred: {e}")
        logging.error("Error in main loop: %s", e)
    finally:
        loop.close()
        GPIO.cleanup()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Exiting client...")
        GPIO.cleanup()
        exit(0)
