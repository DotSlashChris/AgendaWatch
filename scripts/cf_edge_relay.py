#!/usr/bin/env python3
"""TCP relay so cloudflared can reach CF edge:7844 via sand CONNECT proxy."""
import select, socket, threading, sys

PROXY = ("127.0.0.1", 8791)
# Prefer region1; region2 as fallback handled by separate listeners if needed
EDGE_HOST = sys.argv[1] if len(sys.argv) > 1 else "region1.v2.argotunnel.com"
LISTEN_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 7844

def connect_via_proxy(host, port):
    s = socket.create_connection(PROXY, timeout=15)
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
    s.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = s.recv(4096)
        if not chunk:
            raise RuntimeError("proxy closed")
        buf += chunk
    status = buf.split(b"\r\n", 1)[0]
    if b"200" not in status:
        s.close()
        raise RuntimeError(f"proxy CONNECT failed: {status!r}")
    return s

def pipe(a, b):
    try:
        while True:
            r, _, _ = select.select([a, b], [], [], 120)
            if not r:
                break
            for s in r:
                other = b if s is a else a
                data = s.recv(65536)
                if not data:
                    return
                other.sendall(data)
    except Exception:
        pass
    finally:
        for s in (a, b):
            try: s.shutdown(socket.SHUT_RDWR)
            except Exception: pass
            try: s.close()
            except Exception: pass

def handle(client):
    try:
        upstream = connect_via_proxy(EDGE_HOST, 7844)
    except Exception as e:
        print(f"upstream fail: {e}", flush=True)
        client.close()
        return
    t1 = threading.Thread(target=pipe, args=(client, upstream), daemon=True)
    t2 = threading.Thread(target=pipe, args=(upstream, client), daemon=True)
    t1.start(); t2.start(); t1.join(); t2.join()

def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", LISTEN_PORT))
    srv.listen(50)
    print(f"relay 127.0.0.1:{LISTEN_PORT} -> CONNECT {EDGE_HOST}:7844 via {PROXY}", flush=True)
    while True:
        c, addr = srv.accept()
        threading.Thread(target=handle, args=(c,), daemon=True).start()

if __name__ == "__main__":
    main()
