docker run -d \
  --name anki-mcp \
  --restart unless-stopped \
  -p 127.0.0.1:8765:8765 \
  -v anki-data:/data \
  -e ANKICONNECT_BIND=0.0.0.0 \
  anki-connect-server \
  uv run anki-connect-server mcp --transport http