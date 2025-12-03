"""
Backer Windows Agent - GUI Application

A simple Windows GUI for connecting to a Backer server and managing backups.
"""

import json
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
import sys
import os

# Add parent to path for imports when running as frozen exe
if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

CONFIG_DIR = Path(os.environ.get('APPDATA', Path.home())) / 'Backer'
CONFIG_FILE = CONFIG_DIR / 'config.json'


class BackerAgentApp:
    """Main Backer Agent GUI Application."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Backer Agent")
        self.root.geometry("450x350")
        self.root.resizable(False, False)

        # Set icon if available
        icon_path = APP_DIR / "backer.ico"
        if icon_path.exists():
            self.root.iconbitmap(str(icon_path))

        # Center window on screen
        self.center_window()

        # Load existing config
        self.config = self.load_config()

        # Setup UI
        self.setup_ui()

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
        help_label.pack(anchor=tk.W)

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

        # Connect button
        self.connect_btn = ttk.Button(
            button_frame,
            text="Connect",
            command=self.connect_to_server,
            width=15
        )
        self.connect_btn.pack(side=tk.LEFT, padx=(0, 10))

        # Install Service button
        self.install_btn = ttk.Button(
            button_frame,
            text="Install Service",
            command=self.install_service,
            width=15,
            state=tk.DISABLED
        )
        self.install_btn.pack(side=tk.LEFT, padx=(0, 10))

        # Exit button
        exit_btn = ttk.Button(
            button_frame,
            text="Exit",
            command=self.root.quit,
            width=10
        )
        exit_btn.pack(side=tk.RIGHT)

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
        if server_url == 'http://192.168.1.100:8420' and self.server_entry.cget('foreground') == 'gray':
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
            import urllib.request
            import socket
            import platform

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
                'os': platform.system(),
                'os_version': platform.version(),
                'agent_version': '1.0.0'
            }).encode('utf-8')

            req = urllib.request.Request(register_url, data=data, method='POST')
            req.add_header('Content-Type', 'application/json')
            req.add_header('User-Agent', 'Backer-Agent/1.0')

            with urllib.request.urlopen(req, timeout=10) as response:
                result = json.loads(response.read().decode('utf-8'))
                client_id = result.get('client_id', 'unknown')

            # Save config
            self.config['server_url'] = server_url
            self.config['client_id'] = client_id
            self.config['hostname'] = hostname
            self.save_config()

            # Update UI from main thread
            self.root.after(0, lambda: self._connect_success(agent_name))

        except urllib.error.URLError as e:
            error_msg = str(e.reason) if hasattr(e, 'reason') else str(e)
            self.root.after(0, lambda: self._connect_failed(f"Cannot reach server: {error_msg}"))
        except Exception as e:
            self.root.after(0, lambda: self._connect_failed(str(e)))

    def _connect_success(self, agent_name: str):
        """Handle successful connection."""
        self.status_var.set("Connected")
        self.status_label.config(foreground='green')
        self.agent_name_var.set(f"Registered as: {agent_name}")
        self.connect_btn.config(state=tk.NORMAL, text="Reconnect")
        self.install_btn.config(state=tk.NORMAL)
        messagebox.showinfo("Success", f"Successfully connected to server!\n\nThis machine is now registered as '{agent_name}'")

    def _connect_failed(self, error: str):
        """Handle failed connection."""
        self.status_var.set("Connection failed")
        self.status_label.config(foreground='red')
        self.agent_name_var.set("")
        self.connect_btn.config(state=tk.NORMAL)
        messagebox.showerror("Connection Failed", f"Could not connect to server:\n\n{error}")

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
        self.install_btn.config(state=tk.NORMAL)

    def _check_failed(self):
        """Handle failed connection check."""
        self.status_var.set("Not connected")
        self.status_label.config(foreground='black')
        self.agent_name_var.set("Previously configured server is unreachable")

    def install_service(self):
        """Install the agent as a Windows service."""
        if not self.config.get('server_url'):
            messagebox.showwarning("Warning", "Please connect to a server first")
            return

        result = messagebox.askyesno(
            "Install Service",
            "This will install Backer Agent as a Windows service that runs automatically.\n\n"
            "The service will:\n"
            "- Start automatically when Windows boots\n"
            "- Run backups on schedule from the server\n"
            "- Report status to the Backer server\n\n"
            "Administrator privileges are required.\n\n"
            "Continue?"
        )

        if not result:
            return

        try:
            # For now, show instructions - actual service install requires admin
            if getattr(sys, 'frozen', False):
                exe_path = sys.executable
            else:
                exe_path = "backer-agent.exe"

            messagebox.showinfo(
                "Install Service",
                f"To install as a service, run this command as Administrator:\n\n"
                f'sc create BackerAgent binPath= "{exe_path} --service" start= auto\n'
                f'sc start BackerAgent\n\n'
                f"Or use the included install-service.bat file."
            )
        except Exception as e:
            messagebox.showerror("Error", f"Failed to install service: {e}")

    def run(self):
        """Run the application."""
        self.root.mainloop()


def main():
    """Main entry point."""
    app = BackerAgentApp()
    app.run()


if __name__ == '__main__':
    main()
