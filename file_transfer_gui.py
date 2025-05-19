import asyncio
import os
import threading
import time
import sys
import socket
import json
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from concurrent.futures import ThreadPoolExecutor
import glob # To handle potential folder selection (though we'll focus on files)

# Define default settings. These can now be changed in the GUI.
# Default chunk size in BYTES, but we'll display and input in GB in the GUI
DEFAULT_CHUNK_SIZE_BYTES = 190 * 1024 * 1024 # Default chunk size (190MB)
DEFAULT_MAX_WORKERS = 4 # Default max workers for ThreadPoolExecutor (for disk I/O)
DEFAULT_CONCURRENT_TRANSFERS = 3 # Default number of files to send simultaneously

# Conversion factor
BYTES_TO_GB = 1024 * 1024 * 1024

# UDP Broadcast settings for discovery
DISCOVERY_PORT = 9999
DISCOVERY_INTERVAL = 5 # Seconds between broadcasts
DISCOVERY_MESSAGE = b"FILE_TRANSFER_DISCOVERY"

# --- Core Transfer Logic ---

async def send_single_file(writer, file_path, chunk_size_bytes, max_workers, progress_callback, semaphore):
    """
    Asynchronously sends a single file over a network connection, updating GUI progress.
    This is called by the main sending logic for each file in the list.
    Uses configurable chunk_size (in bytes) and max_workers.
    Acquires and releases the semaphore to control concurrency.
    """
    # Acquire the semaphore before starting the transfer for this file
    async with semaphore:
        file_size = os.path.getsize(file_path)
        file_name = os.path.basename(file_path)

        print(f"Attempting to send file: {file_name} ({file_size} bytes)")

        # Send file metadata (name and size) first
        # Use a delimiter to separate file metadata within a single connection
        # We'll use a simple newline, assuming filenames don't contain newlines.
        # A more robust protocol would use fixed-size headers or length prefixes.
        writer.write(f"{file_name}\n{file_size}\n".encode())
        try:
            # Add timeout for drain to prevent hangs
            await asyncio.wait_for(writer.drain(), timeout=10.0)
            print("Sent file metadata.")
        except asyncio.TimeoutError:
            print(f"Timeout sending metadata for {file_name}")
            progress_callback(-1, file_size, 0, file_name=file_name, error="Timeout sending metadata")
            return # Stop sending this file
        except Exception as e:
            print(f"Error sending metadata for {file_name}: {e}")
            progress_callback(-1, file_size, 0, file_name=file_name, error=f"Error sending metadata: {e}")
            return

        sent_bytes = 0
        start_time = time.time()

        loop = asyncio.get_event_loop()
        # Use a thread pool for blocking file read operations.
        # This is crucial for HDDs to prevent disk I/O from blocking the async network tasks.
        # Use the max_workers value from the GUI.
        executor = ThreadPoolExecutor(max_workers=max_workers)

        try:
            with open(file_path, 'rb') as f:
                while True:
                    # Read file chunk in a separate thread using the specified chunk_size (in bytes)
                    # This is a key optimization for HDDs - large sequential reads are efficient.
                    chunk = await loop.run_in_executor(executor, f.read, chunk_size_bytes)
                    if not chunk:
                        break # End of file

                    writer.write(chunk)
                    # Use drain() to apply backpressure if the network buffer is full.
                    # This helps prevent overwhelming the network or receiver.
                    try:
                        await asyncio.wait_for(writer.drain(), timeout=10.0) # Add timeout for drain
                    except asyncio.TimeoutError:
                         print(f"Timeout sending chunk for {file_name}")
                         progress_callback(sent_bytes, file_size, 0, file_name=file_name, error="Timeout sending chunk")
                         break # Stop sending this file

                    sent_bytes += len(chunk)
                    elapsed_time = time.time() - start_time
                    speed_mbps = (sent_bytes / (1024 * 1024)) / elapsed_time if elapsed_time > 0 else 0

                    # Update GUI progress for the current file
                    progress_callback(sent_bytes, file_size, speed_mbps, file_name=file_name)

            if sent_bytes == file_size:
                print(f"\nFile '{file_name}' sent successfully.")
                progress_callback(file_size, file_size, speed_mbps, file_name=file_name) # Ensure progress is 100% for this file
            else:
                 print(f"\nFile '{file_name}' sending incomplete. Expected {file_size}, sent {sent_bytes}.")
                 progress_callback(sent_bytes, file_size, speed_mbps, file_name=file_name, error="Sending incomplete")


        except Exception as e:
            print(f"\nError during sending file '{file_name}': {e}")
            progress_callback(-1, file_size, 0, file_name=file_name, error=str(e)) # Indicate error for this file

        finally:
            # Do NOT close the writer here, as we might send more files over the same connection
            executor.shutdown(wait=False) # Shutdown executor for this file task


