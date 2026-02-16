
# EDT ESEO Backend

Backend API pour la gestion des emplois du temps ESEO avec authentification sécurisée et synchronisation automatique.

## 🚀 Démarrage rapide

### Développement local

```bash
# Installation des dépendances
pip install -r requirements.txt

# Installation de Playwright
playwright install chromium

# Lancer l'application
python -m uvicorn main:app --reload
```

Accès : http://localhost:8000

### Production avec Docker + SWAG

```bash
# 1. Configurer les variables d'environnement
cp .env.example .env
nano .env  # Modifier DOMAIN, EMAIL, JWT_SECRET_KEY

# 2. Rendre le script exécutable
chmod +x deploy.sh

# 3. Démarrer les services
./deploy.sh start

# 4. Vérifier l'état
./deploy.sh status
```

Accès : https://edt-api.votredomaine.com

## 📚 Documentation

- **Guide complet** : Voir [DEPLOYMENT.md](DEPLOYMENT.md)
- **API Docs** : https://edt-api.votredomaine.com/docs (en dev)
- **Health check** : https://edt-api.votredomaine.com/health

## 🔧 Architecture

```
┌─────────────┐
│   Flutter   │
│   Frontend  │
└──────┬──────┘
       │ HTTPS
       ↓
┌─────────────┐
│    SWAG     │ ← SSL/TLS (Let's Encrypt)
│   (nginx)   │
└──────┬──────┘
       │ HTTP
       ↓
┌─────────────┐
│  FastAPI    │ ← Backend (Python 3.11)
│   Backend   │
└──────┬──────┘
       │
┌──────┴──────┐
│   SQLite    │ ← Base de données
│  Database   │
└─────────────┘
```

## 🛠️ Technologies

- **Backend** : FastAPI (Python 3.11)
- **Auth** : JWT avec extraction ESEO ID via Playwright
- **Database** : SQLite avec SQLAlchemy ORM
- **Scheduler** : APScheduler (sync automatique)
- **Scraping** : Playwright + httpx
- **Reverse Proxy** : SWAG (nginx + Let's Encrypt)
- **Containerization** : Docker + Docker Compose

## 📋 Endpoints principaux

### Authentication
- `POST /auth/login` - Login avec identifiants ESEO
- `POST /auth/register-device` - Enregistrer token FCM
- `DELETE /auth/logout` - Logout

### Agenda
- `GET /agenda` - Récupérer l'emploi du temps
  - Paramètres : `start`, `end`, `force`
  - Cache-first strategy
  - Max : 1 an de données

### Health
- `GET /` - Health check

## 🔐 Sécurité

- ✅ JWT pour l'authentification
- ✅ HTTPS obligatoire (HSTS)
- ✅ Headers de sécurité (X-Frame-Options, CSP, etc.)
- ✅ CORS configuré
- ✅ Rate limiting (via nginx)
- ✅ Credentials JAMAIS stockés (uniquement ESEO ID)

## 🕐 Synchronisation automatique

Le scheduler APScheduler synchronise automatiquement :
- **Sync utilisateurs** : Toutes les heures de 7h à 19h
- **Purge anciens events** : Quotidien à 2h (supprime > 6 mois)

## 📦 Commandes utiles

```bash
# Démarrer
./deploy.sh start

# Logs en temps réel
./deploy.sh logs

# Backup base de données
./deploy.sh backup

# Mettre à jour le code
./deploy.sh update

# Redémarrer
./deploy.sh restart

# Arrêter
./deploy.sh stop
```

## 🗄️ Structure du projet

```
edt_app_backend/
├── main.py              # Application FastAPI principale
├── database.py          # Modèles SQLAlchemy (User, Event)
├── security.py          # JWT et authentification
├── scraper.py           # Playwright scraping + API calls
├── scheduler.py         # APScheduler jobs
├── utils.py             # Timezone, parsing, helpers
├── requirements.txt     # Dépendances Python
├── Dockerfile           # Image Docker backend
├── docker-compose.yml   # Orchestration services
├── edt-api.subdomain.conf  # Config nginx SWAG
├── deploy.sh            # Script de déploiement
├── DEPLOYMENT.md        # Guide détaillé
└── .env                 # Variables d'environnement
```

## 🐛 Dépannage

### Backend ne démarre pas

```bash
# Vérifier les logs
docker compose logs edt-backend

# Accéder au container
docker exec -it edt-backend bash
```

### Certificats SSL

```bash
# Vérifier les logs SWAG
docker compose logs swag

# Regénérer les certificats
docker compose down
rm -rf swag/config/keys
docker compose up -d
```

### Base de données corrompue

```bash
# Restaurer un backup
./deploy.sh restore ./backups/edt-backup-YYYYMMDD-HHMMSS.db
```

## 🌐 Variables d'environnement

```bash
# .env
JWT_SECRET_KEY=<généré avec openssl rand -hex 32>
DOMAIN=votredomaine.com
EMAIL=admin@votredomaine.com
DATABASE_URL=sqlite:///./data/edt.db
```

## 📊 Monitoring

```bash
# Health check
curl https://edt-api.votredomaine.com/health

# Logs backend
docker compose logs -f edt-backend

# Logs nginx
docker exec swag cat /config/log/nginx/access.log
```

## 🤝 Contribution

1. Fork le projet
2. Créer une branche (`git checkout -b feature/AmazingFeature`)
3. Commit les changements (`git commit -m 'Add AmazingFeature'`)
4. Push vers la branche (`git push origin feature/AmazingFeature`)
5. Ouvrir une Pull Request

## 📄 License

Ce projet est privé et destiné à un usage interne ESEO.

## 👨‍💻 Auteur

Développé avec ❤️ pour simplifier l'accès aux emplois du temps ESEO.

## 🔗 Liens utiles

- [FastAPI Documentation](https://fastapi.tiangolo.com/)
- [SWAG Documentation](https://docs.linuxserver.io/general/swag)
- [Playwright Python](https://playwright.dev/python/)
- [APScheduler](https://apscheduler.readthedocs.io/)
