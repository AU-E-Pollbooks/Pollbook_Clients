import tkinter.messagebox
import asyncio
import json
import logging
import time
import base64
import struct
import configparser
import ssl
from asyncio import StreamWriter, StreamReader
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from cryptography.exceptions import InvalidSignature
import RPi.GPIO as GPIO
from picamera2 import Picamera2
from io import BytesIO
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
import pytesseract
import cv2
import numpy as np
from PIL import Image, ImageTk
import os
import atexit
import csv
from datetime import datetime

import board
import busio
from adafruit_pn532.i2c import PN532_I2C
import adafruit_pn532.adafruit_pn532 as pn532_mod

GPIO.setwarnings(False)

logging.basicConfig(level=logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


def run_asyncio_event_loop():
    loop = asyncio.get_event_loop()
    loop.call_soon(loop.stop)
    loop.run_forever()


class CaptureWindow(tk.Toplevel):
    def __init__(self, parent, pollbook_client):
        super().__init__(parent)
        self.pollbook_client = pollbook_client
        self.camera = pollbook_client.camera
        self.title("Capture ID")
        self.transient(parent)
        self.grab_set()
        self.lift()
        self.focus_force()
        self.attributes("-topmost", True)

        self.geometry("450x350")

        self.preview_label = tk.Label(self)
        self.preview_label.pack(pady=10)

        self.capture_button = tk.Button(
            self,
            text="Capture Image",
            command=self.check_for_card
        )
        self.capture_button.pack(pady=10)

        self._running = True

        try:
            preview_config = self.camera.create_preview_configuration(
                main={"size": (640, 480)}, controls={"AfMode": 2}
            )
            self.camera.configure(preview_config)
            self.camera.start()
            time.sleep(1)
        except RuntimeError as e:
            print("Failed to start camera preview: %s" % str(e))
            tk.messagebox.showerror(
                "Camera Error", "Failed to start camera preview.")
            self.destroy()
            return

        self.update_image()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def update_image(self):
        if self.camera is None or not self._running:
            return

        image_stream = BytesIO()
        self.camera.capture_file(image_stream, format="jpeg")
        image_stream.seek(0)

        np_image = np.array(Image.open(image_stream))

        card_detected = self.detect_card(np_image)

        if card_detected:
            self.capture_button.config(state="normal")
        else:
            self.capture_button.config(state="disabled")

        image = Image.fromarray(cv2.rotate(np_image, cv2.ROTATE_90_CLOCKWISE))
        image = image.resize((320, 240), Image.LANCZOS)
        image_tk = ImageTk.PhotoImage(image)

        self.preview_label.configure(image=image_tk)
        self.preview_label.image = image_tk

        if self._running:
            self.after(100, self.update_image)

    @staticmethod
    def parse_driver_license_info(text):
        import re

        first_name = middle_name = last_name = ""
        voter_id = 0

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        cleaned_name_lines = []

        dl_index = -1
        for i, line in enumerate(lines):
            match = re.search(r"DL\s*NO\.?\s*(\d{6,})", line, re.IGNORECASE)
            if match:
                voter_id = match.group(1)
                dl_index = i
                break

        name_candidates = lines[dl_index + 1:] if dl_index >= 0 else lines

        for line in name_candidates:
            line = re.sub(r"^[\d=:\.\-\s]*", "", line)
            line = re.sub(r"[^A-Za-z\s\-]", "", line)
            words = line.strip().split()

            if 1 <= len(words) <= 3 and all(w.isalpha() for w in words):
                cleaned_name_lines.append(words)

        for i in range(len(cleaned_name_lines)):
            w1 = cleaned_name_lines[i]

            if len(w1) == 1:
                first_name = w1[0].capitalize()
                if i + 1 < len(cleaned_name_lines):
                    w2 = cleaned_name_lines[i + 1]
                    if len(w2) == 1:
                        last_name = w2[0].capitalize()
                break
            elif len(w1) == 2:
                first_name = w1[0].capitalize()
                last_name = w1[1].capitalize()
                break
            elif len(w1) == 3:
                first_name = w1[0].capitalize()
                middle_name = w1[1].capitalize()
                last_name = w1[2].capitalize()
                break

        return first_name, middle_name, last_name, voter_id

    def detect_card(self, frame):
        hsv = cv2.cvtColor(frame, cv2.COLOR_RGB2HSV)

        lower_color = np.array([30, 40, 40])
        upper_color = np.array([80, 255, 255])

        mask = cv2.inRange(hsv, lower_color, upper_color)

        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        for contour in contours:
            if cv2.contourArea(contour) > 1000:
                return True

        return False

    def turn_off_display(self):
        os.system("vcgencmd display_power 0")

    def turn_on_display(self):
        os.system("vcgencmd display_power 1")

    def check_for_card(self):
        if self.camera is None:
            print("No camera available.")
            return

        print("▶ capture button pressed, starting capture pipeline...")

        self._running = False

        try:
            print("Turning off display to reduce reflection...")
            self.turn_off_display()
            print("Capturing high-resolution image...")

            still_config = self.camera.create_still_configuration(
                main={"size": (1536, 2048)},
                controls={"AfMode": 2}
            )

            self.camera.switch_mode(still_config)
            self.camera.set_controls({"AfMode": 2})
            time.sleep(3)

            frame = self.camera.capture_array()

            cv2.imwrite("captured_id.jpg", frame)

            if frame.shape[0] > frame.shape[1]:
                frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            h, w = frame.shape[:2]
            scale = 1024.0 / float(w)
            frame_resized = cv2.resize(frame, (int(w * scale), int(h * scale)))

            gray = cv2.cvtColor(frame_resized, cv2.COLOR_BGR2GRAY)
            blurred = cv2.GaussianBlur(gray, (0, 0), 3)
            sharpened = cv2.addWeighted(gray, 1.5, blurred, -0.5, 0)

            processed = cv2.adaptiveThreshold(
                sharpened,
                255,
                cv2.ADAPTIVE_THRESH_MEAN_C,
                cv2.THRESH_BINARY,
                31,
                15,
            )

            cv2.imwrite("processed_voter_id_client.jpg", processed)

            final_image = Image.fromarray(processed)
            buffer = BytesIO()
            final_image.save(buffer, format="JPEG")
            encoded_image = base64.b64encode(buffer.getvalue()).decode("utf-8")

            self.pollbook_client.voter_id_data = encoded_image
            print("Image processed and stored in pollbook_client.")

            try:
                image = Image.fromarray(processed)
                custom_config = r"--oem 3 --psm 6"
                extracted_text = pytesseract.image_to_string(
                    image, config=custom_config
                )

                extracted_text = extracted_text.strip()
                print("\n=== Extracted Text ===\n")
                print(extracted_text)
                print("\n=== End of Extracted Text ===\n")

                first_name, middle_name, last_name, voter_id_raw = CaptureWindow.parse_driver_license_info(
                    extracted_text
                )

                if not voter_id_raw:
                    messagebox.showerror(
                        "Parse Error",
                        "Could not extract a numeric ID from the card."
                    )
                    self.pollbook_client.voter_id_data = None
                    return

                try:
                    voter_id_int = int(voter_id_raw)
                except ValueError:
                    messagebox.showerror(
                        "Parse Error",
                        "Extracted ID is not a valid number: %s" % voter_id_raw
                    )
                    self.pollbook_client.voter_id_data = None
                    return

                summary = (
                    "First name: %s\n"
                    "Middle name: %s\n"
                    "Last name: %s\n"
                    "Voter ID: %s"
                    % (first_name, middle_name, last_name, str(voter_id_int))
                )

                confirmed = messagebox.askokcancel(
                    "Confirm extracted data",
                    summary
                )

                if not confirmed:
                    self.pollbook_client.voter_id_data = None
                    return

                self.pollbook_client.first_name = first_name
                self.pollbook_client.middle_name = middle_name
                self.pollbook_client.last_name = last_name

                if self.pollbook_client.ui is not None:
                    self.pollbook_client.ui.show_progress(
                        "Sending check-in request using ID card..."
                    )

                loop = asyncio.get_event_loop()
                loop.create_task(
                    self.pollbook_client.check_in_voter(
                        first_name, middle_name, last_name, voter_id_int
                    )
                )

            except Exception as e:
                print("OCR extraction failed: %s" % str(e))
                messagebox.showerror(
                    "OCR Error", "An error occurred during OCR:\n%s" % str(e)
                )
                self.pollbook_client.voter_id_data = None

        except Exception as e:
            print("Error capturing image: %s" % e)

        finally:
            try:
                self.camera.stop()
            except Exception as e:
                print("Warning: Failed to stop camera: %s" % e)
            print("Turning display back on...")
            self.turn_on_display()
            self.destroy()

    def on_close(self):
        self._running = False
        print("Turning display back on...")
        self.turn_on_display()
        try:
            if self.camera:
                self.camera.stop()
                print("Camera preview stopped.")
        except Exception as e:
            print(
                "Warning: Failed to stop camera preview on close: %s" % str(e)
            )
        self.destroy()


class PollbookUI(tk.Tk):
    def __init__(self, pollbook_client):
        super().__init__()
        self.pollbook_client = pollbook_client
        self.progress_window = None
        self.title("Poll Book Client")
        self.loop = asyncio.get_event_loop()

        self.start_frame = tk.Frame(self, padx=20, pady=20)
        self.form_frame = tk.Frame(self, padx=20, pady=20)

        self.start_frame.grid(row=0, column=0, sticky="nsew")
        self.form_frame.grid(row=0, column=0, sticky="nsew")

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self._build_start_screen()
        self._build_form_screen()

        self.show_start_screen()

        self._integrate_asyncio_loop()

    def show_start_screen(self):
        self.form_frame.grid_remove()
        self.start_frame.grid()

    def show_form_screen(self):
        self.start_frame.grid_remove()
        self.form_frame.grid()

    def _build_start_screen(self):
        title_lbl = tk.Label(
            self.start_frame,
            text="Welcome to the Pollbook Check-in",
            font=("Helvetica", 16, "bold"),
        )
        title_lbl.pack(pady=(10, 20))

        subtitle_lbl = tk.Label(
            self.start_frame,
            text="Please choose how you would like to check in:",
            font=("Helvetica", 11),
        )
        subtitle_lbl.pack(pady=(0, 20))

        btn_id = tk.Button(
            self.start_frame,
            text="📇 Check-in with ID card",
            font=("Helvetica", 13, "bold"),
            bg="#2d7fff",
            fg="white",
            padx=20,
            pady=10,
            command=self.choose_id_mode,
        )
        btn_id.pack(pady=10, fill="x")

        btn_manual = tk.Button(
            self.start_frame,
            text="⌨️ Check-in by entering information",
            font=("Helvetica", 13, "bold"),
            bg="#4caf50",
            fg="white",
            padx=20,
            pady=10,
            command=self.choose_manual_mode,
        )
        btn_manual.pack(pady=10, fill="x")

        footer = tk.Label(
            self.start_frame,
            text="A poll worker can assist you if needed.",
            font=("Helvetica", 9),
            fg="#555555",
        )
        footer.pack(pady=(20, 0))

    def _build_form_screen(self):
        back_btn = tk.Button(
            self.form_frame,
            text="← Back",
            command=self.show_start_screen
        )
        back_btn.grid(row=0, column=0, sticky="w", pady=(0, 10))

        row = 1

        self.label_first_name = tk.Label(self.form_frame, text="First Name:")
        self.label_first_name.grid(
            row=row, column=0, sticky="e", padx=5, pady=2
        )
        self.entry_first_name = tk.Entry(self.form_frame, width=25)
        self.entry_first_name.grid(row=row, column=1, padx=5, pady=2)
        row += 1

        self.label_middle_name = tk.Label(self.form_frame, text="Middle Name:")
        self.label_middle_name.grid(
            row=row, column=0, sticky="e", padx=5, pady=2
        )
        self.entry_middle_name = tk.Entry(self.form_frame, width=25)
        self.entry_middle_name.grid(row=row, column=1, padx=5, pady=2)
        row += 1

        self.label_last_name = tk.Label(self.form_frame, text="Last Name:")
        self.label_last_name.grid(
            row=row, column=0, sticky="e", padx=5, pady=2
        )
        self.entry_last_name = tk.Entry(self.form_frame, width=25)
        self.entry_last_name.grid(row=row, column=1, padx=5, pady=2)
        row += 1

        self.label_voter_id = tk.Label(self.form_frame, text="Voter ID:")
        self.label_voter_id.grid(
            row=row, column=0, sticky="e", padx=5, pady=2
        )
        self.entry_voter_id = tk.Entry(self.form_frame, width=25)
        self.entry_voter_id.grid(row=row, column=1, padx=5, pady=2)
        row += 1

        self.capture_button = tk.Button(
            self.form_frame,
            text="📷 Capture ID card",
            command=self.open_capture_window,
        )
        self.capture_button.grid(row=row, columnspan=2, pady=5)
        row += 1

        self.checkin_button = tk.Button(
            self.form_frame,
            text="✅ Check In",
            command=self.start_check_in_process,
        )
        self.checkin_button.grid(row=row, columnspan=2, pady=10)
        self.checkin_button.config(state="disabled")

    def choose_id_mode(self):
        if self.pollbook_client:
            self.pollbook_client.checkin_mode = "id"
        CaptureWindow(self, self.pollbook_client)

    def choose_manual_mode(self):
        if self.pollbook_client:
            self.pollbook_client.checkin_mode = "manual"
        self.capture_button.grid_remove()
        self.show_form_screen()

    def show_progress(self, message):
        self.progress_window = tk.Toplevel(self)
        self.progress_window.title("Processing")
        self.progress_window.geometry("400x150")
        self.progress_window.configure(bg="#f0f4f8")
        self.progress_window.grab_set()
        self.progress_window.overrideredirect(True)
        self.progress_window.attributes("-topmost", True)
        self.progress_window.transient(self)
        self.progress_window.lift()
        self.progress_window.focus_force()

        self.update_idletasks()
        x = (self.winfo_screenwidth() // 2) - (400 // 2)
        y = (self.winfo_screenheight() // 2) - (150 // 2)
        self.progress_window.geometry("+%d+%d" % (x, y))

        outer_frame = tk.Frame(self.progress_window,
                               bg="#ffffff", padx=10, pady=10)
        outer_frame.pack(fill="both", expand=True)

        label = tk.Label(
            outer_frame,
            text=message,
            font=("Helvetica", 12, "bold"),
            fg="#333333",
            bg="#ffffff",
        )
        label.pack(pady=(10, 5))

        style = ttk.Style()
        style.theme_use("clam")
        style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor="#e1e4e8",
            background="#4a90e2",
            thickness=8,
            troughrelief="flat",
        )

        progress_bar = ttk.Progressbar(
            outer_frame,
            style="Custom.Horizontal.TProgressbar",
            mode="indeterminate",
        )
        progress_bar.pack(fill="x", padx=20, pady=(5, 20))
        progress_bar.start(10)

    def close_progress(self):
        if self.progress_window:
            self.progress_window.destroy()
            self.progress_window = None

    def set_client(self, client):
        self.pollbook_client = client
        self.enable_checkin_button()

    def _integrate_asyncio_loop(self):
        self.loop.call_soon(self.loop.stop)
        self.loop.run_forever()
        self.after(100, self._integrate_asyncio_loop)

    def enable_checkin_button(self):
        self.checkin_button.config(state="normal")

    def open_capture_window(self):
        CaptureWindow(self, self.pollbook_client)

    def start_check_in_process(self):
        first_name = self.entry_first_name.get()
        middle_name = self.entry_middle_name.get()
        last_name = self.entry_last_name.get()
        voter_id_str = self.entry_voter_id.get()

        try:
            voter_id = int(voter_id_str)
        except ValueError:
            messagebox.showerror("Error", "Voter ID must be a number.")
            return

        if self.pollbook_client.ui is not None:
            self.pollbook_client.ui.show_progress(
                "Sending manual check-in request..."
            )

        self.loop.create_task(
            self.pollbook_client.check_in_voter_manual(
                first_name, middle_name, last_name, voter_id
            )
        )

    def reset_form_fields(self):
        self.entry_first_name.delete(0, tk.END)
        self.entry_middle_name.delete(0, tk.END)
        self.entry_last_name.delete(0, tk.END)
        self.entry_voter_id.delete(0, tk.END)
        self.capture_button.grid()  # restore capture button in case it was hidden by manual mode
        self.checkin_button.config(state="disabled")

    def go_to_main_screen(self):
        self.close_progress()
        self.reset_form_fields()
        self.show_start_screen()


class PollbookClient:
    def __init__(self, config, ui):
        self.logger = logging.getLogger("pollbook_client")
        self.logger.setLevel(logging.INFO)

        self.checkin_server_writer = None
        self.id_server_writer = None
        self.id_text_server_writer = None
        self.checkin_connected = False
        self.id_connected = False
        self.id_text_connected = False
        self.checkin_server_reader = None
        self.id_server_reader = None
        self.id_text_server_reader = None

        self.ui = ui
        self.config = config
        self.loop = asyncio.get_event_loop()

        self.client_id = int(config["Basic"]["client_id"])
        self.checkin_service_host = config["Basic"]["checkin_service_host"]
        self.checkin_service_port = int(
            config["Basic"]["checkin_service_port"])
        self.id_service_host = config["Basic"]["id_service_host"]
        self.id_service_port = int(config["Basic"]["id_service_port"])

        self.id_text_service_host = config["Basic"]["id_text_service_host"]
        self.id_text_service_port = int(
            config["Basic"]["id_text_service_port"])

        with open(config["Security"]["private_key"], "rb") as key_file:
            self.private_key_signer = load_pem_private_key(
                key_file.read(), password=None
            )

        with open(config["Security"]["id_service_public_key"], "rb") as key_file:
            self.id_service_verifier = load_pem_public_key(key_file.read())

        with open(config["Security"]["checkin_service_public_key"], "rb") as key_file:
            self.checkin_service_verifier = load_pem_public_key(
                key_file.read())

        self.client_cert = config["Security"]["local_cert"]
        self.client_key = config["Security"]["private_key"]
        self.ca_cert = config["Security"]["ca_cert"]

        i2c = busio.I2C(board.SCL, board.SDA)
        self.reader = PN532_I2C(i2c, debug=False, address=0x24)
        self.reader.SAM_configuration()

        try:
            self.camera = Picamera2()
            self.camera.configure(
                self.camera.create_preview_configuration(
                    main={"size": (640, 480)})
            )
        except Exception as e:
            self.camera = None
            print("Error initializing camera: %s" % e)

        self.captured_image = None
        self.first_name = None
        self.middle_name = None
        self.last_name = None
        self.voter_id_data = None

        self.checkin_mode = "id"
        self.checkin_start_time = None

    def set_ui(self, ui):
        self.ui = ui
        self.ui.enable_checkin_button()

    async def connect(self):
        await self.connect_checkin_server(
            self.checkin_service_host, self.checkin_service_port
        )
        await self.connect_id_server(self.id_service_host, self.id_service_port)
        await self.connect_text_id_server(
            self.id_text_service_host, self.id_text_service_port
        )

        if self.checkin_connected and self.id_connected and self.id_text_connected:
            self.logger.info(
                "Client connected to check-in, image ID, and text ID services"
            )
            self.ui.enable_checkin_button()

    async def connect_checkin_server(self, hostname, port):
        ssl_context = self.create_ssl_context()
        try:
            self.checkin_server_reader, self.checkin_server_writer = (
                await asyncio.open_connection(hostname, port, ssl=ssl_context)
            )
            self.checkin_connected = True
            self.logger.info(
                "Connected to check-in server at %s:%d", hostname, port
            )
        except Exception as e:
            self.logger.error(
                "Failed to connect to check-in server at %s:%d - %s",
                hostname,
                port,
                e,
            )
            self.checkin_connected = False

    async def connect_id_server(self, hostname, port):
        ssl_context = self.create_ssl_context()
        try:
            self.id_server_reader, self.id_server_writer = (
                await asyncio.open_connection(hostname, port, ssl=ssl_context)
            )
            self.id_connected = True
            self.logger.info("Connected to ID server at %s:%d", hostname, port)
        except Exception as e:
            self.logger.error(
                "Failed to connect to ID server at %s:%d - %s",
                hostname,
                port,
                e,
            )
            self.id_connected = False

    async def connect_text_id_server(self, hostname, port):
        ssl_context = self.create_ssl_context()
        try:
            self.id_text_server_reader, self.id_text_server_writer = (
                await asyncio.open_connection(hostname, port, ssl=ssl_context)
            )
            self.id_text_connected = True
            self.logger.info(
                "Connected to text ID server at %s:%d", hostname, port
            )
        except Exception as e:
            self.logger.error(
                "Failed to connect to text ID server at %s:%d - %s",
                hostname,
                port,
                e,
            )
            self.id_text_connected = False

    def create_ssl_context(self):
        ssl_context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=self.ca_cert
        )
        ssl_context.load_cert_chain(
            certfile=self.client_cert, keyfile=self.client_key
        )
        return ssl_context

    async def check_in_voter(self, first_name, middle_name, last_name, voter_id):
        if not self.voter_id_data:
            print("No ID image captured yet!")
            return

        self.first_name = (first_name or "").strip()
        self.middle_name = (middle_name or "").strip()
        self.last_name = (last_name or "").strip()
        self.checkin_start_time = time.time()

        voter_id_data_bytes = base64.b64decode(self.voter_id_data)
        total_size = len(voter_id_data_bytes) + 4
        id_data_with_number = bytearray(total_size)

        struct.pack_into("I", id_data_with_number, 0, voter_id)
        id_data_with_number[4:] = voter_id_data_bytes

        await self.start_id_request_write(id_data_with_number)

    async def check_in_voter_manual(self, first_name, middle_name, last_name, voter_id):
        first_name = (first_name or "").strip()
        middle_name = (middle_name or "").strip()
        last_name = (last_name or "").strip()
        voter_id = int(voter_id)

        self.first_name = first_name
        self.middle_name = middle_name
        self.last_name = last_name
        self.checkin_start_time = time.time()

        await self.start_text_id_request_write(
            voter_id,
            first_name,
            middle_name,
            last_name,
        )

    async def start_id_request_write(self, voter_id_data):
        if not self.id_server_writer:
            self.logger.error("ID server writer is not available.")
            return

        timestamp = int(time.time() * 1000)
        client_id_num = self.client_id

        validation_request_body = {
            "client_id_num": client_id_num,
            "timestamp": timestamp,
            "first_name": self.first_name or "",
            "middle_name": self.middle_name or "",
            "last_name": self.last_name or "",
            "voter_id_data": self.voter_id_data,
        }

        body_string = json.dumps(
            validation_request_body, separators=(",", ":")
        )
        self.logger.debug("Body string being signed: %s", body_string)

        signature = self.private_key_signer.sign(
            body_string.encode(), padding.PKCS1v15(), hashes.SHA256()
        )
        signature_base64 = base64.b64encode(signature).decode("utf-8")

        validation_request = {
            "body": validation_request_body,
            "client_signature": signature_base64,
        }

        response_message_string = json.dumps(
            validation_request, separators=(",", ":")
        )
        message_size = len(response_message_string)
        buf = "%d\n%s\n" % (message_size, response_message_string)

        self.logger.info(
            "Validation request being sent (image-based): %s",
            json.dumps(validation_request, indent=4),
        )

        self.id_server_writer.write(buf.encode())
        await self.id_server_writer.drain()
        await self.start_message_read(True)

    async def start_text_id_request_write(self, voter_id, first_name, middle_name, last_name):
        timestamp = int(time.time() * 1000)
        client_id_num = self.client_id

        voter_id_bytes = struct.pack("<I", voter_id)
        voter_id_b64 = base64.b64encode(voter_id_bytes).decode("utf-8")

        validation_body = {
            "client_id_num": client_id_num,
            "timestamp": timestamp,
            "last_name": last_name,
            "middle_name": middle_name,
            "first_name": first_name,
            "voter_id_data": voter_id_b64,
        }

        body_string = json.dumps(
            validation_body,
            separators=(",", ":"),
            sort_keys=True,
        )
        self.logger.debug(
            "Manual text ID body being signed: %s", body_string)

        signature = self.private_key_signer.sign(
            body_string.encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        signature_b64 = base64.b64encode(signature).decode("utf-8")

        validation_request = {
            "body": validation_body,
            "client_signature": signature_b64,
        }

        message_string = json.dumps(
            validation_request,
            separators=(",", ":"),
            sort_keys=True,
        )
        message_size = len(message_string)
        buf = "%d\n%s\n" % (message_size, message_string)

        self.logger.info(
            "Validation request being sent (manual text-based): %s",
            json.dumps(validation_request, indent=4),
        )

        self.id_text_server_writer.write(buf.encode("utf-8"))
        await self.id_text_server_writer.drain()
        await self.start_message_read_text_id()

    async def read_framed_message(self, reader, timeout=5):
        try:
            size_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            if not size_line:
                self.logger.error(
                    "Connection closed while reading size prefix.")
                return ""

            stripped = size_line.strip()

            # Case 1: server sends only JSON on the same line (e.g. the text ID server)
            if stripped.startswith(b"{") or stripped.startswith(b"["):
                return stripped.decode()

            # Case 2: a length prefix is present
            try:
                size = int(stripped.decode())
            except ValueError:
                self.logger.error(
                    "Invalid size prefix from server: %r", size_line)
                return ""

            # If the length looks too small (e.g. 2), assume the length prefix is
            # malformed and the next line is the full JSON payload instead
            if size < 10:
                self.logger.debug(
                    "Size prefix %d looks too small, falling back to line-based JSON read",
                    size,
                )
                json_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not json_line:
                    self.logger.error(
                        "No JSON line after short size prefix.")
                    return ""
                return json_line.decode().strip()

            # Normal case: LEN\nJSON\n
            payload = await asyncio.wait_for(reader.readexactly(size), timeout=timeout)

            # Optional: consume the trailing newline after the payload
            try:
                await asyncio.wait_for(reader.readline(), timeout=0.1)
            except asyncio.TimeoutError:
                pass

            return payload.decode()

        except asyncio.TimeoutError:
            self.logger.error(
                "Timeout while reading framed message from server.")
            return ""

    async def start_message_read(self, on_id_server):
        reader = self.id_server_reader if on_id_server else self.checkin_server_reader

        if on_id_server:
            self.logger.info("Reading response from image ID server.")
        else:
            self.logger.info("Reading response from check-in server.")

        try:
            message_received = await self.read_framed_message(reader)
            message = str(message_received).rstrip()

            if not message:
                self.logger.error(
                    "Unexpected error: Received empty response from server."
                )
                return

            self.logger.debug(
                "Received raw message: %s", message[message.find("{"):]
            )
            response_json = json.loads(message[message.find("{"):])

            self.logger.debug(
                "Parsed JSON response: %s", json.dumps(response_json, indent=2)
            )

            if on_id_server:
                await self.handle_id_response(response_json)
            else:
                await self.handle_checkin_response(response_json)

            self.logger.info("Successfully processed the server response.")

        except json.JSONDecodeError as e:
            self.logger.error("JSON decode error: %s", str(e))
        except Exception as e:
            self.logger.error("Unexpected error: %s", str(e))

    async def start_message_read_text_id(self):
        self.logger.info("Reading response from text ID server.")
        response_json = None
        try:
            message_received = await self.read_framed_message(
                self.id_text_server_reader
            )

            if not message_received:
                self.logger.error(
                    "Received empty response from text ID server.")
                return

            message = str(message_received).rstrip()
            json_str = message[message.find("{"):]
            response_json = json.loads(json_str)

            self.logger.info(
                "Text ID server response: %s",
                json.dumps(response_json, indent=4),
            )

            await self.handle_id_response(response_json)

            self.logger.info(
                "Processed text ID server response successfully.")

        except json.JSONDecodeError as e:
            self.logger.error(
                "JSON decode error from text ID server: %s", e)
        except KeyError as e:
            self.logger.error(
                "Key '%s' not found in ID response: %s",
                e.args[0],
                response_json,
            )
        except Exception as e:
            self.logger.error(
                "Unexpected error while reading text ID response: %s", e
            )

    async def handle_id_response(self, response):
        try:
            self.logger.debug(
                "Full ID Server Response: %s",
                json.dumps(response, indent=4),
            )

            required_keys = ["presented_id",
                             "voter_unique_id", "id_service_signature"]
            for key in required_keys:
                if key not in response:
                    self.logger.error(
                        "Key '%s' not found in ID response: %s",
                        key,
                        response,
                    )
                    return

            presented_id = response["presented_id"]
            voter_unique_id = response["voter_unique_id"]
            id_service_signature_b64 = response["id_service_signature"]

            to_be_verified = {
                "presented_id": presented_id,
                "voter_unique_id": voter_unique_id,
            }
            signed_data = json.dumps(
                to_be_verified,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")

            signature = base64.b64decode(id_service_signature_b64)

            try:
                self.id_service_verifier.verify(
                    signature,
                    signed_data,
                    padding.PKCS1v15(),
                    hashes.SHA256(),
                )
                self.logger.info("ID response verified successfully")

                await self.start_checkin_request_write(
                    {
                        "presented_id": presented_id,
                        "voter_unique_id": voter_unique_id,
                    },
                    id_service_signature_b64,
                )

            except InvalidSignature:
                self.logger.error(
                    "Invalid signature received from ID server!")

        except Exception as e:
            self.logger.error("Error handling ID response: %s", str(e))

    async def start_checkin_request_write(
        self, verified_id_response, id_service_signature
    ):
        try:
            presented_id = verified_id_response.get("presented_id", {})
            voter_unique_id = int(
                verified_id_response.get("voter_unique_id", 0)
            )

            request_body = {
                "client_id_num": self.client_id,
                "timestamp": int(time.time() * 1000),
                "last_name": self.last_name or "",
                "first_name": self.first_name or "",
                "middle_name": self.middle_name or "",
                "voter_unique_id": voter_unique_id,
                "verified_id_message": {
                    "id_service_signature": id_service_signature,
                    "presented_id": presented_id,
                    "voter_unique_id": voter_unique_id,
                },
            }

            request_body_str = json.dumps(
                request_body, separators=(",", ":"), sort_keys=True
            )

            signature = self.private_key_signer.sign(
                request_body_str.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            client_signature_b64 = base64.b64encode(signature).decode("utf-8")

            request = {
                "body": request_body,
                "client_signature": client_signature_b64,
                "client_type": "FirstClient",
            }

            request_message_string = json.dumps(
                request, separators=(",", ":")
            )
            message_size = len(request_message_string.encode("utf-8"))
            buf = "%d\n%s\n" % (message_size, request_message_string)

            self.logger.info(
                "Final JSON being sent to check-in server:\n%s",
                json.dumps(request, indent=4),
            )

            self.checkin_server_writer.write(buf.encode())
            await self.checkin_server_writer.drain()

            print("Check-in request sent to server.")
            await self.start_message_read(False)
            if self.ui is not None:
                self.ui.close_progress()

        except Exception as e:
            print("Error in start_checkin_request_write: %s" % e)

    def write_ticket_with_retry(self, ticket_hex):
        while True:
            ok, err = self._write_ticket_once(ticket_hex)
            if ok:
                tkinter.messagebox.showinfo(
                    "Success", "Ticket written to RFID tag!"
                )
                return True

            msg = "Could not write the ticket to the RFID card."
            if err:
                msg += "\n\nReason: %s" % err

            retry = tkinter.messagebox.askretrycancel(
                "RFID Write Problem",
                msg
                + "\n\n• Make sure it is a Mifare Classic card"
                + "\n• Hold it flat on the reader for 1–2 seconds"
                + "\n\nTry again?",
            )
            if not retry:
                return False

    def _write_ticket_once(self, ticket_data):
        try:
            if not (
                isinstance(ticket_data, str)
                and len(ticket_data) == 32
                and all(
                    c in "0123456789abcdefABCDEF" for c in ticket_data
                )
            ):
                return False, "Ticket must be a 32-hex-character string."

            ticket_bytes = bytes.fromhex(ticket_data)

            i2c = busio.I2C(board.SCL, board.SDA)
            pn532 = PN532_I2C(i2c, debug=False)
            pn532.SAM_configuration()

            print("Waiting for a tag...")
            uid = None
            for _ in range(30):
                uid = pn532.read_passive_target(timeout=0.1)
                if uid is not None:
                    break

            if uid is None:
                return False, "No RFID tag detected."

            print("Tag detected: UID=%s" % uid.hex())

            block_number = 4
            key = b"\xFF\xFF\xFF\xFF\xFF\xFF"

            if not pn532.mifare_classic_authenticate_block(
                uid, block_number, pn532_mod.MIFARE_CMD_AUTH_A, key
            ):
                return False, "Authentication failed on block 4."

            if not pn532.mifare_classic_write_block(
                block_number, ticket_bytes
            ):
                return False, "Write failed on block 4."

            read_back = pn532.mifare_classic_read_block(block_number)
            if not read_back or len(read_back) != 16:
                return False, "Read-back failed or wrong length."

            rb_hex = read_back.hex()
            print("[UNTRUSTED] Ticket READ-BACK (hex): %s" % rb_hex)
            if rb_hex.lower() != ticket_data.lower():
                return False, "Written bytes do not match the ticket (verify mismatch)."

            print("Ticket written successfully.")
            return True, None

        except Exception as e:
            print("Error writing ticket: %s" % e)
            return False, str(e)

    def show_auto_closing_message(self, message):
        temp_root = tk.Toplevel()
        temp_root.title("RFID Prompt")
        tk.Label(temp_root, text=message).pack(pady=20, padx=20)
        temp_root.geometry("300x100")
        temp_root.update()
        temp_root.after(1000, temp_root.destroy)
        return temp_root

    def record_latency(self, approved, voter_unique_id):
        try:
            if self.checkin_start_time is None:
                return
            end_time = time.time()
            latency_ms = int((end_time - self.checkin_start_time) * 1000)
            self.checkin_start_time = None

            file_path = "checkin_latency_log.csv"
            file_exists = os.path.isfile(file_path)

            with open(file_path, "a", newline="") as f:
                writer = csv.writer(f)
                if not file_exists:
                    writer.writerow(
                        ["timestamp_iso", "mode", "voter_unique_id",
                            "approved", "latency_ms"]
                    )
                writer.writerow(
                    [
                        datetime.utcnow().isoformat(),
                        self.checkin_mode,
                        voter_unique_id if voter_unique_id is not None else "",
                        1 if approved else 0,
                        latency_ms,
                    ]
                )

        except Exception as e:
            self.logger.error("Error recording latency: %s", e)

    async def handle_checkin_response(self, response):
        if self.ui is not None:
            self.ui.close_progress()
        try:
            response_body_str = json.dumps(
                response["body"], separators=(",", ":")
            )
            self.checkin_service_verifier.verify(
                base64.b64decode(response["checkin_service_signature"]),
                response_body_str.encode(),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )

            approved = response["body"].get("approved", False)
            voter_unique_id = response["body"].get("voter_unique_id", None)

            if approved:
                print("Check-in successful")
                ticket_data = response["body"].get("ticket", "")
                if ticket_data:
                    success = self.write_ticket_with_retry(ticket_data)
                    if success:
                        tkinter.messagebox.showinfo(
                            "Check-in Completed", "Your check-in is complete."
                        )
                        if self.ui is not None:
                            self.ui.go_to_main_screen()
                    else:
                        tkinter.messagebox.showwarning(
                            "Check-in Incomplete",
                            "Ticket could not be written to RFID after retries.",
                        )
                else:
                    tkinter.messagebox.showerror(
                        "Missing Data", "No ticket data found."
                    )

            else:
                error_message = response["body"].get(
                    "error", "Check-in denied."
                )
                print("Check-in failed: %s" % error_message)
                tkinter.messagebox.showerror(
                    "Check-in Failed",
                    "Check-in was not approved.\n\nReason: %s"
                    % error_message,
                )
                self.ui.quit()
                await asyncio.sleep(0.5)
                self.loop.stop()

            self.record_latency(approved, voter_unique_id)

        except InvalidSignature:
            self.logger.error("Invalid signature on check-in response!")
        except Exception as e:
            self.logger.error(
                "Error handling checkin response: %s", str(e)
            )


def main():
    config = configparser.ConfigParser()
    config.read("config.ini")

    ui = PollbookUI(None)

    client = PollbookClient(config, ui)
    ui.set_client(client)

    loop = asyncio.get_event_loop()
    loop.create_task(client.connect())

    ui.mainloop()


main()
