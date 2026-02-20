workers = 1          # 1 seul worker pour économiser la RAM (free tier Render)
timeout = 120        # 120s au lieu de 30s par défaut (pour les appels Gemini longs)
keepalive = 5
worker_class = "sync"
bind = "0.0.0.0:10000"