async def send_multiple_files(host, port, file_paths, chunk_size_bytes, max_workers, concurrent_limit, progress_callback):
    """
    Connects to the receiver and sends a list of files concurrently.
    Manages concurrency using a semaphore with the specified limit.
    """
    print(f"Sender attempting to connect to {host}:{port}")
    reader, writer = None, None # Initialize to None

    try:
        reader, writer = await asyncio.open_connection(host, port)
        print("Connection established.")

        # Create a semaphore to limit concurrent file transfers based on user input
        semaphore = asyncio.Semaphore(concurrent_limit)

        # Create a list of tasks for sending each file
        tasks = []
        for file_path in file_paths:
            if not os.path.exists(file_path):
                print(f"Skipping non-existent file: {file_path}")
                progress_callback(0, 0, 0, file_name=os.path.basename(file_path), error="File not found, skipping.")
                continue # Skip to the next file
            if not os.path.isfile(file_path):
                 print(f"Skipping non-file item: {file_path}")
                 progress_callback(0, 0, 0, file_name=os.path.basename(file_path), error="Not a file, skipping.")
                 continue # Skip if it's not a file (e.g., a directory)

            # Create a task for sending the single file and add it to the list
            # Pass the semaphore to the single file send function
            task = asyncio.create_task(send_single_file(writer, file_path, chunk_size_bytes, max_workers, progress_callback, semaphore))
            tasks.append(task)

        # Wait for all tasks to complete
        await asyncio.gather(*tasks)

    except ConnectionRefusedError:
        messagebox.showerror("Connection Error", f"Connection refused. Is the receiver running on {host}:{port}?")
        progress_callback(-1, 0, 0, error="Connection refused")
    except Exception as e:
        messagebox.showerror("Error", f"An unexpected error occurred in sender: {e}")
        progress_callback(-1, 0, 0, error=str(e))

    finally:
        if writer:
            writer.close()
            await writer.wait_closed()
            print("Sender connection closed.")


