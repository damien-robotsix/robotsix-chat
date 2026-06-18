# Example nginx reverse proxy configuration

```nginx
# /etc/nginx/sites-available/chat.example.com
server {
    listen 443 ssl;
    server_name chat.example.com;          # ← replace with your domain

    ssl_certificate     /etc/letsencrypt/live/chat.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8088;  # ← match CHAT_PORT
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 86400s;         # SSE streams are long-lived
    }
}
```
