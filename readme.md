# Stellara-Server
## Installation

### Requirements

- Python 3.11 or 3.12
- Tailscale (required for remote access)

---

### Why Tailscale is required

Stellara Server exposes full filesystem access and a live PowerShell terminal.
It is **not safe to expose this server directly to the public internet**.

Tailscale is the intended and supported way to access Stellara remotely:
- No port forwarding
- Encrypted private network
- Stable private IPs
- No public exposure

You should assume the server is reachable **only** via localhost or Tailscale.

---

### 1. Install and configure Tailscale

1. Install Tailscale on the server machine  
   https://tailscale.com/download

2. Install Tailscale on any client machine that will access Stellara

3. Sign in on both machines using the same Tailscale account

4. Confirm the server has a Tailscale IP (usually `100.x.x.x`) with ```tailscale ip -4```

Example:
```
100.12.345.67
```

---

### 2. Clone the repository

```bash
git clone https://github.com/owenungaro/stellara-server.git
cd stellara-server
```

---

### 4. Install dependencies

```powershell
pip install -r requirements.txt
```

---

### 5. Configure environment variables (recommended)

Create a `.env` file in the project root:

```env
SERVER_HOST=0.0.0.0
```

- `0.0.0.0` allows Tailscale and LAN connections
- Use `127.0.0.1` to restrict access to the local machine only

---

### 6. Run the server

```powershell
python server.py
```

The server will start on:

```
http://<tailscale-ip>:5000
```

Example:

```
http://100.12.345.67:5000
```

Point the Stellara frontend at this address.