async def receive_multiple_files(reader, writer, chunk_size_bytes, max_workers, progress_callback):
    """
    Asynchronously receives multiple files over a single network connection.
    Uses configurable chunk_size (in bytes) and max_workers.
    """
    addr = writer.get_extra_info('peername')
    print(f"Connection received from {addr}")

    loop = asyncio.get_event_loop()
    # Use a thread pool for blocking file write operations.
    # This is crucial for HDDs to prevent disk I/O from blocking the async network tasks.
    # Use the max_workers value from the GUI.
    executor = ThreadPoolExecutor(max_workers=max_workers)

    try:
        while True:
            # Attempt to receive file metadata
            try:
                # Add a timeout to prevent hanging indefinitely if sender disconnects unexpectedly
                file_name_bytes = await asyncio.wait_for(reader.readline(), timeout=60.0)
                if not file_name_bytes:
                    print("\nSender closed the connection.")
                    break # Connection closed by sender

                file_name = file_name_bytes.decode().strip()
                if not file_name:
                    print("\nReceived empty filename, assuming end of transmission.")
                    break # Empty line might signal end of transmission (though connection close is better)

                file_size_bytes = await asyncio.wait_for(reader.readline(), timeout=60.0)
                if not file_size_bytes:
                     print("\nConnection closed prematurely after receiving filename.")
                     progress_callback(0, 0, 0, file_name=file_name, error="Connection closed prematurely after filename")
                     break

                file_size = int(file_size_bytes.decode().strip())

                print(f"Receiving file: {file_name} ({file_size} bytes)")
                progress_callback(0, file_size, 0, file_name=file_name) # Initialize progress for new file

                received_bytes = 0
                start_time = time.time()

                # Create a unique filename to avoid overwriting existing files
                output_file_path = file_name
                counter = 1
                while os.path.exists(output_file_path):
                    name, ext = os.path.splitext(file_name)
                    output_file_path = f"{name}_{counter}{ext}"
                    counter += 1

                with open(output_file_path, 'wb') as f:
                    while received_bytes < file_size:
                        # Read chunk from the network using the specified chunk_size (in bytes)
                        bytes_to_read = min(chunk_size_bytes, file_size - received_bytes)
                        chunk = await reader.read(bytes_to_read)
                        if not chunk:
                            # Connection closed prematurely during file transfer
                            print(f"\nConnection closed prematurely by sender during transfer of {file_name}.")
                            progress_callback(received_bytes, file_size, 0, file_name=file_name, error="Connection closed prematurely during transfer")
                            break

                        # Write file chunk in a separate thread
                        # This is a key optimization for HDDs - large sequential writes are efficient.
                        await loop.run_in_executor(executor, f.write, chunk)

                        received_bytes += len(chunk)
                        elapsed_time = time.time() - start_time
                        speed_mbps = (received_bytes / (1024 * 1024)) / elapsed_time if elapsed_time > 0 else 0

                        # Update GUI progress for the current file
                        progress_callback(received_bytes, file_size, speed_mbps, file_name=file_name)

                if received_bytes == file_size:
                    print(f"\nFile '{output_file_path}' received successfully.")
                    progress_callback(file_size, file_size, speed_mbps, file_name=file_name) # Ensure progress is 100%
                else:
                     print(f"\nFile reception incomplete for '{file_name}'. Expected {file_size}, received {received_bytes}.")
                     progress_callback(received_bytes, file_size, 0, file_name=file_name, error="File reception incomplete")

            except asyncio.TimeoutError:
                 print("\nTimeout waiting for file metadata or data.")
                 progress_callback(0, 0, 0, error="Timeout during reception")
                 break # Exit the loop on timeout
            except Exception as e:
                print(f"\nError during file reception loop: {e}")
                progress_callback(0, 0, 0, error=f"Reception error: {e}")
                break # Exit the loop on other errors

    except Exception as e:
        print(f"\nError handling connection from {addr}: {e}")
        progress_callback(0, 0, 0, error=f"Connection handling error: {e}")

    finally:
        print(f"Closing connection from {addr}")
        writer.close()
        await writer.wait_closed()
        executor.shutdown(wait=False)


# --- Discovery Logic ---

class DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, gui_app):
        super().__init__()
        self.gui_app = gui_app
        self.transport = None
        self.discovered_devices = {} # { ip: last_seen_time }

    def connection_made(self, transport):
        self.transport = transport
        print("Discovery protocol started.")
        # Start periodic broadcast
        asyncio.ensure_future(self.broadcast_presence())
        # Start cleanup task
        asyncio.ensure_future(self.cleanup_devices())


    def datagram_received(self, data, addr):
        ip, port = addr
        # Ignore messages from self
        if ip == self.gui_app.local_ip:
             return

        if data == DISCOVERY_MESSAGE:
            print(f"Received discovery message from {ip}:{port}")
            # We only care about the IP for the device list
            self.discovered_devices[ip] = time.time()
            self.gui_app.update_device_list(self.discovered_devices)


    async def broadcast_presence(self):
        """Periodically broadcasts presence."""
        while True:
            try:
                 # Attempt to send to the broadcast address of the primary interface's subnet
                 # This is complex to get reliably across platforms without external libraries.
                 # Fallback to a general broadcast address if specific subnet broadcast fails or is not determined.
                 broadcast_address = '<broadcast>' # Placeholder - ideally calculate subnet broadcast
                 s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                 s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                 s.sendto(DISCOVERY_MESSAGE, (broadcast_address, DISCOVERY_PORT))
                 s.close()
                 # print(f"Broadcasted presence to {broadcast_address}:{DISCOVERY_PORT}")
            except Exception as e:
                 # print(f"Error broadcasting presence: {e}")
                 # Fallback to a common local broadcast address if the above failed
                 try:
                     s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                     s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                     s.sendto(DISCOVERY_MESSAGE, ('192.168.1.255', DISCOVERY_PORT)) # Example subnet broadcast
                     s.close()
                     # print(f"Broadcasted presence to 192.168.1.255:{DISCOVERY_PORT}")
                 except Exception as e2:
                     # print(f"Error broadcasting presence to fallback: {e2}")
                     pass # Suppress repeated errors if broadcasting isn't working


            await asyncio.sleep(DISCOVERY_INTERVAL)

    async def cleanup_devices(self):
        """Removes devices that haven't been seen recently."""
        while True:
            current_time = time.time()
            # Remove devices not seen in the last 2 * DISCOVERY_INTERVAL
            self.discovered_devices = {
                ip: last_seen for ip, last_seen in self.discovered_devices.items()
                if current_time - last_seen < 2 * DISCOVERY_INTERVAL
            }
            self.gui_app.update_device_list(self.discovered_devices)
            await asyncio.sleep(DISCOVERY_INTERVAL)


