import winpty
import threading
import collections
import time
import subprocess
from pathlib import Path


class MinecraftServerProcess:
    """
    Manages a Minecraft server process running in a PTY.
    
    This class owns the PTY and maintains process state independently
    of any WebSocket connections. Logs continue accumulating even
    when no clients are attached.
    """
    
    def __init__(self, server_id: str, server_dir: Path, java_cmd: list[str]):
        """
        Initialize and spawn the Minecraft server process.
        
        Args:
            server_id: Unique identifier for this server instance
            server_dir: Directory where the server should run
            java_cmd: Full command to launch the server (e.g., ["java", "-Xmx2G", "-jar", "server.jar"])
        """
        self.server_id = server_id
        self.server_dir = server_dir
        self.java_cmd = java_cmd

        self._partial_line = ""
        
        # Rolling log buffer (max 5000 lines)
        self.log_buffer = collections.deque(maxlen=5000)
        self._log_lock = threading.Lock()
        
        # Spawn the process in a PTY
        # Convert command list to properly formatted Windows command string
        # This handles arguments with spaces and special characters correctly
        cmd_str = subprocess.list2cmdline(java_cmd)
        self.process = winpty.PtyProcess.spawn(cmd_str, cwd=str(self.server_dir))
        
        # Start background thread for reading stdout
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._running = True
        self._stdout_thread.start()
    
    def _read_stdout(self):
        """
        Background thread that continuously reads from stdout and appends
        complete lines to the log buffer. Handles partial lines correctly.
        """
        try:
            while self._running:
                try:
                    data = self.process.read(4096)
                    if not data:
                        break

                    if isinstance(data, bytes):
                        data = data.decode(errors="ignore")

                    # Accumulate data
                    self._partial_line += data

                    # Extract complete lines
                    while "\n" in self._partial_line:
                        line, self._partial_line = self._partial_line.split("\n", 1)
                        with self._log_lock:
                            self.log_buffer.append(line + "\n")

                except Exception:
                    break
        finally:
            # Flush any remaining partial line
            if self._partial_line:
                with self._log_lock:
                    self.log_buffer.append(self._partial_line)
                self._partial_line = ""

            self._running = False

    
    def write(self, input: str) -> None:
        """
        Write input to the server's stdin.
        
        Args:
            input: String to send to the server (newline will be added if not present)
        """
        if not self.is_running():
            return
        
        try:
            # Ensure input ends with newline
            if not input.endswith("\n"):
                input = input + "\n"
            self.process.write(input)
        except Exception:
            # Process may have terminated
            pass
    
    def get_logs(self) -> str:
        """
        Get the current log buffer as a single string.
        
        Returns:
            All log lines concatenated together
        """
        with self._log_lock:
            return "".join(self.log_buffer)
    
    def get_log_lines(self) -> list[str]:
        """
        Get a snapshot of the current log buffer as a list of lines.
        
        Returns:
            List of log lines (each line includes its newline if present)
        """
        with self._log_lock:
            return list(self.log_buffer)
    
    def is_running(self) -> bool:
        try:
            return self.process.isalive()
        except Exception:
            return False

    
    def stop(self) -> None:
        """
        Gracefully stop the server by sending "stop" command, then wait.
        If the server doesn't stop within a reasonable time, kill it forcefully.
        """
        if not self.is_running():
            return
        
        try:
            # Send stop command
            self.write("stop")
            
            # Wait for graceful shutdown (max 30 seconds)
            timeout = 30
            start_time = time.time()
            
            while self.is_running() and (time.time() - start_time) < timeout:
                time.sleep(0.5)
            
            # If still running, force kill
            if self.is_running():
                try:
                    self.process.kill()
                except Exception:
                    pass
        except Exception:
            # If anything fails, try to kill anyway
            try:
                self.process.kill()
            except Exception:
                pass
        finally:
            self._running = False

