import tkinter as tk
from tkinter import filedialog, ttk, messagebox
import threading
import time
import math
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
import numpy as np
import random
import re
from datetime import datetime

# ----------------------------------------------------------------------
# PPK2 API INTEGRATION (Adapter Classes)
# ----------------------------------------------------------------------

# --- API LOADING LOGIC ---
try:
	# Attempt to import the real API components
	from ppk2_api.ppk2_api import PPK2_API, PPK2_Command, PPK2_Modes

	API_AVAILABLE = True
	API_MODE = "REAL"
	print("INFO: PPK2 API found. Attempting REAL mode.")
except ImportError:
	API_AVAILABLE = False
	API_MODE = "MOCK"
	print("WARNING: PPK2 API not found. Using MOCK mode for simulation.")


class PPK2Adapter:
	"""Base class for the PPK2 interface to ensure common methods."""

	def connect(self):
		raise NotImplementedError

	def start_measurement(self, voltage_v, samplerate, logic_enabled, spike_filtering_enabled):
		raise NotImplementedError

	def get_measurements(self):
		"""Returns tuple (currents_ma, all_logic_samples) where all_logic_samples is a list of [D0..D7] states per current sample."""
		raise NotImplementedError

	def set_voltage(self, voltage_v):
		raise NotImplementedError

	def stop(self):
		raise NotImplementedError


# --- MOCK DEFINITIONS (Always available for fallback) ---
BASE_I_QUIET = 10.0  # Static base for quiet current (mA)
BASE_I_PEAK = 130.0  # Static base for peak current (mA)
NOISE_RANGE = 0.5  # Random fluctuation range (±0.5 mA)


class PPK2Mock(PPK2Adapter):
	"""Emulates the PPK2_API interface for GUI testing."""

	def connect(self):
		print("MOCK: Connect successful.")
		return True  # Simulate successful connection

	def start_measurement(self, voltage_v, samplerate, logic_enabled, spike_filtering_enabled):
		self.samplerate = samplerate
		self.logic_enabled = logic_enabled
		self.voltage = voltage_v
		self.spike_filtering_enabled = spike_filtering_enabled
		self.start_time = time.time()
		print(
			f"MOCK: Started Source Meter @ {samplerate} Hz (Logic: {logic_enabled}, Spike Filter: {spike_filtering_enabled})")

	def set_voltage(self, voltage_v):
		self.voltage = voltage_v

	def get_measurements(self):
		"""Simulate dynamic current draw and logic data."""
		t = time.time()

		# Consistent with real measurement rate (100kS/s * 0.01s loop step = 1000 samples)
		num_samples = 1000
		currents_ma = []
		all_logic_samples = []

		for i in range(num_samples):
			# Simulate bursts of current every 5 seconds
			current_time = t + i / self.samplerate
			if (current_time) % 5 < 0.2:
				I_base = BASE_I_PEAK
			else:
				I_base = BASE_I_QUIET

			I_noise = random.uniform(-NOISE_RANGE, NOISE_RANGE)
			currents_ma.append(I_base + I_noise)

			# Simulate logic data for one sample
			if self.logic_enabled:
				# D0 toggles every 1s, D1 toggles every 0.5s, D5 toggles every 10s
				all_logic_samples.append([
					(current_time % 2) > 1,
					(current_time % 0.5) > 0.25,
					random.choice([True, False]),
					False,
					True,
					(current_time % 10) > 9.5,
					False,
					False
				])
			else:
				all_logic_samples.append([False] * 8)

		# Apply simple spike filter simulation (if enabled, smooth peaks slightly)
		if self.spike_filtering_enabled:
			currents_ma = np.convolve(currents_ma, np.ones(5) / 5, mode='same').tolist()

		return currents_ma, all_logic_samples

	def stop(self):
		print("MOCK: Stopped.")


# --- REAL PPK2 ADAPTER ---
if API_AVAILABLE:
	class PPK2Real(PPK2Adapter):
		"""Adapts the ppk2_api.PPK2_API to the common interface."""

		def __init__(self, port):
			self.logic_enabled = False
			# Initialize the real API object
			self.ppk2 = PPK2_API(port, timeout=1, write_timeout=1, exclusive=True)
			self.ppk2.get_modifiers()  # Always do this first

		def connect(self):
			return True  # Connection is handled in __init__

		def _set_spike_filtering(self, state):
			"""Internal method to set spike filtering using raw commands."""
			# Uses the available serial write and the command constants provided by the API.
			if state:
				# PPK2_Command.SPIKE_FILTERING_ON = 0x15
				self.ppk2._write_serial((PPK2_Command.SPIKE_FILTERING_ON,))
				print("REAL: Spike Filtering ON")
			else:
				# PPK2_Command.SPIKE_FILTERING_OFF = 0x16
				self.ppk2._write_serial((PPK2_Command.SPIKE_FILTERING_OFF,))
				print("REAL: Spike Filtering OFF")

		def start_measurement(self, voltage_v, samplerate, logic_enabled, spike_filtering_enabled):
			# Convert V to mV for the API
			voltage_mv = int(voltage_v * 1000)
			self.logic_enabled = logic_enabled

			self.set_voltage(voltage_mv)

			# Set mode and power
			self.ppk2.use_source_meter()
			self.ppk2.toggle_DUT_power("ON")

			# Set spike filtering
			self._set_spike_filtering(spike_filtering_enabled)

			# Start continuous measurement
			self.ppk2.start_measuring()

		def set_voltage(self, voltage_v):
			voltage_mv = int(voltage_v * 1000)
			self.ppk2.set_source_voltage(voltage_mv)

		def get_measurements(self):
			"""Returns tuple (currents_ma, all_logic_samples)"""
			raw_data = self.ppk2.get_data()
			if raw_data:
				# Get current samples (in uA) and raw digital data (list of bytes/ints)
				samples_uA, raw_digital_data = self.ppk2.get_samples(raw_data)

				# Convert current from uA to mA
				currents_ma = [i / 1000.0 for i in samples_uA]

				all_logic_samples = []
				if self.logic_enabled:
					# Process the raw digital data (each byte is D0-D7 state for one sample)
					for raw_byte in raw_digital_data:
						# Convert byte value to 8-bit list [D0, D1, ..., D7]
						all_logic_samples.append([bool((raw_byte >> i) & 1) for i in range(8)])
				else:
					# Provide empty data structure for consistency
					all_logic_samples = [[False] * 8 for _ in range(len(currents_ma))]

				return currents_ma, all_logic_samples

			# Return empty data if no data was read
			return [], []

		def stop(self):
			self.ppk2.stop_measuring()
			self.ppk2.toggle_DUT_power("OFF")
			# Clean up the serial connection
			del self.ppk2
			self.ppk2 = None
			print("REAL: Stopped.")


