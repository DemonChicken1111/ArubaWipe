import serial
import serial.tools.list_ports
import time
import re
import csv
import threading
import os
import shutil
from datetime import datetime
from enum import Enum
from collections import deque
from colorama import Fore, Back, Style, init


# --- CONFIGURATION ---
TARGET_PORTS = ['COM5', 'COM6', 'COM7', 'COM8','COM9', '/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2']
BAUD_RATE = 9600
OUTPUT_FILE = 'aruba_wipe_log.csv'
GLOBAL_TIMEOUT = 300  # 5 minutes max per AP
STATE_TIMEOUT = 60    # 60 seconds max per state

# Initialize colorama (autoreset=True makes formatting automatically stop at end of print)
init(autoreset=True)

#Init shutil
columns = shutil.get_terminal_size().columns

# Thread-safe locks
print_lock = threading.Lock()
file_lock = threading.Lock()


class APState(Enum):
    INIT = "Initializing"
    WAITING_BOOT = "Waiting for Boot"
    INTERRUPTING = "Interrupting Boot"
    AT_PROMPT = "At apboot Prompt"
    QUERYING_INFO = "Querying Device Info"
    WIPING = "Factory Reset in Progress"
    SAVING = "Saving Configuration"
    COMPLETE = "Wipe Complete"
    FAILED = "Failed"
    TIMEOUT = "Timeout"


def log_print(port, message, message_color="\033[39m"):
    """Thread-safe console output with port prefix"""
    message_color == message_color

    with print_lock:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] " + Fore.CYAN + f"[{port}]" + message_color + f" {message}")


class APWiper:
    def __init__(self, com_port):
        self.com_port = com_port
        self.state = APState.INIT
        self.serial_num = None
        self.mac_addr = None
        self.ser = None
        self.buffer = ""  # Simple string buffer is sufficient for state-based logic
        self.start_time = None
        self.state_start_time = None
        self.interrupt_attempts = 0
       
        # Regex Patterns (Compiled once)
        # Matches: "SN: 12345", "serial: 12345", "serial_num=12345", "system S/N: 12345" (8-12 chars for complete serial)
        self.re_sn = re.compile(r'(?:SN|serial|serial_num|system S/N)\s*(?::|=)\s*([A-Za-z0-9\-]{8,12})', re.IGNORECASE)
        # Matches: "MAC: 00:...", "ethaddr=00:..."
        self.re_mac = re.compile(r'(?:MAC|Ethernet MAC|ethaddr)\s*(?::|=)\s*([0-9a-fA-F:]{17})', re.IGNORECASE)


    def change_state(self, new_state):
        if self.state != new_state:
            log_print(self.com_port, f"{new_state.value}") #{self.state.value} -> {new_state.value}")
            self.state = new_state
            self.state_start_time = time.time()
            self.buffer = ""  # Clear buffer on state change to avoid parsing old data


    def check_timeouts(self):
        current_time = time.time()
        if current_time - self.start_time > GLOBAL_TIMEOUT:
            log_print(self.com_port, f"GLOBAL TIMEOUT ({GLOBAL_TIMEOUT}s)", Fore.RED)
            self.state = APState.TIMEOUT
            return False
       
        # Extended timeout for querying info as it involves printing lots of text
        timeout = 20 if self.state == APState.QUERYING_INFO else STATE_TIMEOUT
        if current_time - self.state_start_time > timeout:
            log_print(self.com_port, f"STATE TIMEOUT in {self.state.value}", Fore.RED)
            self.state = APState.FAILED
            return False
        return True


    def find_info(self, text):
        """Extracts SN and MAC from text if not already found."""
        if not self.serial_num:
            match = self.re_sn.search(text)
            if match:
                self.serial_num = match.group(1)
                log_print(self.com_port, f"Found SN: {self.serial_num}", Fore.GREEN)


        if not self.mac_addr:
            match = self.re_mac.search(text)
            if match:
                self.mac_addr = match.group(1)
                log_print(self.com_port, f"Found MAC: {self.mac_addr}", Fore.GREEN)


    def process(self):
        try:
            self.ser = serial.Serial(self.com_port, BAUD_RATE, timeout=0.1)
            log_print(self.com_port, "Connected. Waiting for power on...", Fore.YELLOW)
           
            self.start_time = time.time()
            self.state_start_time = time.time()
            self.change_state(APState.WAITING_BOOT)


            while self.state not in [APState.COMPLETE, APState.FAILED, APState.TIMEOUT]:
                if not self.check_timeouts():
                    break


                # Non-blocking read
                if self.ser.in_waiting:
                    try:
                        chunk = self.ser.read(self.ser.in_waiting).decode('utf-8', errors='strict')
                        self.buffer += chunk
                       
                        # Passive info scraping (catch boot logs)
                        self.find_info(chunk)
                    except OSError:
                        log_print(self.com_port, "Cable disconnected!", Fore.RED)
                        self.state = APState.FAILED
                        break
                else:
                    # Prevent CPU spiking
                    time.sleep(0.05)
                    # Continue logic below even if no new data, to check timeouts
               
                # --- STATE MACHINE ---


                if self.state == APState.WAITING_BOOT:
                    # Look for triggers to interrupt
                    if any(x in self.buffer.lower() for x in ["stop autoboot", "hit <enter>", "hit any key"]):
                        self.change_state(APState.INTERRUPTING)


                elif self.state == APState.INTERRUPTING:
                    # Spam Enter
                    self.ser.write(b'\n')
                    time.sleep(0.05) # Rate limit the spam slightly
                    if "apboot>" in self.buffer:
                        log_print(self.com_port, "Boot interrupted.", Fore.GREEN)
                        self.change_state(APState.AT_PROMPT)


                elif self.state == APState.AT_PROMPT:
                    # Send info query
                    # Using 'printenv' is often cleaner than 'mfginfo' for parsing,
                    # but we send both separated by ; to be safe
                    log_print(self.com_port, "Querying info (printenv)...")
                    self.ser.write(b'printenv; mfginfo\n')
                    self.change_state(APState.QUERYING_INFO)


                elif self.state == APState.QUERYING_INFO:
                    # Wait for the command to finish and prompt to return
                    if "apboot>" in self.buffer:
                        # Command finished, parse the full buffer one last time
                        self.find_info(self.buffer)
                       
                        if self.serial_num and self.mac_addr:
                            log_print(self.com_port, "Info secured. Starting Wipe...", Fore.GREEN)
                            self.ser.write(b'fact\n') # Factory Reset
                            self.change_state(APState.WIPING)
                        else:
                            # Retry logic could go here, but for now fail safe
                            log_print(self.com_port, "WARNING: Missing SN or MAC. Wiping anyway.", Fore.YELLOW)
                            self.ser.write(b'fact\n')
                            self.change_state(APState.WIPING)


                elif self.state == APState.WIPING:
                    # Wait for reset to complete and prompt to return
                    if "apboot>" in self.buffer:
                        log_print(self.com_port, "Factory reset done. Saving...", Fore.GREEN)
                        self.ser.write(b'save\n')
                        self.change_state(APState.SAVING)


                elif self.state == APState.SAVING:
                    if "apboot>" in self.buffer:
                        log_print(self.com_port, "Configuration saved.", Fore.GREEN)
                        self.change_state(APState.COMPLETE)


        except serial.SerialException as e:
            log_print(self.com_port, f"Serial Error: {e}", Fore.RED)
            self.state = APState.FAILED
        except Exception as e:
            log_print(self.com_port, f"Error: {e}", Fore.RED)
            self.state = APState.FAILED
        finally:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.log_results()


    def log_results(self):
        status_str = "SUCCESS" if self.state == APState.COMPLETE else self.state.value
        duration = time.time() - self.start_time if self.start_time else 0
       
        with file_lock:
            with open(OUTPUT_FILE, 'a', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    self.serial_num or "Unknown",
                    self.mac_addr or "Unknown",
                    datetime.now().strftime("%m-%d-%Y")
                ])


