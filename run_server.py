import os
import sys
import socket
import subprocess
import webbrowser
import time

def check_port(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def load_env():
    # Load env from .env in current directory
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.startswith("export "):
                        line = line[7:].strip()
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip()
                        if val.startswith("'") and val.endswith("'"):
                            val = val[1:-1]
                        elif val.startswith('"') and val.endswith('"'):
                            val = val[1:-1]
                        os.environ[key] = val

    # Also load from ~/.config/campinas-nfse-automator/env if it exists
    home_config = os.path.expanduser("~/.config/campinas-nfse-automator/env")
    if os.path.exists(home_config):
        with open(home_config, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.startswith("export "):
                        line = line[7:].strip()
                    parts = line.split("=", 1)
                    if len(parts) == 2:
                        key = parts[0].strip()
                        val = parts[1].strip()
                        if val.startswith("'") and val.endswith("'"):
                            val = val[1:-1]
                        elif val.startswith('"') and val.endswith('"'):
                            val = val[1:-1]
                        os.environ[key] = val

def main():
    load_env()
    port = 8001
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    if not check_port(port):
        print(f"Iniciando servidor Uvicorn na porta {port}...")
        log_out_path = os.path.join(script_dir, "app.log")
        log_err_path = os.path.join(script_dir, "app_err.log")
        
        log_out = open(log_out_path, "w", encoding="utf-8")
        log_err = open(log_err_path, "w", encoding="utf-8")
        
        # Configurar flags de criacao para desvincular do console no Windows
        creationflags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS (0x00000008) | CREATE_NO_WINDOW (0x08000000)
            creationflags = 0x00000008 | 0x08000000
            
        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", str(port)],
            stdout=log_out,
            stderr=log_err,
            creationflags=creationflags,
            close_fds=True,
            cwd=script_dir,
            env=env
        )
        # Dar tempo para inicializar
        time.sleep(3)
    else:
        print(f"Servidor ja esta rodando na porta {port}.")
        
    print("Abrindo o navegador...")
    webbrowser.open(f"http://127.0.0.1:{port}")

if __name__ == "__main__":
    main()