def init_ppk2_device(voltage_v, samplerate_hz, logic_enabled=False, spike_filtering_enabled=False):
	"""Initializes the PPK2 device (Real or Mock)."""
	global API_MODE

	if API_MODE == "REAL":
		ppk2s_connected = PPK2_API.list_devices()
		if len(ppk2s_connected) == 1:
			ppk2_port = ppk2s_connected[0][0]
			try:
				device = PPK2Real(ppk2_port)
				device.start_measurement(
					voltage_v,
					samplerate=samplerate_hz,
					logic_enabled=logic_enabled,
					spike_filtering_enabled=spike_filtering_enabled
				)
				return device
			except Exception as e:
				# Fallback to mock on real error
				print(f"Error initializing real PPK2: {e}. Falling back to MOCK.")
				API_MODE = "MOCK"
		else:
			if len(ppk2s_connected) > 1:
				print(f"ERROR: Found too many connected PPK2's: {ppk2s_connected}. Please connect only one.")
			else:
				print("ERROR: No PPK2 device found.")
			print("Falling back to MOCK.")
			API_MODE = "MOCK"

	# Fallback/Default to MOCK
	mock_device = PPK2Mock()
	mock_device.start_measurement(
		voltage_v,
		samplerate=samplerate_hz,
		logic_enabled=logic_enabled,
		spike_filtering_enabled=spike_filtering_enabled
	)
	return mock_device


# ----------------------------------------------------------------------
# EMULATION CONSTANTS AND BATTERY PARAMETERS
# ----------------------------------------------------------------------

DISCHARGE_RATE = 100  # Emulation acceleration factor (100x faster than real time)
TIME_STEP_REAL_SEC = 1.0  # The amount of real time simulated in one emulation step
TIME_STEP_EMUL_SEC = TIME_STEP_REAL_SEC / DISCHARGE_RATE  # The actual time the PPK2 samples current


