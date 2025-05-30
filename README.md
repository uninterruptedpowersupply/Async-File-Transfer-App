Async File Transfer App
-----------------------

This project presents a high-performance, asynchronous file transfer application designed for rapid and efficient data exchange across local area networks. Built with a user-friendly graphical interface, it is specifically engineered to handle the complexities of transferring large files and is optimized with considerations for traditional Hard Disk Drives (HDDs), aiming to maximize throughput even on mechanical storage. The application provides a robust solution for users needing to move significant amounts of data between computers on the same network segment with speed and reliability.

![image](https://github.com/user-attachments/assets/7c5fd797-c3da-4c04-9d54-33be59594f0f)
![image](https://github.com/user-attachments/assets/385c7afd-8cf8-4339-b866-732528b4f2a6)


Overview
-------------

This Python application provides a simple yet powerful solution for transferring multiple files and entire folders between computers on the same local network (LAN). Leveraging asyncio for non-blocking network operations and a ThreadPoolExecutor for concurrent disk I/O, it's designed to maximize throughput and effectively utilize high-speed connections like Gigabit or 2Gbps Ethernet, while also incorporating optimizations beneficial for HDD performance.The intuitive GUI allows users to easily select files and folders, discover other instances of the application running on the network, and configure key transfer parameters like chunk size, disk I/O worker threads, and the number of concurrent file transfers.

Features
--------------------------

Asynchronous Network Transfer: Utilizes asyncio for efficient, non-blocking data transfer over TCP sockets.Multithreaded Disk I/O: Offloads blocking file read/write operations to a thread pool (ThreadPoolExecutor) to prevent the main event loop from freezing, crucial for performance with HDDs.Configurable Chunk Size: Allows users to adjust the size of data chunks read from/written to disk and sent over the network (input in GB), a key factor for optimizing HDD sequential transfer speeds.Configurable Disk I/O Workers: Control the number of threads used for disk operations to fine-tune performance based on storage device capabilities.Configurable Concurrent Transfers: Set the limit on how many files are sent simultaneously over a single connection, balancing concurrency with system resources and network conditions.Local Network Discovery: Automatically discovers other instances of the application running on the same LAN segment using UDP broadcast, displaying them in the sender's interface.GUI Interface: User-friendly graphical interface built with tkinter.Multi-File and Folder Support: Easily add multiple individual files or all files from selected folders to the transfer queue.Progress Monitoring: Displays real-time transfer progress, including current file, bytes transferred, total size, and speed (Mbps).Error Handling: Basic error handling for network issues and file operations.

Usage
----------------

Clone or Download: Get the script file_transfer_gui.py.Run on Both Machines: Execute the script on both computers connected to the same LAN:python file_transfer_gui.py

Select Mode: Choose "Sender" on one machine and "Receiver" on the other.Configure Settings: (Optional) Adjust the transfer settings (Chunk Size, Max Workers, Concurrent Transfers) in the GUI.Receiver: Click "Start Receiving". The app will listen for incoming connections and broadcast its presence for discovery.Sender:Use "Add Files" and/or "Add Folder" to populate the list of files to send.Select the receiver's IP address from the "Select Receiver" list (discovered automatically).Click "Send Files".Files will be transferred sequentially from the sender's list, with multiple files being processed concurrently based on the configured limit. Received files will be saved in the directory where the receiver script is running.Building an ExecutableYou can use PyInstaller to create a standalone executable:pip install pyinstaller
pyinstaller --onefile --windowed file_transfer_gui.py

The executable will be found in the dist folder.

Contributing

Feel free to fork the repository, open issues, or submit pull requests to improve the application.

License
----------------

 Apache 2.0]