# --- GUI Application ---

class FileTransferApp(tk.Tk):
    def __init__(self, loop):
        super().__init__()
        self.loop = loop
        self.title("Async File Transfer")
        self.geometry("700x650") # Increased window size slightly

        self.local_ip = self.get_local_ip()
        self.transfer_port = 8000 # Default transfer port

        self.mode = tk.StringVar(value="select") # "select", "sender", "receiver"
        self.files_to_send = [] # List to store file paths
        self.selected_device = tk.StringVar()

        # Variables for configurable settings
        # Store chunk size in GB for GUI input/display
        self.chunk_size_gb_var = tk.StringVar(value=str(round(DEFAULT_CHUNK_SIZE_BYTES / BYTES_TO_GB, 3)))
        self.max_workers_var = tk.StringVar(value=str(DEFAULT_MAX_WORKERS))
        self.concurrent_transfers_var = tk.StringVar(value=str(DEFAULT_CONCURRENT_TRANSFERS))

        self.create_widgets()
        self.setup_discovery()

    def get_local_ip(self):
        """Attempts to get the local IP address."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Doesn't actually connect, just finds the interface to reach this address
            s.connect(('10.254.254.254', 1))
            ip = s.getsockname()[0]
        except Exception:
            ip = '127.0.0.1' # Fallback
        finally:
            s.close()
        return ip

    def create_widgets(self):
        # Mode Selection Frame
        mode_frame = ttk.LabelFrame(self, text="Select Mode")
        mode_frame.pack(pady=10, padx=10, fill="x")

        ttk.Radiobutton(mode_frame, text="Sender", variable=self.mode, value="sender", command=self.show_mode_frame).pack(side="left", padx=5)
        ttk.Radiobutton(mode_frame, text="Receiver", variable=self.mode, value="receiver", command=self.show_mode_frame).pack(side="left", padx=5)

        # Settings Frame (Common to both modes)
        settings_frame = ttk.LabelFrame(self, text="Transfer Settings", padding="10")
        settings_frame.pack(pady=10, padx=10, fill="x")

        # Changed label to GB
        ttk.Label(settings_frame, text="Chunk Size (GB):").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        chunk_size_entry = ttk.Entry(settings_frame, textvariable=self.chunk_size_gb_var, width=20)
        chunk_size_entry.grid(row=0, column=1, padx=5, pady=5)
        # Bind validation to check for float or integer
        chunk_size_entry.bind("<KeyRelease>", self.validate_numeric_input)


        ttk.Label(settings_frame, text="Max Workers (Disk I/O):").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        max_workers_entry = ttk.Entry(settings_frame, textvariable=self.max_workers_var, width=20)
        max_workers_entry.grid(row=1, column=1, padx=5, pady=5)
        # Bind validation to check for integer
        max_workers_entry.bind("<KeyRelease>", self.validate_numeric_input)

        ttk.Label(settings_frame, text="Concurrent Transfers:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        concurrent_transfers_entry = ttk.Entry(settings_frame, textvariable=self.concurrent_transfers_var, width=20)
        concurrent_transfers_entry.grid(row=2, column=1, padx=5, pady=5)
        concurrent_transfers_entry.bind("<KeyRelease>", self.validate_numeric_input)


        # Sender Frame
        self.sender_frame = ttk.LabelFrame(self, text="Sender", padding="10")
        self.sender_frame.pack(pady=10, padx=10, fill="both", expand=True)

        ttk.Label(self.sender_frame, text="Files to Send:").grid(row=0, column=0, sticky="nw", padx=5, pady=5)
        self.files_listbox = tk.Listbox(self.sender_frame, height=10, selectmode=tk.EXTENDED) # Increased height
        self.files_listbox.grid(row=0, column=1, columnspan=2, padx=5, pady=5, sticky="ewns")

        button_frame = ttk.Frame(self.sender_frame)
        button_frame.grid(row=1, column=1, columnspan=2, pady=5)
        ttk.Button(button_frame, text="Add Files", command=self.add_files).pack(side="left", padx=5)
        # New button for adding folders
        ttk.Button(button_frame, text="Add Folder", command=self.add_folder).pack(side="left", padx=5)
        ttk.Button(button_frame, text="Remove Selected", command=self.remove_selected_files).pack(side="left", padx=5)


        ttk.Label(self.sender_frame, text="Select Receiver:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.device_listbox = tk.Listbox(self.sender_frame, height=5)
        self.device_listbox.grid(row=2, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        self.device_listbox.bind('<<ListboxSelect>>', self.on_device_select)


        ttk.Button(self.sender_frame, text="Send Files", command=self.start_sending).grid(row=3, column=1, pady=10)

        self.sender_progress = ttk.Progressbar(self.sender_frame, orient="horizontal", length=300, mode="determinate")
        self.sender_progress.grid(row=4, column=0, columnspan=3, padx=5, pady=5, sticky="ew")
        self.sender_status_label = ttk.Label(self.sender_frame, text="")
        self.sender_status_label.grid(row=5, column=0, columnspan=3, padx=5, pady=5, sticky="ew")

        # Configure grid weights for resizing
        self.sender_frame.grid_columnconfigure(1, weight=1)
        self.sender_frame.grid_rowconfigure(0, weight=1)


        # Receiver Frame
        self.receiver_frame = ttk.LabelFrame(self, text="Receiver", padding="10")
        self.receiver_frame.pack(pady=10, padx=10, fill="both", expand=True)

        ttk.Label(self.receiver_frame, text=f"Listening on IP: {self.local_ip}").pack(pady=5)
        ttk.Label(self.receiver_frame, text=f"Listening on Port: {self.transfer_port}").pack(pady=5)

        ttk.Button(self.receiver_frame, text="Start Receiving", command=self.start_receiving).pack(pady=10)

        self.receiver_progress = ttk.Progressbar(self.receiver_frame, orient="horizontal", length=300, mode="determinate")
        self.receiver_progress.pack(pady=5, fill="x")
        self.receiver_status_label = ttk.Label(self.receiver_frame, text="")
        self.receiver_status_label.pack(pady=5, fill="x")


        # Initially hide sender and receiver frames
        self.sender_frame.pack_forget()
        self.receiver_frame.pack_forget()

    def validate_numeric_input(self, event):
        """Validates that the input in the entry widgets is numeric."""
        widget = event.widget
        value = widget.get()
        try:
            if widget == self.chunk_size_gb_var._tkobj: # Check if it's the chunk size entry
                 # Allow float or integer for chunk size in GB
                float(value)
            else: # Assume it's max workers or concurrent transfers, must be integer
                int(value)

            # If successful, the input is valid so far
            widget.config(foreground="black")
        except ValueError:
            # If conversion fails, the input is not a valid number
            widget.config(foreground="red")


    def get_transfer_settings(self):
        """Gets and validates transfer settings from the GUI."""
        try:
            # Get chunk size in GB and convert to bytes
            chunk_size_gb = float(self.chunk_size_gb_var.get())
            chunk_size_bytes = int(chunk_size_gb * BYTES_TO_GB)

            max_workers = int(self.max_workers_var.get())
            concurrent_limit = int(self.concurrent_transfers_var.get())


            if chunk_size_bytes <= 0 or max_workers <= 0 or concurrent_limit <= 0:
                messagebox.showerror("Input Error", "Chunk Size, Max Workers, and Concurrent Transfers must be positive numbers.")
                return None, None, None

            return chunk_size_bytes, max_workers, concurrent_limit

        except ValueError:
            messagebox.showerror("Input Error", "Invalid input for Chunk Size (GB), Max Workers, or Concurrent Transfers. Please enter valid numbers.")
            return None, None, None


    def show_mode_frame(self):
        selected_mode = self.mode.get()
        if selected_mode == "sender":
            self.sender_frame.pack(pady=10, padx=10, fill="both", expand=True)
            self.receiver_frame.pack_forget()
        elif selected_mode == "receiver":
            self.receiver_frame.pack(pady=10, padx=10, fill="both", expand=True)
            self.sender_frame.pack_forget()
        else:
            self.sender_frame.pack_forget()
            self.receiver_frame.pack_forget()

    def add_files(self):
        """Opens a file dialog to select multiple files and adds them to the list."""
        filenames = filedialog.askopenfilenames()
        if filenames:
            for filename in filenames:
                if filename not in self.files_to_send:
                    self.files_to_send.append(filename)
                    self.files_listbox.insert(tk.END, filename)

    def add_folder(self):
        """Opens a directory dialog, finds all files within it, and adds them to the list."""
        folder_path = filedialog.askdirectory()
        if folder_path:
            print(f"Adding files from folder: {folder_path}")
            added_count = 0
            # Walk through the directory recursively
            for root, _, files in os.walk(folder_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    # Add only files and avoid duplicates
                    if os.path.isfile(file_path) and file_path not in self.files_to_send:
                        self.files_to_send.append(file_path)
                        self.files_listbox.insert(tk.END, file_path)
                        added_count += 1
            print(f"Added {added_count} files from {folder_path}")


    def remove_selected_files(self):
        """Removes the selected files from the listbox and the internal list."""
        selected_indices = self.files_listbox.curselection()
        if selected_indices:
            # Delete from listbox in reverse order to avoid index issues
            for index in sorted(selected_indices, reverse=True):
                del self.files_to_send[index]
                self.files_listbox.delete(index)


    def on_device_select(self, event):
        selected_indices = self.device_listbox.curselection()
        if selected_indices:
            index = selected_indices[0]
            device_ip = self.device_listbox.get(index)
            self.selected_device.set(device_ip)
            print(f"Selected device: {device_ip}")

    def update_device_list(self, devices):
        """Updates the listbox with discovered device IPs, excluding local IP."""
        self.device_listbox.delete(0, tk.END)
        for ip in devices.keys():
            if ip != self.local_ip: # Exclude local IP
                self.device_listbox.insert(tk.END, ip)

    def update_sender_progress(self, sent_bytes, total_bytes, speed_mbps, file_name=None, error=None):
        """Updates the sender progress bar and status label."""
        if error:
            self.sender_status_label.config(text=f"Error sending {file_name}: {error}", foreground="red")
            # self.sender_progress['value'] = 0 # Don't reset progress on single file error within batch
        elif total_bytes > 0:
            percentage = (sent_bytes / total_bytes) * 100
            self.sender_progress['value'] = percentage
            status_text = f"Sending '{file_name}': {sent_bytes}/{total_bytes} bytes | Speed: {speed_mbps:.2f} Mbps"
            self.sender_status_label.config(text=status_text)
            if sent_bytes == total_bytes:
                 self.sender_status_label.config(text=f"'{file_name}' sent successfully!", foreground="green")
        else:
             self.sender_progress['value'] = 0
             self.sender_status_label.config(text=f"Waiting to send '{file_name}'...")


    def update_receiver_progress(self, received_bytes, total_bytes, speed_mbps, file_name=None, error=None):
        """Updates the receiver progress bar and status label."""
        if error:
            self.receiver_status_label.config(text=f"Error receiving {file_name}: {error}", foreground="red")
            # self.receiver_progress['value'] = 0 # Don't reset progress on single file error within batch
        elif total_bytes > 0:
            percentage = (received_bytes / total_bytes) * 100
            self.receiver_progress['value'] = percentage
            status_text = f"Receiving '{file_name}': {received_bytes}/{total_bytes} bytes | Speed: {speed_mbps:.2f} Mbps"
            self.receiver_status_label.config(text=status_text)
            if received_bytes == total_bytes:
                self.receiver_status_label.config(text=f"'{file_name}' received successfully!", foreground="green")
        else:
             self.receiver_progress['value'] = 0
             self.receiver_status_label.config(text="Waiting for file...") # Initial state before metadata


    def start_sending(self):
        file_paths = self.files_to_send
        receiver_ip = self.selected_device.get()

        chunk_size_bytes, max_workers, concurrent_limit = self.get_transfer_settings()
        if chunk_size_bytes is None or max_workers is None or concurrent_limit is None:
            return # Validation failed

        if not file_paths:
            messagebox.showerror("Error", "Please add files to send.")
            return
        if not receiver_ip:
            messagebox.showerror("Error", "Please select a receiver device from the list.")
            return

        print(f"Starting concurrent send task for {len(file_paths)} files to {receiver_ip}:{self.transfer_port} with Chunk Size: {chunk_size_bytes} bytes, Max Workers: {max_workers}, Concurrent Limit: {concurrent_limit}")
        self.sender_status_label.config(text=f"Connecting to {receiver_ip}...")
        self.sender_progress['value'] = 0
        # Run the async send task in the asyncio loop, passing the settings
        asyncio.ensure_future(send_multiple_files(receiver_ip, self.transfer_port, file_paths, chunk_size_bytes, max_workers, concurrent_limit, self.update_sender_progress))

    def start_receiving(self):
        chunk_size_bytes, max_workers, _ = self.get_transfer_settings() # Concurrent limit not needed for receiver start
        if chunk_size_bytes is None or max_workers is None:
            return # Validation failed

        print(f"Starting receiver task on {self.local_ip}:{self.transfer_port} with Chunk Size: {chunk_size_bytes} bytes, Max Workers: {max_workers}")
        self.receiver_status_label.config(text="Listening for connections...")
        self.receiver_progress['value'] = 0
        # Run the async receiver server in the asyncio loop, passing the settings
        asyncio.ensure_future(self._async_start_receiver(self.local_ip, self.transfer_port, chunk_size_bytes, max_workers))

    async def _async_start_receiver(self, host, port, chunk_size_bytes, max_workers):
        """Async wrapper to start the receiver server."""
        try:
            # Listen for incoming connections. When a connection is made,
            # receive_multiple_files will handle receiving all files sent over that connection,
            # using the provided chunk_size (in bytes) and max_workers.
            server = await asyncio.start_server(
                lambda r, w: receive_multiple_files(r, w, chunk_size_bytes, max_workers, self.update_receiver_progress), host, port)

            addr = server.sockets[0].getsockname()
            print(f"Receiver listening on {addr}")
            self.receiver_status_label.config(text=f"Listening on {addr}...")

            async with server:
                await server.serve_forever()
        except Exception as e:
            messagebox.showerror("Error", f"An unexpected error occurred in receiver: {e}")
            self.receiver_status_label.config(text=f"Error: {e}", foreground="red")

    def setup_discovery(self):
        """Sets up the UDP discovery protocol."""
        # Need to run this in the asyncio loop
        asyncio.ensure_future(self._async_setup_discovery())

    async def _async_setup_discovery(self):
         try:
            # Create a UDP socket for broadcasting and listening
            transport, protocol = await self.loop.create_datagram_endpoint(
                lambda: DiscoveryProtocol(self),
                local_addr=('0.0.0.0', DISCOVERY_PORT), # Listen on all interfaces
                allow_broadcast=True # Allow sending and receiving broadcast messages
            )
            print("Discovery endpoint created.")
         except Exception as e:
             print(f"Error setting up discovery: {e}")
             messagebox.showwarning("Discovery Error", f"Could not set up device discovery: {e}\n"
                                                   "Manual IP entry might be required.")
             # Potentially disable discovery features in the GUI or provide manual entry options


    def run(self):
        """Runs the Tkinter main loop and the asyncio loop concurrently."""
        def async_thread():
            self.loop.run_forever()

        # Run the asyncio loop in a separate thread
        self.async_thread = threading.Thread(target=async_thread, daemon=True)
        self.async_thread.start()

        # Start the Tkinter main loop
        self.mainloop()

        # When Tkinter closes, stop the asyncio loop
        self.loop.call_soon_threadsafe(self.loop.stop)


if __name__ == "__main__":
    # Create an asyncio loop
    loop = asyncio.get_event_loop()

    # Create and run the GUI application
    app = FileTransferApp(loop)
    app.run()
