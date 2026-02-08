# Guide de Déploiement - EDT ESEO Backend avec SWAG

## 🐳 Architecture Docker

```
Internet
    ↓
SWAG (nginx + Let's Encrypt)
    ↓ reverse proxy
EDT Backend (FastAPI)
    ↓
SQLite Database
```

## 📁 Structure des fichiers

```
/opt/edt-eseo/
├── docker-compose.yml
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── database.py
│   ├── utils.py
│   ├── scraper.py
│   ├── scheduler.py
│   ├── security.py
│   └── data/
│       └── edt.db (créé automatiquement)
└── swag/
    └── config/
        └── nginx/
            └── proxy-confs/
                └── edt-api.subdomain.conf
```

## 🔧 Configuration

### 1. Créer le Dockerfile pour le backend

```dockerfile
FROM python:3.11-slim

# Install system dependencies for Playwright
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy application code
COPY . .

# Create data directory for SQLite
RUN mkdir -p /app/data

# Expose port
EXPOSE 8000

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV TZ=Europe/Paris

# Run the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
```

### 2. Créer docker-compose.yml

```yaml
version: '3.8'

services:
  # SWAG - Reverse proxy avec Let's Encrypt
  swag:
    image: lscr.io/linuxserver/swag:latest
    container_name: swag
    cap_add:
      - NET_ADMIN
    environment:
      - PUID=1000
      - PGID=1000
      - TZ=Europe/Paris
      - URL=votredomaine.com
      - SUBDOMAINS=edt-api
      - VALIDATION=http
      - EMAIL=votre@email.com
      - ONLY_SUBDOMAINS=false
      - STAGING=false
    volumes:
      - ./swag/config:/config
      - ./edt-api.subdomain.conf:/config/nginx/proxy-confs/edt-api.subdomain.conf:ro
    ports:
      - 443:443
      - 80:80
    restart: unless-stopped
    networks:
      - edt-network

  # EDT Backend - FastAPI
  edt-backend:
    build:
      context: ./backend
      dockerfile: Dockerfile
    container_name: edt-backend
    environment:
      - TZ=Europe/Paris
      - JWT_SECRET_KEY=${JWT_SECRET_KEY}
      - DATABASE_URL=sqlite:///./data/edt.db
    volumes:
      - ./backend:/app
      - edt-data:/app/data
    expose:
      - 8000
    restart: unless-stopped
    networks:
      - edt-network
    depends_on:
      - swag

networks:
  edt-network:
    driver: bridge

volumes:
  edt-data:
    driver: local
```

### 3. Créer le fichier .env

```bash
# JWT Secret Key (générer avec: openssl rand -hex 32)
JWT_SECRET_KEY=eb36532d36a7c825a249d3aaef288a3bbc762660563bb5946ba40150a756af26

# Domain configuration
DOMAIN=votredomaine.com
EMAIL=votre@email.com
```

## 🚀 Déploiement

### Étape 1 : Préparation du serveur

```bash
# Installer Docker et Docker Compose
curl -fsSL https://get.docker.com -o get-docker.sh
sh get-docker.sh

# Créer la structure
mkdir -p /opt/edt-eseo/backend
cd /opt/edt-eseo
```

### Étape 2 : Copier les fichiers

```bash
# Copier tous les fichiers Python dans /opt/edt-eseo/backend/
# Copier edt-api.subdomain.conf dans /opt/edt-eseo/
# Copier docker-compose.yml dans /opt/edt-eseo/
# Créer le .env avec votre configuration
```

### Étape 3 : Configuration DNS

Créer un enregistrement DNS A pour :
```
edt-api.votredomaine.com → IP_SERVEUR
```

### Étape 4 : Démarrer les services

```bash
cd /opt/edt-eseo

# Build et démarrer
docker-compose up -d --build

# Vérifier les logs
docker-compose logs -f edt-backend
docker-compose logs -f swag
```

### Étape 5 : Vérifier le déploiement

```bash
# Health check
curl https://edt-api.votredomaine.com/health

# API documentation
# Ouvrir dans le navigateur: https://edt-api.votredomaine.com/docs
```

## 🔍 Commandes utiles

```bash
# Voir les logs du backend
docker-compose logs -f edt-backend

# Voir les logs de SWAG
docker-compose logs -f swag

# Redémarrer le backend
docker-compose restart edt-backend

# Arrêter tous les services
docker-compose down

# Mettre à jour le code
cd /opt/edt-eseo/backend
git pull  # ou copier les nouveaux fichiers
cd ..
docker-compose up -d --build edt-backend

# Accéder au shell du backend
docker exec -it edt-backend bash

# Backup de la base de données
docker cp edt-backend:/app/data/edt.db ./backup-$(date +%Y%m%d).db
```

## 🔒 Sécurité

### Firewall

```bash
# Autoriser uniquement HTTP/HTTPS
ufw allow 80/tcp
ufw allow 443/tcp
ufw enable
```

### Configuration SWAG

Le fichier `edt-api.subdomain.conf` configure automatiquement :
- ✅ SSL/TLS avec Let's Encrypt
- ✅ Headers de sécurité (HSTS, X-Frame-Options, etc.)
- ✅ Redirection HTTP → HTTPS
- ✅ Protection contre les attaques courantes

### Rotation des secrets

```bash
# Générer un nouveau JWT secret
openssl rand -hex 32

# Mettre à jour .env
# Redémarrer le backend
docker-compose restart edt-backend
```

## 📊 Monitoring

### Health check automatique

```bash
# Ajouter à crontab
*/5 * * * * curl -f https://edt-api.votredomaine.com/health || systemctl restart docker-compose@edt
```

### Logs

```bash
# Logs du backend en temps réel
docker-compose logs -f --tail=100 edt-backend

# Logs nginx (SWAG)
docker exec swag cat /config/log/nginx/access.log
docker exec swag cat /config/log/nginx/error.log
```

## 🔄 Mise à jour

### Update du code

```bash
cd /opt/edt-eseo/backend
# Copier les nouveaux fichiers ou git pull
cd ..
docker-compose build edt-backend
docker-compose up -d edt-backend
```

### Update des certificats SSL

SWAG renouvelle automatiquement les certificats Let's Encrypt. Rien à faire !

## 🆘 Dépannage

### Le backend ne démarre pas

```bash
# Vérifier les logs
docker-compose logs edt-backend

# Vérifier que Playwright est installé
docker exec -it edt-backend playwright --version
```

### Erreur SSL/certificat

```bash
# Supprimer les certificats et les regénérer
docker-compose down
rm -rf swag/config/keys
docker-compose up -d
```

### L'API ne répond pas

```bash
# Tester depuis le container SWAG
docker exec swag curl http://edt-backend:8000/health

# Si ça marche, problème nginx. Vérifier edt-api.subdomain.conf
docker exec swag nginx -t
```

## 🌐 URLs d'accès

- **API Health** : `https://edt-api.votredomaine.com/health`
- **API Docs** : `https://edt-api.votredomaine.com/docs`
- **Login** : `POST https://edt-api.votredomaine.com/auth/login`
- **Agenda** : `GET https://edt-api.votredomaine.com/agenda`

## 📝 Notes importantes

1. **Base de données** : SQLite est stockée dans le volume `edt-data`. Faire des backups réguliers !
2. **Playwright** : Le container a besoin de ressources pour Chromium (au moins 512MB RAM)
3. **Timezone** : Configuré sur `Europe/Paris` pour le scheduler
4. **Workers** : Configuré avec 1 worker uvicorn (scheduler APScheduler ne supporte pas multi-worker)
