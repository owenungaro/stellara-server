import asyncio
import os
import string
from pathlib import Path

from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import winpty

from minecraft.manager import ServerManager

# -----------------------------
# App
# -----------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Minecraft Server Manager
# -----------------------------
manager = ServerManager()

# Track connected WebSockets per server
# Maps server_id -> dict of WebSocket -> last_sent_line_index
_server_websockets: dict[str, dict[WebSocket, int]] = {}

# Track broadcaster tasks per server
# Maps server_id -> asyncio.Task
_server_broadcasters: dict[str, asyncio.Task] = {}

_websocket_lock = asyncio.Lock()

# -----------------------------
# Helpers (Windows "This PC" style)
# -----------------------------
def list_drives():
    drives = []
    for letter in string.ascii_uppercase:
        root = Path(f"{letter}:/")
        if root.exists():
            drives.append(
                {
                    "name": f"{letter}:",
                    "path": f"{letter}:",
                    "is_dir": True,
                    "kind": "drive",
                }
            )
    return drives


def to_api_path(p: Path) -> str:
    # Return paths in forward-slash style so your React splitting works.
    # Example: E:\Projects\server -> E:/Projects/server
    return p.as_posix()


def log_fs(action: str, path: str, status: str, detail: str = "") -> None:
    msg = f"[fs] action={action} path='{path}' status={status}"
    if detail:
        msg = f"{msg} detail={detail}"
    print(msg)


def resolve_path(path: str) -> Path:
    """
    Accepts:
      - "" -> special handled by caller (This PC)
      - "E:" -> drive root
      - "E:/Projects/server" -> normal absolute-ish path
      - "E:\\Projects\\server" -> normalize to forward slashes
    """
    if path is None:
        raise HTTPException(status_code=400, detail="Missing path")

    raw = path.strip()
    if raw == "":
        raise HTTPException(status_code=400, detail="Empty path is only valid for listing drives")

    raw = raw.replace("\\", "/")

    # Drive root token like "C:"
    if len(raw) == 2 and raw[1] == ":" and raw[0].isalpha():
        return Path(f"{raw[0].upper()}:/")

    # Absolute-ish windows path like "C:/Users"
    if len(raw) >= 3 and raw[1] == ":" and raw[2] == "/" and raw[0].isalpha():
        return Path(raw[0].upper() + raw[1:])

    raise HTTPException(
        status_code=400,
        detail="Path must look like C: or C:/Something",
    )


def iter_dir(dir_path: Path):
    try:
        items = []
        for entry in dir_path.iterdir():
            try:
                items.append(
                    {
                        "name": entry.name,
                        "path": to_api_path(entry),
                        "is_dir": entry.is_dir(),
                        "kind": "dir" if entry.is_dir() else "file",
                    }
                )
            except PermissionError:
                # Skip entries you don't have permission to stat
                continue

        # Folders first, then name
        items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
        return items
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found")


# -----------------------------
# Health
# -----------------------------
@app.get("/")
def home():
    return {"status": "running"}


# -----------------------------
# File Explorer API
# -----------------------------
@app.get("/files")
def files(path: str = ""):
    try:
        # Root = "This PC" (drive list)
        if path.strip() == "":
            result = list_drives()
            log_fs("list", "Root", "success", f"count={len(result)}")
            return result

        dir_path = resolve_path(path)
        if not dir_path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        if not dir_path.is_dir():
            raise HTTPException(status_code=400, detail="Not a directory")

        result = iter_dir(dir_path)
        log_fs("list", path, "success", f"count={len(result)}")
        return result
    except Exception as e:
        log_fs("list", path or "Root", "error", str(e))
        raise


