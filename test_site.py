import os
import sys
import json
import http.server
import socketserver
import threading
import urllib.request
import time

PORT = 8765

def test_file_structure():
    print("Testing file structure...")
    required_files = [
        "site/index.html",
        "site/anime.html",
        "site/izle.html",
        "site/css/style.css",
        "site/css/dark.css",
        "site/css/mobil.css",
        "site/css/custom.css",
        "site/imajlar/logo.png",
        "site/imajlar/loader.gif",
        "site/imajlar/search-dark.png",
        "site/imajlar/search-white.png",
        "site/imajlar/ui.totop.png",
        "site/js/app.js",
        "site/js/home.js",
        "site/js/anime.js",
        "site/js/player.js"
    ]
    for rf in required_files:
        assert os.path.exists(rf), f"Missing required file: {rf}"
        size = os.path.getsize(rf)
        print(f"  [OK] {rf} ({size} bytes)")
    print("All required site files present!\n")

def test_api_files():
    print("Testing API files...")
    assert os.path.exists("api/animes.json")
    assert os.path.exists("api/metadata.json")
    assert os.path.exists("api/slug_map.json")
    assert os.path.exists("api/anime/1.json")
    
    with open("api/slug_map.json", "r", encoding="utf-8") as f:
        slug_map = json.load(f)
    assert "cowboy-bebop" in slug_map
    assert slug_map["cowboy-bebop"] == "1.json"
    print(f"  [OK] slug_map has {len(slug_map)} slugs mapped.")

    with open("api/anime/1.json", "r", encoding="utf-8") as f:
        cb = json.load(f)
    assert cb["turkanime"]["slug"] == "cowboy-bebop"
    assert len(cb["episodes"]) > 0
    print(f"  [OK] Cowboy Bebop has {len(cb['episodes'])} episodes.")
    print("API files valid!\n")

def test_http_server():
    print("Starting temporary HTTP server to test web serving...")
    Handler = http.server.SimpleHTTPRequestHandler
    httpd = socketserver.TCPServer(("", PORT), Handler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(1)

    urls = [
        f"http://localhost:{PORT}/site/index.html",
        f"http://localhost:{PORT}/site/anime.html",
        f"http://localhost:{PORT}/site/izle.html",
        f"http://localhost:{PORT}/site/css/custom.css",
        f"http://localhost:{PORT}/site/js/app.js",
        f"http://localhost:{PORT}/site/imajlar/logo.png",
        f"http://localhost:{PORT}/api/slug_map.json",
        f"http://localhost:{PORT}/api/anime/1.json"
    ]

    for u in urls:
        req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as resp:
            status = resp.getcode()
            length = len(resp.read())
            print(f"  [OK] GET {u} -> Status {status}, Length {length}")
            assert status == 200

    httpd.shutdown()
    print("HTTP serving tests passed successfully!\n")

if __name__ == "__main__":
    try:
        test_file_structure()
        test_api_files()
        test_http_server()
        print("=== ALL VERIFICATION TESTS PASSED! ===")
    except Exception as e:
        print("TEST FAILED:", e)
        sys.exit(1)
