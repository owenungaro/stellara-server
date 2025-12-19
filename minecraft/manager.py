import json
from pathlib import Path
from typing import Optional

from .server_process import MinecraftServerProcess


class ServerManager:
    """
    Manages multiple Minecraft server instances.
    
    Handles server lifecycle, metadata persistence, and automatic startup
    of servers marked with autostart=True.
    """
    
    def __init__(self):
        """Initialize the server manager and load persisted metadata."""
        self._servers: dict[str, MinecraftServerProcess] = {}
        self._metadata: dict[str, dict] = {}
        
        # Path to servers.json in the same directory as this module
        self._metadata_file = Path(__file__).parent / "servers.json"
        
        # Load metadata from disk
        self._load_from_disk()
        
        # Automatically start servers marked with autostart=True
        self._autostart_servers()
    
    def _load_from_disk(self) -> None:
        """Load server metadata from servers.json if it exists."""
        if not self._metadata_file.exists():
            return
        
        try:
            with open(self._metadata_file, "r", encoding="utf-8") as f:
                self._metadata = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            # If file is corrupted or unreadable, start with empty metadata
            print(f"[ServerManager] Warning: Failed to load servers.json: {e}")
            self._metadata = {}
    
    def _autostart_servers(self) -> None:
        """Start all servers marked with autostart=True."""
        for server_id, meta in self._metadata.items():
            if meta.get("autostart", False):
                try:
                    self.start_server(server_id)
                except Exception as e:
                    print(f"[ServerManager] Failed to autostart server '{server_id}': {e}")
    
    def start_server(self, server_id: str) -> None:
        """
        Start a server from its stored metadata.
        
        Args:
            server_id: Server identifier
        
        Raises:
            ValueError: If server_id is unknown (not in metadata)
        """
        # Check if server_id exists in metadata
        if server_id not in self._metadata:
            raise ValueError(f"Server '{server_id}' is unknown")
        
        # Do nothing if server is already running
        existing_server = self._servers.get(server_id)
        if existing_server and existing_server.is_running():
            return
        
        # Read metadata for the server
        meta = self._metadata[server_id]
        server_dir = Path(meta["server_dir"])
        java_cmd = meta["java_cmd"]
        
        # Create server directory if it doesn't exist
        server_dir.mkdir(parents=True, exist_ok=True)
        
        # Start a new MinecraftServerProcess
        server = MinecraftServerProcess(server_id, server_dir, java_cmd)
        
        # Register it in self._servers
        self._servers[server_id] = server
    
    def create_server(
        self,
        server_id: str,
        server_dir: str,
        java_cmd: list[str],
        autostart: bool = True
    ) -> None:
        """
        Create a new server instance.
        
        Args:
            server_id: Unique identifier for the server
            server_dir: Directory where the server should run
            java_cmd: Full command to launch the server
            autostart: Whether to automatically start the server on manager init
        
        Raises:
            ValueError: If server_id already exists
        """
        if server_id in self._metadata:
            raise ValueError(f"Server '{server_id}' already exists")
        
        # Convert server_dir to Path for consistency
        server_dir_path = Path(server_dir)
        
        # Create server directory if missing
        server_dir_path.mkdir(parents=True, exist_ok=True)
        
        # Create server process
        server = MinecraftServerProcess(server_id, server_dir_path, java_cmd)
        
        # Register server
        self._servers[server_id] = server
        
        # Store metadata
        self._metadata[server_id] = {
            "server_id": server_id,
            "server_dir": str(server_dir_path),
            "java_cmd": java_cmd,
            "autostart": autostart,
        }
        
        # Persist metadata to disk
        self.save_to_disk()
    
    def get_server(self, server_id: str) -> Optional[MinecraftServerProcess]:
        """
        Get a running server instance by ID.
        
        Args:
            server_id: Server identifier
        
        Returns:
            MinecraftServerProcess instance if found and running, None otherwise
        """
        return self._servers.get(server_id)
    
    def list_servers(self) -> list[dict]:
        """
        Get metadata for all registered servers.
        
        Returns:
            List of metadata dictionaries
        """
        return list(self._metadata.values())
    
    def stop_server(self, server_id: str) -> None:
        """
        Gracefully stop a server if it's running.
        
        Removes the server from the running servers registry.
        Does not delete metadata - the server remains registered.
        
        Args:
            server_id: Server identifier
        """
        server = self._servers.get(server_id)
        if server and server.is_running():
            try:
                server.stop()
            except Exception as e:
                print(f"[ServerManager] Error stopping server '{server_id}': {e}")
            finally:
                # Remove from running servers registry
                self._servers.pop(server_id, None)
    
    def save_to_disk(self) -> None:
        """Write current metadata to servers.json."""
        try:
            with open(self._metadata_file, "w", encoding="utf-8") as f:
                json.dump(self._metadata, f, indent=2, ensure_ascii=False)
        except IOError as e:
            print(f"[ServerManager] Failed to save servers.json: {e}")