@app.get("/file")
def read_file(path: str):
    try:
        file_path = resolve_path(path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Not found")
        if file_path.is_dir():
            raise HTTPException(status_code=400, detail="Not a file")

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        log_fs("read", path, "success")
        return {"content": content}
    except Exception as e:
        log_fs("read", path, "error", str(e))
        raise


@app.post("/file")
def write_file(path: str, content: str = ""):
    file_path = resolve_path(path)
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8", errors="ignore")
        log_fs("write", path, "success")
        return {"status": "saved"}
    except Exception as e:
        log_fs("write", path, "error", str(e))
        if isinstance(e, PermissionError):
            raise HTTPException(status_code=403, detail="Permission denied")
        raise


@app.post("/mkdir")
def make_dir(path: str):
    dir_path = resolve_path(path)
    try:
        dir_path.mkdir(parents=True, exist_ok=True)
        log_fs("mkdir", path, "success")
        return {"status": "created"}
    except Exception as e:
        log_fs("mkdir", path, "error", str(e))
        if isinstance(e, PermissionError):
            raise HTTPException(status_code=403, detail="Permission denied")
        raise


@app.delete("/file")
def delete_path(path: str):
    target = resolve_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")

    try:
        if target.is_dir():
            # only deletes empty dirs
            target.rmdir()
        else:
            target.unlink()
        log_fs("delete", path, "success")
        return {"status": "deleted"}
    except OSError:
        log_fs("delete", path, "error", "Directory not empty (or in use)")
        raise HTTPException(status_code=400, detail="Directory not empty (or in use)")
    except PermissionError:
        log_fs("delete", path, "error", "Permission denied")
        raise HTTPException(status_code=403, detail="Permission denied")


# -----------------------------
# Minecraft Server Console WebSocket
# -----------------------------
async def _broadcast_logs(server_id: str, server):
    """
    Background task that broadcasts new log lines to all connected WebSocket clients.
    One task runs per server_id.
    """
    last_line_count = 0
    
    while True:
        try:
            # Check if server is still running
            if not server.is_running():
                raise asyncio.CancelledError("Server is not running")

            
            # Get current log lines (snapshot)
            log_lines = await asyncio.to_thread(server.get_log_lines)
            current_line_count = len(log_lines)
            
            # If there are new lines, broadcast them
            if current_line_count > last_line_count:
                new_lines = log_lines[last_line_count:]
                new_content = "".join(new_lines)
                last_line_count = current_line_count
                
                # Get list of WebSockets to broadcast to (copy while holding lock)
                websockets_to_update = []
                async with _websocket_lock:
                    if server_id not in _server_websockets:
                        break
                    # Copy the dict to avoid holding lock during sends
                    websockets_to_update = list(_server_websockets[server_id].items())
                
                # Broadcast to all clients (outside lock to avoid blocking)
                disconnected = []
                for client_ws, last_sent_index in websockets_to_update:
                    try:
                        # Only send lines this client hasn't received yet
                        if current_line_count > last_sent_index:
                            lines_to_send = log_lines[last_sent_index:]
                            content_to_send = "".join(lines_to_send)
                            await client_ws.send_text(content_to_send)
                            # Update tracked index
                            async with _websocket_lock:
                                if server_id in _server_websockets and client_ws in _server_websockets[server_id]:
                                    _server_websockets[server_id][client_ws] = current_line_count
                    except Exception:
                        # Client disconnected
                        disconnected.append(client_ws)
                
                # Remove disconnected clients
                if disconnected:
                    async with _websocket_lock:
                        if server_id in _server_websockets:
                            for client_ws in disconnected:
                                _server_websockets[server_id].pop(client_ws, None)
                            # If no more clients, stop broadcaster
                            if not _server_websockets[server_id]:
                                del _server_websockets[server_id]
                                # Note: Broadcaster will be cancelled by the last client disconnect handler
                                break
            
            # Poll interval (100ms for responsive updates)
            await asyncio.sleep(0.1)
            
        except asyncio.CancelledError:
            async with _websocket_lock:
                _server_websockets.pop(server_id, None)
                _server_broadcasters.pop(server_id, None)
            break
        except Exception as e:
            print(f"[minecraft] Error in broadcaster for '{server_id}': {e}")
            break


@app.websocket("/servers/{server_id}/console")
async def minecraft_console(ws: WebSocket, server_id: str):
    await ws.accept()
    client = ws.client
    client_addr = f"{client.host}:{client.port}" if client else "unknown"
    print(f"[minecraft] connect server_id={server_id} client={client_addr}")
    
    # Look up the server
    server = manager.get_server(server_id)
    if not server or not server.is_running():
        error_msg = f"[server] Server '{server_id}' is not running\n"
        try:
            await ws.send_text(error_msg)
        except Exception:
            pass
        await ws.close()
        return
    
    # Get current log lines to determine starting index
    log_lines = await asyncio.to_thread(server.get_log_lines)
    initial_line_count = len(log_lines)
    
    # Track this WebSocket connection and start broadcaster if needed
    async with _websocket_lock:
        if server_id not in _server_websockets:
            _server_websockets[server_id] = {}
        # Initially mark as not having received any lines yet (will update after sending)
        _server_websockets[server_id][ws] = 0
        
        # Start broadcaster if this is the first client
        if server_id not in _server_broadcasters:
            broadcaster_task = asyncio.create_task(_broadcast_logs(server_id, server))
            _server_broadcasters[server_id] = broadcaster_task
    
    # Send current log buffer immediately (full history)
    try:
        if log_lines:
            initial_content = "".join(log_lines)
            await ws.send_text(initial_content)
            # Update tracked index after successfully sending
            async with _websocket_lock:
                if server_id in _server_websockets and ws in _server_websockets[server_id]:
                    _server_websockets[server_id][ws] = initial_line_count
    except Exception as e:
        print(f"[minecraft] Error sending initial logs for '{server_id}': {e}")
    
    try:
        # Forward incoming messages to server
        while True:
            try:
                data = await ws.receive_text()
                # Write to server stdin (non-blocking)
                await asyncio.to_thread(server.write, data)
            except Exception:
                # WebSocket disconnected or error
                break
    except Exception:
        pass
    finally:
        # Remove this WebSocket from tracking
        async with _websocket_lock:
            if server_id in _server_websockets:
                _server_websockets[server_id].pop(ws, None)
                # If this was the last client, stop the broadcaster
                if not _server_websockets[server_id]:
                    del _server_websockets[server_id]
                    if server_id in _server_broadcasters:
                        task = _server_broadcasters.pop(server_id)
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            pass
        
        print(f"[minecraft] disconnect server_id={server_id} client={client_addr}")


# -----------------------------
# Terminal WebSocket
# -----------------------------
@app.websocket("/terminal/{session_id}")
async def terminal(ws: WebSocket, session_id: str):
    await ws.accept()
    client = ws.client
    client_addr = f"{client.host}:{client.port}" if client else "unknown"
    print(f"[ws] connect session_id={session_id} client={client_addr}")

    try:
        pty = winpty.PtyProcess.spawn("powershell.exe")
    except Exception as e:
        await ws.send_text(f"[server] Failed to start PowerShell: {e}\n")
        await ws.close()
        return

    async def read_from_shell():
        while True:
            try:
                data = await asyncio.to_thread(pty.read, 4096)
                if data:
                    if isinstance(data, bytes):
                        data = data.decode(errors="ignore")
                    await ws.send_text(data)
            except asyncio.CancelledError:
                # Normal cancellation during shutdown
                break
            except Exception:
                break

    reader = asyncio.create_task(read_from_shell())

    try:
        while True:
            data = await ws.receive_text()
            await asyncio.to_thread(pty.write, data)
    except asyncio.CancelledError:
        # Normal shutdown on Ctrl+C
        pass
    except Exception:
        # WebSocket disconnect or client close
        pass


    finally:
        # Clean shutdown: kill PTY first to unblock any blocking read, then cancel reader task
        try:
            pty.kill()
        except Exception:
            pass
        
        # Cancel reader task and await it to suppress CancelledError noise
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            # Expected during shutdown - suppress noise
            pass
        except Exception:
            pass
        
        print(f"[ws] disconnect session_id={session_id} client={client_addr}")


if __name__ == "__main__":
    # Use SERVER_HOST environment variable for configurable bind address
    # Defaults to 0.0.0.0 to allow connections from any interface (useful for Tailscale, LAN, etc.)
    # Set SERVER_HOST to a specific IP (e.g., "127.0.0.1") to restrict to localhost only
    host = os.getenv("SERVER_HOST", "0.0.0.0")
    port = 5000
    token_enabled = bool(os.getenv("SERVER_TOKEN"))
    print(f"[server] starting host={host} port={port} token_enabled={token_enabled}")
    uvicorn.run("server:app", host=host, port=port)
