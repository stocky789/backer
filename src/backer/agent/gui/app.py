"""
Backer Windows Agent - GUI Application

A simple Windows GUI for connecting to a Backer server and managing backups.
Includes system tray support for background operation.
"""

import json
import logging
import os
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

# Add parent to path for imports when running as frozen exe
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_DIR = Path(os.environ.get('APPDATA', Path.home())) / 'Backer'
CONFIG_FILE = CONFIG_DIR / 'config.json'
LOG_DIR = CONFIG_DIR / 'logs'

# Global log file path (set after logging is initialized)
LOG_FILE: Path | None = None

# Try to import pystray for system tray support
try:
    import pystray
    from PIL import Image
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False


def create_tray_icon_image():
    """Create a simple icon image for the system tray."""
    # Create a simple 64x64 icon with a "B" on it
    try:
        # Try to load the icon file if it exists
        icon_path = APP_DIR / "backer.ico"
        if icon_path.exists():
            return Image.open(str(icon_path))
    except Exception:
        pass

    # Create a simple colored square as fallback
    img = Image.new('RGB', (64, 64), color=(0, 120, 212))
    return img


# GitHub repository URL
GITHUB_REPO_URL = "https://github.com/stocky789/backer"


class BackerAgentApp:
    """Main Backer Agent GUI Application."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Backer Agent")
        self.root.geometry("450x460")
        self.root.resizable(True, True)
        self.root.minsize(400, 400)

        # Set icon if available
        icon_path = APP_DIR / "backer.ico"
        if icon_path.exists():
            self.root.iconbitmap(str(icon_path))

        # Center window on screen
        self.center_window()

        # Load existing config
        self.config = self.load_config()

        # Agent service instance
        self.service = None

        # System tray icon
        self.tray_icon = None
        self._tray_thread = None

        # Setup menu bar first
        self.setup_menu()

        # Setup UI
        self.setup_ui()

        # Handle window close button (minimize to tray instead of closing)
        self.root.protocol("WM_DELETE_WINDOW", self.on_window_close)

        # Check connection status on startup
        if self.config.get('server_url'):
            self.root.after(500, self.check_connection)

    def center_window(self):
        """Center the window on the screen."""
        self.root.update_idletasks()
        width = self.root.winfo_width()
        height = self.root.winfo_height()
        x = (self.root.winfo_screenwidth() // 2) - (width // 2)
        y = (self.root.winfo_screenheight() // 2) - (height // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')

    def setup_menu(self):
        """Setup the menu bar."""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)

        # Main menu
        main_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="Menu", menu=main_menu)

        main_menu.add_command(label="Connect", command=self.connect_to_server)
        main_menu.add_command(label="Disconnect", command=self.disconnect_from_server)
        main_menu.add_separator()
        main_menu.add_command(label="Update", command=self.check_for_updates)
        main_menu.add_separator()
        main_menu.add_command(label="Help", command=self.open_help)
        main_menu.add_separator()
        main_menu.add_command(label="Exit", command=self.on_exit)

    def load_config(self) -> dict:
        """Load configuration from file."""
        if CONFIG_FILE.exists():
            try:
                return json.loads(CONFIG_FILE.read_text())
            except Exception:
                pass
        return {}

    def save_config(self):
        """Save configuration to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(self.config, indent=2))

    def setup_ui(self):
        """Setup the main UI."""
        # Main frame with padding
        main_frame = ttk.Frame(self.root, padding="20")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # Title
        title_label = ttk.Label(
            main_frame,
            text="Backer Agent",
            font=('Segoe UI', 18, 'bold')
        )
        title_label.pack(pady=(0, 5))

        # Subtitle
        subtitle_label = ttk.Label(
            main_frame,
            text="Connect to your Backer server to enable backups",
            font=('Segoe UI', 9),
            foreground='gray'
        )
        subtitle_label.pack(pady=(0, 20))

        # Server URL frame
        url_frame = ttk.LabelFrame(main_frame, text="Server Connection", padding="15")
        url_frame.pack(fill=tk.X, pady=(0, 15))

        # Server URL label
        url_label = ttk.Label(url_frame, text="Server Address:")
        url_label.pack(anchor=tk.W)

        # Server URL entry
        self.server_url_var = tk.StringVar(value=self.config.get('server_url', ''))
        self.server_entry = ttk.Entry(url_frame, textvariable=self.server_url_var, width=50)
        self.server_entry.pack(fill=tk.X, pady=(5, 5))

        # Placeholder text
        if not self.server_url_var.get():
            self.server_entry.insert(0, 'http://192.168.1.100:8420')
            self.server_entry.config(foreground='gray')
            self.server_entry.bind('<FocusIn>', self.on_entry_focus_in)
            self.server_entry.bind('<FocusOut>', self.on_entry_focus_out)

        # Help text
        help_label = ttk.Label(
            url_frame,
            text="Enter the IP address or hostname of your Backer server",
            font=('Segoe UI', 8),
            foreground='gray'
        )
        help_label.pack(anchor=tk.W, pady=(0, 10))

        # Connection buttons frame
        conn_btn_frame = ttk.Frame(url_frame)
        conn_btn_frame.pack(pady=(5, 0))

        # Connect button
        self.connect_btn = ttk.Button(
            conn_btn_frame,
            text="Connect",
            command=self.connect_to_server,
            width=15
        )
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 5))

        # Disconnect button (initially hidden)
        self.disconnect_btn = ttk.Button(
            conn_btn_frame,
            text="Disconnect",
            command=self.disconnect_from_server,
            width=15
        )
        # Don't pack yet - will be shown after connection

        # Status frame
        status_frame = ttk.LabelFrame(main_frame, text="Status", padding="15")
        status_frame.pack(fill=tk.X, pady=(0, 15))

        # Status indicator
        self.status_var = tk.StringVar(value="Not connected")
        self.status_label = ttk.Label(
            status_frame,
            textvariable=self.status_var,
            font=('Segoe UI', 10)
        )
        self.status_label.pack(anchor=tk.W)

        # Agent name
        self.agent_name_var = tk.StringVar(value="")
        self.agent_name_label = ttk.Label(
            status_frame,
            textvariable=self.agent_name_var,
            font=('Segoe UI', 9),
            foreground='gray'
        )
        self.agent_name_label.pack(anchor=tk.W)

        # Buttons frame
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X, pady=(10, 0))

        # Start Agent button
        self.start_btn = ttk.Button(
            button_frame,
            text="Start Agent",
            command=self.start_agent,
            width=15,
            state=tk.DISABLED
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        # Exit button
        exit_btn = ttk.Button(
            button_frame,
            text="Exit",
            command=self.on_exit,
            width=10
        )
        exit_btn.pack(side=tk.RIGHT)

        # View Logs button
        logs_btn = ttk.Button(
            button_frame,
            text="View Logs",
            command=self.open_logs,
            width=10
        )
        logs_btn.pack(side=tk.RIGHT, padx=(0, 10))

    def on_entry_focus_in(self, event):
        """Handle entry focus in - clear placeholder."""
        if self.server_entry.get() == 'http://192.168.1.100:8420':
            self.server_entry.delete(0, tk.END)
            self.server_entry.config(foreground='black')

    def on_entry_focus_out(self, event):
        """Handle entry focus out - restore placeholder if empty."""
        if not self.server_entry.get():
            self.server_entry.insert(0, 'http://192.168.1.100:8420')
            self.server_entry.config(foreground='gray')

    def connect_to_server(self):
        """Connect to the Backer server."""
        server_url = self.server_url_var.get().strip()

        # Check for placeholder
        is_placeholder = server_url == 'http://192.168.1.100:8420'
        is_gray = self.server_entry.cget('foreground') == 'gray'
        if is_placeholder and is_gray:
            messagebox.showwarning("Warning", "Please enter your server address")
            return

        if not server_url:
            messagebox.showwarning("Warning", "Please enter a server address")
            return

        # Ensure URL has protocol
        if not server_url.startswith('http://') and not server_url.startswith('https://'):
            server_url = f'http://{server_url}'
            self.server_url_var.set(server_url)

        # Ensure URL has port
        if ':8420' not in server_url and server_url.count(':') == 1:
            # Has protocol but no port
            if not server_url.rstrip('/').split(':')[-1].isdigit():
                server_url = f'{server_url.rstrip("/")}:8420'
                self.server_url_var.set(server_url)

        self.status_var.set("Connecting...")
        self.connect_btn.config(state=tk.DISABLED)

        # Run connection in background thread
        thread = threading.Thread(target=self._do_connect, args=(server_url,))
        thread.daemon = True
        thread.start()

    def _do_connect(self, server_url: str):
        """Perform connection in background thread."""
        try:
            import platform
            import socket
            import urllib.request

            # Test connection to server
            health_url = f"{server_url.rstrip('/')}/health"
            req = urllib.request.Request(health_url, method='GET')
            req.add_header('User-Agent', 'Backer-Agent/1.0')

            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status != 200:
                    raise Exception(f"Server returned status {response.status}")

            # Get machine info for registration
            hostname = socket.gethostname()
            agent_name = f"{hostname}"

            # Register with server
            register_url = f"{server_url.rstrip('/')}/api/v1/clients/register"
            data = json.dumps({
                'hostname': hostname,
                'version': '1.0.0',
                'os_info': f"{platform.system()} {platform.release()}",
            }).encode('utf-8')

            req = urllib.request.Request(register_url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('User-Agent', 'Backer-Agent/1.0')

            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                client_id = result.get('client_id', 'unknown')
                client_secret = result.get('client_secret', '')

            # Save config (including secret for future auth)
            self.config['server_url'] = server_url
            self.config['client_id'] = client_id
            self.config['client_secret'] = client_secret
            self.config['hostname'] = hostname
            self.save_config()

            # Update UI from main thread
            self.root.after(0, lambda: self._connect_success(agent_name))

        except urllib.error.URLError as e:
            error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
            self.root.after(0, lambda m=error_msg: self._connect_failed(f"Cannot reach server: {m}"))
        except Exception as e:
            err_str = str(e)
            self.root.after(0, lambda m=err_str: self._connect_failed(m))

    def _connect_success(self, agent_name: str):
        """Handle successful connection."""
        self.status_var.set("Connected")
        self.status_label.config(foreground='green')
        self.agent_name_var.set(f"Registered as: {agent_name}")
        self.connect_btn.config(state=tk.NORMAL, text="Reconnect")
        self.disconnect_btn.pack(side=tk.LEFT)  # Show disconnect button
        self.start_btn.config(state=tk.NORMAL)
        msg = (
            f"Successfully connected to server!\n\n"
            f"This machine is now registered as '{agent_name}'\n\n"
            f"Click 'Start Agent' to begin receiving backup jobs."
        )
        messagebox.showinfo("Success", msg)

    def _connect_failed(self, error: str):
        """Handle failed connection."""
        self.status_var.set("Connection failed")
        self.status_label.config(foreground='red')
        self.agent_name_var.set("")
        self.connect_btn.config(state=tk.NORMAL, text="Connect")
        self.disconnect_btn.pack_forget()  # Hide disconnect button
        messagebox.showerror("Connection Failed", f"Could not connect to server:\n\n{error}")

    def disconnect_from_server(self):
        """Disconnect from the Backer server and clear saved config."""
        # Stop agent if running
        if self.service is not None:
            if not messagebox.askyesno(
                "Confirm Disconnect",
                "The agent is currently running.\n\n"
                "Disconnecting will stop the agent and clear the server configuration.\n\n"
                "Continue?"
            ):
                return
            # Stop the service
            self.service.stop()
            self.service = None
            self.start_btn.config(text="Start Agent", state=tk.DISABLED)
        else:
            if not messagebox.askyesno(
                "Confirm Disconnect",
                "This will clear the saved server configuration.\n\n"
                "Continue?"
            ):
                return

        # Clear config
        self.config.pop('server_url', None)
        self.config.pop('client_id', None)
        self.config.pop('client_secret', None)
        self.config.pop('hostname', None)
        self.save_config()

        # Reset UI
        self.server_url_var.set('')
        self.server_entry.delete(0, tk.END)
        self.server_entry.insert(0, 'http://192.168.1.100:8420')
        self.server_entry.config(foreground='gray')
        self.status_var.set("Not connected")
        self.status_label.config(foreground='black')
        self.agent_name_var.set("")
        self.connect_btn.config(text="Connect", state=tk.NORMAL)
        self.disconnect_btn.pack_forget()  # Hide disconnect button
        self.start_btn.config(state=tk.DISABLED)

        # Stop tray icon if running
        if self.tray_icon:
            try:
                self.tray_icon.stop()
                self.tray_icon = None
            except Exception:
                pass

        messagebox.showinfo("Disconnected", "Successfully disconnected from server.")

    def check_connection(self):
        """Check if already connected to server."""
        server_url = self.config.get('server_url')
        if not server_url:
            return

        self.status_var.set("Checking connection...")

        def check():
            try:
                import urllib.request
                health_url = f"{server_url.rstrip('/')}/health"
                req = urllib.request.Request(health_url, method='GET')
                req.add_header('User-Agent', 'Backer-Agent/1.0')

                with urllib.request.urlopen(req, timeout=5) as response:
                    if response.status == 200:
                        hostname = self.config.get('hostname', 'Unknown')
                        self.root.after(0, lambda: self._check_success(hostname))
                    else:
                        self.root.after(0, self._check_failed)
            except Exception:
                self.root.after(0, self._check_failed)

        thread = threading.Thread(target=check)
        thread.daemon = True
        thread.start()

    def _check_success(self, hostname: str):
        """Handle successful connection check."""
        self.status_var.set("Connected")
        self.status_label.config(foreground='green')
        self.agent_name_var.set(f"Registered as: {hostname}")
        self.connect_btn.config(text="Reconnect")
        self.disconnect_btn.pack(side=tk.LEFT)  # Show disconnect button
        self.start_btn.config(state=tk.NORMAL)

    def _check_failed(self):
        """Handle failed connection check."""
        self.status_var.set("Not connected")
        self.status_label.config(foreground='black')
        self.agent_name_var.set("Previously configured server is unreachable")

    def start_agent(self):
        """Start the agent background service."""
        if not self.config.get('server_url') or not self.config.get('client_id'):
            messagebox.showwarning("Warning", "Please connect to a server first")
            return

        if self.service is not None:
            messagebox.showinfo("Info", "Agent is already running")
            return

        # Disable button and show starting status
        self.start_btn.config(text="Starting...", state=tk.DISABLED)
        self.status_var.set("Initializing agent...")
        self.status_label.config(foreground='blue')

        # Run initialization in background thread
        thread = threading.Thread(target=self._do_start_agent, daemon=True)
        thread.start()

    def _do_start_agent(self):
        """Perform agent startup in background thread."""
        try:
            logging.info("Starting agent service...")
            logging.info(f"Server URL: {self.config['server_url']}")
            logging.info(f"Client ID: {self.config['client_id']}")

            from backer.agent.service import AgentService

            self.service = AgentService(
                server_url=self.config['server_url'],
                client_id=self.config['client_id'],
                client_secret=self.config.get('client_secret', ''),
                status_callback=self._on_service_status,
            )

            logging.info(f"Agent tools directory: {self.service.tools_dir}")

            # Automatically download/verify backup tools
            self.root.after(0, lambda: self.status_var.set("Checking backup tools..."))

            def tool_progress(msg: str):
                """Update UI with tool download progress."""
                self.root.after(0, lambda: self.agent_name_var.set(msg))

            tool_results = self.service.ensure_tools_installed(progress_callback=tool_progress)

            # Check if all tools are ready
            failed_tools = [t for t, ready in tool_results.items() if not ready]
            if failed_tools:
                error_msg = f"Failed to install backup tools: {', '.join(failed_tools)}"
                logging.error(error_msg)
                self.root.after(0, lambda: self._start_agent_failed(error_msg))
                return

            # Tools are ready, start the service
            self.root.after(0, lambda: self.status_var.set("Starting agent service..."))
            self.service.start()

            logging.info("Agent service started successfully")
            self.root.after(0, self._start_agent_success)

        except Exception as e:
            logging.error(f"Failed to start agent: {e}", exc_info=True)
            err_str = str(e)
            self.root.after(0, lambda m=err_str: self._start_agent_failed(m))

    def _start_agent_success(self):
        """Handle successful agent startup."""
        self.start_btn.config(text="Running...", state=tk.DISABLED)
        self.status_var.set("Agent running - waiting for jobs")
        self.status_label.config(foreground='green')
        self.agent_name_var.set("All backup tools ready")

        # Setup system tray icon
        self.setup_tray_icon()

        # Show notification about tray
        if TRAY_AVAILABLE:
            messagebox.showinfo(
                "Agent Started",
                "Backer Agent is now running!\n\n"
                "You can close this window - the agent will continue running in the background.\n\n"
                "Look for the Backer icon in your system tray to access the agent."
            )

    def _start_agent_failed(self, error: str):
        """Handle failed agent startup."""
        self.service = None
        self.start_btn.config(text="Start Agent", state=tk.NORMAL)
        self.status_var.set("Failed to start")
        self.status_label.config(foreground='red')
        self.agent_name_var.set("")
        messagebox.showerror(
            "Startup Failed",
            f"Failed to start agent:\n\n{error}\n\n"
            f"Check the log file for details:\n{LOG_FILE}"
        )

    def _on_service_status(self, status: str):
        """Callback for service status updates."""
        # Update UI from main thread
        self.root.after(0, lambda: self.agent_name_var.set(status))

        # Also update tray icon tooltip if available
        if self.tray_icon:
            try:
                self.tray_icon.title = f"Backer Agent - {status}"
            except Exception:
                pass

    def setup_tray_icon(self):
        """Setup system tray icon."""
        if not TRAY_AVAILABLE:
            logging.warning("System tray not available (pystray/pillow not installed)")
            return

        try:
            # Create the tray icon
            image = create_tray_icon_image()

            menu = pystray.Menu(
                pystray.MenuItem("Show Window", self._tray_show_window, default=True),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Status: Running", None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("View Logs", self._tray_open_logs),
                pystray.MenuItem("Exit", self._tray_exit),
            )

            self.tray_icon = pystray.Icon(
                "backer_agent",
                image,
                "Backer Agent - Running",
                menu
            )

            # Run tray icon in a separate thread
            self._tray_thread = threading.Thread(target=self.tray_icon.run, daemon=True)
            self._tray_thread.start()

            logging.info("System tray icon created successfully")

        except Exception as e:
            logging.error(f"Failed to create system tray icon: {e}")
            self.tray_icon = None

    def _tray_show_window(self, icon=None, item=None):
        """Show the main window from tray."""
        self.root.after(0, self._show_window)

    def _show_window(self):
        """Show and focus the main window."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_open_logs(self, icon=None, item=None):
        """Open logs folder from tray."""
        self.root.after(0, self.open_logs)

    def _tray_exit(self, icon=None, item=None):
        """Exit application from tray."""
        self.root.after(0, self.on_exit)

    def open_logs(self):
        """Open the logs folder in file explorer."""
        import subprocess

        LOG_DIR.mkdir(parents=True, exist_ok=True)

        if sys.platform == 'win32':
            # Windows: open folder in explorer (use shell=True to avoid console window)
            subprocess.Popen(
                ['explorer', str(LOG_DIR)],
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )
        elif sys.platform == 'darwin':
            # macOS
            subprocess.Popen(['open', str(LOG_DIR)])
        else:
            # Linux
            subprocess.Popen(['xdg-open', str(LOG_DIR)])

        # Also show info about log location
        if LOG_FILE and LOG_FILE.exists():
            messagebox.showinfo(
                "Log Files",
                f"Log folder opened.\n\nCurrent log file:\n{LOG_FILE}\n\n"
                f"Check this file if backups are not working as expected."
            )
        else:
            messagebox.showinfo("Log Files", f"Log folder: {LOG_DIR}")

    def open_help(self):
        """Open the GitHub repository in a web browser."""
        import webbrowser
        webbrowser.open(GITHUB_REPO_URL)

    def check_for_updates(self):
        """Check for updates and offer to install them."""
        # Check if agent is running
        if self.service is not None:
            if not messagebox.askyesno(
                "Update",
                "The agent is currently running.\n\n"
                "Updating will stop the agent and restart it after the update.\n\n"
                "Continue?"
            ):
                return

        # Confirm update
        if not messagebox.askyesno(
            "Update Backer Agent",
            "This will download and install the latest version of Backer Agent from GitHub.\n\n"
            "The application will restart after the update.\n\n"
            "Continue?"
        ):
            return

        # Show progress
        self.status_var.set("Updating...")
        self.status_label.config(foreground='blue')
        self.agent_name_var.set("Downloading latest version...")

        # Run update in background thread
        thread = threading.Thread(target=self._do_update, daemon=True)
        thread.start()

    def _do_update(self):
        """Perform the update in a background thread."""
        try:
            import subprocess
            import urllib.request

            # Stop the service if running
            if self.service is not None:
                self.root.after(0, lambda: self.agent_name_var.set("Stopping agent..."))
                self.service.stop()
                self.service = None

            self.root.after(0, lambda: self.agent_name_var.set("Downloading update..."))

            if sys.platform == 'win32':
                # Windows: Download the latest installer and run it
                installer_url = f"{GITHUB_REPO_URL}/releases/latest/download/backer-agent-setup.exe"
                installer_path = CONFIG_DIR / "backer-agent-setup.exe"

                CONFIG_DIR.mkdir(parents=True, exist_ok=True)

                # Download the installer
                logging.info(f"Downloading installer from {installer_url}")
                urllib.request.urlretrieve(installer_url, str(installer_path))

                if not installer_path.exists():
                    raise Exception("Failed to download installer")

                logging.info(f"Installer downloaded to {installer_path}")
                self.root.after(0, lambda: self.agent_name_var.set("Running installer..."))

                # Run the installer silently and exit this app
                # The /S flag runs it silently, installer will replace files and restart
                subprocess.Popen(
                    [str(installer_path), '/S'],
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
                )

                # Give installer time to start
                import time
                time.sleep(1)

                # Exit this instance - installer will handle the rest
                self.root.after(0, self._exit_for_update)

            else:
                # Linux: Use pip to upgrade
                self.root.after(0, lambda: self.agent_name_var.set("Upgrading via pip..."))

                result = subprocess.run(
                    [sys.executable, '-m', 'pip', 'install', '--upgrade',
                     f'git+{GITHUB_REPO_URL}.git@main'],
                    capture_output=True,
                    text=True
                )

                if result.returncode != 0:
                    raise Exception(f"pip upgrade failed: {result.stderr}")

                logging.info("Update installed successfully")
                self.root.after(0, self._update_success)

        except urllib.error.HTTPError as e:
            if e.code == 404:
                error_msg = "No release found. The installer may not be available yet."
            else:
                error_msg = f"Download failed: HTTP {e.code}"
            logging.error(error_msg)
            self.root.after(0, lambda m=error_msg: self._update_failed(m))
        except Exception as e:
            error_msg = str(e)
            logging.error(f"Update failed: {error_msg}")
            self.root.after(0, lambda m=error_msg: self._update_failed(m))

    def _exit_for_update(self):
        """Exit the application for update (Windows installer will restart)."""
        # Stop tray icon
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        self.root.quit()
        sys.exit(0)

    def _update_success(self):
        """Handle successful update."""
        self.status_var.set("Update complete")
        self.status_label.config(foreground='green')
        self.agent_name_var.set("")

        messagebox.showinfo(
            "Update Complete",
            "Backer Agent has been updated successfully!\n\n"
            "Please restart the application to use the new version."
        )

    def _update_failed(self, error: str):
        """Handle failed update."""
        self.status_var.set("Update failed")
        self.status_label.config(foreground='red')
        self.agent_name_var.set("")

        messagebox.showerror(
            "Update Failed",
            f"Failed to update Backer Agent:\n\n{error}\n\n"
            f"You can manually download the latest version from:\n{GITHUB_REPO_URL}/releases"
        )

    def on_window_close(self):
        """Handle window close button - minimize to tray if agent is running."""
        if self.service is not None and TRAY_AVAILABLE and self.tray_icon is not None:
            # Agent is running and tray is available - hide to tray
            self.root.withdraw()
            logging.info("Window minimized to system tray")
        else:
            # No agent running or no tray - ask to confirm exit
            self.on_exit()

    def on_exit(self):
        """Handle exit - stop service if running."""
        if self.service is not None:
            if not messagebox.askyesno(
                "Confirm Exit",
                "The Backer Agent is currently running.\n\n"
                "Exiting will stop the agent and no backups will be performed until you restart it.\n\n"
                "Are you sure you want to exit?"
            ):
                return

        logging.info("Backer Agent GUI shutting down")

        # Stop the tray icon
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass

        # Stop the service
        if self.service:
            self.service.stop()

        self.root.quit()

    def run(self):
        """Run the application."""
        self.root.mainloop()


def main():
    """Main entry point."""
    global LOG_FILE

    # Initialize logging before anything else
    try:
        from backer.agent.service import setup_agent_logging
        LOG_FILE = setup_agent_logging(LOG_DIR)
        logging.info(f"Backer Agent GUI starting - log file: {LOG_FILE}")
    except Exception as e:
        # Fallback if logging setup fails
        print(f"Warning: Could not initialize logging: {e}")

    app = BackerAgentApp()
    app.run()


if __name__ == '__main__':
    main()