def wipe_ap_thread(com_port):
    wiper = APWiper(com_port)
    wiper.process()

def clear_screen():

    if os.name == 'nt':
        os.system('cls')
    else:
        os.system('clear')

def main():

    name = "--- ARUBA AP BATCH WIPER v4 "
    atribution = "Created by: Kyle Cahill and Dylan Schulze"
    aruba_logo = r"""
     _     _____  _   _ ____       _
    / \   |  _  \| | | |  __ \    / \
   / _ \  | |_) || | | | |__) |  / _ \
  / /_\ \ |  _  /| | | |  __ <  / /_\ \
 /  ___  \| | \ \| |_| | |__) |/  ___  \
/__/   \__\_|  \_\\___/|_____//__/   \__\
	      N E T W O R K I N G

"""

    clear_screen()    
    print(Fore.BLUE + Style.BRIGHT + name.ljust(columns, "-"))
    print(Fore.CYAN + Style.DIM + atribution)
    print(Fore.RED + Style.BRIGHT + aruba_logo.center(columns))
   
    # Init CSV
    try:
        with open(OUTPUT_FILE, 'x', newline='') as f:
            csv.writer(f).writerow(["Serial", "MAC", "Timestamp"])
    except FileExistsError:
        pass


    # Check Ports
    available_ports = [p.device for p in serial.tools.list_ports.comports()]
    active_threads = []
    num_AP = int(input("Enter the number of AP's to wipe: "))

    for i in range(0,num_AP):

        for port in TARGET_PORTS:
            if port in available_ports:
                t = threading.Thread(target=wipe_ap_thread, args=(port,), daemon=True)
                t.start()
                active_threads.append(t)
    #       else:
    #            print(f"Skipping {port} (Not detected)")

        if not active_threads:
            print("No valid ports found.")
            return

        print(f"\nRunning on {len(active_threads)} ports. Power on APs now...")
   
        try:
            for t in active_threads:
                t.join()
        except KeyboardInterrupt:
            print("\nStopping...")

        #reset active threads for next loop
        active_threads = []

        #needed so utf-8 parse error isn't thrown when swapping devices
        if i != (num_AP - 1):
            print("\nMove console cable now..\n")
            time.sleep(5)

    print("Batch Complete.")


if __name__ == "__main__":
    main()