class BatteryEmulator(tk.Frame):
	# Static data for battery technologies and their discharge limits
	BATTERY_PROFILES = {
		"Li-Po/Li-Ion (3.7V)": {"V_START": 4.2, "V_STOP": 3.2},
		"LiFePO4 (3.2V)": {"V_START": 3.6, "V_STOP": 2.8},
		"NiMH/NiCd (1.2V)": {"V_START": 1.4, "V_STOP": 1.0},
		"Alkaline (1.5V)": {"V_START": 1.6, "V_STOP": 1.2}
	}

	def __init__(self, master=None):
		super().__init__(master)
		self.master = master
		# Update title based on determined API mode
		self.master.title(f"PPK2 [{API_MODE}] - Battery Simulator and Current Profiler")
		self.ppk2 = None
		self.is_running = False
		self.data_thread = None

		# State Variables
		self.current_voltage = 0.0
		self.current_capacity_mah = 0.0
		self.time_elapsed_real_sec = 0
		self.all_current_samples = []
		self.all_time_real_s = []
		self.all_voltage_v = []
		self.all_logic_data = []

		# Configuration
		self.config = {
			"V_STOP": self.BATTERY_PROFILES["Li-Po/Li-Ion (3.7V)"]["V_STOP"],
			"SAMPLE_RATE_HZ": 1000
		}

		self.CAPACITY_OPTIONS = [1500, 3500, 5000, 8000]
		self.V_START_DEFAULT = self.BATTERY_PROFILES["Li-Po/Li-Ion (3.7V)"]["V_START"]
		self.CAPACITY_DEFAULT = 3500
		self.SIM_TIME_DEFAULT_STR = "01:00"

		# GUI Variables
		self.battery_type_var = tk.StringVar(value="Li-Po/Li-Ion (3.7V)")
		self.v_start_var = tk.DoubleVar(value=self.V_START_DEFAULT)
		self.v_stop_var = tk.DoubleVar(value=self.config["V_STOP"])
		self.capacity_var = tk.StringVar(value=str(self.CAPACITY_DEFAULT))
		self.logic_enabled_var = tk.BooleanVar(value=False)
		self.spike_filtering_var = tk.BooleanVar(value=False)  # <--- NEW VAR
		self.sim_discharge_var = tk.BooleanVar(value=False)
		self.sim_time_str_var = tk.StringVar(value=self.SIM_TIME_DEFAULT_STR)

		# Bind voltage variable changes to update the config
		self.v_start_var.trace_add("write", self.update_config_voltage)
		self.v_stop_var.trace_add("write", self.update_config_voltage)

		# Simulation specific variables
		self.total_sim_time_sec = 0
		self.required_mah_loss_per_sec = 0

		self.create_widgets()
		self.load_initial_state()
		self.print_battery_info()

	def print_battery_info(self):
		print("\n--- Common Battery Technology Information ---")
		for tech, limits in self.BATTERY_PROFILES.items():
			print(
				f"| {tech:<20} | V_Start: {limits['V_START']:.1f}V | V_Stop: {limits['V_STOP']:.1f}V | Capacity: 100 mAh to 50,000 mAh+")
		print("-------------------------------------------\n")

	def convert_mm_ss_to_sec(self, mm_ss_str):
		"""Converts MM:SS string to total seconds."""
		match = re.match(r"(\d+):(\d+)", mm_ss_str.strip())
		if match:
			minutes = int(match.group(1))
			seconds = int(match.group(2))
			return minutes * 60 + seconds
		try:
			return float(mm_ss_str) * 60
		except:
			return 0

	def format_seconds_to_hms(self, total_seconds):
		"""Converts total seconds into HH:MM:SS format."""
		if total_seconds == float('inf'):
			return "INF"

		total_seconds = int(total_seconds)
		hours = total_seconds // 3600
		minutes = (total_seconds % 3600) // 60
		seconds = total_seconds % 60
		return f"{hours:02}:{minutes:02}:{seconds:02}"

	def update_config_voltage(self, *args):
		"""Updates the internal config from the GUI DoubleVars."""
		try:
			self.config["V_START"] = self.v_start_var.get()
			self.config["V_STOP"] = self.v_stop_var.get()
		except tk.TclError:
			# Handle case where conversion fails (e.g., user is typing non-float data)
			pass

	def update_voltage_from_type(self, *args):
		"""Updates V_START and V_STOP based on the selected battery type."""
		selected_type = self.battery_type_var.get()
		profile = self.BATTERY_PROFILES.get(selected_type)

		if profile:
			self.v_start_var.set(profile["V_START"])
			self.v_stop_var.set(profile["V_STOP"])
			print(f"INFO: Set V_START={profile['V_START']}V and V_STOP={profile['V_STOP']}V for {selected_type}.")
		else:
			print(f"WARNING: Unknown battery type selected: {selected_type}")

	def create_voltage_spinbox(self, parent, variable, row, column, label_text):
		"""Creates a label and an editable Spinbox with 0.01 increment/decrement."""
		ttk.Label(parent, text=label_text).grid(row=row, column=column, padx=5, pady=2, sticky='w')

		spinbox = tk.Spinbox(parent,
		                     from_=0.0, to_=5.0,
		                     increment=0.01,
		                     textvariable=variable,
		                     format="%.2f",
		                     width=6,
		                     command=self.update_config_voltage)  # Update on button press

		spinbox.grid(row=row, column=column + 1, padx=5, pady=2, sticky='w')

		# Bind the Enter key to update the config (for manual entry)
		spinbox.bind('<Return>', lambda event: self.update_config_voltage())

		return spinbox

	def load_initial_state(self):
		# Apply current settings to configuration variables
		self.config["V_STOP"] = self.v_stop_var.get()
		self.config["V_START"] = self.v_start_var.get()

		try:
			capacity = float(self.capacity_var.get())
		except ValueError:
			capacity = self.CAPACITY_DEFAULT
			self.capacity_var.set(str(self.CAPACITY_DEFAULT))

		self.config["CAPACITY_NOMINAL_MAH"] = capacity

		self.current_capacity_mah = self.config["CAPACITY_NOMINAL_MAH"]
		self.current_voltage = self.config["V_START"]
		self.v_label.config(text=f"V_SYS Voltage (V): {self.current_voltage:.3f}")
		self.soc_label.config(text="SoC (%): 100.0 (Avg I: 0.0 mA)")

		# Prepare for Simulated Discharge Mode
		if self.sim_discharge_var.get():
			self.total_sim_time_sec = self.convert_mm_ss_to_sec(self.sim_time_str_var.get())

			# --- Calculation for console/summary output (not used for V drop) ---
			soc_start = 100.0
			sim_V_stop = max(self.config["V_STOP"], self.config["V_START"] - 0.01)
			soc_stop = self.get_soc_percent_from_voltage(sim_V_stop)

			required_soc_drop = soc_start - soc_stop
			required_mah_drop = required_soc_drop * (self.config["CAPACITY_NOMINAL_MAH"] / 100.0)

			if self.total_sim_time_sec > 0:
				self.required_mah_loss_per_sec = required_mah_drop / self.total_sim_time_sec
			else:
				self.required_mah_loss_per_sec = 0

			self.current_capacity_mah = self.config["CAPACITY_NOMINAL_MAH"]

	def create_widgets(self):
		# --- TOP CONTROL FRAME ---
		control_frame = ttk.LabelFrame(self.master, text="Control and Configuration")
		control_frame.pack(padx=10, pady=5, fill="x")

		# Row 0: Battery Type Selection and Capacity
		ttk.Label(control_frame, text="Battery Type:").grid(row=0, column=0, padx=5, pady=2, sticky='w')
		battery_menu = ttk.Combobox(control_frame,
		                            textvariable=self.battery_type_var,
		                            values=list(self.BATTERY_PROFILES.keys()),
		                            width=20,
		                            state="readonly")
		battery_menu.grid(row=0, column=1, padx=5, pady=2, sticky='w', columnspan=2)
		self.battery_type_var.trace_add("write", self.update_voltage_from_type)

		ttk.Label(control_frame, text="Capacity (mAh):").grid(row=0, column=3, padx=5, pady=2, sticky='w')
		capacity_menu = ttk.Combobox(control_frame, textvariable=self.capacity_var, values=self.CAPACITY_OPTIONS,
		                             width=8)
		capacity_menu.grid(row=0, column=4, padx=5, pady=2, sticky='w')

		# Row 1: Voltage Settings (Using new Spinbox helper)
		self.create_voltage_spinbox(control_frame, self.v_start_var, 1, 0, "Start V (V):")
		self.create_voltage_spinbox(control_frame, self.v_stop_var, 1, 2, "Stop V (V):")

		# Row 2: Logic Analyzer, Spike Filtering and Start Button
		# Logic Analyzer
		ttk.Checkbutton(control_frame, text="Enable Logic Analyzer", variable=self.logic_enabled_var).grid(row=2,
		                                                                                                   column=0,
		                                                                                                   padx=5,
		                                                                                                   pady=2,
		                                                                                                   sticky='w')

		# New: Spike Filtering Checkbox
		ttk.Checkbutton(control_frame, text="Enable Spike Filtering", variable=self.spike_filtering_var).grid(row=2,
		                                                                                                      column=1,
		                                                                                                      padx=5,
		                                                                                                      pady=2,
		                                                                                                      sticky='w',
		                                                                                                      columnspan=2)

		self.start_button = ttk.Button(control_frame, text="START Emulation", command=self.toggle_emulation)
		self.start_button.grid(row=2, column=4, padx=10, pady=5)

		# Row 3: Simulated Discharge Controls
		sim_frame = ttk.LabelFrame(control_frame, text="Simulated Time Discharge")
		sim_frame.grid(row=3, column=0, columnspan=5, padx=5, pady=5, sticky='we')

		ttk.Checkbutton(sim_frame, text="Enable Time Simulation", variable=self.sim_discharge_var).pack(side='left',
		                                                                                                padx=5, pady=2)
		ttk.Label(sim_frame, text="Target Time (MM:SS):").pack(side='left', padx=5, pady=2)
		ttk.Entry(sim_frame, textvariable=self.sim_time_str_var, width=8).pack(side='left', padx=5, pady=2)
		ttk.Label(sim_frame, text=f"({self.SIM_TIME_DEFAULT_STR} default)").pack(side='left', padx=5, pady=2)

		# --- STATE DISPLAY FRAME ---
		state_frame = ttk.LabelFrame(self.master, text="Simulation State")
		state_frame.pack(padx=10, pady=5, fill="x")
		self.v_label = ttk.Label(state_frame, text="V_SYS Voltage (V): 0.000", font=('Arial', 12, 'bold'))
		self.v_label.grid(row=0, column=0, padx=5, pady=5)
		self.soc_label = ttk.Label(state_frame, text="SoC (%): 0.0 (Avg I: 0.0 mA)", font=('Arial', 12, 'bold'))
		self.soc_label.grid(row=0, column=1, padx=5, pady=5)
		self.runtime_label = ttk.Label(state_frame, text="Real Runtime (h): 0.00")
		self.runtime_label.grid(row=0, column=2, padx=5, pady=5)

		# Logic Analyzer Status
		logic_status_frame = ttk.Frame(state_frame)
		logic_status_frame.grid(row=1, column=0, columnspan=3, pady=5)
		ttk.Label(logic_status_frame, text="Logic Pins:").pack(side='left', padx=5)
		self.logic_indicators = []
		for i in range(8):
			label = ttk.Label(logic_status_frame, text=f"D{i}: LOW", width=6)
			label.pack(side='left', padx=2)
			self.logic_indicators.append(label)

		# --- PLOT FRAME ---
		plot_frame = ttk.LabelFrame(self.master, text="Current and Voltage Profile")
		plot_frame.pack(padx=10, pady=5, fill="both", expand=True)

		self.fig, (self.ax_i, self.ax_v) = plt.subplots(2, 1, figsize=(8, 6), sharex=True)

		# Current Plot (Top)
		self.ax_i.set_ylabel("Current (mA)")
		self.ax_i.grid(True)
		self.line_i, = self.ax_i.plot([], [], 'r-')

		# Voltage Plot (Bottom)
		self.ax_v.set_xlabel("Relative Time (s)")
		self.ax_v.set_ylabel("Voltage (V)")
		self.ax_v.grid(True)
		self.line_v, = self.ax_v.plot([], [], 'b-')

		self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
		self.canvas_widget = self.canvas.get_tk_widget()
		self.canvas_widget.pack(fill="both", expand=True)

		# --- STATISTICS FRAME ---
		stats_frame = ttk.LabelFrame(self.master, text="Measurement Statistics")
		stats_frame.pack(padx=10, pady=5, fill="x")
		self.peak_label = ttk.Label(stats_frame, text="Peak Current (mA): 0.0")
		self.peak_label.grid(row=0, column=0, padx=5, pady=5)
		self.rms_label = ttk.Label(stats_frame, text="RMS Current (mA): 0.0")
		self.rms_label.grid(row=0, column=1, padx=5, pady=5)
		self.save_button = ttk.Button(stats_frame, text="Save Log (.csv)", command=self.save_data)
		self.save_button.grid(row=0, column=2, padx=10, pady=5)

	def toggle_emulation(self):
		if not self.is_running:
			self.start_emulation()
		else:
			self.stop_emulation()

	def start_emulation(self):
		global API_MODE
		try:
			self.load_initial_state()

			# 1. Initialize PPK2 using the unified function
			self.ppk2 = init_ppk2_device(
				self.config["V_START"],
				self.config["SAMPLE_RATE_HZ"],
				self.logic_enabled_var.get(),
				self.spike_filtering_var.get()  # <--- PASS NEW ARGUMENT
			)

			# Update the title in case the mode changed to MOCK
			self.master.title(f"PPK2 [{API_MODE}] - Battery Simulator and Current Profiler")

			# 2. Reset State and Logs
			self.all_current_samples = []
			self.all_time_real_s = []
			self.all_voltage_v = []
			self.all_logic_data = []
			self.time_elapsed_real_sec = 0

			# 3. Start Data Thread
			self.is_running = True
			self.start_button.config(text="STOP Emulation", state=tk.NORMAL)
			self.data_thread = threading.Thread(target=self.emulation_loop)
			self.data_thread.daemon = True
			self.data_thread.start()

			# Initial console debug message
			print("\n--- EMULATION STARTING ---")
			print(
				f"Mode: {'Time Simulation' if self.sim_discharge_var.get() else 'Current Emulation'} | Battery: {self.battery_type_var.get()}")
			print(
				f"Capacity: {self.config['CAPACITY_NOMINAL_MAH']} mAh | V_Start: {self.config['V_START']} V | V_Stop: {self.config['V_STOP']} V")
			if self.sim_discharge_var.get():
				print(
					f"Target Discharge Time: {self.sim_time_str_var.get()} (MM:SS) | Required Loss Rate: {self.required_mah_loss_per_sec * 3600.0:.2f} mA (Theoretical)")
			print("--------------------------")

		except Exception as e:
			messagebox.showerror("PPK2 Error", f"Failed to initialize PPK2: {e}. Check API connection.")
			self.stop_emulation(is_error=True)

	def stop_emulation(self, is_error=False):
		if not self.is_running and not is_error:
			return

		self.is_running = False

		if self.ppk2:
			try:
				self.ppk2.stop()  # Calls unified stop method
			except Exception as e:
				print(f"Error while stopping PPK2 device: {e}")
			self.ppk2 = None

		if self.data_thread and self.data_thread.is_alive():
			self.data_thread.join(timeout=2)

		self.start_button.config(text="START Emulation", state=tk.NORMAL)
		self.display_final_results()

	# --- Voltage/SoC Mapping Functions (Retained from original script) ---
	def get_voltage_from_soc(self, soc_percent):
		"""Calculates voltage from SoC percentage based on simplified discharge curve."""
		V_start = self.config["V_START"]
		V_stop = self.config["V_STOP"]
		if V_start <= V_stop:
			return V_start
		V_range = V_start - V_stop

		if soc_percent >= 90:
			V_drop_10_percent = V_range * 0.1
			V_current = V_start - ((100 - soc_percent) * (V_drop_10_percent / 10))
			return max(V_stop, V_current)
		elif soc_percent > 10:
			V_plateau_start = V_start - (V_range * 0.1)
			V_plateau_end = V_stop + (V_range * 0.1)
			V_plateau_drop = V_plateau_start - V_plateau_end
			V_current = V_plateau_start - ((90 - soc_percent) * (V_plateau_drop / 80))
			return max(V_stop, V_current)
		else:
			V_plateau_end = V_stop + (V_range * 0.1)
			V_current = V_stop + (soc_percent * (V_plateau_end - V_stop) / 10)
			return max(V_stop, V_current)

	def get_soc_percent_from_voltage(self, voltage):
		"""Reverse calculation: Estimates SoC from voltage (used for setup)."""
		V_start = self.config["V_START"]
		V_stop = self.config["V_STOP"]
		if V_start <= V_stop:
			return 100.0
		V_range = V_start - V_stop
		if voltage >= V_start:
			return 100.0
		elif voltage <= V_stop:
			return 0.0

		# Simplified inverse mapping
		V_drop_10_percent = V_range * 0.1
		V_plateau_start = V_start - V_drop_10_percent
		if voltage > V_plateau_start:
			# Top 10%
			return 100 - ((V_start - voltage) / V_drop_10_percent) * 10

		V_plateau_end = V_stop + V_drop_10_percent
		if voltage >= V_plateau_end:
			# Middle 80%
			V_plateau_drop = V_plateau_start - V_plateau_end
			if V_plateau_drop <= 0: return 90.0
			return 90 - ((V_plateau_start - voltage) / V_plateau_drop) * 80

		# Bottom 10%
		V_final_range = V_plateau_end - V_stop
		if V_final_range <= 0: return 10.0
		return ((voltage - V_stop) / V_final_range) * 10

	# --- Main Emulation Loop ---
	def emulation_loop(self):
		last_emulation_time = time.time()
		is_sim_discharge = self.sim_discharge_var.get()

		while self.is_running and self.current_voltage > self.config["V_STOP"]:
			if not self.is_running: break

			# Time synchronization for the loop step
			time_to_wait = last_emulation_time + TIME_STEP_EMUL_SEC - time.time()
			if time_to_wait > 0:
				time.sleep(time_to_wait)
			last_emulation_time = time.time()

			# 1. CURRENT AND LOGIC DATA MEASUREMENT
			try:
				# UNIFIED CALL: Get currents_ma (list of floats) and all_logic_samples (list of lists of bools)
				currents_ma, all_logic_samples = self.ppk2.get_measurements()

				if not currents_ma:
					# If no samples, skip this loop iteration
					continue

				# Get the state of the last sample for GUI indicator
				logic_data = all_logic_samples[-1] if all_logic_samples else [False] * 8

			except Exception as e:
				print(f"Error fetching data: {e}")
				self.is_running = False
				break

			# 2. CURRENT CALCULATIONS and BATTERY EMULATION
			currents_ma_np = np.array(currents_ma)
			I_avg_ma_sampled = np.mean(currents_ma_np)

			# Current value scaled by discharge rate for the emulation step
			I_avg_ma_realtime = I_avg_ma_sampled / DISCHARGE_RATE

			# Time duration of the simulated step in hours (e.g., 1 second of real time)
			time_h_simulated = TIME_STEP_REAL_SEC / 3600.0

			self.time_elapsed_real_sec += TIME_STEP_REAL_SEC

			if is_sim_discharge:
				# SIMULATION MODE: Voltage loss is based on target time and SoC curve.
				# Calculate the required capacity loss for this step
				required_mah_loss_step = self.required_mah_loss_per_sec * TIME_STEP_REAL_SEC
				self.current_capacity_mah -= required_mah_loss_step
				soc_percent = (self.current_capacity_mah / self.config["CAPACITY_NOMINAL_MAH"]) * 100.0

				# Check if we hit the time limit (or V_STOP based on curve)
				if self.time_elapsed_real_sec >= self.total_sim_time_sec or soc_percent <= 0:
					self.current_voltage = self.config["V_STOP"]  # Force V_STOP at end
					self.is_running = False
					soc_percent = self.get_soc_percent_from_voltage(self.current_voltage)
					self.current_capacity_mah = self.config["CAPACITY_NOMINAL_MAH"] * (soc_percent / 100.0)
				else:
					self.current_voltage = self.get_voltage_from_soc(soc_percent)

				# Time remaining calculation (simple countdown)
				time_remaining_sec = max(0, self.total_sim_time_sec - self.time_elapsed_real_sec)

			else:
				# REAL EMULATION MODE: Voltage loss is current-based.
				consumed_mah = I_avg_ma_realtime * time_h_simulated
				self.current_capacity_mah -= consumed_mah
				soc_percent = (self.current_capacity_mah / self.config["CAPACITY_NOMINAL_MAH"]) * 100.0

				if soc_percent <= 0:
					self.current_voltage = self.config["V_STOP"]
					self.is_running = False
				else:
					self.current_voltage = self.get_voltage_from_soc(soc_percent)

				# Estimated remaining time is based on current consumption rate
				mah_remaining = self.current_capacity_mah - self.config[
					"CAPACITY_NOMINAL_MAH"] * self.get_soc_percent_from_voltage(self.config["V_STOP"]) / 100.0
				time_remaining_sec = (mah_remaining / I_avg_ma_realtime) * 3600.0 if I_avg_ma_realtime > 0 else float(
					'inf')

			# 3. UPDATE VOLTAGE ON PPK2
			self.ppk2.set_voltage(self.current_voltage)

			# 4. Update Logs
			# Log the current voltage for every sample in this step (assuming all samples in the step share the new voltage)
			for current_sample in currents_ma:
				self.all_current_samples.append(current_sample)
				self.all_time_real_s.append(self.time_elapsed_real_sec)
				self.all_voltage_v.append(self.current_voltage)

			# Append logic data (list of lists)
			self.all_logic_data.extend(all_logic_samples)

			# Calculate statistics for the GUI display from the latest samples
			peak_current = np.max(currents_ma_np)
			rms_current = np.sqrt(np.mean(currents_ma_np ** 2))

			# --- CONSOLE DEBUG OUTPUT ---
			# Print every 5 seconds (5 steps of 1s)
			if int(self.time_elapsed_real_sec) % 5 == 0 or not self.is_running:
				time_rem_str = self.format_seconds_to_hms(time_remaining_sec)
				print(
					f"TIME: {self.time_elapsed_real_sec:.1f} s | "
					f"V_OUT: {self.current_voltage:.3f} V | "
					f"I_AVG: {I_avg_ma_realtime:.2f} mA | "
					f"SoC: {soc_percent:.1f}% | "
					f"Rem. Time: {time_rem_str}"
				)

			# Update GUI (Thread-safe way)
			self.master.after(0, self.update_gui, soc_percent, I_avg_ma_realtime, peak_current, rms_current,
			                  currents_ma, logic_data)

		# Ensure stop is called when loop finishes
		self.master.after(0, self.stop_emulation)

	# --- GUI Update and Plotting Functions (Retained from original script) ---
	def update_gui(self, soc_percent, I_avg_ma_realtime, peak_current, rms_current, currents_ma, logic_data):
		self.v_label.config(text=f"V_SYS Voltage (V): {self.current_voltage:.3f}")
		self.soc_label.config(text=f"SoC (%): {max(0, soc_percent):.1f} (Avg I: {I_avg_ma_realtime:.2f} mA)")

		runtime_h = self.time_elapsed_real_sec / 3600.0
		self.runtime_label.config(text=f"Real Runtime (h): {runtime_h:.2f}")

		# Update Logic Indicators
		if self.logic_enabled_var.get():
			for i, state in enumerate(logic_data):
				color = "red" if state else "green"
				text = f"D{i}: {'HIGH' if state else 'LOW'}"
				self.logic_indicators[i].config(text=text, foreground=color)
		else:
			for indicator in self.logic_indicators:
				indicator.config(text="D#: ---", foreground="black")

		# Update Statistics
		self.peak_label.config(text=f"Peak Current (mA): {peak_current:.2f}")
		self.rms_label.config(text=f"RMS Current (mA): {rms_current:.2f}")

		# Update Plot (Only plot last ~1000 samples for performance)
		plot_limit = 1000

		# Calculate X-axis in seconds (relative to start)
		time_s = np.array(self.all_time_real_s[-plot_limit:])

		# Current data (all samples are relevant)
		I_data = np.array(self.all_current_samples[-plot_limit:])

		# Voltage data (all samples are relevant, but voltage is stepped)
		V_data = np.array(self.all_voltage_v[-plot_limit:])

		self.line_i.set_data(time_s, I_data)
		self.line_v.set_data(time_s, V_data)

		# Auto-scale X axis
		self.ax_i.set_xlim(time_s[0], time_s[-1] if time_s.size > 1 else time_s[0] + 1)

		# Auto-scale Y axis (Current)
		i_max = max(I_data) if I_data.size > 0 and max(I_data) > 0 else BASE_I_PEAK * 1.5
		i_min = min(I_data) if I_data.size > 0 else 0
		self.ax_i.set_ylim(i_min * 0.9, i_max * 1.1)

		# Auto-scale Y axis (Voltage)
		v_min = self.config["V_STOP"]
		v_max = self.config["V_START"]
		self.ax_v.set_ylim(v_min * 0.9, v_max * 1.1)
		self.fig.tight_layout()
		self.canvas.draw()

	def display_final_results(self):
		total_time_real_sec = self.time_elapsed_real_sec
		total_time_real_h = total_time_real_sec / 3600.0
		# Format runtime to HH:MM:SS for the final message
		runtime_hms = self.format_seconds_to_hms(total_time_real_sec)

		# Re-calculate overall average current from stored logs
		overall_avg_I_ma = np.mean(self.all_current_samples) if self.all_current_samples else 0.0
		I_test_avg_ma = overall_avg_I_ma / DISCHARGE_RATE

		if self.sim_discharge_var.get():
			# In time mode, we calculate the charge loss required to hit V_STOP based on the SoC curve.
			final_soc = self.get_soc_percent_from_voltage(self.config["V_STOP"])
			initial_soc = self.get_soc_percent_from_voltage(self.config["V_START"])
			soc_drop = initial_soc - final_soc
			mah_consumed_simulated = self.config["CAPACITY_NOMINAL_MAH"] * (soc_drop / 100.0)

			final_message = (
				f"!!! SIMULATION FINISHED (Time Mode) !!!\n"
				f"Targeted simulation time: {self.sim_time_str_var.get()} (MM:SS).\n"
				f"Discharge from {self.config['V_START']:.2f}V to {self.config['V_STOP']:.2f}V achieved.\n"
				f"\n--- RESULTS ---\n"
				f"Simulated Runtime: {runtime_hms} (HH:MM:SS)\n"
				f"Overall Average Current Drawn (PPK2 Samples): {overall_avg_I_ma:.2f} mA\n"
				f"Overall Average Current Drawn (Emulated/Realtime): {I_test_avg_ma:.2f} mA"
			)
		else:
			total_consumed_mah = self.config["CAPACITY_NOMINAL_MAH"] - self.current_capacity_mah
			estimated_runtime_h = self.config["CAPACITY_NOMINAL_MAH"] / I_test_avg_ma if I_test_avg_ma > 0 else float(
				'inf')
			estimated_runtime_hms = self.format_seconds_to_hms(estimated_runtime_h * 3600.0)

			final_message = (
				f"!!! EMULATION FINISHED !!!\n"
				f"Algorithm reached V_STOP = {self.config['V_STOP']} V.\n"
				f"\n--- TEST RESULTS ---\n"
				f"Simulated Runtime until V_STOP: {runtime_hms} (HH:MM:SS)\n"
				f"Overall Average Current Drawn (PPK2 Samples): {overall_avg_I_ma:.2f} mA\n"
				f"Overall Average Current Drawn (Emulated/Realtime): {I_test_avg_ma:.2f} mA\n"
				f"*** ESTIMATED TOTAL RUNTIME (Full Capacity): {estimated_runtime_hms} (HH:MM:SS) ***"
			)

		messagebox.showinfo("Simulation Finished", final_message)

	def save_data(self):
		filepath = filedialog.asksaveasfilename(
			defaultextension=".csv",
			filetypes=[("CSV files", "*.csv")],
			initialfile=f"ppk2_batt_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
		)

		if not filepath:
			return

		# --- 1. Get Log Time Data (Time in s:ms format for consistency) ---
		time_s_ms = []
		for t_s in self.all_time_real_s:
			s = int(t_s)
			ms = int((t_s - s) * 1000)
			time_s_ms.append(f"{s}:{ms:03d}")

		# --- 2. Logic Data Conversion Helper (Function to convert list of bools to Hex) ---
		def logic_to_hex(logic_list):
			decimal_value = 0
			# D0 is the least significant bit (index 0)
			for i, state in enumerate(logic_list):
				if state:
					decimal_value += (2 ** i)
			# Return with '0x' prefix
			return f'0x{decimal_value:02X}'

		# --- 3. Prepare Data Frame for main log ---
		min_len = min(len(time_s_ms), len(self.all_current_samples), len(self.all_voltage_v), len(self.all_logic_data))

		# Create columns for logic data (D0, D1, ..., D7)
		logic_cols = {f"D{i}": [log[i] for log in self.all_logic_data[:min_len]] for i in range(8)}

		# New Hexadecimal column
		logic_hex_values = [logic_to_hex(log) for log in self.all_logic_data[:min_len]]

		data_dict = {
			"Time_s:ms": time_s_ms[:min_len],
			"Current_mA_Measured": self.all_current_samples[:min_len],
			"Voltage_V_Emulated": self.all_voltage_v[:min_len],
			**logic_cols,
			"Logic_Hex_D0-D7": logic_hex_values
		}
		df = pd.DataFrame(data_dict)

		# --- 4. Calculate Summary Statistics ---
		total_time_real_sec = self.time_elapsed_real_sec
		total_time_real_h = total_time_real_sec / 3600.0
		runtime_hms = self.format_seconds_to_hms(total_time_real_sec)

		# Calculate overall average/rms/peak from all collected samples (not just the last step)
		all_currents_np = np.array(self.all_current_samples)
		I_avg_ma = np.mean(all_currents_np) if all_currents_np.size > 0 else 0.0
		I_rms_ma = np.sqrt(np.mean(all_currents_np ** 2)) if all_currents_np.size > 0 else 0.0
		I_peak_ma = np.max(all_currents_np) if all_currents_np.size > 0 else 0.0

		if self.sim_discharge_var.get():
			# In time mode, we calculate the charge loss required to hit V_STOP based on the SoC curve.
			final_soc = self.get_soc_percent_from_voltage(self.config["V_STOP"])
			initial_soc = self.get_soc_percent_from_voltage(self.config["V_START"])
			soc_drop = initial_soc - final_soc
			mah_consumed_simulated = self.config["CAPACITY_NOMINAL_MAH"] * (soc_drop / 100.0)
			I_test_avg_ma_realtime = mah_consumed_simulated / total_time_real_h if total_time_real_h > 0 else 0
			estimated_runtime_hms = "N/A"  # Not applicable in time mode

		else:
			total_consumed_mah = self.config["CAPACITY_NOMINAL_MAH"] - self.current_capacity_mah
			I_test_avg_ma_realtime = total_consumed_mah / total_time_real_h if total_time_real_h > 0 else 0
			estimated_runtime_h = self.config[
				                      "CAPACITY_NOMINAL_MAH"] / I_test_avg_ma_realtime if I_test_avg_ma_realtime > 0 else float(
				'inf')
			estimated_runtime_hms = self.format_seconds_to_hms(estimated_runtime_h * 3600.0)

		# --- 5. Create Summary Block ---
		summary_lines = [
			"--------------------------",
			"--- EMULATION SUMMARY ---",
			f"API_Mode,{API_MODE}",
			f"Battery_Type,{self.battery_type_var.get()}",
			f"Capacity_mAh,{self.config['CAPACITY_NOMINAL_MAH']}",
			f"Discharge_Mode,{'Time Simulation' if self.sim_discharge_var.get() else 'Current Emulation'}",
			f"Logic_Enabled,{self.logic_enabled_var.get()}",
			f"Spike_Filtering_Enabled,{self.spike_filtering_var.get()}",
			f"Avg_Current_mA_Realtime,{I_test_avg_ma_realtime:.2f}",
			f"Avg_Current_mA_PPK2_Sampled,{I_avg_ma:.2f}",
			f"RMS_Current_mA,{I_rms_ma:.2f}",
			f"Peak_Current_mA,{I_peak_ma:.2f}",
			f"Runtime_to_V_STOP_H:M:S,{runtime_hms}",  # Use H:M:S format here
			f"Estimated_Total_Runtime_H:M:S,{estimated_runtime_hms}",
			f"V_START_V,{self.config['V_START']:.2f}",
			f"V_STOP_V,{self.config['V_STOP']:.2f}",
			"--------------------------"
		]
		summary_block = "\n".join(summary_lines)

		# --- 6. Save to CSV ---
		with open(filepath, 'w') as f:
			# Write data frame (main log)
			f.write(df.to_csv(index=False))
			# Append summary block
			f.write(summary_block)

		messagebox.showinfo("Saved", f"Data and Summary successfully saved to:\n{filepath}")


# --- Main application loop ---
if __name__ == "__main__":
	# Ensure dependencies are available (though no guarantee)
	try:
		import numpy as np
		import pandas as pd
		import matplotlib.pyplot as plt
	except ImportError:
		print(
			"FATAL ERROR: Missing dependencies (numpy, pandas, matplotlib). Please install them using: pip install numpy pandas matplotlib")
		exit()

	root = tk.Tk()
	app = BatteryEmulator(master=root)
	app.pack(fill="both", expand=True)


	def on_closing():
		if messagebox.askokcancel("Quit", "Do you want to quit the application?"):
			app.stop_emulation()
			root.destroy()


	root.protocol("WM_DELETE_WINDOW", on_closing)
	root.mainloop()