import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

HOST = "0.0.0.0"
PORT = int(os.getenv("METRICS_PORT", "11435"))


def get_system_ram_gb() -> tuple[float, float]:
    """Linux /proc/meminfo から OS全体の (実使用GB, 合計GB) を取得する"""
    mem_total_kb = 0
    mem_avail_kb = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    mem_total_kb = int(line.split()[1])
                elif line.startswith("MemAvailable:"):
                    mem_avail_kb = int(line.split()[1])
    except Exception:
        return 0.0, 0.0

    total_gb = mem_total_kb / (1024 * 1024)
    used_gb = max(0, mem_total_kb - mem_avail_kb) / (1024 * 1024)
    return round(used_gb, 2), round(total_gb, 2)


def get_gpu_vram_gb() -> tuple[float, float]:
    """nvidia-smi から GPU全体の (実使用VRAM GB, 合計VRAM GB) を取得する"""
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            used_mb = 0.0
            total_mb = 0.0
            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 2:
                    used_mb += float(parts[0])
                    total_mb += float(parts[1])
            return round(used_mb / 1024, 2), round(total_mb / 1024, 2)
    except Exception:
        pass
    return 0.0, 0.0


def get_ollama_process_memory_gb() -> tuple[float, float]:
    """Ollama関連プロセスが実際に消費している (VRAM GB, RAM GB) を実測する"""
    ollama_ram_kb = 0
    try:
        res = subprocess.run(
            ["ps", "-eo", "rss,comm,args"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines()[1:]:
                parts = line.strip().split(None, 2)
                if len(parts) >= 2:
                    rss_kb = int(parts[0])
                    cmd_str = " ".join(parts[1:]).lower()
                    if "ollama" in cmd_str or "llama" in cmd_str:
                        ollama_ram_kb += rss_kb
    except Exception:
        pass

    ollama_vram_mb = 0.0
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=used_memory,process_name", "--format=csv,noheader,nounits"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 2 and ("ollama" in parts[1].lower() or "llama" in parts[1].lower()):
                    ollama_vram_mb += float(parts[0])
    except Exception:
        pass

    return round(ollama_vram_mb / 1024, 2), round(ollama_ram_kb / (1024 * 1024), 2)


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return

        ram_used, ram_total = get_system_ram_gb()
        vram_used, vram_total = get_gpu_vram_gb()
        ollama_vram, ollama_ram = get_ollama_process_memory_gb()

        payload = {
            "vram_used_gb": vram_used,
            "vram_total_gb": vram_total,
            "ram_used_gb": ram_used,
            "ram_total_gb": ram_total,
            "ollama_vram_gb": ollama_vram if ollama_vram > 0 else vram_used,
            "ollama_ram_gb": ollama_ram,
        }
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # コンソールログの抑制


if __name__ == "__main__":
    print(f"[MetricsServer] Listening on http://{HOST}:{PORT}/metrics")
    HTTPServer((HOST, PORT), MetricsHandler).serve_forever()